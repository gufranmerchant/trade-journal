from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import api_usage, keyword_research, wikipedia


# ---------------------------------------------------------------- a fake Wikipedia

class FakeWikipedia:
    """opensearch results, article descriptions and monthly views, keyed by what a test sets up.
    Every request is recorded so tests can assert what was (not) asked."""

    def __init__(self):
        self.search = {}        # search term -> [titles]
        self.pages = {}         # title -> {"description": str, "disambiguation": bool}
        self.redirects = {}     # title -> canonical title
        self.views = {}         # canonical title -> {"YYYYMM": views}
        self.requests = []
        self.status = None      # force this HTTP status on every request

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        if self.status:
            return httpx.Response(self.status, json={})
        url = str(request.url)
        q = dict(request.url.params)
        if "pageviews/per-article" in url:
            title = request.url.path.split("/user/")[1].split("/")[0].replace("_", " ")
            monthly = self.views.get(title)
            if not monthly:
                return httpx.Response(404, json={"detail": "not found"})
            items = [{"timestamp": ym + "0100", "views": v} for ym, v in sorted(monthly.items())]
            return httpx.Response(200, json={"items": items})
        if q.get("action") == "opensearch":
            return httpx.Response(200, json=[q["search"], self.search.get(q["search"].lower(), []), [], []])
        if q.get("action") == "query":
            pages, redirects = [], []
            for t in q["titles"].split("|"):
                target = self.redirects.get(t, t)
                if t != target:
                    redirects.append({"from": t, "to": target})
                page = self.pages.get(target)
                if not page:
                    pages.append({"title": target, "missing": True})
                    continue
                entry = {"title": target, "description": page["description"]}
                if page.get("disambiguation"):
                    entry["pageprops"] = {"disambiguation": ""}
                pages.append(entry)
            return httpx.Response(200, json={"query": {"redirects": redirects, "pages": pages}})
        return httpx.Response(500)

    def pageview_requests(self):
        return [r for r in self.requests if "pageviews" in str(r.url)]


@pytest.fixture
def wiki(monkeypatch):
    fake = FakeWikipedia()
    monkeypatch.setattr(wikipedia, "_client", wikipedia.make_client(httpx.MockTransport(fake)))
    return fake


def _steady_views(last_month="202609", n=15, views=1000, **overrides):
    """n monthly rows ending at last_month."""
    y, m = int(last_month[:4]), int(last_month[4:])
    out = {}
    for i in range(n):
        idx = y * 12 + (m - 1) - i
        ym = f"{idx // 12}{idx % 12 + 1:02d}"
        out[ym] = overrides.get(ym, views)
    return out


TODAY = date(2026, 10, 4)


def _cozy(wiki, **view_overrides):
    wiki.search["cozy mystery"] = ["Cozy mystery"]
    wiki.pages["Cozy mystery"] = {"description": "Subgenre of crime fiction"}
    wiki.views["Cozy mystery"] = _steady_views(**view_overrides)


# ---------------------------------------------------------------- what counts as genre-shaped

@pytest.mark.parametrize("description", [
    "Literary genre", "Subgenre of crime fiction", "Subgenre of fiction", "Genre of novel",
    "Genre of literature, film, and television", "Nonfiction literary, radio, and film genre",
    "Book of recipes with instructions", "Instructional book for solving personal problems",
    "Aesthetic of nostalgia popular among youths", "Subculture centered on Gothic and classic education",
    "19th-century genre of popular novel", "Literature written for adolescents and young adults",
    "Form of literature", "Type of autobiographical or biographical writing",
])
def test_genre_descriptions_are_accepted(description):
    assert wikipedia.genre_shaped(description)


@pytest.mark.parametrize("description", [
    "", None, "Film genre", "American mystery drama television series", "Musical artist", "Love focused on feelings",
    "Type of fermented bread", "Direct descendants of Vulgar Latin", "Type of musical instrument", "Kind of video game",
    "1925 novel by F. Scott Fitzgerald", "2019 studio album by Camila Cabello", "1859 book by Samuel Smiles",
    "Novel by Stephen King", "American horror novelist",
    "14th-century Chinese historical novel",                      # a specific work, not a genre
    "American science fiction comedy television series",          # "fiction" must not rescue a TV show
    "Video game genre", "Film genre evoking excitement and suspense",
])
def test_non_genre_descriptions_are_rejected(description):
    assert not wikipedia.genre_shaped(description)


@pytest.mark.parametrize("topic,key", [
    ("cozy mystery novels", "cozy mystery"), ("Cozy Mysteries", "cozy mystery"), ("self-help books", "self help"),
    ("Romance", "romance"), ("true crime", "true crime"), ("science fiction", "science fiction"),
    ("historical romance novels", "historical romance"), ("  ", ""),
])
def test_topic_key(topic, key):
    assert wikipedia.topic_key(topic) == key


# ---------------------------------------------------------------- matching a topic to ONE genre article

def test_picks_the_genre_article_not_a_disambiguated_page_for_another_sense(wiki):
    wiki.search["romance"] = ["Romance", "Romance novel", "Romance (prose fiction)", "Romance (Camila Cabello album)", "Romance languages"]
    wiki.pages.update({
        "Romance": {"description": "Love focused on feelings"},
        "Romance novel": {"description": "Literary genre"},
        "Romance (prose fiction)": {"description": "Genre of novel"},      # genre-shaped, but the medieval sense
        "Romance (Camila Cabello album)": {"description": "2019 studio album by Camila Cabello"},
    })
    assert wikipedia.find_genre_article("romance")["title"] == "Romance novel"


def test_sub_niche_topic_has_no_article_and_asks_for_no_pageviews(wiki):
    _cozy(wiki)                                       # a near match exists ("Cozy mystery") ...
    assert wikipedia.interest_over_time("cozy mystery set in vermont bakeries", today=TODAY) is None
    assert wiki.pageview_requests() == []             # ... and is NOT used for the narrower topic


def test_a_specific_work_or_person_is_never_a_genre_article(wiki):
    wiki.search["the great gatsby"] = ["The Great Gatsby"]
    wiki.pages["The Great Gatsby"] = {"description": "1925 novel by F. Scott Fitzgerald"}
    wiki.views["The Great Gatsby"] = _steady_views()
    assert wikipedia.interest_over_time("the great gatsby", today=TODAY) is None
    assert wiki.pageview_requests() == []


def test_disambiguation_pages_are_rejected(wiki):
    wiki.search["mystery"] = ["Mystery"]
    # a genre-shaped description, so that only the disambiguation flag can reject the page
    wiki.pages["Mystery"] = {"description": "Genre of fiction", "disambiguation": True}
    assert wikipedia.find_genre_article("mystery") is None


def test_suffix_titles_are_accepted_for_the_bare_genre_word(wiki):
    wiki.search["horror"] = ["Horror (disambiguation)", "Horror fiction"]
    wiki.pages["Horror fiction"] = {"description": "Genre of fiction"}
    assert wikipedia.find_genre_article("horror")["title"] == "Horror fiction"


def test_wikipedia_redirects_are_followed_and_reported(wiki):
    wiki.search["epic fantasy"] = ["Epic fantasy"]
    wiki.redirects["Epic fantasy"] = "High fantasy"
    wiki.pages["High fantasy"] = {"description": "Subgenre of fiction"}
    found = wikipedia.find_genre_article("epic fantasy")
    assert found["title"] == "High fantasy" and found["redirected_from"] == "Epic fantasy"


def test_exact_title_has_no_redirect_note(wiki):
    _cozy(wiki)
    assert wikipedia.find_genre_article("cozy mystery")["redirected_from"] is None


# ---------------------------------------------------------------- the indicator

def test_interest_over_time_end_to_end(wiki):
    # last 3 months 2000, the same 3 months a year earlier 1000, everything else 1500
    _cozy(wiki, views=1500, **{"202607": 2000, "202608": 2000, "202609": 2000, "202507": 1000, "202508": 1000, "202509": 1000})
    r = wikipedia.interest_over_time("Cozy mystery novels", today=TODAY)
    assert r["article"]["title"] == "Cozy mystery"
    assert r["article"]["url"] == "https://en.wikipedia.org/wiki/Cozy_mystery"
    assert len(r["months"]) == 12 and r["months"][0]["month"] == "2025-10" and r["months"][-1]["month"] == "2026-09"
    assert r["change_pct"] == 100 and r["trend"] == "rising" and r["recent_monthly_avg"] == 2000


def test_pageviews_request_covers_exactly_the_complete_months_before_today(wiki):
    _cozy(wiki)
    wikipedia.interest_over_time("cozy mystery", today=TODAY)
    assert wiki.pageview_requests()[0].url.path.endswith("/monthly/20250701/20260930")   # Oct 2026 is in progress: excluded


def test_year_boundary_window(wiki):
    _cozy(wiki, last_month="202512")
    r = wikipedia.interest_over_time("cozy mystery", today=date(2026, 1, 15))
    assert wiki.pageview_requests()[0].url.path.endswith("/monthly/20241001/20251231")
    assert r["months"][-1]["month"] == "2025-12"


def test_months_without_data_count_as_zero_views(wiki):
    wiki.search["cozy mystery"] = ["Cozy mystery"]
    wiki.pages["Cozy mystery"] = {"description": "Subgenre of crime fiction"}
    views = _steady_views(views=1000)
    del views["202605"]                               # the API omits months with no data
    wiki.views["Cozy mystery"] = views
    r = wikipedia.interest_over_time("cozy mystery", today=TODAY)
    assert next(m for m in r["months"] if m["month"] == "2026-05")["views"] == 0


def _months(**by_month):
    """15 months, Jul 2025 .. Sep 2026, 1000 views each unless overridden (keys like '202609')."""
    rows = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 10)]
    return [{"month": f"{y}-{m:02d}", "views": by_month.get(f"{y}{m:02d}", 1000)} for y, m in rows]


# the latest 3 months vs the same 3 months a year earlier (all 1000): exact percentages and the +/-10 boundary
@pytest.mark.parametrize("recent,pct,trend", [
    (1100, 10, "rising"), (1094, 9, "steady"), (1000, 0, "steady"), (906, -9, "steady"), (900, -10, "falling"), (2000, 100, "rising"),
])
def test_trend_thresholds(recent, pct, trend):
    s = wikipedia.summarize(_months(**{"202607": recent, "202608": recent, "202609": recent}))
    assert (s["change_pct"], s["trend"]) == (pct, trend)


def test_thin_traffic_gives_no_indicator():
    assert wikipedia.summarize([{"month": f"2025-{m:02d}", "views": 40} for m in range(1, 13)] * 2) is None


def test_no_percentage_when_the_year_ago_base_is_too_small():
    s = wikipedia.summarize(_months(**{"202507": 20, "202508": 20, "202509": 20}))
    assert s["change_pct"] is None and s["trend"] is None and s["recent_monthly_avg"] == 1000


# ---------------------------------------------------------------- failure behaviour

def test_wikipedia_unreachable_means_no_data_not_an_error(wiki):
    wiki.status = 500
    assert wikipedia.interest_over_time("cozy mystery", today=TODAY) is None
    assert wikipedia.suggest("cozy") == []


def test_rate_limited_pauses_further_requests(wiki):
    wiki.status = 429
    wikipedia.suggest("cozy")
    n = len(wiki.requests)
    wikipedia.suggest("fantasy")
    wikipedia.interest_over_time("romance", today=TODAY)
    assert len(wiki.requests) == n                    # breaker open: nothing more is sent


def test_results_are_cached(wiki):
    _cozy(wiki)
    wikipedia.interest_over_time("cozy mystery", today=TODAY)
    n = len(wiki.requests)
    wikipedia.interest_over_time("cozy mystery novels", today=TODAY)   # same genre phrase
    assert len(wiki.requests) == n


def test_every_request_to_wikipedia_is_counted(wiki):
    _cozy(wiki)
    wikipedia.interest_over_time("cozy mystery", today=TODAY)
    assert len(wiki.requests) == 3                    # opensearch, descriptions, pageviews
    assert next(r for r in api_usage.status()["apis"] if r["api"] == "wikipedia")["used"] == 3


def test_sends_a_descriptive_user_agent(wiki):
    wikipedia.suggest("cozy")
    assert "MirrorJournal" in wiki.requests[0].headers["user-agent"]


# ---------------------------------------------------------------- autocomplete

def test_suggest_keeps_only_genre_shaped_titles_and_strips_qualifiers(wiki):
    wiki.search["thri"] = ["Thriller (genre)", "Thriller (film)", "Thrilla in Manila", "Thrift store"]
    wiki.pages.update({
        "Thriller (genre)": {"description": "Genre of literature, film, and television"},
        "Thriller (film)": {"description": "1984 film"},
        "Thrilla in Manila": {"description": "1975 boxing match"},
        "Thrift store": {"description": "Retail store selling used goods"},
    })
    assert wikipedia.suggest("thri") == ["Thriller"]


def test_suggest_rejects_disambiguated_pages_for_other_media(wiki):
    wiki.search["dark"] = ["Dark academia", "Dark Matter (2024 TV series)", "Epic (novel)"]
    wiki.pages.update({
        "Dark academia": {"description": "Subculture centered on Gothic and classic education"},
        "Dark Matter (2024 TV series)": {"description": "Subgenre of science fiction television"},   # genre-ish words, wrong kind of page
    })
    assert wikipedia.suggest("dark") == ["Dark academia"]


def test_suggest_shows_the_title_that_matched_the_typed_text_not_the_redirect_target(wiki):
    wiki.search["cozy"] = ["Cozy catastrophe"]
    wiki.redirects["Cozy catastrophe"] = "Apocalyptic and post-apocalyptic fiction"
    wiki.pages["Apocalyptic and post-apocalyptic fiction"] = {"description": "Genre of fiction"}
    assert wikipedia.suggest("cozy") == ["Cozy catastrophe"]


def test_suggest_needs_three_characters_and_makes_no_request_for_less(wiki):
    assert wikipedia.suggest("co") == [] and wiki.requests == []


def test_suggest_dedupes_and_caps(wiki):
    titles = [f"Genre {i}" for i in range(10)]
    wiki.search["gen"] = titles + ["genre 1"]
    wiki.pages.update({t: {"description": "Literary genre"} for t in titles})
    out = wikipedia.suggest("gen")
    assert len(out) == wikipedia.MAX_SUGGESTIONS and len({o.lower() for o in out}) == len(out)


# ---------------------------------------------------------------- the keyword pipeline

_RAW = '{"keywords": [{"keyword": "k", "reason": "r"}], "competitors": ["pattern"], "categories": ["Kindle eBooks > Mystery"]}'


def _groq_returns(raw=_RAW):
    resp = MagicMock(choices=[MagicMock(message=MagicMock(content=raw))])
    return patch.object(keyword_research.client.chat.completions, "create", return_value=resp)


def test_keyword_research_adds_interest_only_when_there_is_data(monkeypatch):
    indicator = {"article": {"title": "Cozy mystery", "url": "https://en.wikipedia.org/wiki/Cozy_mystery", "redirected_from": None},
                 "months": [], "recent_monthly_avg": 3000, "change_pct": -5, "trend": "steady"}
    with _groq_returns():
        monkeypatch.setattr(wikipedia, "interest_over_time", lambda topic: indicator)
        assert keyword_research.research_keywords("cozy mystery")["interest"] == indicator
        monkeypatch.setattr(wikipedia, "interest_over_time", lambda topic: None)
        assert "interest" not in keyword_research.research_keywords("cozy mystery set in vermont")


def test_a_failing_interest_lookup_never_breaks_the_response(monkeypatch):
    def boom(topic):
        raise RuntimeError("wikipedia exploded")
    monkeypatch.setattr(wikipedia, "interest_over_time", boom)
    with _groq_returns():
        result = keyword_research.research_keywords("cozy mystery")
    assert "interest" not in result and result["keywords"]


def test_interest_data_is_never_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(wikipedia, "interest_over_time",
                        lambda t: {"article": {"title": "WIKIMARKER"}, "months": [], "recent_monthly_avg": 1, "change_pct": 1, "trend": "steady"})
    with _groq_returns() as create:
        keyword_research.research_keywords("cozy mystery")
    assert "WIKIMARKER" not in repr(create.call_args)
