"""
The two-pass AI pipeline — the heart of the app.

Every pass takes 1-3 images of the same trade (a plain list[bytes]), not a
single screenshot — the real-world case is a charting-tool image (e.g.
TradingView) for chart structure alongside a broker/platform image (e.g.
MT5, FundedNext) for the actual fills. All three system prompts below spell
out the same source-priority rule: broker/platform numbers win for anything
numeric (entry/exit/SL/TP/PnL/stated R:R), charting-tool images are for chart
structure and pattern context only. A single image still works exactly as
before, just as a one-element list.

Pass 1 (parse):  1-3 images + one line of context -> structured fields.
                 Same shape as ledger_ocr.py, pointed at a chart not a receipt.

Pass 2 (verdict): 1-3 images + parsed trade + the user's OWN rules -> per-rule
                 pass/fail, a coach note, and XP. Also given the images
                 (not just Pass 1's extracted fields) so chart-structure rules
                 can be checked against them, not just the trader's note.
                 The model never judges whether the trade was "good" — only
                 whether the trader followed the rules they themselves
                 defined. Accountability, not advice.

Off-plan advisory (suggest_setup): 1-3 images + note, off-plan trades only ->
                 either a drafted name + checkable rules if the trade shows a
                 genuine repeatable setup, or an honest "not a setup" verdict
                 if it looks like an impulse/discretionary entry. Never runs
                 when a strategy was chosen. Advisory only — a failure here
                 must never block logging the trade itself.

All three passes force strict JSON out (no prose, no markdown fences), same
discipline as the Mondo finance_ai anomaly checks.
"""

import json
import logging
import re
import base64
from groq import Groq

from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

client = Groq(api_key=GROQ_API_KEY)

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


PARSE_SYSTEM = """You read one or more images of a single trade (broker \
order, chart, or position summary) and extract ONLY the facts visible in \
them. Do not infer anything not shown. Do not evaluate the trade. Return \
STRICT JSON, no prose, no code fences, with exactly these keys:
{
  "instrument": string or null,
  "direction": "long" | "short" | null,
  "entry_price": number or null,
  "exit_price": number or null,
  "sl_price": number or null,
  "tp_price": number or null,
  "risk_pct": number or null,
  "r_multiple": number or null,
  "stated_rr": number or null,
  "pnl_usd": number or null,
  "session": string or null,
  "traded_at": string or null
}
stated_rr is ONLY the risk/reward ratio when it is printed as text on the \
chart itself (e.g. "Risk/reward ratio: 2.56", "R:R 1:2.5", "RR: 3.1") — read \
the number straight off that label. Do not compute or infer it from prices; \
if no such label is visible, use null.
pnl_usd is ONLY the realized profit/loss, printed under a label containing \
words like "Closed PnL", "Realized PnL", "P&L", or "Profit" (e.g. "Closed \
PnL: 66.373" means pnl_usd is 66.373). On TradingView-style screenshots this \
is a completely different number from the "Amount" figure printed next to a \
Target or Stop annotation (e.g. "Target: 2050.00 (2.5%) 1:2.56, Amount: \
6,637.30") — that Amount is the position's dollar size/exposure at that \
price level, NOT profit or loss, and must NEVER be used for pnl_usd even if \
it is the largest or only dollar figure visible on the chart. If no line is \
explicitly labeled as closed/realized PnL, use null rather than substituting \
any other dollar amount.
Use null for anything not clearly visible. Never guess.
If more than one image is provided, they are all of the SAME trade, not \
different trades — one may be a charting tool (e.g. TradingView) showing \
chart structure/trendlines/drawings, and another may be the actual broker \
or trading-platform screenshot (e.g. MT5, FundedNext, a prop-firm dashboard) \
showing the real execution. When extracting numeric values — entry_price, \
exit_price, sl_price, tp_price, pnl_usd, stated_rr — ALWAYS prefer the \
numbers shown on a broker/trading-platform screenshot over a charting-tool \
screenshot, because charting-tool drawings can be forecasts, planned levels, \
or annotations rather than what was actually filled. Use a charting-tool \
image only to fill in a field that no broker/platform image shows at all. \
Never average or blend a number across images — pick the single most \
trustworthy source for each field using this priority."""


def parse_screenshot(images: list[bytes], context_note: str) -> dict:
    """Pass 1 — 1-3 screenshots of the same trade + user's one-line context
    -> structured fields. See PARSE_SYSTEM for the source-priority rule when
    more than one image is given (broker/platform numbers over charting-tool
    numbers)."""
    image_blocks = [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"}}
        for img in images
    ]
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
                         f"{len(images)} image(s) of this same trade follow. "
                         f"Extract the visible trade facts as JSON."},
                *image_blocks,
            ]},
        ],
    )
    raw_content = resp.choices[0].message.content
    # INFO, not DEBUG — this is the one place the exact model output is
    # visible; logged unconditionally so a bad parse can always be diagnosed
    # from the server log without turning on debug logging after the fact.
    logger.info("parse_screenshot raw model output: %r", raw_content)
    return _strip_to_json(raw_content)


VERDICT_SYSTEM = """You are a trading-discipline coach. You are given one or \
more images of a trade, its extracted data, and the trader's OWN rules for \
the setup they say they used. Your job is NOT to judge whether the trade was \
smart, or to give trading advice. Your ONLY job is to check, rule by rule, \
whether the trader followed the rules THEY defined.

Some rules describe chart structure (e.g. a trendline break, a retest, a \
confirmation candle) — look at the images themselves to verify those, not \
just the extracted numbers or the trader's note. The images are your \
primary evidence for anything visual; the note is context, not proof.

If more than one image is provided, they are all of the SAME trade: one may \
be a charting tool (e.g. TradingView) showing chart structure/trendlines/ \
patterns, and another may be the actual broker/trading-platform screenshot \
(e.g. MT5, FundedNext) showing the real execution. Use the charting-tool \
image as your primary evidence for chart-structure rules (trendlines, \
support/resistance, candle patterns) — that is exactly what it's for. But \
for anything numeric (actual entry/exit/SL/TP/PnL), trust the broker/ \
platform image over the charting tool, since a charting-tool image can show \
planned or forecast levels rather than what was actually filled; the \
extracted `trade` data you're given already reflects that same priority.

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


def check_rules(images: list[bytes], trade: dict, strategy_name: str, rules: list, context_note: str) -> dict:
    """Pass 2 — 1-3 images of the trade + parsed trade + user's rules -> per-
    rule verdict.

    Takes the images (not just Pass 1's extracted fields) so chart-structure
    rules — trendline breaks, retests, confirmation candles — can actually be
    checked against the image instead of only the trader's note. See
    VERDICT_SYSTEM for the same charting-tool-vs-broker source priority used
    in parse_screenshot when more than one image is given.
    """
    image_blocks = [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"}}
        for img in images
    ]
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
                         f"Trade data + note (JSON):\n{json.dumps(payload)}\n\n"
                         f"{len(images)} image(s) of this same trade follow."},
                *image_blocks,
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
off-plan trade — one or more images of the trade and the trader's own note — \
that matched none of the trader's defined strategies. Decide honestly \
whether this trade reflects a coherent, REPEATABLE setup (identifiable entry \
logic, chart structure, and conditions someone could check on a future \
trade) or whether it looks like an impulse, random, or purely discretionary \
entry with no repeatable process.

If more than one image is provided, they are all of the SAME trade — a \
charting-tool image (e.g. TradingView) for chart structure/trendlines/ \
patterns, and possibly a broker/platform image (e.g. MT5, FundedNext) for \
the actual execution. Base the repeatable-structure judgment mainly on the \
charting-tool image; if you reference any specific price, prefer the \
broker/platform image's numbers over the charting tool's.

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


def suggest_setup(images: list[bytes], context_note: str) -> dict:
    """Off-plan-only advisory pass — 1-3 images of the trade + note -> either
    a drafted repeatable-setup name/rules, or an honest "not a setup"
    verdict. Single-trade version: this judgment isn't persisted or reused
    across trades."""
    image_blocks = [
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(img).decode()}"}}
        for img in images
    ]
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0.2,
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": SUGGEST_SETUP_SYSTEM},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Trader's note about this trade: {context_note!r}. "
                         f"{len(images)} image(s) of this same trade follow. "
                         f"Judge whether this is a repeatable setup."},
                *image_blocks,
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
