"""
Shared settings — loaded once here so app/ai.py and app/auth.py (and anything
else that needs a key) read from the same place instead of each parsing .env
themselves.

Same hand-rolled .env parser ai.py used to carry inline (no python-dotenv
dependency) — just centralized now that a second module needs env vars.
"""

import base64
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _clerk_frontend_api(publishable_key: str) -> str:
    """The Clerk frontend-api host the hosted clerk-js script must load from.

    A publishable key is `pk_(test|live)_<base64 of "<frontend-api-host>$">` —
    the host isn't a separate env var, it's encoded inside the key itself, so
    switching CLERK_PUBLISHABLE_KEY (dev -> prod instance) must always change
    this too. Decoding it here means index.html never hardcodes a host that
    can silently go stale relative to the key.
    """
    if not publishable_key or "_" not in publishable_key:
        return ""
    b64_part = publishable_key.rsplit("_", 1)[-1]
    b64_part += "=" * (-len(b64_part) % 4)
    try:
        return base64.b64decode(b64_part).decode("utf-8").rstrip("$")
    except (ValueError, UnicodeDecodeError):
        return ""


CLERK_FRONTEND_API = _clerk_frontend_api(CLERK_PUBLISHABLE_KEY)
