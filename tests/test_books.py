"""Google Books competitor lookup. Fixtures follow the real API's response schema
(items[].volumeInfo with title/subtitle/authors/description/categories/...).
No test touches the network: see conftest.py."""
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import books, keyword_research

REAL_FETCH = books._fetch_volumes  # captured before conftest's autouse patch


def vol(title, authors=("Some Author",), description=None, categories=("Fiction",), rating=None,
        count=None, lang="en", ptype="BOOK", year="2019", pages=312, subtitle=None, vid=None):
    info = {"title": title, "authors": list(authors), "categories": list(categories),
            "language": lang, "printType": ptype, "publishedDate": f"{year}-05-01", "pageCount": pages,
            "infoLink": f"https://books.google.com/books?id={vid or title}"}
    if description is not None:
        info["description"] = description
    if subtitle:
        info["subtitle"] = subtitle
    if rating is not None:
        info["averageRating"] = rating
    if count is not None:
        info["ratingsCount"] = count
    return {"id": vid or title.lower().replace(" ", "-"), "volumeInfo": info}


COZY_DESC = "A cozy mystery set in a small coastal village where a retired librarian investigates."


# ------------------------------------------------------------------ filtering & ranking

def test_keeps_title_and_author_verbatim_from_the_api():
    volumes = [vol("Murder at the Bakery", ["Jane Q. Writer"], COZY_DESC, ["Fiction / Mystery & Detective / Cozy"])]
    [book] = books.pick_competitors("cozy mystery novels", volumes)
    assert book["kind"] == "book"
    assert book["title"] == "Murder at the Bakery"
    assert book["author"] == "Jane Q. Writer"


def test_every_returned_pair_comes_from_the_response_never_invented():
    volumes = [vol(f"Cozy Mystery Number {i}", [f"Author {i}"], COZY_DESC, ["Cozy Mystery"]) for i in range(12)]
    allowed = {(v["volumeInfo"]["title"], v["volumeInfo"]["authors"][0]) for v in volumes}
    picked = books.pick_competitors("cozy mystery novels", volumes)
    assert 0 < len(picked) <= books.MAX_COMPETITORS
    assert {(b["title"], b["author"]) for b in picked} <= allowed


def test_filters_out_bad_listings():
    good = vol("The Library Murders", ["A. Writer"], COZY_DESC, ["Cozy Mystery"])
    volumes = [
        good,
        vol("Summary of The Library Murders", ["Bookhabits"], COZY_DESC, ["Cozy Mystery"]),          # derivative
        vol("The Library Murders Study Guide", ["Sparky"], COZY_DESC, ["Cozy Mystery"]),             # derivative
        vol("Le Meurtre", ["M. Auteur"], COZY_DESC, ["Cozy Mystery"], lang="fr"),                     # not English
        vol("Cozy Mystery Monthly", ["Editors"], COZY_DESC, ["Cozy Mystery"], ptype="MAGAZINE"),      # not a book
        vol("Anonymous Cozy Mystery", [], COZY_DESC, ["Cozy Mystery"]),                              # no author
        vol("A Terrible Cozy Mystery", ["B. Hack"], COZY_DESC, ["Cozy Mystery"], rating=1.8, count=40),  # poorly rated
        vol("Orbital Mechanics Textbook", ["P. Hysicist"], "A physics text.", ["Science"]),          # wrong genre
    ]
    assert [b["title"] for b in books.pick_competitors("cozy mystery novels", volumes)] == ["The Library Murders"]


def test_unrated_books_are_kept_indie_titles_often_have_no_ratings():
    [b] = books.pick_competitors("cozy mystery", [vol("Quiet Village Crime", ["I. Ndie"], COZY_DESC, ["Cozy Mystery"])])
    assert b["title"] == "Quiet Village Crime"


def test_deduplicates_editions_of_the_same_book():
    volumes = [
        vol("The Library Murders", ["A. Writer"], COZY_DESC, ["Cozy Mystery"], vid="a"),
        vol("The Library Murders: A Cozy Mystery", ["A. Writer"], COZY_DESC, ["Cozy Mystery"], vid="b"),
        vol("Library Murders (Large Print)", ["A. Writer"], COZY_DESC, ["Cozy Mystery"], vid="c"),
        vol("The Library Murders", ["Someone Else"], COZY_DESC, ["Cozy Mystery"], vid="d"),  # different author: kept
    ]
    picked = books.pick_competitors("cozy mystery", volumes)
    assert [(b["title"], b["author"]) for b in picked].count(("The Library Murders", "A. Writer")) == 1
    assert len(picked) == 2


def test_ranks_well_reviewed_books_first_and_caps_at_five():
    volumes = [vol(f"Cozy Mystery {i}", [f"Author {i}"], COZY_DESC, ["Cozy Mystery"], rating=4.4, count=i * 10)
               for i in range(1, 9)]
    picked = books.pick_competitors("cozy mystery", volumes)
    assert len(picked) == 5
    assert [b["title"] for b in picked] == [f"Cozy Mystery {i}" for i in (8, 7, 6, 5, 4)]


def test_genre_relevance_uses_all_meaningful_topic_words():
    dragons = vol("Riders of the Dragon Throne", ["F. Antasy"], "An epic fantasy of dragon riders at war.", ["Fantasy"])
    wrong = vol("Wind Turbine Basics", ["E. Ngineer"], "A guide to rider safety on turbine towers.", ["Technology"])
    assert [b["title"] for b in books.pick_competitors("epic fantasy dragon riders", [wrong, dragons])] == [
        "Riders of the Dragon Throne"]


# ------------------------------------------------------------------ descriptions

def test_description_is_the_apis_own_text_html_stripped_and_truncated():
    long_html = "<p>" + "A cozy mystery in a village. " * 30 + "</p><b>Bold &amp; bright</b>"
    [b] = books.pick_competitors("cozy mystery", [vol("T", ["A"], long_html, ["Cozy Mystery"])])
    assert "<" not in b["description"] and "&amp;" not in b["description"]
    assert b["description"].endswith("…")
    assert len(b["description"]) <= books.DESCRIPTION_MAX_CHARS + 1


def test_short_description_is_not_truncated():
    [b] = books.pick_competitors("cozy mystery", [vol("T", ["A"], "A cozy mystery set in a quiet bakery town.", ["Cozy Mystery"])])
    assert b["description"] == "A cozy mystery set in a quiet bakery town."


def test_no_description_falls_back_to_subtitle_then_to_metadata_never_model_text():
    with_subtitle = vol("Bakery Crimes", ["A"], None, ["Cozy Mystery"], subtitle="A Village Bakery Cozy Mystery")
    bare = vol("Tea Room Crimes", ["B"], None, ["Cozy Mystery"], year="2021", pages=240)
    picked = {b["title"]: b for b in books.pick_competitors("cozy mystery", [with_subtitle, bare])}
    assert picked["Bakery Crimes"]["description"] == "A Village Bakery Cozy Mystery"
    assert picked["Tea Room Crimes"]["description"] == "Cozy Mystery · 2021 · 240 pages"


# ------------------------------------------------------------------ the HTTP layer and every failure mode

def _use_transport(monkeypatch, handler, api_key=""):
    monkeypatch.setattr(books, "_fetch_volumes", REAL_FETCH)
    monkeypatch.setattr(books, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(books.config, "GOOGLE_BOOKS_API_KEY", api_key)


def _ok(items):
    return httpx.Response(200, json={"items": items})


def test_success_path_sends_expected_params_and_returns_books(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return _ok([vol("Murder at the Bakery", ["J. Writer"], COZY_DESC, ["Cozy Mystery"])])

    _use_transport(monkeypatch, handler, api_key="test-key")
    [b] = books.find_competitor_books("cozy mystery novels")
    assert (b["title"], b["author"]) == ("Murder at the Bakery", "J. Writer")
    assert seen["q"] == "cozy mystery novels" and seen["key"] == "test-key"
    assert seen["langRestrict"] == "en" and seen["printType"] == "books" and "fields" in seen


def test_no_key_param_when_no_key_configured(monkeypatch):
    seen = {}
    _use_transport(monkeypatch, lambda r: (seen.update(dict(r.url.params)), _ok([]))[1])
    books.find_competitor_books("cozy mystery")
    assert "key" not in seen


@pytest.mark.parametrize("status, trips_breaker", [(429, True), (400, True), (403, True), (500, False), (503, False)])
def test_http_errors_return_empty_and_quota_errors_pause_lookups(monkeypatch, status, trips_breaker):
    calls = []
    _use_transport(monkeypatch, lambda r: (calls.append(1), httpx.Response(status, json={"error": {"code": status}}))[1])
    assert books.find_competitor_books("cozy mystery") == []
    assert books._breaker_until > time.time() if trips_breaker else books._breaker_until == 0.0
    before = len(calls)
    assert books.find_competitor_books("a different topic") == []
    # once paused, no further requests are made (and no further latency is added)
    assert (len(calls) == before) if trips_breaker else (len(calls) > before)


def test_timeout_malformed_json_and_empty_results_all_return_empty(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("slow")
    _use_transport(monkeypatch, timeout)
    assert books.find_competitor_books("topic one") == []

    _use_transport(monkeypatch, lambda r: httpx.Response(200, text="<html>not json</html>"))
    assert books.find_competitor_books("topic two") == []

    _use_transport(monkeypatch, lambda r: httpx.Response(200, json={"totalItems": 0}))
    assert books.find_competitor_books("topic three") == []

    _use_transport(monkeypatch, lambda r: httpx.Response(200, json={"items": ["not-a-volume", None, {"volumeInfo": "x"}]}))
    assert books.find_competitor_books("topic four") == []


def test_repeat_topic_is_served_from_cache(monkeypatch):
    calls = []
    _use_transport(monkeypatch, lambda r: (calls.append(1), _ok([vol("Murder at the Bakery", ["J"], COZY_DESC, ["Cozy Mystery"])]))[1])
    assert books.find_competitor_books("cozy mystery")
    assert books.find_competitor_books("Cozy   Mystery")  # same query, different case/spacing
    assert len(calls) == 1


def test_queries_run_in_parallel(monkeypatch):
    active, peak, lock = 0, 0, threading.Lock()

    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.3)
        with lock:
            active -= 1
        q = request.url.params['q']
        return _ok([vol(f"Cozy Mystery {q}", [f"Author of {q}"], COZY_DESC, ["Cozy Mystery"])])

    _use_transport(monkeypatch, handler)
    started = time.time()
    result = books.find_competitor_books("cozy mystery", extra_queries=["village cozy", "bakery cozy"])
    elapsed = time.time() - started
    assert peak == 3 and elapsed < 0.8     # three 0.3s requests overlapped instead of taking ~0.9s
    assert len(result) == 3


def test_a_slow_lookup_is_abandoned_at_the_deadline(monkeypatch):
    monkeypatch.setattr(books, "LOOKUP_DEADLINE_SECONDS", 0.2)
    release = threading.Event()
    _use_transport(monkeypatch, lambda r: (release.wait(2), _ok([]))[1])
    started = time.time()
    assert books.find_competitor_books("slow topic") == []
    assert time.time() - started < 1.0
    release.set()


# ------------------------------------------------------------------ integration with research_keywords

def _model_reply(competitors, keywords=("cozy bakery mystery", "village cozy mystery", "small town sleuth")):
    import json
    raw = json.dumps({"keywords": [{"keyword": k, "reason": "r"} for k in keywords],
                      "competitors": competitors, "categories": ["Kindle eBooks > Mystery"]})
    return MagicMock(choices=[MagicMock(message=MagicMock(content=raw))])


def _research(topic, reply, volumes_for_query, categories=("Kindle eBooks > Mystery",)):
    """Runs research_keywords with a fake model reply and a fake Google lookup.
    volumes_for_query(topic, extra_queries) -> raw volumes."""
    import json
    payload = json.loads(reply.choices[0].message.content)
    payload["categories"] = list(categories)
    reply = MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))])
    with patch.object(keyword_research.client.chat.completions, "create", return_value=reply),          patch.object(keyword_research.books, "lookup_volumes", side_effect=volumes_for_query) as lookup:
        return keyword_research.research_keywords(topic), lookup


def _volumes(n, start=0):
    return [vol(f"Real Book {i}", [f"Real Author {i}"], COZY_DESC, ["Cozy Mystery"]) for i in range(start, start + n)]


PATTERNS = ["Village cozy with a retired teacher and a dog, light tone",
            "Cozy series set in a seaside bakery with a baker sleuth",
            "Standalone cozy in a rural bookshop with a rare-books theme"]


def test_five_real_books_fill_every_slot_and_no_pattern_blurbs_show():
    result, lookup = _research("cozy mystery", _model_reply(PATTERNS), lambda *a, **k: _volumes(6))
    assert [c["kind"] for c in result["competitors"]] == ["book"] * 5
    assert all(c["title"].startswith("Real Book ") for c in result["competitors"])
    assert lookup.call_count == 1          # enough books: no second wave


def test_fewer_books_than_slots_are_topped_up_per_slot_with_pattern_blurbs():
    result, lookup = _research("cozy mystery", _model_reply(PATTERNS), lambda *a, **k: _volumes(2))
    assert [c["kind"] for c in result["competitors"]] == ["book", "book", "pattern", "pattern", "pattern"]
    assert lookup.call_count == 2          # second parallel wave on the model's top keywords
    assert lookup.call_args.kwargs["extra_queries"] == ["cozy mystery", "cozy bakery mystery", "village cozy mystery"]


def test_fiction_topics_add_subject_fiction_to_the_second_wave():
    result, lookup = _research("cozy mystery", _model_reply(PATTERNS), lambda *a, **k: _volumes(1),
                               categories=("Kindle eBooks > Literature & Fiction > Mystery",))
    assert lookup.call_args.kwargs["extra_queries"] == [
        "cozy mystery subject:fiction", "cozy bakery mystery subject:fiction", "village cozy mystery subject:fiction"]


def test_zero_relevant_books_falls_back_to_exactly_the_old_pattern_list():
    result, _ = _research("cozy mystery", _model_reply(PATTERNS), lambda *a, **k: [])
    assert result["competitors"] == [{"kind": "pattern", "text": t} for t in PATTERNS]


def test_pattern_fallback_never_contains_a_named_work():
    sneaky = ["Murder in the Stacks by Jane Smith, a bookshop cozy",
              'Much like "The Quiet Garden Murders" but lighter',
              "The Thursday Club series about retired sleuths",
              "Village cozy with a retired teacher and a dog, light tone"]
    result, _ = _research("cozy mystery", _model_reply(sneaky), lambda *a, **k: [])
    assert result["competitors"] == [{"kind": "pattern", "text": "Village cozy with a retired teacher and a dog, light tone"}]


def test_api_outage_end_to_end_still_returns_the_section_without_an_error(monkeypatch):
    _use_transport(monkeypatch, lambda r: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}))
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_model_reply(PATTERNS)):
        result = keyword_research.research_keywords("cozy mystery novels")
    assert result["competitors"] == [{"kind": "pattern", "text": t} for t in PATTERNS]
    assert result["keywords"] and result["categories"]     # the rest of the page is unaffected


# ------------------------------------------------------------------ diagnostics (what the Railway logs will say)

FAKE_KEY = "AIzaSyFAKE0000000000000000000000000000"


@pytest.mark.parametrize("status, body, expect", [
    (403, {"error": {"status": "PERMISSION_DENIED", "message": "Books API has not been used in project 1 before or it is disabled.",
                     "errors": [{"reason": "accessNotConfigured"}]}}, "accessNotConfigured"),
    (403, {"error": {"status": "PERMISSION_DENIED", "message": "Requests from referer <empty> are blocked.",
                     "errors": [{"reason": "forbidden"}]}}, "referer"),
    (400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key.",
                     "errors": [{"reason": "badRequest"}]}}, "API key not valid"),
    (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded for quota metric 'Queries'.",
                     "errors": [{"reason": "rateLimitExceeded"}]}}, "Quota exceeded"),
])
def test_refusals_are_logged_with_googles_reason_and_never_the_key(monkeypatch, caplog, status, body, expect):
    _use_transport(monkeypatch, lambda r: httpx.Response(status, json=body), api_key=FAKE_KEY)
    with caplog.at_level("INFO", logger="app.books"):
        assert books.find_competitor_books("cozy mystery novels") == []
    text = caplog.text
    assert f"HTTP {status}" in text and expect in text
    assert "key sent" in text
    assert FAKE_KEY not in text                     # the secret never reaches the logs


def test_a_successful_lookup_logs_the_volume_count(monkeypatch, caplog):
    _use_transport(monkeypatch, lambda r: _ok([vol("Murder at the Bakery", ["J"], COZY_DESC, ["Cozy Mystery"])]), api_key=FAKE_KEY)
    with caplog.at_level("INFO", logger="app.books"):
        books.find_competitor_books("cozy mystery")
    assert "HTTP 200" in caplog.text and "1 volumes" in caplog.text


def test_describe_key_reports_shape_without_revealing_the_secret(monkeypatch):
    monkeypatch.setattr(books.config, "GOOGLE_BOOKS_API_KEY", "")
    assert "NOT SET" in books.describe_key() and "GOOGLE_BOOKS_API_KEY" in books.describe_key()
    monkeypatch.setattr(books.config, "GOOGLE_BOOKS_API_KEY", FAKE_KEY)
    described = books.describe_key()
    assert "present" in described and str(len(FAKE_KEY)) in described and "AIza (normal)" in described
    assert FAKE_KEY not in described and FAKE_KEY[4:12] not in described
    monkeypatch.setattr(books.config, "GOOGLE_BOOKS_API_KEY", " " + FAKE_KEY + "\n")
    assert "WHITESPACE" in books.describe_key()
    monkeypatch.setattr(books.config, "GOOGLE_BOOKS_API_KEY", "sk-not-a-google-key")
    assert "check it" in books.describe_key()


# ------------------------------------------------------------------ quality rules found by running against the live API

def test_writing_tools_and_planners_are_not_competitors():
    # Real live results for "cozy mystery novels": "Cozy Mystery Novel Storybuilder" etc.
    volumes = [
        vol("Cozy Mystery Novel Storybuilder", ["Kit Tunstall"], "Unravel the secrets of writing cozy mysteries.", ["Language Arts & Disciplines"]),
        vol("Cozy Mystery Novel Plus Storybuilder", ["Kit Tunstall"], "Craft an unforgettable cozy mystery.", ["Fiction"]),
        vol("Cozy Mystery Plot Planner", ["P. Lanner"], "A planner for your cozy mystery.", ["Fiction"]),
        vol("How to Write a Cozy Mystery", ["W. Riter"], "Writing a cozy mystery.", ["Fiction"]),
        vol("Mystery at Seagrave Hall", ["Clare Chase"], COZY_DESC, ["Fiction / Mystery & Detective / Cozy"]),
    ]
    assert [b["title"] for b in books.pick_competitors("cozy mystery novels", volumes, fiction=True)] == ["Mystery at Seagrave Hall"]


def test_fiction_topics_exclude_criticism_film_and_craft_categories():
    # Real live results for "hard-boiled noir" were mostly scholarship about noir.
    volumes = [
        vol("War Noir", ["Sarah Trott"], "The hard-boiled style and war experience.", ["Literary Criticism / American / General"]),
        vol("American, Hard-boiled and Noir", ["William Marling"], "A Guide to the Fiction and Film of hard-boiled noir.", ["Performing Arts / Film"]),
        vol("Hard-boiled", ["P. Thompson"], "Great lines from classic hard-boiled noir films.", ["Reference / Quotations"]),
        vol("The Long Goodbye Type", ["R. Chandlerish"], "A hard-boiled noir detective novel.", ["Fiction / Mystery & Detective / Hard-Boiled"]),
        vol("Untagged Noir Novel", ["I. Ndie"], "A hard-boiled noir detective story.", ()),  # no categories at all: kept
    ]
    picked = [b["title"] for b in books.pick_competitors("hard-boiled noir", volumes, fiction=True)]
    assert set(picked) == {"The Long Goodbye Type", "Untagged Noir Novel"}


def test_non_fiction_topics_keep_reference_and_education_categories():
    volumes = [vol("Keto for Beginners", ["C. Ook"], "A keto cookbook for beginners.", ["Cooking / Health & Healing / Weight Loss", "Reference"])]
    assert len(books.pick_competitors("keto cookbook for beginners", volumes, fiction=False)) == 1


def test_childrens_books_are_not_competitors_for_adult_genres_but_are_for_childrens_topics():
    kids = vol("Dragon Riders Academy", ["K. Idsauthor"], "Dragon riders in fantasy school.", ["Juvenile Fiction / Fantasy & Magic"])
    assert books.pick_competitors("epic fantasy dragon riders", [kids], fiction=True) == []
    assert len(books.pick_competitors("children's dragon riders fantasy", [kids], fiction=True)) == 1


def test_one_author_cannot_fill_the_whole_list():
    # Real live results for "epic fantasy dragon riders": three volumes by R M Schultz.
    schultz = [vol(f"Through Blood and Dragons {i}", ["R M Schultz"], "An epic fantasy of dragon riders.", ["Fiction / Fantasy / Epic"])
               for i in range(3)]
    others = [vol(f"Dragon Riders {i}", [f"Author {i}"], "Epic fantasy dragon riders.", ["Fiction / Fantasy / Epic"]) for i in range(3)]
    picked = books.pick_competitors("epic fantasy dragon riders", schultz + others, fiction=True)
    assert len(picked) == 5
    assert [b["author"] for b in picked].count("R M Schultz") == books.MAX_PER_AUTHOR
