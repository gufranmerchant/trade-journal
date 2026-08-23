"""
Shared SQLAlchemy engine — split out of main.py so app/auth.py can look up
and provision users without importing main.py (which would import auth.py
right back, a circular import).
"""

from sqlalchemy import create_engine

from app.config import DATABASE_URL

# Railway (and most Postgres hosts) hand out "postgres://" URLs, but
# SQLAlchemy's psycopg2 dialect requires "postgresql://" — swap the scheme
# rather than erroring, so the URL Railway injects can be used as-is.
_url = DATABASE_URL
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)

engine = create_engine(_url or "sqlite:///journal.db")
# Schema is created/changed via Alembic migrations only (see alembic/), not
# here — a module-level create_all would race the Alembic migration on a
# fresh database: env.py imports this module to get the engine, so create_all
# would fire first and create every table, then the migration's own
# CREATE TABLE would immediately collide with them.
