"""Reddit threads for Keyword Research and Post Idea Finder - shown as posted, never summarised.

Hard rules this module exists to uphold (Reddit Developer Terms 7.2, 2.4, 7.3):

* Reddit content never leaves this process. In particular it is NEVER sent to Groq or
  any other model or third-party service (7.2: "You will not share Reddit Services and
  Data with any third party"). Everything here is deterministic code: ranking and string
  matching. Nothing in this module imports a model client, and a test asserts the
  Groq request carries nothing from Reddit.
* Only thread titles, metadata and a link back are ever returned (`public()` is the single
  whitelist). Post bodies are fetched only so titles can be matched against book titles;
  they are held in memory for the current response and a short cache, and are never
  returned, logged or persisted. Comments are never fetched.
* No persistence of any kind: no database, no files, and logs carry counts and status codes
  only - never titles or bodies. The in-memory cache expires after CACHE_TTL_SECONDS.
* Free-tier Reddit API access is non-commercial. Everything is behind REDDIT_ENABLED (OFF by
  default; credentials alone do not enable it) and every failure degrades to "section omitted", so it can be switched off (or lost) without
  breaking either tool.

Access is OAuth "application only" (client_credentials) with an app registered at
reddit.com/prefs/apps; anonymous requests are refused (HTTP 403).
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import httpx

from app import config

logger = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
THREAD_BASE = "https://www.reddit.com"

REQUEST_TIMEOUT_SECONDS = 4.0
FETCH_DEADLINE_SECONDS = 6.0
LISTING_LIMIT = 50
SEARCH_LIMIT = 25
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 150
BREAKER_COOLDOWN_SECONDS = 600       # rejected credentials / forbidden
DEAD_SUBREDDIT_SECONDS = 6 * 3600    # private / banned / nonexistent communities
MATCH_TEXT_CHARS = 2000              # how much of a post body is kept, for title matching only
MAX_TRENDING = 6
MAX_PER_SUBREDDIT = 3
MAX_SOCIAL_TRENDING = 8
MAX_RECOMMENDED = 5
MAX_LINKS_PER_BOOK = 3

_client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="reddit")

_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_token: str | None = None
_token_expires = 0.0
_token_lock = threading.Lock()
_breaker_until = 0.0
_dead_subs: dict[str, float] = {}


class RedditError(RuntimeError):
    """A Reddit request failed. Always caught inside this module."""


# ---------------------------------------------------------------- configuration

def enabled() -> bool:
    return bool(config.REDDIT_ENABLED and config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)


def user_agent() -> str:
    """Reddit requires a unique, descriptive User-Agent in the form
    "<platform>:<app id>:<version> (by /u/<username>)" and forbids masking it (Data API
    Terms 2.8)."""
    ua = "web:marketer-mirror:v1.0"
    if config.REDDIT_USERNAME:
        ua += f" (by /u/{config.REDDIT_USERNAME.lstrip('/').removeprefix('u/')})"
    return ua


def describe_config() -> str:
    """Redacted description for the startup log: presence only, never the secret."""
    if not config.REDDIT_ENABLED:
        return "disabled (REDDIT_ENABLED is off)"
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return "NOT CONFIGURED (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET) - Reddit sections are omitted"
    return (f"configured (client id {len(config.REDDIT_CLIENT_ID)} chars, secret {len(config.REDDIT_CLIENT_SECRET)} chars, "
            f"username {'set' if config.REDDIT_USERNAME else 'NOT set - add REDDIT_USERNAME for the User-Agent'})")


def log_configuration() -> None:
    """Called from main.py's startup hook, after logging is configured."""
    logger.info("Reddit integration: %s", describe_config())


# ---------------------------------------------------------------- HTTP

def _trip_breaker(seconds: float, reason: str) -> None:
    global _breaker_until
    _breaker_until = time.time() + seconds
    logger.warning("reddit lookups paused for %ds: %s", seconds, reason)


def _get_token() -> str:
    global _token, _token_expires
    with _token_lock:
        if _token and time.time() < _token_expires - 60:
            return _token
        try:
            resp = _client.post(
                TOKEN_URL,
                auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": user_agent()},
            )
        except httpx.HTTPError as e:
            raise RedditError(f"token request failed: {e.__class__.__name__}") from e
        if resp.status_code in (400, 401, 403):
            _trip_breaker(BREAKER_COOLDOWN_SECONDS, f"token request refused (HTTP {resp.status_code}) - check REDDIT_CLIENT_ID/SECRET")
            raise RedditError(f"token HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise RedditError(f"token HTTP {resp.status_code}")
        try:
            body = resp.json()
            token = body["access_token"]
            _token, _token_expires = token, time.time() + float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as e:
            raise RedditError("token response unusable") from e
        return _token


def _api_get(path: str, params: dict) -> dict:
    global _token
    token = _get_token()
    try:
        resp = _client.get(
            API_BASE + path,
            params={**params, "raw_json": 1},
            headers={"Authorization": f"bearer {token}", "User-Agent": user_agent()},
        )
    except httpx.HTTPError as e:
        raise RedditError(f"request failed: {e.__class__.__name__}") from e

    remaining = resp.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            if float(remaining) < 3:
                _trip_breaker(float(resp.headers.get("x-ratelimit-reset", 60)), "rate-limit window nearly exhausted")
        except ValueError:
            pass
    if resp.status_code == 429:
        _trip_breaker(float(resp.headers.get("retry-after", 60)), "HTTP 429")
        raise RedditError("HTTP 429")
    if resp.status_code == 401:
        _token = None  # expired or revoked: fetch a fresh one next time
        raise RedditError("HTTP 401")
    if resp.status_code in (403, 404):
        raise RedditError(f"HTTP {resp.status_code}")  # a private / banned / missing community
    if resp.status_code != 200:
        raise RedditError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise RedditError("response was not JSON") from e


# ---------------------------------------------------------------- listings -> thread dicts

def _parse_listing(payload: dict, subreddit: str) -> list[dict]:
    """Reddit listing JSON -> thread dicts. `_match` (title + the start of the post body)
    exists only for in-memory title matching and is never returned to a page."""
    threads = []
    try:
        children = payload["data"]["children"]
    except (KeyError, TypeError):
        return threads
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            continue
        d = child.get("data") or {}
        title = (d.get("title") or "").strip()
        if not title or d.get("over_18") or d.get("stickied") or d.get("removed_by_category"):
            continue
        selftext = d.get("selftext") or ""
        if selftext in ("[removed]", "[deleted]"):
            selftext = ""
        permalink = d.get("permalink") or ""
        if not permalink.startswith("/r/"):
            continue
        threads.append({
            "id": d.get("id") or permalink,
            "title": title,
            "subreddit": d.get("subreddit") or subreddit,
            "score": int(d.get("score") or 0),
            "comments": int(d.get("num_comments") or 0),
            "created_utc": float(d.get("created_utc") or 0),
            "url": THREAD_BASE + permalink,
            "_match": title + "\n" + selftext[:MATCH_TEXT_CHARS],
        })
    return threads


def public(thread: dict) -> dict:
    """The ONLY shape that leaves this module: title, metadata, link. No body text."""
    return {k: thread[k] for k in ("title", "subreddit", "score", "comments", "created_utc", "url")}


_LISTINGS = {
    # kind -> (path template, params). Search kinds add q + restrict_sr.
    "hot": ("/r/{sub}/hot", {"limit": LISTING_LIMIT}),
    "top_month": ("/r/{sub}/top", {"t": "month", "limit": LISTING_LIMIT}),
    "search_month": ("/r/{sub}/search", {"restrict_sr": 1, "sort": "top", "t": "month", "limit": SEARCH_LIMIT}),
    "search_year": ("/r/{sub}/search", {"restrict_sr": 1, "sort": "relevance", "t": "year", "limit": SEARCH_LIMIT}),
}


def fetch_listing(subreddit: str, kind: str, query: str | None = None) -> list[dict]:
    """One listing, behind a short in-memory cache. Raises RedditError."""
    key = (subreddit.lower(), kind, (query or "").lower())
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    if _dead_subs.get(subreddit.lower(), 0) > now:
        raise RedditError("community unavailable")
    path, params = _LISTINGS[kind]
    params = dict(params)
    if query is not None:
        params["q"] = query
    try:
        payload = _api_get(path.format(sub=subreddit), params)
    except RedditError as e:
        if str(e) in ("HTTP 403", "HTTP 404"):
            _dead_subs[subreddit.lower()] = now + DEAD_SUBREDDIT_SECONDS
        raise
    threads = _parse_listing(payload, subreddit)
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, threads)
    return threads


def fetch_many(requests: list[tuple]) -> dict[tuple, list[dict]]:
    """requests: [(subreddit, kind, query|None), ...] fetched in parallel under one deadline.
    Failures just leave that key out. Never raises."""
    if not enabled() or time.time() < _breaker_until:
        return {}
    futures = {_executor.submit(fetch_listing, *req): req for req in requests}
    done, not_done = wait(futures, timeout=FETCH_DEADLINE_SECONDS)
    out, failures = {}, 0
    for future in done:
        try:
            out[futures[future]] = future.result()
        except Exception as e:  # RedditError or anything unexpected
            failures += 1
            logger.info("reddit %s r/%s failed: %s", futures[future][1], futures[future][0], e)
    for future in not_done:
        future.cancel()
        failures += 1
    logger.info("reddit fetch: %d requests, %d failed, %d threads", len(requests), failures,
                sum(len(v) for v in out.values()))
    return out


# ---------------------------------------------------------------- choosing communities

# (pattern on the topic, communities). All are reader communities unless listed in
# WRITER_SUBREDDITS. These names are matched by code, not generated by a model; a name that
# turns out to be private/banned/nonexistent is skipped automatically (see _dead_subs).
_GENRE_SUBREDDITS = [
    (r"\b(cozy|cosy)\b", ["CozyMystery"]),
    (r"\b(mystery|mysteries|detective|whodunits?|noir|crime|sleuth|hard-?boiled)\b", ["Mystery"]),
    (r"\b(fantasy|dragons?|epic|wizards?|sword|magic)\b", ["Fantasy"]),
    (r"\bprogression\b", ["ProgressionFantasy"]),
    (r"\blitrpg\b", ["litrpg"]),
    (r"\b(science fiction|sci-?fi|space opera|hard sf|dystopian|cyberpunk)\b", ["printSF"]),
    (r"\b(romance|romances|regency|romantic)\b", ["RomanceBooks"]),
    (r"\bhistorical( fiction)?\b", ["HistoricalFiction"]),
    (r"\b(horror|gothic|haunted)\b", ["horrorlit"]),
    (r"\b(young adult|ya|teen)\b", ["YAlit"]),
    (r"\b(children|picture book|kids)\b", ["childrensbooks"]),
    (r"\b(cookbook|keto|recipes?|diet)\b", ["cookbooks"]),
]
GENERAL_READER_SUBREDDITS = ["books"]
WRITER_SUBREDDITS = ["selfpublish"]
MAX_GENRE_SUBREDDITS = 3


def pick_subreddits(topic: str) -> dict:
    """{"genre": [...], "general": [...], "writer": [...]} for a book topic: communities that
    match the genre, plus r/books for general reading discussion, plus r/selfpublish as the
    fallback when nothing genre-specific matched."""
    text = topic.lower()
    genre: list[str] = []
    for pattern, subs in _GENRE_SUBREDDITS:
        if re.search(pattern, text):
            for s in subs:
                if s not in genre:
                    genre.append(s)
    genre = genre[:MAX_GENRE_SUBREDDITS]
    return {"genre": genre, "general": list(GENERAL_READER_SUBREDDITS),
            "writer": [] if genre else list(WRITER_SUBREDDITS)}


_PLATFORM_SUBREDDITS = {
    "linkedin": ["linkedin"],
    "instagram": ["Instagram"],
    "tiktok": ["TikTok"],
    "youtube": ["NewTubers"],
    "medium": ["Medium"],
}
# Who the post is FOR (not what it is about): themes in the user's topic -> audience communities.
_AUDIENCE_THEMES = [
    (r"\b(book|books|author|authors|novel|writing|writer|publish|publishing|kindle)\b", ["selfpublish", "writing"]),
    (r"\b(bak\w*|bread|food|recipe|recipes|cook\w*|restaurant|cafe|coffee)\b", ["Baking", "Cooking"]),
    (r"\b(fitness|gym|running|workout|yoga|marathon|trail)\b", ["Fitness", "running"]),
    (r"\b(financ\w*|budget|invest\w*|bookkeeping|tax|taxes|accounting)\b", ["personalfinance", "smallbusiness"]),
    (r"\b(travel|trip|hotel|vacation)\b", ["travel"]),
    (r"\b(photo|photography|photographer)\b", ["photography"]),
    (r"\b(pet|pets|dog|dogs|cat|cats)\b", ["dogs", "cats"]),
    (r"\b(parent|parents|parenting|baby|toddler)\b", ["Parenting"]),
    (r"\b(game|games|gaming|gamer)\b", ["gaming"]),
    (r"\b(beauty|skincare|makeup|fashion)\b", ["SkincareAddiction", "fashion"]),
    (r"\b(business|startup|freelanc\w*|marketing|brand|branding|agency)\b", ["smallbusiness", "Entrepreneur"]),
]
_SOCIAL_FALLBACK = ["socialmedia"]
MAX_SOCIAL_SUBREDDITS = 4


def pick_social_subreddits(topic: str, platforms) -> list[str]:
    """Communities for the AUDIENCE and PLATFORM of a post, not its specific subject: one per
    selected platform, then audience themes spotted in the topic, then a general fallback."""
    picked: list[str] = []
    for p in platforms:
        for s in _PLATFORM_SUBREDDITS.get(p, [])[:1]:
            if s not in picked:
                picked.append(s)
    picked = picked[:2]
    text = topic.lower()
    for pattern, subs in _AUDIENCE_THEMES:
        if re.search(pattern, text):
            for s in subs:
                if s not in picked:
                    picked.append(s)
    if not picked:
        picked = list(_SOCIAL_FALLBACK)
    return picked[:MAX_SOCIAL_SUBREDDITS]


# ---------------------------------------------------------------- ranking ("what's active now")

def _activity(thread: dict, now: float) -> float:
    """Discussion size (comments weigh most) discounted by age, so a lively thread from today
    outranks a bigger one from last week. Same idea as Reddit's own "hot"."""
    age_days = max(0.0, (now - thread["created_utc"]) / 86400)
    return (thread["comments"] + thread["score"] / 5 + 1) / (age_days + 1) ** 0.8


def rank_threads(threads: list[dict], limit: int, per_subreddit: int = MAX_PER_SUBREDDIT,
                 now: float | None = None) -> list[dict]:
    now = now or time.time()
    seen, per_sub, out = set(), {}, []
    for t in sorted(threads, key=lambda t: _activity(t, now), reverse=True):
        if t["id"] in seen or per_sub.get(t["subreddit"], 0) >= per_subreddit:
            continue
        seen.add(t["id"])
        per_sub[t["subreddit"]] = per_sub.get(t["subreddit"], 0) + 1
        out.append(public(t))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- matching real book titles (no AI)

_GENERIC_TITLE_WORDS = {"new", "best", "great", "book", "books", "story", "stories", "collection", "volume",
                        "complete", "guide", "tales", "anthology", "series", "box", "set", "novel", "novels"}


def _clean(text: str) -> str:
    """Normalise '&' and apostrophes so 'Cupcake & Murder' ~ 'Cupcake and Murder' and
    "Don't" ~ 'Dont'. Applied to BOTH the book title and the thread text before matching."""
    return text.replace("&", " and ").replace("’", "").replace("'", "")


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", _clean(text))


def _base_title(title: str) -> str:
    """The title without its subtitle / series tail: 'Cupcake & Murder: A Dana Sweet Cozy…' -> 'Cupcake & Murder'."""
    return re.split(r"\s*[:(\[–—]\s*|\s+-\s+", title, maxsplit=1)[0].strip()


def _author_surname(author: str) -> str | None:
    first = re.split(r",| and | & ", author)[0].strip()
    parts = _words(first)
    return parts[-1] if parts and len(parts[-1]) >= 3 else None


def _title_pattern(title: str, topic_tokens: set[str], author: str) -> tuple[re.Pattern, bool] | None:
    """(regex, needs_author) for finding this book's title in thread text, or None if the title
    is too short/ambiguous to match safely. Matching is on whole words, and for ordinary
    multi-word titles it is CASE-SENSITIVE (Title Case or as Google lists it), so a generic
    phrase in lower case isn't mistaken for a book. One-word titles and titles made mostly of
    genre words ("The New Space Opera") only count when the author's surname appears in the
    same thread."""
    words = _words(_base_title(title))
    if not words or len(" ".join(words)) < 8:
        return None
    lowered = [w.lower() for w in words]
    from app.books import _stem  # same light stemmer the topic matching uses
    generic = sum(1 for w in lowered if w in _GENERIC_TITLE_WORDS or _stem(w) in topic_tokens)
    needs_author = len(words) < 2 or generic / len(words) >= 0.5
    if needs_author and not _author_surname(author):
        return None
    joined = r"\W+".join(re.escape(w) for w in words)
    forms = {joined, r"\W+".join(re.escape(w.capitalize()) for w in words)}
    pattern = "(?<![A-Za-z0-9])(?:" + "|".join(sorted(forms)) + ")(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE if needs_author else 0), needs_author


def match_titles(candidates: list[dict], threads: list[dict], topic_tokens: set[str]) -> list[dict]:
    """Which of these real books (title/author straight from Google Books) are mentioned in
    these threads? Pure string matching. Returns books with their mention count and thread links."""
    entries = []
    for book in candidates:
        compiled = _title_pattern(book["title"], topic_tokens, book["author"])
        if compiled is None:
            continue
        pattern, needs_author = compiled
        surname = _author_surname(book["author"]) if needs_author else None
        # (?:s)? allows the possessive: "Spencer's" -> "Spencers" once apostrophes are stripped
        surname_re = (re.compile(r"(?<![A-Za-z0-9])" + re.escape(surname) + r"(?:s)?(?![A-Za-z0-9])", re.I)
                      if surname else None)
        hits = []
        for t in threads:
            text = _clean(t["_match"])
            if pattern.search(text) and (surname_re is None or surname_re.search(text)):
                hits.append(t)
        if hits:
            hits.sort(key=lambda t: t["comments"], reverse=True)
            entries.append({
                "title": book["title"],            # verbatim from Google Books
                "author": book["author"],
                "book_url": book.get("url"),
                "mentions": len(hits),
                "subreddits": sorted({t["subreddit"] for t in hits}),
                "threads": [public(t) for t in hits[:MAX_LINKS_PER_BOOK]],
                "_comments": sum(t["comments"] for t in hits),
            })
    entries.sort(key=lambda e: (e["mentions"], e["_comments"]), reverse=True)
    for e in entries:
        e.pop("_comments")
    return entries[:MAX_RECOMMENDED]


# ---------------------------------------------------------------- the two page-level entry points

def fetch_genre_corpus(topic: str) -> dict | None:
    """Stage 1 for Keyword Research (run alongside the model call): pick communities and pull
    their current threads. Never raises; None when Reddit is off or returned nothing."""
    try:
        if not enabled() or time.time() < _breaker_until:
            return None
        subs = pick_subreddits(topic)
        requests = []
        for s in subs["genre"]:
            requests += [(s, "hot", None), (s, "top_month", None), (s, "search_year", topic)]
        for s in subs["general"]:
            requests += [(s, "search_month", topic), (s, "search_year", topic)]
        for s in subs["writer"]:
            requests.append((s, "search_month", topic))
        fetched = fetch_many(requests)
        if not fetched:
            return None
        return {"subs": subs, "fetched": fetched}
    except Exception:  # a surprise here must never break the tool
        logger.exception("reddit genre corpus failed")
        return None


def build_keyword_block(topic: str, corpus: dict | None, book_candidates: list[dict]) -> dict | None:
    """Stage 2: from the corpus and the real Google Books candidates, build
    {"trending": [...], "recommended": [...], "subreddits": [...]}; None if there's nothing to show."""
    if not corpus:
        return None
    try:
        from app.books import _topic_tokens
        subs, fetched = corpus["subs"], corpus["fetched"]
        reader_subs = {s.lower() for s in subs["genre"] + subs["general"]}

        # Trending: what's being discussed right now. Genre communities' "hot"; for communities
        # that aren't genre-specific (r/books, r/selfpublish) only threads matching the topic.
        trending_pool = []
        for (sub, kind, _q), threads in fetched.items():
            if (sub in subs["genre"] and kind == "hot") or (
                    (sub in subs["general"] or sub in subs["writer"]) and kind == "search_month"):
                trending_pool += threads
        trending = rank_threads(trending_pool, MAX_TRENDING)

        # Recommended Reading: real books readers are discussing, found by plain string matching
        # against reader-community threads. Writer communities are excluded (those are authors,
        # not readers).
        reader_threads, seen = [], set()
        for (sub, _kind, _q), threads in fetched.items():
            if sub.lower() in reader_subs:
                for t in threads:
                    if t["id"] not in seen:
                        seen.add(t["id"])
                        reader_threads.append(t)
        recommended = match_titles(book_candidates, reader_threads, set(_topic_tokens(topic)))

        if not trending and not recommended:
            return None
        used = sorted({t["subreddit"] for t in trending} | {s for e in recommended for s in e["subreddits"]})
        return {"trending": trending, "recommended": recommended, "subreddits": used}
    except Exception:
        logger.exception("reddit keyword block failed")
        return None


def fetch_social_trending(topic: str, platforms) -> dict | None:
    """Trending Right Now for Post Idea Finder: current threads from the AUDIENCE/PLATFORM
    communities, deliberately NOT filtered to the post's topic. Never raises."""
    try:
        if not enabled() or time.time() < _breaker_until:
            return None
        subs = pick_social_subreddits(topic, platforms)
        fetched = fetch_many([(s, "hot", None) for s in subs])
        threads = [t for ts in fetched.values() for t in ts]
        ranked = rank_threads(threads, MAX_SOCIAL_TRENDING)
        if not ranked:
            return None
        return {"threads": ranked, "subreddits": sorted({t["subreddit"] for t in ranked})}
    except Exception:
        logger.exception("reddit social trending failed")
        return None
