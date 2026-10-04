"""Bluesky posts for Keyword & Category Research - shown exactly as posted, never processed.

Display-only, on the same terms as app/reddit.py:

* Nothing from Bluesky is ever sent to a model or any other third-party service. Everything
  here is plain code (filtering, counting, sorting). This module imports no model client, and a
  test asserts none of it reaches the Groq request. Whether Bluesky's terms allow sending posts to
  an outside AI/NLP service is an open question (they are silent on it), which is exactly why
  that pairing is parked and nothing here may become a path to it.
* No persistence: no database, no files, logs carry counts and status codes only (never post
  text or handles). The only storage is a short in-memory cache that expires.
* Posts are shown as posted, with the author's handle and a link back to the post on bsky.app.
  `public()` is the single whitelist of what leaves this module.
* Authors' choices are respected: a post is skipped if the post OR its author carries ANY
  moderation label. That includes `!no-unauthenticated`, which means "do not show me to
  logged-out viewers" - and a public web page is exactly a logged-out viewer.
* OFF BY DEFAULT (BLUESKY_ENABLED), and every failure degrades to "section omitted", so it can be
  switched off - or fail - without affecting the tool.

Uses Bluesky's public AppView search (api.bsky.app, unauthenticated, no key).
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import api_usage, config

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
USER_AGENT = "MirrorJournal/1.0 (https://www.themirrorjournal.org; topic research tool)"

REQUEST_TIMEOUT_SECONDS = 4.0
SEARCH_LIMIT = 40
LOOKBACK_DAYS = 7
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 100
BREAKER_COOLDOWN_SECONDS = 600
MAX_POSTS = 5
MAX_QUERY_WORDS = 6
MAX_TEXT_CHARS = 600                 # Bluesky's own limit is 300 characters; this is only a safety cap

_TRAILING_GENERIC = {"books", "book", "novels", "novel", "stories", "story", "ebooks", "ebook"}
_HANDLE_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

def make_client(transport=None) -> httpx.Client:
    """The one place the client is configured (timeout, User-Agent); tests pass a mock transport."""
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport)


_client = make_client()
_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_breaker_until = 0.0


def enabled() -> bool:
    return bool(config.BLUESKY_ENABLED)


def log_configuration() -> None:
    """Called from main.py's startup hook, after logging is configured."""
    logger.info("Bluesky integration: %s", "enabled (display-only)" if enabled() else "disabled (BLUESKY_ENABLED is off)")


# ---------------------------------------------------------------- query

def query_words(topic: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", (topic or "").lower().replace("-", " "))
    while words and words[-1] in _TRAILING_GENERIC:
        words.pop()
    return words[:MAX_QUERY_WORDS]


# ---------------------------------------------------------------- parsing / filtering

def _has_phrase(text: str, words: list[str]) -> bool:
    """The topic's words, adjacent and in order (punctuation/spacing between them allowed): "true crime"
    must not match "wasn't it true that ... the crime"."""
    pattern = r"\b" + r"\W+".join(re.escape(w) for w in words) + r"\b"
    return re.search(pattern, text.lower()) is not None


def _post_url(handle: str, uri: str) -> str | None:
    rkey = uri.rsplit("/", 1)[-1] if isinstance(uri, str) and "/app.bsky.feed.post/" in uri else ""
    if not rkey or not re.fullmatch(r"[A-Za-z0-9._~-]+", rkey) or not _HANDLE_RE.match(handle or ""):
        return None
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _parse(payload: dict, words: list[str]) -> list[dict]:
    posts = payload.get("posts") if isinstance(payload, dict) else None
    out = []
    for p in posts if isinstance(posts, list) else []:
        try:
            record, author = p["record"], p["author"]
            text = record.get("text")
            handle = author.get("handle", "")
            if (not isinstance(text, str) or not text.strip()
                    or "reply" in record                      # a reply without its thread is out of context
                    or p.get("labels") or author.get("labels")  # any moderation / opt-out label
                    or (record.get("langs") and "en" not in record["langs"])
                    or not _has_phrase(text, words)):
                continue
            url = _post_url(handle, p.get("uri"))
            if not url:
                continue
            out.append({"text": text[:MAX_TEXT_CHARS], "handle": handle, "likes": int(p.get("likeCount") or 0),
                        "replies": int(p.get("replyCount") or 0), "created_at": record.get("createdAt") or p.get("indexedAt"),
                        "url": url})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def public(post: dict) -> dict:
    """The only fields that ever leave this module."""
    return {k: post[k] for k in ("text", "handle", "likes", "replies", "created_at", "url")}


def rank(posts: list[dict], limit: int = MAX_POSTS) -> list[dict]:
    """Most engaged first, at most one post per author, no duplicate text."""
    out, authors, texts = [], set(), set()
    for p in sorted(posts, key=lambda p: (-(p["likes"] + 2 * p["replies"]), p["created_at"] or "")):
        key = " ".join(p["text"].lower().split())
        if p["handle"] in authors or key in texts:
            continue
        authors.add(p["handle"])
        texts.add(key)
        out.append(p)
        if len(out) == limit:
            break
    return out


# ---------------------------------------------------------------- HTTP

def _trip_breaker(reason: str) -> None:
    global _breaker_until
    _breaker_until = time.time() + BREAKER_COOLDOWN_SECONDS
    logger.warning("bluesky lookups paused for %ds: %s", BREAKER_COOLDOWN_SECONDS, reason)


def _search(words: list[str], now: datetime) -> list[dict] | None:
    key = " ".join(words)
    t = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and t - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    if t < _breaker_until:
        return None
    params = {"q": key, "sort": "top", "limit": SEARCH_LIMIT, "lang": "en",
              "since": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    api_usage.record("bluesky")
    try:
        resp = _client.get(SEARCH_URL, params=params)
    except httpx.HTTPError as e:
        logger.info("bluesky fetch: request failed (%s)", e.__class__.__name__)
        return None
    if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
        _trip_breaker(f"HTTP {resp.status_code}")
        return None
    if resp.status_code != 200:
        logger.info("bluesky fetch: HTTP %d", resp.status_code)
        return None
    try:
        posts = _parse(resp.json(), words)
    except ValueError:
        return None
    logger.info("bluesky fetch: HTTP 200, %d usable posts", len(posts))   # counts only, never content
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (t, posts)
    return posts


def fetch_genre_posts(topic: str, now: datetime | None = None) -> dict | None:
    """-> {"query": "cozy mystery", "posts": [public post, ...]} or None (off, no results, or any
    failure). Never raises."""
    try:
        if not enabled():
            return None
        words = query_words(topic)
        if not words:
            return None
        posts = _search(words, now or datetime.now(timezone.utc))
        ranked = [public(p) for p in rank(posts or [])]
        return {"query": " ".join(words), "posts": ranked} if ranked else None
    except Exception:
        logger.exception("bluesky lookup failed")
        return None
