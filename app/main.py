"""
POST /trades — the one endpoint that is the product.

Flow:
  1. receive screenshot + context note + chosen strategy (or none)
  2. parse the screenshot into structured fields          (ai.parse_screenshot)
  3. if a strategy was chosen: check the trade against its rules (ai.check_rules)
     if not: tag off-plan, no rule check, zero XP, and ask ai.suggest_setup
     whether it looks like a repeatable setup worth saving
  4. persist the trade with its verdict
  5. return everything the detail screen renders

Off-plan is represented by strategy_id = None. The app cannot invent a
strategy — "matched nothing" is stored as nothing. The off-plan
suggest_setup judgment is advisory and single-trade scoped: it rides along
in this response only (see the `setup_suggestion` key), never persisted to
the Trade row and never returned by GET/PATCH /trades/{id}.

GET /trades and GET /users/{user_id}/dashboard read that history back:
a filterable trade list, and the rolling discipline score + rule-adherence
streak the dashboard renders. DELETE /trades/{id} hard-deletes a trade —
nothing else references a trade by id, so there's no soft-delete concern.

POST/GET/PATCH /strategies manage the rulebooks trades get checked against.
Rule ids are stable across edits — see _apply_rule_updates — because
Trade.rule_results and any future per-rule stats are keyed on them.
Strategies are soft-deleted only (PATCH .../is_active=false) — a trade's
rule_results reference the strategy's rules, so removing a strategy must
never break the history of trades already checked against it.

POST /users creates the user row everything else hangs off of (email must
be unique) — there's no auth yet, so this is just enough to seed data.
"""

import logging
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, User, Strategy, Trade
from app import ai

# INFO, not just DEBUG, so ai.parse_screenshot's raw-model-output logging
# (see app/ai.py) shows up by default under `uvicorn app.main:app` without
# needing separate logging config — the whole point is to always be able to
# see what the vision model actually returned for a given screenshot.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

engine = create_engine("sqlite:///journal.db")
Base.metadata.create_all(engine)
app = FastAPI(title="Trade Discipline Journal")

# Plain HTML/CSS/JS frontend, no build step — served as static files so it
# can become a PWA later without changing how it's hosted. Mounted under
# /static (not "/") to keep it unambiguous alongside the API routes below.
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(STATIC_DIR / "index.html")

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


class UserCreate(BaseModel):
    email: str
    display_name: str | None = None


class RuleIn(BaseModel):
    id: int | None = None
    text: str


class StrategyCreate(BaseModel):
    user_id: int
    name: str
    description: str | None = None
    direction_bias: str | None = None
    rules: list[RuleIn] = []


class StrategyUpdate(BaseModel):
    user_id: int
    name: str | None = None
    description: str | None = None
    direction_bias: str | None = None
    is_active: bool | None = None
    rules: list[RuleIn] | None = None


def _apply_rule_updates(existing_rules: list[dict], rule_updates: list[RuleIn]) -> list[dict]:
    """Merge incoming rule edits onto existing rules, keeping ids stable.

    An update entry whose id matches an existing rule keeps that id (only its
    text changes). An entry with no id, or an id that doesn't match any
    existing rule, is a new rule and gets the next unused id. Ids are never
    reused or renumbered, and omitting a rule drops it — the rule-checker and
    a trade's stored rule_results reference rules by id, so a stable id is
    what keeps that history meaningful across edits.
    """
    existing_ids = {r["id"] for r in existing_rules}
    next_id = max(existing_ids, default=0) + 1
    merged = []
    for u in rule_updates:
        if u.id is not None and u.id in existing_ids:
            merged.append({"id": u.id, "text": u.text})
        else:
            merged.append({"id": next_id, "text": u.text})
            next_id += 1
    return merged


def _strategy_out(strategy: Strategy) -> dict:
    return {
        "id": strategy.id,
        "user_id": strategy.user_id,
        "name": strategy.name,
        "description": strategy.description,
        "direction_bias": strategy.direction_bias,
        "rules": strategy.rules,
        "is_active": strategy.is_active,
        "created_at": strategy.created_at,
    }


@app.post("/users")
def create_user(payload: UserCreate):
    with Session(engine) as s:
        existing = s.scalar(select(User).where(User.email == payload.email))
        if existing is not None:
            raise HTTPException(409, "A user with this email already exists")

        user = User(email=payload.email, display_name=payload.display_name)
        s.add(user)
        s.commit()
        s.refresh(user)

        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "xp": user.xp,
            "discipline_score": user.discipline_score,
        }


class TradeUpdate(BaseModel):
    """All fields the detail screen lets a user correct after a screenshot
    parse got something wrong. Unlike StrategyUpdate, a field left out of the
    request body still clears to null here (rather than being left alone) —
    the edit form always submits every field together, so "not sent" and
    "cleared" are the same intent, not a partial patch of unrelated fields.
    """
    user_id: int
    instrument: str | None = None
    direction: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    risk_pct: float | None = None
    r_multiple: float | None = None
    pnl_usd: float | None = None
    session: str | None = None


def _trade_detail_out(trade: Trade, strategy_name: str | None) -> dict:
    return {
        "id": trade.id,
        "strategy_id": trade.strategy_id,
        "strategy_name": strategy_name,
        "instrument": trade.instrument,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "sl_price": trade.sl_price,
        "tp_price": trade.tp_price,
        "risk_pct": trade.risk_pct,
        "r_multiple": trade.r_multiple,
        "pnl_usd": trade.pnl_usd,
        "session": trade.session,
        "context_note": trade.context_note,
        "is_off_plan": trade.is_off_plan,
        "rule_results": trade.rule_results,
        "rules_passed": trade.rules_passed,
        "rules_total": trade.rules_total,
        "coach_note": trade.coach_note,
        "did_well": trade.did_well,
        "xp_earned": trade.xp_earned,
        "created_at": trade.created_at,
    }


@app.post("/trades")
async def log_trade(
    user_id: int = Form(...),
    context_note: str = Form(""),
    strategy_id: int | None = Form(None),
    screenshot: UploadFile = File(...),
):
    image_bytes = await screenshot.read()

    # Pass 1 — parse
    try:
        parsed = ai.parse_screenshot(image_bytes, context_note)
    except ai.AIResponseError:
        raise HTTPException(502, "Couldn't read that screenshot — try again or use a clearer image.")

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
            sl_price=parsed.get("sl_price"),
            tp_price=parsed.get("tp_price"),
            risk_pct=parsed.get("risk_pct"),
            r_multiple=parsed.get("r_multiple"),
            pnl_usd=parsed.get("pnl_usd"),
            session=parsed.get("session"),
            context_note=context_note,
        )

        setup_suggestion = None
        if strategy is None:
            # Off-plan: the biggest red flag. No rule check, no XP.
            trade.is_off_plan = True
            trade.coach_note = ("No setup matched this trade. Off-plan entries "
                                "are worth reviewing — was this a real setup, "
                                "or an impulse?")
            # Advisory only, and single-trade scoped (not persisted) — a
            # failure here must never block logging the off-plan trade
            # itself, so swallow it and just show no suggestion.
            try:
                setup_suggestion = ai.suggest_setup(image_bytes, context_note)
            except ai.AIResponseError:
                setup_suggestion = None
        else:
            # Pass 2 — verdict against the user's own rules (screenshot included
            # so chart-structure rules can be checked against the image, not
            # just the extracted fields)
            try:
                verdict = ai.check_rules(image_bytes, parsed, strategy.name, strategy.rules, context_note)
            except ai.AIResponseError:
                raise HTTPException(502, "Couldn't check this trade against your rules — try again.")
            score = ai.score_trade(verdict)
            trade.rule_results = verdict.get("rule_results", [])
            trade.rules_passed = score["rules_passed"]
            trade.rules_total = score["rules_total"]
            trade.xp_earned = score["xp_earned"]
            trade.coach_note = verdict.get("coach_note", "")
            trade.did_well = verdict.get("did_well", "")

            user = s.get(User, user_id)
            if user:
                user.xp += trade.xp_earned

        s.add(trade)
        s.commit()
        s.refresh(trade)

        # Same full shape GET/PATCH /trades/{id} return — the result screen
        # needs entry/exit/SL/TP too, not just the R-multiple/verdict fields,
        # and reusing this helper keeps "everything the detail screen shows"
        # true from the very first render instead of just after a refetch.
        # setup_suggestion is bolted on only here, not in _trade_detail_out —
        # it's a one-time judgment for this submission, not a stored trade
        # field, so GET/PATCH /trades/{id} never return it.
        return {
            **_trade_detail_out(trade, strategy.name if strategy else None),
            "setup_suggestion": setup_suggestion,
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
                "pnl_usd": t.pnl_usd,
                "is_off_plan": t.is_off_plan,
                "rules_passed": t.rules_passed,
                "rules_total": t.rules_total,
                "xp_earned": t.xp_earned,
                "coach_note": t.coach_note,
                "created_at": t.created_at,
            }
            for t in trades
        ]


@app.get("/trades/{trade_id}")
def get_trade(trade_id: int, user_id: int):
    with Session(engine) as s:
        trade = s.get(Trade, trade_id)
        if trade is None or trade.user_id != user_id:
            raise HTTPException(404, "Trade not found for this user")

        strategy_name = None
        if trade.strategy_id is not None:
            strategy = s.get(Strategy, trade.strategy_id)
            strategy_name = strategy.name if strategy else None

        return _trade_detail_out(trade, strategy_name)


@app.patch("/trades/{trade_id}")
def update_trade(trade_id: int, payload: TradeUpdate):
    with Session(engine) as s:
        trade = s.get(Trade, trade_id)
        if trade is None or trade.user_id != payload.user_id:
            raise HTTPException(404, "Trade not found for this user")

        trade.instrument = payload.instrument
        trade.direction = payload.direction
        trade.entry_price = payload.entry_price
        trade.exit_price = payload.exit_price
        trade.sl_price = payload.sl_price
        trade.tp_price = payload.tp_price
        trade.risk_pct = payload.risk_pct
        trade.r_multiple = payload.r_multiple
        trade.pnl_usd = payload.pnl_usd
        trade.session = payload.session

        s.commit()
        s.refresh(trade)

        strategy_name = None
        if trade.strategy_id is not None:
            strategy = s.get(Strategy, trade.strategy_id)
            strategy_name = strategy.name if strategy else None

        return _trade_detail_out(trade, strategy_name)


@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: int, user_id: int):
    """Hard delete — unlike strategies, a trade has no rulebook other trades
    depend on, so there's nothing to preserve by soft-deleting it."""
    with Session(engine) as s:
        trade = s.get(Trade, trade_id)
        if trade is None or trade.user_id != user_id:
            raise HTTPException(404, "Trade not found for this user")

        # xp is a running total stored on the user row, not derived on read
        # like discipline_score/streak are — so undo what this trade
        # contributed before removing it, or the XP would linger forever.
        if trade.xp_earned:
            user = s.get(User, user_id)
            if user:
                user.xp = max(0, user.xp - trade.xp_earned)

        s.delete(trade)
        s.commit()

    return Response(status_code=204)


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
            "discipline_window_trades": DISCIPLINE_WINDOW,
            "current_streak": streak,
            "trades_logged": len(recent_trades),
        }


@app.post("/strategies")
def create_strategy(payload: StrategyCreate):
    with Session(engine) as s:
        user = s.get(User, payload.user_id)
        if user is None:
            raise HTTPException(404, "User not found")

        strategy = Strategy(
            user_id=payload.user_id,
            name=payload.name,
            description=payload.description,
            direction_bias=payload.direction_bias,
            rules=_apply_rule_updates([], payload.rules),
        )
        s.add(strategy)
        s.commit()
        s.refresh(strategy)
        return _strategy_out(strategy)


@app.get("/strategies")
def list_strategies(user_id: int, is_active: bool | None = None):
    with Session(engine) as s:
        query = select(Strategy).where(Strategy.user_id == user_id)
        if is_active is not None:
            query = query.where(Strategy.is_active == is_active)
        query = query.order_by(Strategy.created_at.desc())

        strategies = s.scalars(query).all()
        return [_strategy_out(st) for st in strategies]


@app.patch("/strategies/{strategy_id}")
def update_strategy(strategy_id: int, payload: StrategyUpdate):
    with Session(engine) as s:
        strategy = s.get(Strategy, strategy_id)
        if strategy is None or strategy.user_id != payload.user_id:
            raise HTTPException(404, "Strategy not found for this user")

        if payload.name is not None:
            strategy.name = payload.name
        if payload.description is not None:
            strategy.description = payload.description
        if payload.direction_bias is not None:
            strategy.direction_bias = payload.direction_bias
        if payload.is_active is not None:
            strategy.is_active = payload.is_active
        if payload.rules is not None:
            strategy.rules = _apply_rule_updates(strategy.rules, payload.rules)

        s.commit()
        s.refresh(strategy)
        return _strategy_out(strategy)
