"""Real competitor books for Keyword & Category Research, from the Google Books API.

Why this exists: asking the model for competitor titles produced invented books
under real authors' names, and asking it to only describe patterns produced
vague blurbs. Here the title and author come straight from the API response —
this module never generates or edits them — so a fabricated book can't appear
by construction. Everything here is best-effort: any failure (no key, quota,
timeout, malformed JSON, nothing relevant) returns an empty list and the caller
falls back to the pattern blurbs, never an error the user sees.

Google Books needs our own API key: anonymous requests now get HTTP 429 with a
zero daily quota (checked against the live API), and an invalid key gets 400.
Set GOOGLE_BOOKS_API_KEY (a free key from a Google Cloud project with the Books
API enabled; the default quota is 1,000 requests/day). One search costs one
request per query, cached below, so a few hundred searches a day is comfortable.
"""

import html
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from app import config

logger = logging.getLogger(__name__)

VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"

REQUEST_TIMEOUT_SECONDS = 4.0      # per HTTP request
LOOKUP_DEADLINE_SECONDS = 5.0      # for a whole parallel lookup, however many queries
MAX_RESULTS_PER_QUERY = 40         # the API's maximum page size
MAX_COMPETITORS = 5
DESCRIPTION_MAX_CHARS = 150        # fits the two-line result card
CACHE_TTL_SECONDS = 6 * 3600
CACHE_MAX_ENTRIES = 200
# After a quota / bad-key response every further call would fail the same way,
# so stop calling (and stop adding latency) for a while.
BREAKER_COOLDOWN_SECONDS = 600

# Only these fields are requested — keeps each response small and fast.
_FIELDS = (
    "items(id,volumeInfo(title,subtitle,authors,publishedDate,description,categories,"
    "averageRating,ratingsCount,pageCount,language,printType,infoLink,canonicalVolumeLink))"
)

_client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="books")

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_breaker_until = 0.0


class BooksLookupError(RuntimeError):
    """One Google Books request failed. Always caught inside this module."""


def describe_key() -> str:
    """Redacted description of the configured key for the startup log: whether it
    is present and its shape (length, and the "AIza" prefix every Google API key
    has) - enough to spot a missing, truncated or mis-pasted value without ever
    printing the secret."""
    key = config.GOOGLE_BOOKS_API_KEY
    if not key:
        return "NOT SET (env var GOOGLE_BOOKS_API_KEY) - anonymous requests are refused by Google, so competitors will be pattern-only"
    prefix_ok = key.startswith("AIza")
    whitespace = key != key.strip()
    return (f"present, {len(key)} chars, starts with {'AIza (normal)' if prefix_ok else 'something other than AIza (check it)'}"
            + (", HAS LEADING/TRAILING WHITESPACE" if whitespace else ""))


def log_configuration() -> None:
    """Called from main.py's startup hook. (A module-level logger.info runs at import time, BEFORE
    main.py configures logging, so the line would be silently dropped - which it was.)"""
    logger.info("GOOGLE_BOOKS_API_KEY: %s", describe_key())


def _google_error_detail(resp: "httpx.Response") -> str:
    """Google's own explanation for a refused request (reason + message), so the
    log says WHY (quota, API not enabled, key restricted, key invalid) rather than
    just the status code. The key is never part of these messages."""
    try:
        err = resp.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)) or err.get("status", "")
        return f"{reasons}: {str(err.get('message', ''))[:200]}"
    except Exception:
        return resp.text[:200].replace("\n", " ")


# ---------------------------------------------------------------- HTTP

def _trip_breaker(reason: str) -> None:
    global _breaker_until
    _breaker_until = time.time() + BREAKER_COOLDOWN_SECONDS
    logger.warning("google books lookups paused for %ds: %s", BREAKER_COOLDOWN_SECONDS, reason)


def _fetch_volumes(query: str) -> list[dict]:
    """One search request -> the raw `items` list ([] if the query matched nothing)."""
    params = {
        "q": query,
        "maxResults": MAX_RESULTS_PER_QUERY,
        "printType": "books",
        "langRestrict": "en",
        "orderBy": "relevance",
        "fields": _FIELDS,
    }
    if config.GOOGLE_BOOKS_API_KEY:
        params["key"] = config.GOOGLE_BOOKS_API_KEY
    try:
        resp = _client.get(VOLUMES_URL, params=params)
    except httpx.HTTPError as e:  # timeout, connection error, ...
        raise BooksLookupError(f"request failed: {e.__class__.__name__}") from e

    if resp.status_code in (400, 401, 403, 429):
        # Quota exhausted, anonymous access refused, or a bad/disabled key: none
        # of these fix themselves within a request, so don't keep asking.
        detail = _google_error_detail(resp)
        _trip_breaker(f"HTTP {resp.status_code} ({detail}) [key {'sent' if config.GOOGLE_BOOKS_API_KEY else 'NOT sent'}]")
        raise BooksLookupError(f"HTTP {resp.status_code}: {detail}")
    if resp.status_code != 200:
        raise BooksLookupError(f"HTTP {resp.status_code}: {_google_error_detail(resp)}")
    try:
        data = resp.json()
    except ValueError as e:
        raise BooksLookupError("response was not JSON") from e
    items = data.get("items") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else []
    logger.info("google books HTTP 200 for %r: %d volumes", query, len(items))
    return items


def search_volumes(query: str) -> list[dict]:
    """_fetch_volumes behind a small TTL cache (saves quota and latency on repeat topics)."""
    key = " ".join(query.lower().split())
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    items = _fetch_volumes(query)
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, items)
    return items


# ---------------------------------------------------------------- relevance / cleanup

_STOP_WORDS = {
    "a", "an", "the", "of", "and", "or", "for", "in", "on", "to", "with", "about", "my", "your",
    "novel", "novels", "book", "books", "story", "stories", "series", "set", "that", "is", "how",
}

# Summaries, study guides and other derivative listings that name a real book
# but aren't competitors (and are the usual low-quality noise in book search).
_DERIVATIVE_TITLE_RE = re.compile(
    r"\b(summary|summaries|study guide|analysis of|workbook|sparknotes|cliffsnotes|quicklet|"
    r"bookhabits|book review|reading guide|trivia|unofficial|conversation starters|"
    r"notes on|teaching guide|lesson plan|journal for|notebook|"
    r"story ?builder|plot (?:generator|builder|planner)|character (?:sheet|builder)|planner|template|"
    r"writing prompts|prompts for|how to write|writing guide|writing a |write your|"
    r"colou?ring book|quiz book|activity book)\b",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    for suffix in ("ers", "er", "ies", "es", "s", "ing"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _topic_tokens(topic: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", topic.lower())
    return [_stem(w) for w in words if w not in _STOP_WORDS and len(w) > 1]


def _normalize_title(title: str) -> str:
    base = re.split(r"[:(\[]", title, maxsplit=1)[0]
    base = re.sub(r"^(the|a|an)\s+", "", base.strip().lower())
    return re.sub(r"[^a-z0-9]+", "", base)


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(html.unescape(text).split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-—")
    return cut + "…"


def _relevance(tokens: list[str], info: dict) -> float:
    """Share of the topic's meaningful words found in the book's own metadata.
    Title, subtitle and categories count double: a book that's actually in the
    genre says so there, while a stray word in a description is weak evidence."""
    if not tokens:
        return 1.0
    strong = " ".join(
        [info.get("title") or "", info.get("subtitle") or ""] + list(info.get("categories") or [])
    ).lower()
    weak = (info.get("description") or "").lower()
    score = 0.0
    for t in tokens:
        pattern = r"\b" + re.escape(t)
        if re.search(pattern, strong):
            score += 1.0
        elif re.search(pattern, weak):
            score += 0.5
    return score / len(tokens)


def _book_from_volume(volume: dict) -> dict | None:
    """Raw API volume -> the fields we show, or None if it fails basic sanity.
    Title and author are passed through verbatim."""
    info = volume.get("volumeInfo") if isinstance(volume, dict) else None
    if not isinstance(info, dict):
        return None
    title = (info.get("title") or "").strip()
    authors = [a.strip() for a in (info.get("authors") or []) if isinstance(a, str) and a.strip()]
    if not title or not authors or len(title) > 120:
        return None
    if info.get("printType") not in (None, "BOOK"):
        return None
    if info.get("language") not in (None, "en"):
        return None
    if _DERIVATIVE_TITLE_RE.search(title) or _DERIVATIVE_TITLE_RE.search(info.get("subtitle") or ""):
        return None
    rating, count = info.get("averageRating"), info.get("ratingsCount") or 0
    if isinstance(rating, (int, float)) and rating < 3.0 and count >= 5:
        return None  # enough readers to call it poorly received
    return {"volume_id": volume.get("id"), "title": title, "authors": authors, "info": info}


def _description_for(info: dict) -> str:
    """The API's own description when it has one; otherwise a line built only
    from its metadata fields (category, year, length) — never model text."""
    raw = _strip_html(info.get("description") or "")
    if len(raw) >= 30:
        return _truncate(raw, DESCRIPTION_MAX_CHARS)
    subtitle = (info.get("subtitle") or "").strip()
    if subtitle:
        return _truncate(subtitle, DESCRIPTION_MAX_CHARS)
    parts = []
    categories = info.get("categories") or []
    if categories:
        parts.append(str(categories[0]))
    year = (info.get("publishedDate") or "")[:4]
    if year.isdigit():
        parts.append(year)
    if isinstance(info.get("pageCount"), int) and info["pageCount"] > 0:
        parts.append(f"{info['pageCount']} pages")
    return " · ".join(parts)


MIN_RELEVANCE = 0.5
MAX_PER_AUTHOR = 2   # volumes of one series by one author otherwise crowd out the market

# Google files criticism, craft books, film guides and so on under these. For a
# fiction topic they are never competitors (a search for "hard-boiled noir" is
# mostly scholarship about noir otherwise).
_NONFICTION_CATEGORY_MARKERS = (
    "literary criticism", "authorship", "language arts", "reference", "study aids", "education",
    "performing arts", "self-help", "business", "social science", "biography", "computers",
    "art /", "photography", "history /", "psychology", "philosophy", "political science",
)
_JUVENILE_CATEGORY_PREFIXES = ("juvenile", "young adult")
_YOUNG_READER_TOPIC_RE = re.compile(r"\b(children|child|kids?|juvenile|teens?|young adult|ya|middle grade|picture)\b", re.I)


# Words that mark a topic or an Amazon category as a fiction genre. Looking only for the word
# "fiction" missed e.g. a "Mystery, Thriller & Suspense > ... > Hard-Boiled" suggestion.
_FICTION_WORDS_RE = re.compile(
    r"\b(fiction|novels?|mystery|mysteries|thrillers?|suspense|romance|romances|fantasy|horror|noir|"
    r"detective|whodunits?|cozy|cosy|western|saga|space opera|sci-?fi|dystopian|paranormal|litrpg|"
    r"dragons?|vampires?|werewolf|werewolves)\b", re.I)
_NONFICTION_OVERRIDE_RE = re.compile(r"\b(true crime|cookbooks?|recipes?|memoirs?|self-help|how to|guide)\b", re.I)


def looks_like_fiction(topic: str, categories=()) -> bool:
    """True when the topic or the model's category suggestions describe a fiction genre."""
    text = " ".join([topic, *[str(c) for c in categories]])
    return bool(_FICTION_WORDS_RE.search(text)) and not _NONFICTION_OVERRIDE_RE.search(topic)


# For fiction topics, a title/subtitle that says it's about films, criticism or a guide is a
# reference work even when Google gives it no category.
_ABOUT_FICTION_SUBTITLE_RE = re.compile(
    r"\b(films?|cinema|movies?|screenplays?|criticism|essays?|guide to|history of|great lines|quotations?)\b", re.I)
_ABOUT_FICTION_TITLE_RE = re.compile(r"\b(criticism|essays on|encyclopedia|quotations)\b", re.I)


def _category_ok(topic: str, info: dict, fiction: bool | None) -> bool:
    if fiction and (_ABOUT_FICTION_SUBTITLE_RE.search(info.get("subtitle") or "")
                    or _ABOUT_FICTION_TITLE_RE.search(info.get("title") or "")):
        return False
    cats = [str(c).lower() for c in (info.get("categories") or [])]
    if not cats:
        return True  # indie titles often carry no categories; judge them on the other signals
    if not _YOUNG_READER_TOPIC_RE.search(topic) and any(c.startswith(_JUVENILE_CATEGORY_PREFIXES) for c in cats):
        return False  # a children's book is not a competitor for an adult genre
    if fiction and any(m in c for c in cats for m in _NONFICTION_CATEGORY_MARKERS):
        return False
    return True


def pick_competitors(topic: str, volumes: list[dict], limit: int = MAX_COMPETITORS,
                     fiction: bool | None = None) -> list[dict]:
    """Filter, dedupe and rank raw volumes into at most `limit` competitor entries.
    `fiction` is True when the topic is a fiction genre (the caller knows this from
    the model's category suggestions); it switches on the non-fiction category rule."""
    tokens = _topic_tokens(topic)
    candidates = []
    seen = set()
    for volume in volumes:
        book = _book_from_volume(volume)
        if book is None:
            continue
        if not _category_ok(topic, book["info"], fiction):
            continue
        rel = _relevance(tokens, book["info"])
        if rel < MIN_RELEVANCE:
            continue  # wrong genre / unrelated to what was searched
        dedupe_key = (_normalize_title(book["title"]), book["authors"][0].lower())
        if dedupe_key in seen or not dedupe_key[0]:
            continue
        seen.add(dedupe_key)
        info = book["info"]
        has_description = len(info.get("description") or "") >= 30
        rank = rel * 10 + math.log1p(info.get("ratingsCount") or 0) * 0.5 + (1.0 if has_description else 0.0)
        candidates.append((rank, book))
    candidates.sort(key=lambda c: c[0], reverse=True)

    results = []
    per_author: dict[str, int] = {}
    for _, book in candidates:
        if len(results) >= limit:
            break
        author_key = book["authors"][0].lower()
        if per_author.get(author_key, 0) >= MAX_PER_AUTHOR:
            continue
        per_author[author_key] = per_author.get(author_key, 0) + 1
        info = book["info"]
        results.append({
            "kind": "book",
            "title": book["title"],
            "author": ", ".join(book["authors"][:2]),
            "description": _description_for(info),
            "year": (info.get("publishedDate") or "")[:4] or None,
            "url": info.get("canonicalVolumeLink") or info.get("infoLink"),
        })
    return results


# ---------------------------------------------------------------- public entry point

def lookup_volumes(topic: str, extra_queries=()) -> list[dict]:
    """Raw volumes for `topic` (plus any extra queries), looked up in parallel and
    merged. Never raises: failures just contribute nothing."""
    if time.time() < _breaker_until:
        return []
    queries = []
    for q in [topic, *extra_queries]:
        q = " ".join(str(q).split())
        if q and q.lower() not in [x.lower() for x in queries]:
            queries.append(q)

    started = time.time()
    futures = {_executor.submit(search_volumes, q): q for q in queries}
    done, not_done = wait(futures, timeout=LOOKUP_DEADLINE_SECONDS)

    volumes: list[dict] = []
    seen_ids = set()
    failures = 0
    for future in done:
        try:
            for v in future.result():
                vid = v.get("id") if isinstance(v, dict) else None
                if vid is not None and vid in seen_ids:
                    continue
                seen_ids.add(vid)
                volumes.append(v)
        except Exception as e:  # BooksLookupError or anything unexpected
            failures += 1
            logger.info("books lookup %r failed: %s", futures[future], e)
    for future in not_done:
        future.cancel()
        failures += 1
        logger.info("books lookup %r timed out", futures[future])
    logger.info("books lookup: %d queries (%d failed) -> %d volumes in %.2fs",
                len(queries), failures, len(volumes), time.time() - started)
    return volumes


def find_competitor_books(topic: str, extra_queries=(), limit: int = MAX_COMPETITORS,
                          fiction: bool | None = None) -> list[dict]:
    """lookup + pick in one call. Never raises: any failure yields [] so the caller
    can fall back per entry."""
    volumes = lookup_volumes(topic, extra_queries)
    try:
        results = pick_competitors(topic, volumes, limit, fiction)
    except Exception:  # defensive: a surprising payload must never break the page
        logger.exception("books result processing failed")
        results = []
    logger.info("books: %d kept of %d volumes for %r", len(results), len(volumes), topic)
    return results
