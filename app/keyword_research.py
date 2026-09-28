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

from groq import Groq

from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY)

MODEL = "qwen/qwen3.8-27b"  # verified via client.models.list() against the real Groq account

MAX_TOPIC_LENGTH = 300

_THINK_BLOCK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


class KeywordResearchError(RuntimeError):
    """The model didn't return parseable JSON."""


SYSTEM_PROMPT = """You are an Amazon KDP marketing researcher. Given a \
book's topic or working title, suggest:

1. keywords: 10-15 specific search terms a reader would actually type into \
Amazon search to find this book. Prefer long-tail, buyer-intent phrases \
over single generic words. For each, give a one-sentence reason a reader \
would search that phrase.
2. competitors: 5-8 real, specific book titles (with author if you know it) \
that are likely direct competitors on Amazon for this topic.
3. categories: 3-6 real Amazon Kindle/Book browse categories this book \
could be listed under, using Amazon's actual category naming (e.g. "Kindle \
eBooks > Literature & Fiction > Genre Fiction > Mystery, Thriller & \
Suspense > Mystery > Cozy").

Be specific to the given topic — never return generic placeholders. If the \
topic is too vague to research meaningfully, still make a best-effort \
attempt based on the closest reasonable genre/niche rather than refusing.

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
        raise KeywordResearchError("Model returned no parseable content")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise KeywordResearchError(f"Model returned malformed JSON: {e}") from e


def _normalize(parsed: dict) -> dict:
    """Defensive coercion, same rationale as app.ai._normalize_setup_suggestion
    — a half-formed entry is dropped rather than shown broken to the user."""
    keywords = []
    for k in parsed.get("keywords") or []:
        text = str((k or {}).get("keyword") or "").strip()
        if not text:
            continue
        keywords.append({"keyword": text, "reason": str((k or {}).get("reason") or "").strip()})

    competitors = [s for s in (str(c).strip() for c in parsed.get("competitors") or []) if s]
    categories = [s for s in (str(c).strip() for c in parsed.get("categories") or []) if s]

    return {"keywords": keywords, "competitors": competitors, "categories": categories}


def research_keywords(topic: str) -> dict:
    """topic -> {"keywords": [...], "competitors": [...], "categories": [...]}.

    Raises ValueError for bad input (caught by main.py as a 422) and
    KeywordResearchError if the model's response couldn't be parsed (caught
    as a 502) — the same split app.ai.AIResponseError draws for the trading
    pipeline.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"topic must be {MAX_TOPIC_LENGTH} characters or fewer")

    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Book topic/title: {topic}"},
        ],
    )
    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as app.ai's passes: always visible so
    # a bad or malformed response can be diagnosed from the server log.
    logger.info("research_keywords raw model output: %r", raw_content)
    parsed = _strip_to_json(raw_content)
    return _normalize(parsed)
