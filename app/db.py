"""
Shared SQLAlchemy engine — split out of main.py so app/auth.py can look up
and provision users without importing main.py (which would import auth.py
right back, a circular import).
"""

from sqlalchemy import create_engine

from app.models import Base

engine = create_engine("sqlite:///journal.db")
Base.metadata.create_all(engine)
