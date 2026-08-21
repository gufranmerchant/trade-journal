"""
Data model for the trade-discipline journal.

Design principle driving these tables: the app has NO built-in strategies.
Each user defines their own setups in their own words during onboarding.
That user-authored rulebook is what every trade gets judged against.

A "different strategy" isn't an edge case — it's the normal case. The app
is a mirror for whatever rules you feed it, not a library of preset ones.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, JSON
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Progression — kept deliberately dumb for v1 (flat XP, simple thresholds).
    # Tuning the curve is a rabbit hole; v1 just stores the running totals.
    xp = Column(Integer, default=0)
    discipline_score = Column(Integer, default=0)  # rolling 0-100, recomputed

    strategies = relationship("Strategy", back_populates="user")
    trades = relationship("Trade", back_populates="user")


class Strategy(Base):
    """
    A user-defined setup (e.g. "Setup A"). This is the heart of the product.

    `rules` is a list of checkable rule objects, not free prose — because the
    real work of onboarding is turning a trader's fuzzy mental rules into
    things the AI can actually verify one by one. Example rule object:
        {"id": 1, "text": "Trendline break confirmed before entry"}
        {"id": 2, "text": "Entry only after 5-min confirmation candle closes"}

    Strategies can be added anytime (the "+" chip), so the rulebook evolves.
    """
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)          # "Setup A"
    description = Column(Text)                       # one-line summary
    rules = Column(JSON, nullable=False, default=list)  # list[{id, text}]
    direction_bias = Column(String)                 # "long" | "short" | "both"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="strategies")
    trades = relationship("Trade", back_populates="strategy")


class Trade(Base):
    """
    One logged trade. Parsed from a screenshot + one line of context, then
    checked against its strategy's rules.

    strategy_id is NULLABLE on purpose: a trade that matches none of the
    user's defined setups is "Off-plan" — the single biggest red flag in
    trading. The app never invents a strategy to launder an impulse trade;
    it tags it off-plan and scores it accordingly.
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)

    # Parsed from the screenshot
    instrument = Column(String)                     # "XAUUSD"
    direction = Column(String)                      # "long" | "short"
    entry_price = Column(Float)
    exit_price = Column(Float)
    sl_price = Column(Float)                          # stop-loss
    tp_price = Column(Float)                          # take-profit
    risk_pct = Column(Float)
    r_multiple = Column(Float)                       # +2.1, -1.0
    stated_rr = Column(Float)                         # chart-printed ratio, e.g. "Risk/reward ratio: 2.56" -> 2.56; preferred over computing R:R from entry/SL/exit when present
    pnl_usd = Column(Float)                           # +184.50, -92.00 — supplementary, null if not visible
    session = Column(String)                         # "London open"
    traded_at = Column(DateTime)

    # User's own words
    context_note = Column(Text)

    # The verdict (produced by the AI rule-check pass)
    is_off_plan = Column(Boolean, default=False)
    # True only for a trade that was off-plan when taken and later got
    # retroactively linked to a strategy discovered from it (the off-plan
    # "Save as strategy" flow). Distinct from is_off_plan, which this flips
    # to False once linked — off_plan_origin is the permanent record that no
    # plan existed *at the time*, so it's never rule-checked and never earns
    # XP even after linking; the frontend renders it as a plain "Previously
    # off-plan" tag instead of a rule checklist.
    off_plan_origin = Column(Boolean, default=False)
    rule_results = Column(JSON, default=list)        # [{rule_id, text, passed}]
    rules_passed = Column(Integer, default=0)
    rules_total = Column(Integer, default=0)
    coach_note = Column(Text)
    did_well = Column(Text)                           # one genuine positive from the verdict, or "" if none
    xp_earned = Column(Integer, default=0)

    screenshot_path = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="trades")
    strategy = relationship("Strategy", back_populates="trades")
