"""Usage counters for the external APIs we call, with warnings as a free-tier limit nears.

Ops visibility only: nothing here is shown to end users, and nothing here can affect a request.
`record()` never raises and never blocks on the database - the count lives in memory (that is
the hot path) and is written behind it by a single background writer, so a slow or missing
table costs a request nothing. The count is persisted so that a redeploy (frequent here) does
not reset "how much of today's quota is used"; with the table unavailable it falls back to
memory-only and says so on the status page.

Counts are *requests we sent* (cache hits and circuit-breaker skips are not requests). That is
a slight over-estimate of what the provider bills when a request is refused, which is the safe
direction for a warning.

Limits come from config / the registry below. An API with `limit=None` is counted and shown
but never warned about (there is no published cap to compare against).
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, insert

from app import config
from app.db import engine
from app.models import ApiUsage

logger = logging.getLogger(__name__)

WARN_LEVELS = (80, 95)  # percent of the limit


@dataclass(frozen=True)
class ApiSpec:
    label: str
    period: str                 # "day" | "month"
    limit: int | None
    tz: str = "UTC"             # the timezone the provider's quota period rolls over in
    resets_note: str = ""


def _specs() -> dict[str, ApiSpec]:
    # Add an entry here when a new external API is wired in (e.g. Google Natural Language, a
    # *monthly* quota: ApiSpec("Google Natural Language", "month", <its free-tier units>)).
    return {
        "google_books": ApiSpec("Google Books API", "day", config.GOOGLE_BOOKS_DAILY_LIMIT,
                                tz="America/Los_Angeles", resets_note="Google quotas reset at midnight Pacific time"),
        "wikipedia": ApiSpec("Wikipedia / Wikimedia APIs", "day", None,
                             resets_note="no published cap - counted for visibility only"),
    }


persistence_enabled = True       # tests switch this off
_lock = threading.Lock()
_counts: dict[tuple[str, str], int] = {}
_warned: set[tuple[str, str, int]] = set()
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="api-usage")  # one worker = writes stay in order
_db_problem: str | None = None


@lru_cache(maxsize=None)
def _tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:  # no tz database installed (the `tzdata` package provides one): UTC is hours off, but only at the period boundary
        logger.warning("api usage: timezone %s unavailable, using UTC for the quota period (install `tzdata`)", name)
        return timezone.utc


def current_period(spec: ApiSpec, now: datetime | None = None) -> str:
    local = (now or datetime.now(timezone.utc)).astimezone(_tz(spec.tz))
    return local.strftime("%Y-%m-%d" if spec.period == "day" else "%Y-%m")


# ---------------------------------------------------------------- persistence (never raises)

def _load(api: str, period: str) -> int:
    global _db_problem
    if not persistence_enabled:
        return 0
    try:
        with engine.connect() as conn:
            value = conn.execute(select(ApiUsage.count).where(ApiUsage.api == api, ApiUsage.period == period)).scalar()
        return int(value or 0)
    except Exception as e:
        _db_problem = f"{e.__class__.__name__}: {str(e).splitlines()[0][:160]}"
        return 0


def _persist(api: str, period: str, count: int) -> None:
    global _db_problem
    try:
        with engine.begin() as conn:
            done = conn.execute(update(ApiUsage).where(ApiUsage.api == api, ApiUsage.period == period).values(count=count))
            if done.rowcount == 0:
                conn.execute(insert(ApiUsage).values(api=api, period=period, count=count))
        _db_problem = None
    except Exception as e:
        _db_problem = f"{e.__class__.__name__}: {str(e).splitlines()[0][:160]}"
        logger.debug("api usage: could not persist %s %s: %s", api, period, _db_problem)


# ---------------------------------------------------------------- counting

def _count_locked(api: str, period: str) -> int:
    key = (api, period)
    if key not in _counts:
        _counts[key] = _load(api, period)   # first sight of this period in this process: resume from the stored count
    return _counts[key]


def _warn_if_crossed(api: str, spec: ApiSpec, period: str, count: int) -> None:
    if not spec.limit:
        return
    pct = count * 100 / spec.limit
    crossed = [lvl for lvl in WARN_LEVELS if pct >= lvl and (api, period, lvl) not in _warned]
    if not crossed:
        return
    for lvl in WARN_LEVELS:                  # a jump past both levels warns once, at the higher one
        if pct >= lvl:
            _warned.add((api, period, lvl))
    logger.warning("API USAGE WARNING: %s passed %d%% of its %s limit: %d of %d used (%.0f%%), period %s. Fallback behaviour when exhausted is unchanged.",
                   spec.label, max(crossed), spec.period, count, spec.limit, pct, period)


def record(api: str) -> None:
    """Count one request to `api`. Safe to call from anywhere; never raises."""
    try:
        spec = _specs().get(api)
        if spec is None:
            return
        period = current_period(spec)
        with _lock:
            count = _count_locked(api, period) + 1
            _counts[(api, period)] = count
            _warn_if_crossed(api, spec, period, count)
        if persistence_enabled:
            _writer.submit(_persist, api, period, count)
    except Exception:  # pragma: no cover - bookkeeping must never break a request
        logger.exception("api usage: record(%s) failed", api)


def status() -> dict:
    """Everything the admin page shows. Read-only."""
    rows = []
    with _lock:
        for api, spec in _specs().items():
            period = current_period(spec)
            used = _count_locked(api, period)
            pct = (used * 100 / spec.limit) if spec.limit else None
            level = None
            if pct is not None:
                level = "critical" if pct >= WARN_LEVELS[1] else "warning" if pct >= WARN_LEVELS[0] else "ok"
            rows.append({
                "api": api, "label": spec.label, "period_kind": spec.period, "period": period,
                "used": used, "limit": spec.limit, "percent": None if pct is None else round(pct, 1),
                "level": level, "resets_note": spec.resets_note,
            })
    return {
        "apis": rows,
        "warn_levels": list(WARN_LEVELS),
        "persistence": ("disabled" if not persistence_enabled else "memory-only (database unavailable: %s)" % _db_problem
                        if _db_problem else "database"),
    }
