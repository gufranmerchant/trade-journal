"""
The two-pass AI pipeline — the heart of the app.

Pass 1 (parse):  trade screenshot + one line of context -> structured fields.
                 Same shape as ledger_ocr.py, pointed at a chart not a receipt.

Pass 2 (verdict): parsed trade + the user's OWN rules -> per-rule pass/fail,
                 a coach note, and XP. The model never judges whether the
                 trade was "good" — only whether the trader followed the rules
                 they themselves defined. Accountability, not advice.

Both passes force strict JSON out (no prose, no markdown fences), same
discipline as the Mondo finance_ai anomaly checks.
"""

import os
import json
import base64
from pathlib import Path
from groq import Groq

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
VISION_MODEL = "qwen/qwen3.6-27b"  # vision-capable
TEXT_MODEL = "openai/gpt-oss-120b"


def _strip_to_json(raw: str) -> dict:
    """Models sometimes wrap JSON in prose or ```json fences. Be defensive."""
    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


PARSE_SYSTEM = """You read a trading screenshot (broker order, chart, or \
position summary) and extract ONLY the facts visible in it. Do not infer \
anything not shown. Do not evaluate the trade. Return STRICT JSON, no prose, \
no code fences, with exactly these keys:
{
  "instrument": string or null,
  "direction": "long" | "short" | null,
  "entry_price": number or null,
  "exit_price": number or null,
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
    return _strip_to_json(resp.choices[0].message.content)


VERDICT_SYSTEM = """You are a trading-discipline coach. You are given a trade \
and the trader's OWN rules for the setup they say they used. Your job is NOT \
to judge whether the trade was smart, or to give trading advice. Your ONLY \
job is to check, rule by rule, whether the trader followed the rules THEY \
defined.

Core principle: a winning trade that broke a rule still failed the rule. The \
outcome never validates the process. Be honest but not harsh — name one thing \
done well when it's true.

Return STRICT JSON, no prose, no fences:
{
  "rule_results": [{"rule_id": int, "text": string, "passed": bool}],
  "coach_note": string,   // 1-2 sentences, plain and direct
  "did_well": string      // one genuine positive, or "" if none
}
Evaluate every rule you are given. If the evidence for a rule is not present, \
mark it not passed rather than assuming."""


def check_rules(trade: dict, strategy_name: str, rules: list, context_note: str) -> dict:
    """Pass 2 — parsed trade + user's rules -> per-rule verdict + coach note."""
    rules_text = "\n".join(f'- (id {r["id"]}) {r["text"]}' for r in rules)
    payload = {
        "setup": strategy_name,
        "trade": trade,
        "trader_note": context_note,
        "rules": rules,
    }
    resp = client.chat.completions.create(
        model=TEXT_MODEL,
        temperature=0.2,
        messages=[
            {"role": "system", "content": VERDICT_SYSTEM},
            {"role": "user", "content":
                f"Setup: {strategy_name}\nRules:\n{rules_text}\n\n"
                f"Trade data + note (JSON):\n{json.dumps(payload)}"},
        ],
    )
    return _strip_to_json(resp.choices[0].message.content)


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
