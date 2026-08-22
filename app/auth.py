"""
Resolves every protected request to a row in OUR OWN users table — the
actual security fix this module exists for. Nothing in main.py trusts a
user_id the client sends; every endpoint depends on get_current_user_id,
which is the only thing allowed to say who the caller is.

Flow per request:
  1. Pull the Clerk session token out of `Authorization: Bearer <token>`
     (the frontend gets this from `clerk.session.getToken()` — see app.js).
  2. Verify it's a genuine, unexpired token Clerk issued, using Clerk's own
     public JWKS (fetched once via the Backend API with CLERK_SECRET_KEY,
     then cached in memory — session tokens rotate every ~60s, but the
     signing keys behind them don't, so there's no need to refetch per
     request).
  3. The verified token's `sub` claim is the Clerk user id. Look up a local
     User row with that clerk_user_id; if none exists yet, this is the
     user's first sign-in — fetch their profile from Clerk's Backend API
     (this is what CLERK_SECRET_KEY is for) and create the row now. Every
     other table (Strategy, Trade) hangs off this row's integer id exactly
     as it did before Clerk existed.

A failure at any step is a 401 — there's no fallback to a client-supplied
user_id, because that fallback is the vulnerability this replaces.
"""

import logging
import time

import httpx
import jwt

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import CLERK_SECRET_KEY
from app.db import engine
from app.models import User

logger = logging.getLogger(__name__)

CLERK_API_BASE = "https://api.clerk.com/v1"

# JWKS (the public keys used to verify session tokens) rarely rotates — cache
# it in memory instead of hitting Clerk's Backend API on every request. The
# TTL bounds how long a rotation could otherwise go unnoticed; a cache MISS
# (an unrecognized kid) always forces an immediate refresh regardless of TTL,
# so a freshly-rotated key is picked up on the very next request rather than
# waiting up to an hour — see _get_signing_key.
_JWKS_TTL_SECONDS = 600
_jwks_cache: dict[str, jwt.PyJWK] = {}
_jwks_fetched_at: float = 0.0


def _fetch_jwks() -> dict[str, jwt.PyJWK]:
    resp = httpx.get(
        f"{CLERK_API_BASE}/jwks",
        headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}", "Cache-Control": "no-cache"},
        timeout=10,
    )
    if resp.status_code != 200:
        # Every request's verification depends on this succeeding — a
        # non-200 here almost always means CLERK_SECRET_KEY itself is wrong,
        # revoked, or belongs to a different Clerk instance than the one
        # that issued CLERK_PUBLISHABLE_KEY (and thus signs session tokens),
        # which is worth surfacing distinctly from a single bad token.
        logger.warning(
            "Clerk JWKS fetch failed: status=%s body=%.500s — check CLERK_SECRET_KEY "
            "is set and belongs to the same Clerk instance as CLERK_PUBLISHABLE_KEY.",
            resp.status_code, resp.text,
        )
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    return {key["kid"]: jwt.PyJWK(key) for key in keys}


def _get_signing_key(kid: str) -> jwt.PyJWK:
    """Looks up the JWKS key for `kid`, forcing a fresh fetch from Clerk
    whenever the cache doesn't have it yet — either because it's past
    _JWKS_TTL_SECONDS, or because `kid` itself isn't in there (e.g. Clerk
    rotated in a new signing key since our last fetch). The second case is
    checked independently of the TTL so a just-rotated key doesn't have to
    wait out the TTL to be picked up.
    """
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    cache_stale = (now - _jwks_fetched_at) > _JWKS_TTL_SECONDS
    if kid not in _jwks_cache or cache_stale:
        _jwks_cache = _fetch_jwks()
        _jwks_fetched_at = now
    if kid not in _jwks_cache:
        # Still missing even in a JWKS we just fetched fresh — this isn't a
        # stale-cache problem, the key genuinely isn't there. The most likely
        # cause at that point is CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY
        # belonging to different Clerk instances (each instance has its own
        # signing keys), so log both sides to make that diagnosable without
        # needing to re-add debug logging.
        logger.warning(
            "JWT kid %r not found even after a fresh JWKS fetch — known kids: %s. "
            "If this persists, CLERK_SECRET_KEY and CLERK_PUBLISHABLE_KEY likely "
            "belong to different Clerk instances.",
            kid, list(_jwks_cache.keys()),
        )
        raise ValueError(f"Unknown signing key id: {kid}")
    return _jwks_cache[kid]


# Clerk session tokens are short-lived (~60s exp-iat), so ordinary clock
# drift between this machine and Clerk's servers is enough to make a
# genuinely-fresh token look "not yet valid" (iat/nbf in the future) or
# "expired" a few seconds early. A small leeway on both checks is standard
# practice for JWT verification across two different clocks — this doesn't
# meaningfully widen the token's real validity window, it just stops normal
# skew from being treated as a forged/expired token.
_CLOCK_LEEWAY_SECONDS = 30


def _verify_session_token(token: str) -> dict:
    """Returns the verified token claims, or raises if the token is missing,
    expired, or wasn't actually signed by this Clerk instance."""
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("Session token has no key id")
    signing_key = _get_signing_key(kid)
    return jwt.decode(
        token,
        key=signing_key.key,
        algorithms=["RS256"],
        options={"verify_aud": False},
        leeway=_CLOCK_LEEWAY_SECONDS,
    )


def _fetch_clerk_user(clerk_user_id: str) -> dict:
    resp = httpx.get(
        f"{CLERK_API_BASE}/users/{clerk_user_id}",
        headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _primary_email(clerk_user: dict) -> str | None:
    primary_id = clerk_user.get("primary_email_address_id")
    for addr in clerk_user.get("email_addresses") or []:
        if addr.get("id") == primary_id:
            return addr.get("email_address")
    # Fall back to the first known address rather than failing sign-in
    # outright if Clerk's primary-id bookkeeping is ever inconsistent.
    addresses = clerk_user.get("email_addresses") or []
    return addresses[0].get("email_address") if addresses else None


def _display_name(clerk_user: dict) -> str | None:
    first = (clerk_user.get("first_name") or "").strip()
    last = (clerk_user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or clerk_user.get("username") or None


def _provision_user(s: Session, clerk_user_id: str) -> User:
    """First sign-in for this Clerk user — create the local row everything
    else (Strategy, Trade) hangs off, mirroring what POST /users used to do
    manually before auth existed."""
    clerk_user = _fetch_clerk_user(clerk_user_id)
    email = _primary_email(clerk_user)
    if not email:
        raise HTTPException(502, "Clerk account has no email address on file")

    user = s.scalar(select(User).where(User.email == email))
    if user is not None:
        # An email-only row from before Clerk was wired up — link it rather
        # than colliding with the unique email constraint.
        user.clerk_user_id = clerk_user_id
    else:
        user = User(
            clerk_user_id=clerk_user_id,
            email=email,
            display_name=_display_name(clerk_user),
        )
        s.add(user)
    s.commit()
    s.refresh(user)
    return user


def get_current_user_id(authorization: str | None = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[len("Bearer "):]

    try:
        claims = _verify_session_token(token)
    except Exception as exc:
        # _get_signing_key already logs the specific kid-mismatch case; this
        # covers everything else (expired, bad signature, malformed token)
        # with just enough detail to diagnose from the server log without
        # ever logging the token itself.
        logger.warning("JWT verification failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(401, "Invalid or expired session")

    clerk_user_id = claims.get("sub")
    if not clerk_user_id:
        raise HTTPException(401, "Invalid session token")

    with Session(engine) as s:
        user = s.scalar(select(User).where(User.clerk_user_id == clerk_user_id))
        if user is None:
            user = _provision_user(s, clerk_user_id)
        return user.id
