"""Wikipedia as a genre-level signal for Keyword & Category Research.

Two uses, both keyless (Wikimedia's APIs are free; they ask for a descriptive User-Agent, below):

* `suggest(prefix)` - topic autocomplete: Wikipedia's opensearch for matching article titles,
  kept only when the article is itself genre/category-shaped (judged from its short description).
* `interest_over_time(topic)` - monthly pageviews of the Wikipedia article for the topic's genre,
  as a rough "public curiosity" indicator.

Genre granularity only, on purpose. An article is used only when its title *is* the topic (or the
topic plus "fiction"/"novel"/...), and only when it is genre-shaped - never a nearest match.
"Cozy mystery set in Vermont bakeries" has no article, so it gets no interest section rather than
the numbers of an unrelated page. Pageviews measure attention to a Wikipedia page, not book demand;
the UI says so.

Only numbers, titles and links are ever surfaced. Everything here is plain code: nothing from
Wikipedia is sent to a model. All failures are swallowed into "no data" (nothing here raises).
"""

import logging
import re
import threading
import time
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from app import api_usage

logger = logging.getLogger(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
PAGEVIEWS_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/"
                 "all-access/user/{title}/monthly/{start}/{end}")
# Wikimedia asks for a User-Agent that identifies the app and a way to contact it.
USER_AGENT = "MirrorJournal/1.0 (https://www.themirrorjournal.org; topic research tool)"

REQUEST_TIMEOUT_SECONDS = 3.0
SUGGEST_CACHE_TTL_SECONDS = 3600
INTEREST_CACHE_TTL_SECONDS = 6 * 3600       # monthly data barely moves
CACHE_MAX_ENTRIES = 500
BREAKER_COOLDOWN_SECONDS = 300              # after HTTP 429 / 5xx, stop asking for a while
MAX_SUGGESTIONS = 6
OPENSEARCH_LIMIT = 30                       # most prefix matches are not genres: a short window starves the filter
ARTICLE_LOOKUP_LIMIT = 25                  # wider than the suggestion list: "Mystery fiction" sits past the first 10 for "mystery"
MIN_PREFIX_CHARS = 3
MAX_TOPIC_WORDS = 6                         # longer than this is a sub-niche, never a genre article
WINDOW_MONTHS = 12                          # months shown
COMPARE_MONTHS = 3                          # latest N months vs the same N months a year earlier
MIN_AVG_MONTHLY_VIEWS = 100                 # below this the percentages are noise
TREND_THRESHOLD_PCT = 10

def make_client(transport=None) -> httpx.Client:
    """The one place the client is configured (timeout, User-Agent); tests pass a mock transport."""
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport)


_client = make_client()

_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_breaker_until = 0.0


class WikipediaError(RuntimeError):
    """One Wikipedia request failed. Always caught inside this module."""


# ---------------------------------------------------------------- HTTP

def _get_json(url: str, params: dict | None = None):
    global _breaker_until
    if time.time() < _breaker_until:
        raise WikipediaError("paused after a rate-limit / server error")
    api_usage.record("wikipedia")
    try:
        resp = _client.get(url, params=params)
    except httpx.HTTPError as e:
        raise WikipediaError(f"request failed: {e.__class__.__name__}") from e
    if resp.status_code == 429 or resp.status_code >= 500:
        _breaker_until = time.time() + BREAKER_COOLDOWN_SECONDS
        logger.warning("wikipedia lookups paused for %ds: HTTP %d", BREAKER_COOLDOWN_SECONDS, resp.status_code)
        raise WikipediaError(f"HTTP {resp.status_code}")
    if resp.status_code == 404:
        return None                         # e.g. pageviews for a title with no data
    if resp.status_code != 200:
        raise WikipediaError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise WikipediaError("response was not JSON") from e


def _cached(key: tuple, ttl: float, compute):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = compute()                       # may raise: failures are deliberately not cached
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, value)
    return value


def _opensearch(term: str, limit: int = OPENSEARCH_LIMIT) -> list[str]:
    data = _get_json(API_URL, {"action": "opensearch", "search": term, "limit": limit, "namespace": 0, "format": "json"})
    titles = data[1] if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) else []
    return [t for t in titles if isinstance(t, str)]


def _describe(titles: list[str]) -> dict[str, dict]:
    """requested title -> {"title": canonical title after redirects, "description": short description,
    "disambiguation": bool}, for titles that exist."""
    if not titles:
        return {}
    data = _get_json(API_URL, {
        "action": "query", "titles": "|".join(titles), "prop": "description|pageprops", "ppprop": "disambiguation",
        "redirects": 1, "format": "json", "formatversion": 2,
    })
    query = (data or {}).get("query") or {}
    pages = {p.get("title"): p for p in query.get("pages") or [] if isinstance(p, dict) and not p.get("missing")}
    redirected = {r["from"]: r["to"] for r in query.get("redirects") or [] if "from" in r and "to" in r}
    out = {}
    for requested in titles:
        page = pages.get(redirected.get(requested, requested))
        if page:
            out[requested] = {"title": page["title"], "description": page.get("description") or "",
                              "disambiguation": "disambiguation" in (page.get("pageprops") or {})}
    return out


# ---------------------------------------------------------------- "is this a genre / category?"

# A genre/category description names a category ("Subgenre of crime fiction", "Aesthetic of
# nostalgia ...") or a kind of book ("Book of recipes ...", "Instructional book for ..."). A
# description of one particular work is just "<kind> novel" ("14th-century Chinese historical novel")
# and is not enough.
_CATEGORY_WORD_RE = re.compile(r"\b(genre|subgenre|aesthetic|subculture|trope)\b", re.I)
_BOOK_TYPE_RE = re.compile(
    r"\b(book|books)\s+(of|for|about|on|that)\b|\b(literature|fiction|nonfiction|non-fiction|writing)\s+(written|about|for|set|that|featuring)\b"
    r"|^(literature|fiction|nonfiction|non-fiction)\b"
    r"|\b(form|type|kind)\s+of\s+(\w+\s+){0,3}(literature|writing|fiction|narrative)\b", re.I)
# Only literature-ish words rescue a media description ("Genre of literature, film, and television");
# "fiction" alone does not ("American science fiction television series").
_BOOKISH_RE = re.compile(r"\b(literature|literary|novels?|books?|prose)\b", re.I)
_OTHER_MEDIA_RE = re.compile(
    r"\b(film|films|television|tv|series|album|albums|band|song|songs|music|musical|video game|game|anime|manga|"
    r"musician|singer|rapper|artist|company)\b", re.I)
# A description of one particular work or person ("1925 novel by F. Scott Fitzgerald"), not a genre.
_SPECIFIC_WORK_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b|\bby\s+[A-Z]")


def genre_shaped(description: str) -> bool:
    if not description or _SPECIFIC_WORK_RE.search(description):
        return False
    if not (_CATEGORY_WORD_RE.search(description) or _BOOK_TYPE_RE.search(description)):
        return False
    if _OTHER_MEDIA_RE.search(description) and not _BOOKISH_RE.search(description):
        return False                        # "Film genre", "Video game genre", "American science fiction television series"
    return True


# ---------------------------------------------------------------- matching a topic to an article title

_TRAILING_GENERIC = {"books", "book", "novels", "novel", "stories", "story", "ebooks", "ebook", "audiobooks", "audiobook"}
_ACCEPTED_SUFFIXES = ("", " fiction", " novel", " literature", " genre", " book")   # in preference order
# "Thriller (genre)" is the genre article; "Romance (prose fiction)" or "Self-Help (Smiles book)" are
# disambiguated pages for some other sense of the word and must not stand in for the topic.
_GENRE_QUALIFIERS = {"genre", "fiction", "literature", "novel", "book", "books", "literary genre"}
_QUALIFIER_RE = re.compile(r"\(([^)]*)\)\s*$")


def _qualifier_ok(title: str) -> bool:
    m = _QUALIFIER_RE.search(title)
    return m is None or m.group(1).strip().lower() in _GENRE_QUALIFIERS


def _words(text: str) -> list[str]:
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    return re.findall(r"[a-z0-9']+", text.replace("-", " "))


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _key(words: list[str]) -> str:
    if words:
        words = words[:-1] + [_singular(words[-1])]
    return " ".join(words)


def topic_key(topic: str) -> str:
    """The genre phrase of a topic: lowercased, 'books'/'novels' etc. dropped from the end, last word singular."""
    words = _words(topic)
    while words and words[-1] in _TRAILING_GENERIC:
        words.pop()
    return _key(words)


def find_genre_article(topic: str) -> dict | None:
    """-> {"title": canonical article title, "description": ...} or None when the topic has no
    genre-level article. Never a nearest match."""
    key = topic_key(topic)
    if not key or len(key.split()) > MAX_TOPIC_WORDS:
        return None
    wanted = {key + suffix: i for i, suffix in enumerate(_ACCEPTED_SUFFIXES)}
    candidates = sorted({t for t in _opensearch(key, ARTICLE_LOOKUP_LIMIT) if _key(_words(t)) in wanted and _qualifier_ok(t)},
                        key=lambda t: (wanted[_key(_words(t))], t))
    described = _describe(candidates)
    for requested in candidates:
        info = described.get(requested)
        if info and not info["disambiguation"] and genre_shaped(info["description"]):
            # `redirected_from` is set when Wikipedia itself redirects the matched title to another
            # article (e.g. "Epic fantasy" -> "High fantasy"), so the UI can say which page is measured.
            same = _key(_words(requested)) == _key(_words(info["title"]))
            return {**info, "redirected_from": None if same else requested}
    return None


# ---------------------------------------------------------------- pageviews

def _month_start(d: date, offset: int) -> date:
    """First day of the month `offset` months from d's month (negative = earlier)."""
    index = d.year * 12 + (d.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def _pageviews(title: str, today: date) -> list[dict] | None:
    """Last WINDOW_MONTHS complete months (+ the comparison months before them), zero-filled."""
    last = _month_start(today, -1)                              # latest complete month
    history = WINDOW_MONTHS + COMPARE_MONTHS                      # the shown year + the year-ago comparison months
    first = _month_start(last, -(history - 1))
    end = _month_start(last, 1) - timedelta(days=1)
    url = PAGEVIEWS_URL.format(title=quote(title.replace(" ", "_"), safe=""), start=first.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))
    data = _get_json(url)
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not items:
        return None
    by_month = {str(i.get("timestamp", ""))[:6]: int(i.get("views") or 0) for i in items if isinstance(i, dict)}
    months = []
    for back in range(history - 1, -1, -1):
        m = _month_start(last, -back)
        months.append({"month": m.strftime("%Y-%m"), "views": by_month.get(m.strftime("%Y%m"), 0)})
    return months


def summarize(months: list[dict]) -> dict | None:
    """months (oldest first, WINDOW_MONTHS + COMPARE_MONTHS of them) -> the indicator, or None if too thin."""
    shown = months[-WINDOW_MONTHS:]
    if sum(m["views"] for m in shown) / len(shown) < MIN_AVG_MONTHLY_VIEWS:
        return None
    recent = months[-COMPARE_MONTHS:]
    year_ago = months[-COMPARE_MONTHS - 12:-12]
    recent_avg = sum(m["views"] for m in recent) / len(recent)
    prior_avg = sum(m["views"] for m in year_ago) / len(year_ago) if year_ago else 0
    change = round((recent_avg - prior_avg) / prior_avg * 100) if prior_avg >= MIN_AVG_MONTHLY_VIEWS else None
    trend = None if change is None else "rising" if change >= TREND_THRESHOLD_PCT else "falling" if change <= -TREND_THRESHOLD_PCT else "steady"
    return {"months": shown, "recent_monthly_avg": round(recent_avg), "change_pct": change, "trend": trend,
            "compared": f"{recent[0]['month']}..{recent[-1]['month']} vs {year_ago[0]['month']}..{year_ago[-1]['month']}" if year_ago else None}


def interest_over_time(topic: str, today: date | None = None) -> dict | None:
    """-> {"article": {"title", "url"}, "months": [{"month", "views"}...], "recent_monthly_avg",
    "change_pct", "trend"} for the topic's genre article, or None (no genre-level article, too little
    traffic to mean anything, or Wikipedia unreachable). Never raises."""
    today = today or date.today()
    try:
        def compute():
            article = find_genre_article(topic)
            if not article:
                return None
            months = _pageviews(article["title"], today)
            summary = summarize(months) if months else None
            if not summary:
                return None
            return {"article": {"title": article["title"], "redirected_from": article["redirected_from"],
                                "url": "https://en.wikipedia.org/wiki/" + quote(article["title"].replace(" ", "_"), safe="()_,'")},
                    **summary}
        return _cached(("interest", topic_key(topic), today.strftime("%Y-%m")), INTEREST_CACHE_TTL_SECONDS, compute)
    except WikipediaError as e:
        logger.info("wikipedia interest lookup for %r unavailable: %s", topic, e)
        return None
    except Exception:
        logger.exception("wikipedia interest lookup for %r failed", topic)
        return None


# ---------------------------------------------------------------- autocomplete

def _display(title: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()


def suggest(prefix: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Genre/category-shaped Wikipedia article titles for what the user has typed so far. Never raises."""
    prefix = " ".join(prefix.split())
    if len(prefix) < MIN_PREFIX_CHARS:
        return []
    try:
        def compute():
            titles = _opensearch(prefix)
            out, seen = [], set()
            for requested, info in sorted(_describe(titles).items(), key=lambda kv: titles.index(kv[0])):
                text = _display(requested)      # the title that matched the typed text (a redirect's own name), not its target
                if (info["disambiguation"] or not _qualifier_ok(requested) or not genre_shaped(info["description"])
                        or text.lower() in seen):
                    continue
                seen.add(text.lower())
                out.append(text)
            return out
        return _cached(("suggest", prefix.lower()), SUGGEST_CACHE_TTL_SECONDS, compute)[:limit]
    except WikipediaError as e:
        logger.info("wikipedia suggestions for %r unavailable: %s", prefix, e)
        return []
    except Exception:
        logger.exception("wikipedia suggestions for %r failed", prefix)
        return []
