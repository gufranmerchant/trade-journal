# Mirror

A discipline coach for prop/funded-challenge traders — not a trading-advice
tool. Upload a trade screenshot and a one-line note; Mirror parses it into a
structured journal entry and judges it against **your own** strategy rules.
A winning trade that broke a rule still fails that rule.

## Live
[themirrorjournal.org](https://www.themirrorjournal.org)

## Core ideas
- You define your own setups (rules) at onboarding — nothing is preset.
- Each trade is checked rule-by-rule against the setup you say you used.
- Trades matching no setup are tagged **Off-plan** — the biggest red flag,
  never laundered into a strategy it doesn't actually match.
- **Smart off-plan detection**: when a trade doesn't match anything, the AI
  looks at it and tells you whether it looks like a repeatable setup worth
  saving as a new strategy, or just a discretionary/impulse entry.
- XP comes only from rule adherence — never from profit. A winning trade
  that broke a rule earns nothing.

## Stack
- **Backend**: FastAPI, SQLAlchemy, Alembic migrations
- **Database**: Postgres in production, SQLite for local dev (picked
  automatically via `DATABASE_URL`)
- **AI**: Groq — a vision pass parses the screenshot, a text pass checks it
  rule-by-rule against your strategy
- **Auth**: Clerk (session-token verification against Clerk's JWKS)
- **Frontend**: plain HTML/CSS/JS — no framework, no build step
- **Deploy**: Railway

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # paste real GROQ_API_KEY / CLERK_SECRET_KEY / CLERK_PUBLISHABLE_KEY
alembic upgrade head        # creates the local schema (SQLite by default; set DATABASE_URL for Postgres)
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/ for the app, or http://127.0.0.1:8000/docs for
the API — both require signing in via Clerk now, so the keys above aren't
optional.

## Features
- AI-powered trade parsing from a screenshot (Groq vision)
- Rule-by-rule verdict against your own strategy, not a generic scorer
- User-defined strategies with stable rule IDs across edits
- Off-plan detection, plus AI-suggested strategy discovery from off-plan trades
- Discipline score & rule-adherence streak, windowed to your last 20 trades
- XP/progression tied to discipline, never to P&L
- Full auth via Clerk — every trade is private to its owner
- Light/dark theme
- Deployed to production on Railway, backed by Postgres with Alembic migrations
