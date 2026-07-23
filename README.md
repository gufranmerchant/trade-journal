# Trade Discipline Journal

A trade-discipline journal for prop / funded-challenge traders. Upload a trade
screenshot + one line of context; the app parses it into a filled journal entry
and judges it against **your own** strategy rules. A discipline coach, not a
trading-advice tool — the win doesn't validate the entry.

## Core ideas
- You define your own setups (rules) at onboarding. Nothing is preset.
- Each trade is checked rule-by-rule against the setup you say you used.
- Trades matching no setup are tagged **Off-plan** (the biggest red flag).
- XP comes only from rule adherence & journaling — never from profit.

## Stack
FastAPI · SQLAlchemy · SQLite (v1) · Groq (vision parse + rule-check).

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your real GROQ_API_KEY into .env
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/docs to try the API.

## Status
Backend core built: data model, two-pass AI pipeline, `POST /trades`.
Next: dashboard GET endpoints, onboarding, frontend, auth, deploy.
