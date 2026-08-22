# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A trade-discipline journal for prop/funded-challenge traders. A user uploads a trade
screenshot + one line of context; the app parses it into a structured journal entry and
judges it against the **user's own** strategy rules (defined at onboarding — nothing is
preset). It is a discipline coach, not a trading-advice tool: a winning trade that broke
a rule still fails that rule.

Status: backend (data model, two-pass AI pipeline, trade/dashboard/strategy endpoints)
plus a static frontend (`app/static/`) covering the dashboard, screenshot-upload flow,
trade detail/edit screen, and strategy create/edit screen. Auth is Clerk: the frontend
gates on Clerk sign-in/sign-up and every protected endpoint resolves the caller from a
verified Clerk session token (see Auth section below) — there's no `?user_id=`/`POST
/users` flow anymore, and no endpoint accepts a client-supplied user id.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste real GROQ_API_KEY / CLERK_SECRET_KEY / CLERK_PUBLISHABLE_KEY into .env
uvicorn app.main:app --reload
```

The dashboard frontend is served at http://127.0.0.1:8000/ (`app/static/`, no build
step) and requires signing in via the Clerk widget it mounts. Interactive API docs at
http://127.0.0.1:8000/docs still exist but every route there needs a real
`Authorization: Bearer <clerk session token>` header now too — there's no more
`user_id` query/body param to fill in instead.

No test/lint/build tooling is configured yet — don't assume `pytest`, `ruff`, etc. exist.

## Architecture

- `app/models.py` — SQLAlchemy models: `User` (now carries `clerk_user_id`), `Strategy`, `Trade`.
- `app/ai.py` — the two-pass Groq pipeline that turns a screenshot into a judged trade.
- `app/config.py` — the one place `.env` is read (hand-rolled parser, no python-dotenv); exports `GROQ_API_KEY`, `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`.
- `app/db.py` — the shared SQLAlchemy `engine` (+ `Base.metadata.create_all`), split out so `app/auth.py` can use it without importing `app/main.py`.
- `app/auth.py` — `get_current_user_id`, the FastAPI dependency every protected route uses: verifies the caller's Clerk session token against Clerk's JWKS, and resolves/provisions the matching local `User` row. See Auth section below.
- `app/main.py` — the endpoints: `POST /trades` (wires models + ai.py and persists the result), `GET /trades` (filterable list), `GET /dashboard` (discipline score + streak), `POST`/`GET`/`PATCH /strategies` (rulebook management); also mounts `app/static/` at `/static` and serves `app/static/index.html` at `/`.
- `app/static/` — the dashboard frontend: `index.html` + `css/style.css` + `js/app.js`. No framework, no build step.

### Auth (`app/auth.py`, Clerk)

- The frontend loads Clerk's vanilla-JS SDK via the hosted `<script data-clerk-
  publishable-key>` tag in `index.html` (Clerk's own quickstart pattern — the
  publishable key is meant to be client-embedded, unlike the secret key) and mounts
  `clerk.mountSignIn(...)` into `#clerkAuthMount` when there's no signed-in user;
  `app.js`'s `boot()` gates the whole app behind `clerk.user` existing.
- Every API call from `app.js` goes through `authFetch`/`fetchJSON`, which attach
  `Authorization: Bearer <clerk.session.getToken()>` — the frontend never sends a
  user id anywhere.
- `app.auth.get_current_user_id` (a FastAPI dependency) verifies that bearer token's
  signature against Clerk's JWKS (fetched via the Backend API with `CLERK_SECRET_KEY`
  and cached in memory — see `_get_signing_key`), then looks up a local `User` by the
  token's `sub` claim (`clerk_user_id`). If none exists yet — first sign-in — it calls
  Clerk's Backend API for that user's profile (email, name) and creates the row right
  there (`_provision_user`), the same thing `POST /users` used to do manually. Every
  endpoint in `main.py` takes `user_id: int = Depends(get_current_user_id)` instead of
  a query/body param — there is no code path left that trusts a client-supplied user id.
- `GET /dashboard` replaced `GET /users/{user_id}/dashboard` — the user id is now
  implicit in the token, not a path param.

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

`.env` is loaded once in `app/config.py` via a small hand-rolled parser (no python-dotenv
dependency) — keep it that way unless there's a reason to add the dependency; `ai.py`
and `auth.py` both just import the constants they need from there.

### Request flow (`app/main.py`)

`POST /trades` (multipart: `context_note`, optional `strategy_id`, `screenshot`;
`user_id` comes from `Depends(get_current_user_id)`, not the request):

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

- `GET /trades?strategy_id=&direction=` — trade list for the authenticated user,
  optionally filtered, newest first.
- `GET /dashboard` — recomputes and returns `discipline_score` and
  `current_streak` from the user's trade history (also writes `discipline_score` back
  onto the `User` row, since the model documents it as a stored/recomputed field).
  Both metrics are windowed to the most recent `DISCIPLINE_WINDOW` (20) trades and treat
  an off-plan trade as 0% rule compliance — same "no rules followed" logic as the
  off-plan branch in `POST /trades`. A streak is unbroken only while every trade in it
  passed *all* of its own rules; one off-plan trade or one failed rule resets it to the
  point after that trade. The response echoes the window size as
  `discipline_window_trades` so the frontend can label the score correctly — it's a
  trade-count window, not a calendar window ("last 20 trades", not "30 days").

### Strategy management (`app/main.py`)

- `POST /strategies` — create, with an initial `rules` list (`{"text": str}`, no `id`
  needed). `GET /strategies?is_active=` — list for the authenticated user, optionally
  filtered to active/inactive. `PATCH /strategies/{id}` — partial update (name,
  description, direction_bias, is_active, rules); 404s if the strategy doesn't belong
  to the authenticated user, same ownership check `POST /trades` already does.
- Rule ids are assigned by the server and never renumbered (`_apply_rule_updates`): a
  `PATCH` rule entry with an `id` matching an existing rule updates that rule's text
  in place; an entry with no `id` (or an unrecognized one) is treated as new and gets
  the next unused id; omitting a rule entry drops it. This matters because
  `Trade.rule_results` and the rule-checker key off rule id — deactivating and
  recreating ids on every edit would silently break that link to trade history.
- Strategies have no hard delete — deactivate via `PATCH .../is_active=false` instead,
  matching the model's existing soft-delete field, so a trade's stored `rule_results`
  keeps referencing a real (if inactive) rulebook. Trades themselves *are* hard-deleted
  (`DELETE /trades/{id}`), since nothing else references a trade by id.
  `GET /strategies` with no `is_active` filter returns both active and inactive
  strategies — the only way to see (and reactivate) a deactivated one.

### Frontend (`app/static/`)

- Plain HTML/CSS/JS, no build step, no framework — `app/static/js/app.js` does direct
  DOM rendering. This is deliberate (see the comment at the top of `main.py`): it's
  mounted under `/static` rather than served as raw files off `/` so it can become a PWA
  later without changing how the API is hosted.
- `boot()` (bottom of `app.js`) is the auth gate: it waits for Clerk's hosted script to
  set `window.Clerk`, calls `clerk.load()`, mounts the sign-in/up widget once into
  `#clerkAuthMount`, then toggles between the `authView` and the rest of the app purely
  on `clerk.user` — `clerk.addListener` re-runs that check on every sign-in/sign-out so
  there's no manual reload. `wireAppOnce()` (all the `wire*` calls that used to run
  directly off `DOMContentLoaded`) only runs the first time a session appears.
- On load, `app.js` fetches `GET /dashboard`, `GET /trades`, and
  `GET /strategies?is_active=true` in parallel and renders: the discipline ring, XP
  bar/level, rule streak, "Rules followed" and "Net P&L · 30d" stat tiles, strategy/
  direction filter chips, and the trade list. If any of those calls fails (token
  expired mid-session, server hiccup) it shows a load-error state suggesting the user
  sign in again — there's no more "no user #N yet" case, since the account is always
  created for you on first sign-in.
- "Net P&L · 30d" prefers dollars (`Trade.pnl_usd`, summed) but only when *every* trade
  in the 30-day window has one — otherwise it falls back to summing `r_multiple`, same as
  before `pnl_usd` existed. `r_multiple` stays the actual discipline unit everywhere else
  (trade rows, filters); dollars are supplementary display only.
- The "Log trade from screenshot" CTA opens the upload screen (pick/drag a screenshot,
  optional context note, strategy picker), which POSTs multipart to `/trades` and renders
  the judged result; tapping a trade row (or "Review & edit full trade details") opens the
  detail/edit screen backed by `GET`/`PATCH /trades/{id}`.
- The "+" (new strategy) filter chip, and a "+ New strategy" tile plus a per-strategy edit
  (pencil) button inside the upload screen's strategy picker, all open the same
  create/edit screen: name, direction bias (long/short/both), and a rules list with
  add/remove rows and inline examples of *checkable* rule phrasing. It POSTs to
  `/strategies` (create) or PATCHes `/strategies/{id}` (edit), sending each rule's
  existing `id` (or none, for a new rule) so `_apply_rule_updates` keeps ids stable —
  rows are just `{id, text}` in the DOM, keyed by `data-rule-id`. Which screen "back"/save
  returns to (dashboard, upload screen, or Manage Strategies) is tracked in
  `strategyReturnView`, since the screen is reachable from all three places.
- A gear icon at the *start* of the dashboard filter-chip row (before "All"; the "+"
  new-strategy chip stays at the end) opens Manage Strategies — the only screen that
  lists inactive strategies alongside active ones (fetched via `GET /strategies?user_id=`
  with no `is_active` filter) and the only way to reactivate one, since deactivating
  drops a strategy out of every other picker/chip with no other way back. Each row has an
  edit (pencil) link to the same create/edit screen plus an active↔inactive toggle;
  toggling an active strategy off confirms first (shared confirm-modal component, same
  one the trade-delete flow uses), toggling an inactive one back on is immediate since
  it's non-destructive. The existing "Remove strategy" control on the edit screen still
  works the same way (deactivate, with confirmation) — Manage Strategies doesn't replace
  it, just adds the missing reactivate path and a more discoverable entry point.
- Trade delete: a trash icon on the trade-detail screen confirms, then hard-deletes via
  `DELETE /trades/{id}`, which also decrements the user's `xp` by whatever that trade had
  earned — `xp` is a running total stored on `User`, not derived on read like
  `discipline_score`/streak are, so a deleted trade's XP would otherwise linger forever.
- Sign-out is a plain icon button in the home topbar (`#signOutBtn`) that calls
  `clerk.signOut()` — Clerk's own state-change listener (see `boot()` above) is what
  actually swaps the view back to the sign-in screen, so this button doesn't do any
  view-switching itself.

## Conventions worth knowing

- SQLite file `journal.db` is created at the working directory root on app startup
  (`create_engine("sqlite:///journal.db")` in `app/db.py`) — not committed, not migrated;
  schema changes currently mean dropping and recreating the DB.
- Uploaded screenshots have a home (`uploads/`) but `main.py` does not yet write to it —
  `screenshot_path` on `Trade` is defined but unset by the current endpoint.
