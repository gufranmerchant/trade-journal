# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A trade-discipline journal for prop/funded-challenge traders. A user uploads a trade
screenshot + one line of context; the app parses it into a structured journal entry and
judges it against the **user's own** strategy rules (defined at onboarding — nothing is
preset). It is a discipline coach, not a trading-advice tool: a winning trade that broke
a rule still fails that rule.

Status: backend core only (data model, two-pass AI pipeline, `POST /trades`). No
dashboard endpoints, onboarding, frontend, or auth yet.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste a real GROQ_API_KEY into .env
uvicorn app.main:app --reload
```

Interactive API docs at http://127.0.0.1:8000/docs (only real way to exercise the API
right now — there is no test suite, linter, or frontend in the repo).

No test/lint/build tooling is configured yet — don't assume `pytest`, `ruff`, etc. exist.

## Architecture

Three files carry the whole backend:

- `app/models.py` — SQLAlchemy models: `User`, `Strategy`, `Trade`.
- `app/ai.py` — the two-pass Groq pipeline that turns a screenshot into a judged trade.
- `app/main.py` — the endpoints: `POST /trades` (wires models + ai.py and persists the result), `GET /trades` (filterable list), `GET /users/{user_id}/dashboard` (discipline score + streak).

### Data model shape (`app/models.py`)

- `Strategy.rules` is a JSON list of `{"id": int, "text": str}` objects — user-authored,
  checkable rules, not free prose. Turning a trader's fuzzy mental rules into things the
  AI can verify one-by-one is the actual product.
- `Trade.strategy_id` is nullable **on purpose**. A trade matching none of the user's
  defined setups is tagged **off-plan** — the app never invents a matching strategy to
  launder an impulse trade.
- XP (`User.xp`, `Trade.xp_earned`) is earned only from rule adherence, never from
  profit/outcome. Off-plan trades earn zero XP and skip rule-checking entirely.
- Scoring is deliberately flat/dumb for v1 (`ai.XP_PER_RULE = 10`, no curve tuning) — the
  code says as much in comments; don't add ranking/leveling logic unasked.

### AI pipeline (`app/ai.py`)

Two independent Groq calls per trade, both forced to strict JSON (no prose, no markdown
fences) via `_strip_to_json`:

1. **Pass 1 — `parse_screenshot`** (vision model `meta-llama/llama-4-scout-17b-16e-instruct`,
   temperature 0): screenshot + context note → structured fields only (instrument,
   direction, prices, r_multiple, session, traded_at). Must not infer or evaluate — null
   for anything not clearly visible.
2. **Pass 2 — `check_rules`** (text model `llama-3.3-70b-versatile`, temperature 0.2):
   parsed trade + the user's own strategy rules → per-rule pass/fail + one coach note +
   one genuine positive (or `""`). The model is never asked whether the trade was smart;
   only whether the trader's own stated process was followed. Missing evidence for a rule
   means "not passed," never an assumed pass.

`.env` is loaded manually in `ai.py` via a small hand-rolled parser (no python-dotenv
dependency) — keep it that way unless there's a reason to add the dependency.

### Request flow (`app/main.py`)

`POST /trades` (multipart: `user_id`, `context_note`, optional `strategy_id`, `screenshot`):

1. Read screenshot bytes, run Pass 1 (`parse_screenshot`) — this always runs, even
   off-plan, since the trade still needs structured fields.
2. Look up `strategy_id` if given; 404 if it doesn't belong to `user_id`.
3. Build the `Trade` row from parsed fields.
4. Branch:
   - No strategy → `is_off_plan = True`, static coach note, no Pass 2, no XP.
   - Strategy chosen → run Pass 2 (`check_rules`), score via `ai.score_trade`, add
     `xp_earned` to the user's running `xp` total.
5. Persist and return the fields the trade-detail screen needs (not the full ORM row).

When extending this flow, preserve the off-plan short-circuit — it's the core product
rule ("matched nothing" is stored as nothing), not an edge case to optimize away.

### Dashboard reads (`app/main.py`)

- `GET /trades?user_id=&strategy_id=&direction=` — trade list for a user, optionally
  filtered, newest first.
- `GET /users/{user_id}/dashboard` — recomputes and returns `discipline_score` and
  `current_streak` from the user's trade history (also writes `discipline_score` back
  onto the `User` row, since the model documents it as a stored/recomputed field).
  Both metrics are windowed to the most recent `DISCIPLINE_WINDOW` (20) trades and treat
  an off-plan trade as 0% rule compliance — same "no rules followed" logic as the
  off-plan branch in `POST /trades`. A streak is unbroken only while every trade in it
  passed *all* of its own rules; one off-plan trade or one failed rule resets it to the
  point after that trade.

## Conventions worth knowing

- SQLite file `journal.db` is created at the working directory root on app startup
  (`create_engine("sqlite:///journal.db")` in `main.py`) — not committed, not migrated;
  schema changes currently mean dropping and recreating the DB.
- Uploaded screenshots have a home (`uploads/`) but `main.py` does not yet write to it —
  `screenshot_path` on `Trade` is defined but unset by the current endpoint.
