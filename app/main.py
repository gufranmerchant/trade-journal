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

GET /trades and GET /users/{user_id}/dashboard read that history back:
a filterable trade list, and the rolling discipline score + rule-adherence
streak the dashboard renders.
"""

from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, User, Strategy, Trade
from app import ai

engine = create_engine("sqlite:///journal.db")
Base.metadata.create_all(engine)
app = FastAPI(title="Trade Discipline Journal")

# Rolling window for the discipline score, flat v1 like ai.XP_PER_RULE.
DISCIPLINE_WINDOW = 20


def _trade_compliance(trade: Trade) -> float:
    """Fraction of a trade's own rules it followed. Off-plan trades followed none."""
    if trade.is_off_plan or trade.rules_total == 0:
        return 0.0
    return trade.rules_passed / trade.rules_total


def compute_discipline_score(recent_trades: list[Trade]) -> int:
    """Rolling 0-100 score over the most recent DISCIPLINE_WINDOW trades.

    recent_trades must be ordered most-recent first.
    """
    window = recent_trades[:DISCIPLINE_WINDOW]
    if not window:
        return 0
    return round(100 * sum(_trade_compliance(t) for t in window) / len(window))


def compute_streak(recent_trades: list[Trade]) -> int:
    """Count of consecutive most-recent trades that fully followed their own rules.

    Off-plan trades and any failed rule break the streak. recent_trades must be
    ordered most-recent first.
    """
    streak = 0
    for t in recent_trades:
        if t.is_off_plan or t.rules_total == 0 or t.rules_passed != t.rules_total:
            break
        streak += 1
    return streak


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


@app.get("/trades")
def list_trades(
    user_id: int,
    strategy_id: int | None = None,
    direction: str | None = None,
):
    with Session(engine) as s:
        query = select(Trade).where(Trade.user_id == user_id)
        if strategy_id is not None:
            query = query.where(Trade.strategy_id == strategy_id)
        if direction is not None:
            query = query.where(Trade.direction == direction)
        query = query.order_by(Trade.created_at.desc())

        trades = s.scalars(query).all()

        return [
            {
                "id": t.id,
                "strategy_id": t.strategy_id,
                "instrument": t.instrument,
                "direction": t.direction,
                "r_multiple": t.r_multiple,
                "is_off_plan": t.is_off_plan,
                "rules_passed": t.rules_passed,
                "rules_total": t.rules_total,
                "xp_earned": t.xp_earned,
                "coach_note": t.coach_note,
                "created_at": t.created_at,
            }
            for t in trades
        ]


@app.get("/users/{user_id}/dashboard")
def get_dashboard(user_id: int):
    with Session(engine) as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "User not found")

        recent_trades = s.scalars(
            select(Trade)
            .where(Trade.user_id == user_id)
            .order_by(Trade.created_at.desc())
        ).all()

        discipline_score = compute_discipline_score(recent_trades)
        streak = compute_streak(recent_trades)

        # discipline_score is a stored, recomputed field (see models.py) —
        # keep it in sync with every dashboard read.
        user.discipline_score = discipline_score
        s.commit()

        return {
            "user_id": user.id,
            "xp": user.xp,
            "discipline_score": discipline_score,
            "current_streak": streak,
            "trades_logged": len(recent_trades),
        }
