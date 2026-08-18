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

POST/GET/PATCH /strategies manage the rulebooks trades get checked against.
Rule ids are stable across edits — see _apply_rule_updates — because
Trade.rule_results and any future per-rule stats are keyed on them.

POST /users creates the user row everything else hangs off of (email must
be unique) — there's no auth yet, so this is just enough to seed data.
"""

from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, User, Strategy, Trade
from app import ai

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
