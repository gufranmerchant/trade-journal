"""
The two-pass AI pipeline — the heart of the app.

Pass 1 (parse):  trade screenshot + one line of context -> structured fields.
                 Same shape as ledger_ocr.py, pointed at a chart not a receipt.

Pass 2 (verdict): screenshot + parsed trade + the user's OWN rules -> per-rule
                 pass/fail, a coach note, and XP. Also given the screenshot
                 (not just Pass 1's extracted fields) so chart-structure rules
                 can be checked against the image, not just the trader's note.
                 The model never judges whether the trade was "good" — only
                 whether the trader followed the rules they themselves
                 defined. Accountability, not advice.

Off-plan advisory (suggest_setup): screenshot + note, off-plan trades only ->
                 either a drafted name + checkable rules if the trade shows a
                 genuine repeatable setup, or an honest "not a setup" verdict
                 if it looks like an impulse/discretionary entry. Never runs
                 when a strategy was chosen. Advisory only — a failure here
                 must never block logging the trade itself.

All three passes force strict JSON out (no prose, no markdown fences), same
discipline as the Mondo finance_ai anomaly checks.
"""

import os
import json
import logging
import re
import base64
from pathlib import Path
from groq import Groq

logger = logging.getLogger(__name__)

# Lightweight .env loader (no extra dependency) — reads KEY=value lines.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))

# llama-4-scout-17b-16e-instruct and llama-3.3-70b-versatile were both
# deprecated by Groq (2026-06-17, shut off by August); migrated to their
# recommended replacements. qwen3.6-27b is Groq's other multimodal option
# besides the now-deprecated llama-4 vision models.
VISION_MODEL = "qwen/qwen3.6-27b"  # vision-capable, used for both passes now


_THINK_BLOCK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


class AIResponseError(RuntimeError):
    """The model didn't return parseable JSON — bad input, or it ran out of
    output budget mid-reasoning. Caught in main.py and turned into a clean
    error the user can retry, instead of an unhandled 500."""


def _strip_to_json(raw: str) -> dict:
    """Models sometimes wrap JSON in prose, ```json fences, or a <think>
    reasoning trace. Be defensive — reasoning text can itself contain a
    stray {...} example that would otherwise confuse the brace-matching
    below, so drop any think block first rather than just markdown fences."""
    cleaned = _THINK_BLOCK_RE.sub("", raw or "").strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]
    if not cleaned:
        raise AIResponseError("Model returned no parseable content")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise AIResponseError(f"Model returned malformed JSON: {e}") from e


PARSE_SYSTEM = """You read a trading screenshot (broker order, chart, or \
position summary) and extract ONLY the facts visible in it. Do not infer \
anything not shown. Do not evaluate the trade. Return STRICT JSON, no prose, \
no code fences, with exactly these keys:
{
  "instrument": string or null,
  "direction": "long" | "short" | null,
  "entry_price": number or null,
  "exit_price": number or null,
  "sl_price": number or null,
  "tp_price": number or null,
  "risk_pct": number or null,
  "r_multiple": number or null,
  "pnl_usd": number or null,
  "session": string or null,
  "traded_at": string or null
}
Use null for anything not clearly visible. Never guess."""


def parse_screenshot(image_bytes: bytes, context_note: str) -> dict:
    """Pass 1 — screenshot + user's one-line context -> structured fields."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0,
        # This is a structured-extraction task, not something that benefits
        # from chain-of-thought — and letting qwen3.6-27b "think" burned its
        # whole output budget on complex screenshots, leaving content empty.
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": PARSE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Trader's note about this trade: {context_note!r}. "
                         f"Extract the visible trade facts as JSON."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
    )
    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — this is the one place the exact model output is
    # visible; logged unconditionally so a bad parse can always be diagnosed
    # from the server log without turning on debug logging after the fact.
    logger.info("parse_screenshot raw model output: %r", raw_content)
    return _strip_to_json(raw_content)


VERDICT_SYSTEM = """You are a trading-discipline coach. You are given a trade \
screenshot, its extracted data, and the trader's OWN rules for the setup they \
say they used. Your job is NOT to judge whether the trade was smart, or to \
give trading advice. Your ONLY job is to check, rule by rule, whether the \
trader followed the rules THEY defined.

Some rules describe chart structure (e.g. a trendline break, a retest, a \
confirmation candle) — look at the screenshot itself to verify those, not \
just the extracted numbers or the trader's note. The screenshot is your \
primary evidence for anything visual; the note is context, not proof.

Core principle: a winning trade that broke a rule still failed the rule. The \
outcome never validates the process. Be honest but not harsh — name one thing \
done well when it's true.

Return STRICT JSON, no prose, no fences:
{
  "rule_results": [{"rule_id": int, "text": string, "passed": bool}],
  "coach_note": string,   // 1-2 sentences, plain and direct
  "did_well": string      // one genuine positive, or "" if none
}
Evaluate every rule you are given. If the evidence for a rule — in the chart \
or the data — is not present, mark it not passed rather than assuming."""


def check_rules(image_bytes: bytes, trade: dict, strategy_name: str, rules: list, context_note: str) -> dict:
    """Pass 2 — screenshot + parsed trade + user's rules -> per-rule verdict.

    Takes the screenshot (not just Pass 1's extracted fields) so chart-
    structure rules — trendline breaks, retests, confirmation candles — can
    actually be checked against the image instead of only the trader's note.
    """
    b64 = base64.b64encode(image_bytes).decode()
    rules_text = "\n".join(f'- (id {r["id"]}) {r["text"]}' for r in rules)
    payload = {
        "setup": strategy_name,
        "trade": trade,
        "trader_note": context_note,
        "rules": rules,
    }
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0.2,
        # Same reasoning_effort choice as parse_screenshot, and for the same
        # reason: letting qwen3.6-27b "think" on this model can burn its
        # whole output budget and leave content empty.
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": VERDICT_SYSTEM},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Setup: {strategy_name}\nRules:\n{rules_text}\n\n"
                         f"Trade data + note (JSON):\n{json.dumps(payload)}"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
    )
    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as parse_screenshot's logging: this is
    # the one place the exact verdict-model output is visible, logged
    # unconditionally so a bad verdict can be diagnosed from the server log.
    logger.info("check_rules raw model output: %r", raw_content)
    return _strip_to_json(raw_content)


SUGGEST_SETUP_SYSTEM = """You are a trading-discipline analyst. You are given an \
off-plan trade — a screenshot of the chart and the trader's own note — that \
matched none of the trader's defined strategies. Decide honestly whether this \
trade reflects a coherent, REPEATABLE setup (identifiable entry logic, chart \
structure, and conditions someone could check on a future trade) or whether \
it looks like an impulse, random, or purely discretionary entry with no \
repeatable process.

Do not default to yes. Most off-plan trades are impulse trades — say so \
plainly when that is what the evidence shows. Only say a setup exists when \
you can point to specific, checkable structure in the chart and note (e.g. a \
break-and-retest, a liquidity sweep, a specific candle pattern, a defined \
risk rule) — not just "price went up and I bought."

Return STRICT JSON, no prose, no fences. If it is NOT a repeatable setup:
{"is_setup": false}
If it IS a repeatable setup:
{
  "is_setup": true,
  "suggested_name": string,
  "suggested_rules": [{"id": int, "text": string}, ...]
}
suggested_name should be short (e.g. "Liquidity Sweep Reversal"). \
suggested_rules should be 2-5 checkable lines describing what the trader \
actually did, not generic trading advice."""


def _normalize_setup_suggestion(parsed: dict) -> dict:
    """Defensive coercion — the model's output feeds straight into a
    strategy-creation prefill, so a half-formed "setup" (no name, no rules)
    is treated as no suggestion at all rather than shown as one."""
    if not parsed.get("is_setup"):
        return {"is_setup": False}

    name = str(parsed.get("suggested_name") or "").strip()
    rules = []
    for r in parsed.get("suggested_rules") or []:
        text = str((r or {}).get("text") or "").strip()
        if text:
            rules.append({"id": len(rules) + 1, "text": text})

    if not name or not rules:
        return {"is_setup": False}

    return {"is_setup": True, "suggested_name": name, "suggested_rules": rules}


def suggest_setup(image_bytes: bytes, context_note: str) -> dict:
    """Off-plan-only advisory pass — screenshot + note -> either a drafted
    repeatable-setup name/rules, or an honest "not a setup" verdict. Single-
    trade version: this judgment isn't persisted or reused across trades."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0.2,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": SUGGEST_SETUP_SYSTEM},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Trader's note about this trade: {context_note!r}. "
                         f"Judge whether this is a repeatable setup."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
    )
    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — same rationale as the other passes: always visible
    # so a bad or surprising judgment can be diagnosed from the server log.
    logger.info("suggest_setup raw model output: %r", raw_content)
    return _normalize_setup_suggestion(_strip_to_json(raw_content))


# Flat, dumb-simple v1 scoring. Tuning the curve is deliberately deferred.
XP_PER_RULE = 10

def score_trade(verdict: dict) -> dict:
    """Turn a verdict into passed/total counts and XP. No cleverness in v1."""
    results = verdict.get("rule_results", [])
    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    return {
        "rules_passed": passed,
        "rules_total": total,
        "xp_earned": passed * XP_PER_RULE,
    }
