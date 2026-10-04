"""Keyword & Category Research — one Groq call: a book topic/title ->
Amazon-search keyword suggestions (with a short reason each), likely
competitor titles, and Amazon browse-category suggestions. The same kind of
output Publisher Rocket/BookBeam sell, minus their scraped Amazon
volume/rank data — this is LLM judgment, a starting point for research, not
ground truth. The "competitors" section is the exception: real titles and
authors come from the Google Books API (app/books.py), never from the model, and
the model's pattern-only blurbs are just the per-slot fallback when no real match
is found or the lookup fails.

Unlike app/kdp.py and app/ads_analyser.py this needs the network (an LLM
call rather than a pure calculation), so it follows app/ai.py's Groq
conventions instead — strict JSON out via the same think-block/fence
stripping, temperature low, reasoning_effort "none" so the model doesn't
burn its output budget on a <think> block and return empty content.
"""

import json
import logging
import re

from concurrent.futures import ThreadPoolExecutor

from groq import APIError, Groq

from app import bluesky, books, reddit, wikipedia
from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY)

MODEL = "qwen/qwen3.8-27b"  # verified via client.models.list() against the real Groq account

MAX_TOPIC_LENGTH = 300

# The Groq account's on-demand tier caps output at 1000 tokens/minute per
# request (OTPM). Groq checks this against max_completion_tokens itself
# (not actual usage) before the call even runs, so leaving it unset let the
# SDK's default reservation request over 1000 and 429 on essentially every
# real topic — this cap keeps the reservation comfortably under the limit.
# The prompt below is sized (verified against real completions, ~350-500
# actual tokens per topic) to finish well short of this cap on its own, so
# it isn't just a truncation net.
MAX_COMPLETION_TOKENS = 900

_THINK_BLOCK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
# Matches a whole JSON string (kept as-is) OR a comma followed only by
# whitespace and a closer (dropped) — matching strings first means a ",}" that
# sits inside a string value is never mistaken for a trailing comma.
_TRAILING_COMMA_RE = re.compile(r'("(?:\\.|[^"\\])*")|,(?=\s*[}\]])')


class KeywordResearchError(RuntimeError):
    """The model didn't return parseable JSON, or the Groq call itself failed
    (rate limit, model error, connection issue, ...). Always carries
    FRIENDLY_ERROR_MESSAGE — main.py shows str(e) straight to the user, so
    the technical reason goes in the logger.warning/info calls instead."""


FRIENDLY_ERROR_MESSAGE = "Something went wrong generating results — try again in a moment."


SYSTEM_PROMPT = """You are an Amazon KDP marketing researcher. Given a \
book topic or working title, suggest, briefly and concisely:

1. keywords: 8-10 specific search terms a reader would actually type into \
Amazon search to find this book. Prefer long-tail, buyer-intent phrases \
over single generic words. For each, give a short reason (max 8 words, not \
a full sentence) a reader would search that phrase.
2. competitors: 4-6 entries describing likely direct competitors on Amazon
for this topic, each as a PATTERN DESCRIPTION ONLY. ABSOLUTE RULE: never name
any specific book title, series name or author - not even famous ones, not
even "if you are sure". Your memory of exact titles and authors is not
reliable enough and an invented title under a real author's name is harmful.
Describe the type of book instead.
GENRE-FIT RULES (strict): every entry must sit squarely inside the topic's
own genre AND sub-genre. Check each entry against that genre's defining
conventions - who drives the plot, the tone, how dark or violent it gets, the
kind of setting. An entry whose lead character or tone belongs to a
neighbouring genre is WRONG, e.g. a police-procedural lead or a serial-killer
hunt in a cosy mystery, a grimdark tone in a comfort fantasy, a modern-day
setting in a historical romance, a children's book for an adult genre. If you
cannot think of an entry that truly fits, write fewer entries rather than
stretch.
SPECIFICITY RULES (strict): each entry must give at least two concrete
distinguishing details that let an author judge how close a competitor is -
the setting, the protagonist type (occupation, age, situation), the central
hook or trope, the tone, or series-vs-standalone. No two entries may share
the same details or be interchangeable boilerplate. Keep each entry under 25
words. Do not use unverifiable claims such as "bestselling", "popular",
"award-winning" or "new release".
3. categories: 3-4 real Amazon Kindle/Book browse categories this book \
could be listed under (each one distinct - no duplicates or \
near-duplicates), using Amazon's actual category naming (e.g. "Kindle \
eBooks > Literature & Fiction > Genre Fiction > Mystery, Thriller & \
Suspense > Mystery > Cozy").

Be specific to the given topic — never return generic placeholders. If the \
topic is too vague to research meaningfully, still make a best-effort \
attempt based on the closest reasonable genre/niche rather than refusing.
Keep the whole response terse — short phrases, no extra commentary.

Return STRICT JSON, no prose, no fences:
{
  "keywords": [{"keyword": string, "reason": string}, ...],
  "competitors": [string, ...],
  "categories": [string, ...]
}"""


def _strip_to_json(raw: str) -> dict:
    """Same defensive extraction as app.ai._strip_to_json — models sometimes
    wrap JSON in prose, ```json fences, or a <think> reasoning trace."""
    cleaned = _THINK_BLOCK_RE.sub("", raw or "").strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]
    if not cleaned:
        logger.warning("research_keywords: model returned no parseable content")
        raise KeywordResearchError(FRIENDLY_ERROR_MESSAGE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_error:
        # The one failure mode worth forgiving: a trailing comma before a
        # closing } or ] (the model does this intermittently). Strict parse
        # runs first so valid output is never touched; anything still broken
        # after this single repair (truncation, missing quotes, ...) fails.
        try:
            repaired = json.loads(_TRAILING_COMMA_RE.sub(r"\1", cleaned))
            logger.info("research_keywords: repaired trailing comma in model JSON (%s)", first_error)
            return repaired
        except json.JSONDecodeError:
            pass
        logger.warning("research_keywords: model returned malformed JSON: %s", first_error)
        raise KeywordResearchError(FRIENDLY_ERROR_MESSAGE) from first_error


def _dedupe(items) -> list[str]:
    """Drop empties and case/whitespace-insensitive repeats, keeping first-seen order."""
    seen, out = set(), []
    for item in items:
        key = " ".join(item.lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _normalize(parsed: dict) -> dict:
    """Defensive coercion, same rationale as app.ai._normalize_setup_suggestion
    — a half-formed entry is dropped rather than shown broken to the user."""
    keywords = []
    for k in parsed.get("keywords") or []:
        text = str((k or {}).get("keyword") or "").strip()
        if not text:
            continue
        keywords.append({"keyword": text, "reason": str((k or {}).get("reason") or "").strip()})

    competitors = _dedupe(str(c).strip() for c in parsed.get("competitors") or [])
    categories = _dedupe(str(c).strip() for c in parsed.get("categories") or [])

    return {"keywords": keywords, "competitors": competitors, "categories": categories}


# Separate from books' own per-query pool: an outer lookup waits on inner tasks, so
# sharing one pool could deadlock under load.
_lookup_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kw-books")

# Defence in depth for the pattern-only blurbs (the prompt already forbids
# names): drop any blurb that still reads like a named work — "... by First
# Last", a quoted Title Case phrase, or "The Something Series". A false
# positive only loses a blurb; a false negative could show an invented book.
_BY_AUTHOR_RE = re.compile(r"\bby\s+[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)+")
_QUOTED_TITLE_RE = re.compile(r"[\"\u201c\u2018'][A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){1,}[\"\u201d\u2019']")
_NAMED_SERIES_RE = re.compile(r"\b[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*\s+(?:series|trilogy|saga|chronicles)\b")


def _looks_like_named_work(text: str) -> bool:
    rest = text.split(" ", 1)[1] if " " in text else ""  # ignore the sentence-initial capital
    return bool(_BY_AUTHOR_RE.search(text) or _QUOTED_TITLE_RE.search(text) or _NAMED_SERIES_RE.search(rest))


def _assemble_competitors(book_entries: list[dict], patterns: list[str]) -> list[dict]:
    """Verified books first, then pattern blurbs to fill any remaining slots of
    books.MAX_COMPETITORS. With no books at all this is exactly the old
    pattern-only list. Book entries are passed through untouched."""
    entries = list(book_entries[: books.MAX_COMPETITORS])
    for text in patterns:
        if len(entries) >= books.MAX_COMPETITORS:
            break
        if _looks_like_named_work(text):
            logger.warning("dropped pattern blurb that looks like a named work: %r", text)
            continue
        entries.append({"kind": "pattern", "text": text})
    return entries


def validate_topic(topic: str) -> str:
    """Returns the stripped topic, or raises ValueError. Split out so main.py
    can reject bad input before it spends a rate-limit hit."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"topic must be {MAX_TOPIC_LENGTH} characters or fewer")
    return topic


INTEREST_WAIT_SECONDS = 4.0   # the lookup started with the model call, so it has almost always finished already


def _interest_result(future) -> dict | None:
    try:
        return future.result(timeout=INTEREST_WAIT_SECONDS)
    except Exception:  # timeout or an unexpected error: the section is optional
        future.cancel()
        return None


def research_keywords(topic: str) -> dict:
    """topic -> {"keywords": [...], "competitors": [...], "categories": [...]}.

    Raises ValueError for bad input (caught by main.py as a 422) and
    KeywordResearchError if the model's response couldn't be parsed (caught
    as a 502) — the same split app.ai.AIResponseError draws for the trading
    pipeline.
    """
    topic = validate_topic(topic)

    # Start the book lookup now so it runs while the model call is in flight
    # (find_competitor_books never raises and has its own deadline).
    books_future = _lookup_executor.submit(books.lookup_volumes, topic)
    # Reddit threads are fetched alongside, by plain code. Nothing from Reddit is ever put in
    # a model request: the Groq call below carries only the user's own topic (Reddit Developer
    # Terms 7.2 forbids sharing Reddit data with third parties).
    reddit_future = _lookup_executor.submit(reddit.fetch_genre_corpus, topic) if reddit.enabled() else None
    # Wikipedia pageviews for the topic's genre article (numbers only; never sent to the model).
    # interest_over_time never raises and answers None when the topic has no genre-level article.
    interest_future = _lookup_executor.submit(wikipedia.interest_over_time, topic)
    # Recent Bluesky posts, displayed as posted (display-only: never sent to the model either).
    bluesky_future = _lookup_executor.submit(bluesky.fetch_genre_posts, topic) if bluesky.enabled() else None

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            reasoning_effort="none",
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Book topic/title: {topic}"},
            ],
        )
    except APIError as e:
        # Covers RateLimitError, APIConnectionError, model/auth errors, etc.
        # (all subclass groq.APIError) — main.py turns this into a clean 502
        # instead of a raw 500 crashing out of the request.
        logger.warning("research_keywords Groq API call failed: %s", e)
        if reddit_future:
            reddit_future.cancel()
        interest_future.cancel()
        if bluesky_future:
            bluesky_future.cancel()
        books_future.cancel()  # the answer is an error anyway; don't spend a Books request on it if it hasn't started
        raise KeywordResearchError(FRIENDLY_ERROR_MESSAGE) from e

    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as app.ai's passes: always visible so
    # a bad or malformed response can be diagnosed from the server log.
    logger.info("research_keywords raw model output: %r", raw_content)
    parsed = _strip_to_json(raw_content)
    result = _normalize(parsed)

    volumes = books_future.result()
    # The model's own category suggestions tell us whether this is a fiction genre,
    # which decides if criticism / craft / film books count as off-topic.
    fiction = books.looks_like_fiction(topic, result["categories"])
    book_entries = books.pick_competitors(topic, volumes, fiction=fiction)
    if len(book_entries) < books.MAX_COMPETITORS and result["keywords"]:
        # Not enough real books for the topic itself: a second parallel wave on the
        # model's top keywords (the topic query is cached, so only these are new).
        suffix = " subject:fiction" if fiction else ""
        extra = [f"{topic}{suffix}"] + [f"{k['keyword']}{suffix}" for k in result["keywords"][:2]]
        volumes = volumes + books.lookup_volumes(topic, extra_queries=extra)
        book_entries = books.pick_competitors(topic, volumes, fiction=fiction)
    result["competitors"] = _assemble_competitors(book_entries, result["competitors"])

    interest = _interest_result(interest_future)
    if interest:
        result["interest"] = interest
    if bluesky_future:
        posts = _interest_result(bluesky_future)    # same optional-section wait; None on timeout or error
        if posts:
            result["bluesky"] = posts

    if reddit_future:
        # Match the real Google Books titles (a wider candidate list than the 5 shown) against
        # reader-community threads with plain string matching; omit the section if nothing fits.
        candidates = [{"title": b["title"], "author": b["author"], "url": b.get("url")}
                      for b in books.pick_competitors(topic, volumes, limit=25, fiction=fiction)]
        block = reddit.build_keyword_block(topic, reddit_future.result(), candidates)
        if block:
            result["reddit"] = block
    return result
