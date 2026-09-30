"""Keyword & Category Research — one Groq call: a book topic/title ->
Amazon-search keyword suggestions (with a short reason each), likely
competitor titles, and Amazon browse-category suggestions. The same kind of
output Publisher Rocket/BookBeam sell, minus their scraped Amazon
volume/rank data — this is LLM judgment, a starting point for research, not
ground truth.

Unlike app/kdp.py and app/ads_analyser.py this needs the network (an LLM
call rather than a pure calculation), so it follows app/ai.py's Groq
conventions instead — strict JSON out via the same think-block/fence
stripping, temperature low, reasoning_effort "none" so the model doesn't
burn its output budget on a <think> block and return empty content.
"""

import json
import logging
import re

from groq import APIError, Groq

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
2. competitors: 4-6 entries describing likely direct competitors on Amazon for this topic. ANTI-FABRICATION RULES (strict, non-negotiable): NEVER invent a book. A specific "Title by Author" entry is allowed ONLY if you are highly confident that exact book really exists and is by that author; if there is any doubt about the title, the author, or whether the pairing is real, do NOT write an author name or a title at all. In that case describe the competitor by pattern only, e.g. "Long-running village-set cozy mystery series with an amateur sleuth and a pet sidekick" or "Bestselling standalone domestic thriller with an unreliable narrator". Never attach a made-up title to a real author's name, never make up a series name, and never guess. Pattern-only entries are always better than a doubtful real-looking title. Competitors must match the topic's audience: an adult genre gets adult books, a children's topic gets children's books - never mix them. Do not repeat an entry.
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
    except json.JSONDecodeError as e:
        logger.warning("research_keywords: model returned malformed JSON: %s", e)
        raise KeywordResearchError(FRIENDLY_ERROR_MESSAGE) from e


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


def validate_topic(topic: str) -> str:
    """Returns the stripped topic, or raises ValueError. Split out so main.py
    can reject bad input before it spends a rate-limit hit."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"topic must be {MAX_TOPIC_LENGTH} characters or fewer")
    return topic


def research_keywords(topic: str) -> dict:
    """topic -> {"keywords": [...], "competitors": [...], "categories": [...]}.

    Raises ValueError for bad input (caught by main.py as a 422) and
    KeywordResearchError if the model's response couldn't be parsed (caught
    as a 502) — the same split app.ai.AIResponseError draws for the trading
    pipeline.
    """
    topic = validate_topic(topic)

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
        raise KeywordResearchError(FRIENDLY_ERROR_MESSAGE) from e

    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as app.ai's passes: always visible so
    # a bad or malformed response can be diagnosed from the server log.
    logger.info("research_keywords raw model output: %r", raw_content)
    parsed = _strip_to_json(raw_content)
    return _normalize(parsed)
