"""
Shared settings — loaded once here so app/ai.py and app/auth.py (and anything
else that needs a key) read from the same place instead of each parsing .env
themselves.

Same hand-rolled .env parser ai.py used to carry inline (no python-dotenv
dependency) — just centralized now that a second module needs env vars.
"""

import os
from pathlib import Path

_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
