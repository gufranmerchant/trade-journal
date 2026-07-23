"""
POST /trades — the one endpoint that is the product.

Flow:
  1. receive screenshot + context note + chosen strategy (or none)
  2. parse the screenshot into structured fields          (ai.parse_screenshot)
  3. if a strategy was chosen: check the trade against its rules (ai.check_rules)
     if not: tag off-plan, no rule check, zero XP
  4. persist the trade with its verdict
  5. return everything the detail screen renders

Off-plan is represented by strategy_id = None. The app cannot invent a
strategy — "matched nothing" is stored as nothing.
"""

from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, User, Strategy, Trade
from app import ai

engine = create_engine("sqlite:///journal.db")
Base.metadata.create_all(engine)
app = FastAPI(title="Trade Discipline Journal")


@app.post("/trades")
async def log_trade(
    user_id: int = Form(...),
    context_note: str = Form(""),
    strategy_id: int | None = Form(None),
    screenshot: UploadFile = File(...),
):
    image_bytes = await screenshot.read()

    # Pass 1 — parse
    parsed = ai.parse_screenshot(image_bytes, context_note)

    with Session(engine) as s:
        strategy = None
        if strategy_id is not None:
            strategy = s.get(Strategy, strategy_id)
            if strategy is None or strategy.user_id != user_id:
                raise HTTPException(404, "Strategy not found for this user")

        trade = Trade(
            user_id=user_id,
            strategy_id=strategy.id if strategy else None,
            instrument=parsed.get("instrument"),
            direction=parsed.get("direction"),
            entry_price=parsed.get("entry_price"),
            exit_price=parsed.get("exit_price"),
            risk_pct=parsed.get("risk_pct"),
            r_multiple=parsed.get("r_multiple"),
            session=parsed.get("session"),
            context_note=context_note,
        )

        if strategy is None:
            # Off-plan: the biggest red flag. No rule check, no XP.
            trade.is_off_plan = True
            trade.coach_note = ("No setup matched this trade. Off-plan entries "
                                "are worth reviewing — was this a real setup, "
                                "or an impulse?")
        else:
            # Pass 2 — verdict against the user's own rules
            verdict = ai.check_rules(parsed, strategy.name, strategy.rules, context_note)
            score = ai.score_trade(verdict)
            trade.rule_results = verdict.get("rule_results", [])
            trade.rules_passed = score["rules_passed"]
            trade.rules_total = score["rules_total"]
            trade.xp_earned = score["xp_earned"]
            note = verdict.get("coach_note", "")
            well = verdict.get("did_well", "")
            trade.coach_note = f"{note} {('Well done: ' + well) if well else ''}".strip()

            user = s.get(User, user_id)
            if user:
                user.xp += trade.xp_earned

        s.add(trade)
        s.commit()
        s.refresh(trade)

        return {
            "id": trade.id,
            "instrument": trade.instrument,
            "direction": trade.direction,
            "r_multiple": trade.r_multiple,
            "is_off_plan": trade.is_off_plan,
            "rule_results": trade.rule_results,
            "rules_passed": trade.rules_passed,
            "rules_total": trade.rules_total,
            "coach_note": trade.coach_note,
            "xp_earned": trade.xp_earned,
        }
