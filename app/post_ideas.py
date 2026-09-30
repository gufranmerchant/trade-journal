"""Post Idea Finder + platform hashtags/keywords — one Groq call: a content
topic and the platform(s) the user picked -> 3 post ideas (each with a
short rationale) plus, for each selected platform, a short list of
hashtags/keywords with a one-line reason each. The social-content
equivalent of app/keyword_research.py's book keyword research — same
LLM-judgment-not-scraped-data caveat, same Groq conventions.

Reuses keyword_research.MODEL rather than introducing a second, unverified
model name — a wrong model name (copied from a stale comment instead of
checked against client.models.list()) took that tool down completely the
first time it shipped; see app/keyword_research.py's MODEL comment.
"""

import json
import logging
import re

from groq import APIError

from app.keyword_research import MODEL, client

logger = logging.getLogger(__name__)

MAX_TOPIC_LENGTH = 300

# Canonical order also used to key platform_tags in the response — keeps
# frontend rendering order stable regardless of what order the user checked
# platforms in, or what order the model returns keys.
PLATFORM_LABELS = {
    "linkedin": "LinkedIn",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "medium": "Medium",
}

# Each selected platform adds its own block of tagged output, so the worst
# case (all 5 platforms) is what has to fit under MAX_COMPLETION_TOKENS —
# see the token-budget comment below. An earlier version of this prompt
# asked for 5 tags/platform with a max-6-word reason and measured ~900-910
# actual tokens on the worst case (a long topic + all 5 platforms) - too
# close to the 1000 OTPM ceiling with zero margin for a second concurrent
# request. Trimmed to 4 tags/platform + max-4-word reasons instead, which
# measured ~720 actual tokens on the same worst case: comfortable margin,
# not just barely-passing.
TAGS_PER_PLATFORM = 4

# Same 1000-output-tokens/minute (OTPM) on-demand-tier ceiling
# app/keyword_research.py hit — Groq reserves against this value itself
# before the call runs, not actual usage, so it has to stay under 1000
# regardless of how many platforms are selected. Set with real margin above
# the ~720-token worst case measured above, not right at the ceiling.
MAX_COMPLETION_TOKENS = 850

_THINK_BLOCK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


class PostIdeasError(RuntimeError):
    """The model didn't return parseable JSON, or the Groq call itself failed
    (rate limit, model error, connection issue, ...). Always carries
    FRIENDLY_ERROR_MESSAGE — main.py shows str(e) straight to the user, so
    the technical reason goes in the logger.warning calls instead."""


FRIENDLY_ERROR_MESSAGE = "Something went wrong generating results — try again in a moment."


SYSTEM_PROMPT = f"""You are a social media content strategist. Given a content \
topic and a list of platforms, suggest, briefly and concisely:

1. ideas: exactly 3 distinct post ideas for this topic. For each, give a \
very short reason (max 8 words) why it would work.
2. platform_tags: for EACH platform given in the user's message, exactly \
{TAGS_PER_PLATFORM} hashtags or keywords relevant to this topic on that \
platform, each with a very short reason (max 4 words). Use each platform's \
own convention — "#" hashtags for Instagram/TikTok, plain SEO keyword \
phrases (no "#") for LinkedIn/YouTube/Medium.

Be specific to the given topic — never return generic placeholders. Keep \
the whole response terse — short phrases, no extra commentary.

Return STRICT JSON, no prose, no fences:
{{
  "ideas": [{{"idea": string, "rationale": string}}, ...],
  "platform_tags": {{
    "<platform key exactly as given, lowercase>": [{{"tag": string, "reason": string}}, ...],
    ...
  }}
}}"""


def _normalize_platforms(platforms) -> list[str]:
    """Raises ValueError for no/unknown platforms; returns the requested
    ones in PLATFORM_LABELS' canonical order, deduped."""
    requested = {str(p).strip().lower() for p in (platforms or []) if str(p).strip()}
    if not requested:
        raise ValueError("select at least one platform")

    unknown = requested - PLATFORM_LABELS.keys()
    if unknown:
        raise ValueError(f"unknown platform: {', '.join(sorted(unknown))}")

    return [p for p in PLATFORM_LABELS if p in requested]


def _strip_to_json(raw: str) -> dict:
    """Same defensive extraction as app.keyword_research._strip_to_json —
    models sometimes wrap JSON in prose, ```json fences, or a <think>
    reasoning trace."""
    cleaned = _THINK_BLOCK_RE.sub("", raw or "").strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]
    if not cleaned:
        logger.warning("generate_post_ideas: model returned no parseable content")
        raise PostIdeasError(FRIENDLY_ERROR_MESSAGE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("generate_post_ideas: model returned malformed JSON: %s", e)
        raise PostIdeasError(FRIENDLY_ERROR_MESSAGE) from e


def _normalize(parsed: dict, platforms: list[str]) -> dict:
    """Defensive coercion, same rationale as app.keyword_research._normalize
    — a half-formed entry is dropped rather than shown broken to the user.
    platform_tags is keyed strictly off the requested platforms (case-
    insensitively matched against whatever the model actually returned),
    never off whatever keys happen to show up in the model's JSON."""
    ideas = []
    for item in (parsed.get("ideas") or [])[:3]:
        idea = str((item or {}).get("idea") or "").strip()
        if not idea:
            continue
        ideas.append({"idea": idea, "rationale": str((item or {}).get("rationale") or "").strip()})

    raw_platform_tags = parsed.get("platform_tags") or {}
    lowered_platform_tags = {
        str(key).strip().lower(): value for key, value in raw_platform_tags.items()
    }

    platform_tags = {}
    for platform in platforms:
        tags = []
        for item in lowered_platform_tags.get(platform) or []:
            tag = str((item or {}).get("tag") or "").strip()
            if not tag:
                continue
            tags.append({"tag": tag, "reason": str((item or {}).get("reason") or "").strip()})
        platform_tags[platform] = tags

    return {"ideas": ideas, "platform_tags": platform_tags}


def validate_request(topic: str, platforms) -> tuple[str, list[str]]:
    """Returns (stripped topic, normalized platforms), or raises ValueError.
    Split out so main.py can reject bad input before it spends a rate-limit hit."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"topic must be {MAX_TOPIC_LENGTH} characters or fewer")
    return topic, _normalize_platforms(platforms)


def generate_post_ideas(topic: str, platforms) -> dict:
    """topic + platforms -> {"ideas": [...], "platform_tags": {platform: [...]}}.

    Raises ValueError for bad input (caught by main.py as a 422) and
    PostIdeasError if the model's response couldn't be parsed, or the Groq
    call itself failed (caught as a 502) — same split as
    app.keyword_research.research_keywords.
    """
    topic, normalized_platforms = validate_request(topic, platforms)
    labels = [PLATFORM_LABELS[p] for p in normalized_platforms]

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            reasoning_effort="none",
            max_completion_tokens=MAX_COMPLETION_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Topic: {topic}\nPlatforms (use these exact lowercase keys "
                    f"in platform_tags): {', '.join(normalized_platforms)} "
                    f"({', '.join(labels)})"
                )},
            ],
        )
    except APIError as e:
        # Covers RateLimitError, APIConnectionError, model/auth errors, etc.
        # (all subclass groq.APIError) — main.py turns this into a clean 502
        # instead of a raw 500 crashing out of the request.
        logger.warning("generate_post_ideas Groq API call failed: %s", e)
        raise PostIdeasError(FRIENDLY_ERROR_MESSAGE) from e

    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as app.ai's passes: always visible so
    # a bad or malformed response can be diagnosed from the server log.
    logger.info("generate_post_ideas raw model output: %r", raw_content)
    parsed = _strip_to_json(raw_content)
    return _normalize(parsed, normalized_platforms)
