import logging
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select

from app import api_usage, books, config
from app.models import ApiUsage

LOGGER = "app.api_usage"


@pytest.fixture
def limit100(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_BOOKS_DAILY_LIMIT", 100)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING and "API USAGE WARNING" in r.getMessage()]


def _books_used():
    return next(r for r in api_usage.status()["apis"] if r["api"] == "google_books")["used"]


# ---------------------------------------------------------------- warnings at 80% and 95%

def test_warns_once_at_80_and_once_at_95_and_not_before_or_between(limit100, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for _ in range(79):
        api_usage.record("google_books")
    assert _warnings(caplog) == []                              # 79%: nothing yet
    api_usage.record("google_books")                            # 80
    assert len(_warnings(caplog)) == 1 and "80%" in _warnings(caplog)[0] and "80 of 100" in _warnings(caplog)[0]
    for _ in range(14):
        api_usage.record("google_books")                        # 94
    assert len(_warnings(caplog)) == 1
    api_usage.record("google_books")                            # 95
    assert len(_warnings(caplog)) == 2 and "95%" in _warnings(caplog)[1]
    for _ in range(10):
        api_usage.record("google_books")                        # 96..105: no repeats, past the limit too
    assert len(_warnings(caplog)) == 2


def test_jumping_past_both_levels_warns_once_at_the_higher_one(limit100, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    period = api_usage.current_period(api_usage._specs()["google_books"])
    monkeypatch.setitem(api_usage._counts, ("google_books", period), 97)      # e.g. resumed from the database after a restart
    api_usage.record("google_books")
    assert len(_warnings(caplog)) == 1 and "98%" in _warnings(caplog)[0]
    api_usage.record("google_books")
    assert len(_warnings(caplog)) == 1


def test_a_new_period_warns_again(limit100, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    days = iter(["2026-10-04"] * 80 + ["2026-10-05"] * 80)
    monkeypatch.setattr(api_usage, "current_period", lambda spec, now=None: next(days))
    for _ in range(160):
        api_usage.record("google_books")
    assert len(_warnings(caplog)) == 2


def test_an_api_with_no_published_limit_is_counted_but_never_warned_about(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    for _ in range(5000):
        api_usage.record("wikipedia")
    assert _warnings(caplog) == []
    row = next(r for r in api_usage.status()["apis"] if r["api"] == "wikipedia")
    assert row["used"] == 5000 and row["limit"] is None and row["level"] is None and row["percent"] is None


def test_unknown_api_names_are_ignored():
    api_usage.record("not_a_registered_api")                     # must not raise


# ---------------------------------------------------------------- quota periods

def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_google_books_day_rolls_over_at_midnight_pacific():
    spec = api_usage._specs()["google_books"]
    assert api_usage.current_period(spec, _utc(2026, 10, 5, 6, 59)) == "2026-10-04"   # 23:59 PDT (UTC-7)
    assert api_usage.current_period(spec, _utc(2026, 10, 5, 7, 0)) == "2026-10-05"    # 00:00 PDT
    assert api_usage.current_period(spec, _utc(2026, 12, 5, 7, 59)) == "2026-12-04"   # 23:59 PST (UTC-8): the offset changes with DST
    assert api_usage.current_period(spec, _utc(2026, 12, 5, 8, 0)) == "2026-12-05"


def test_monthly_periods():
    spec = api_usage.ApiSpec("Some monthly API", "month", 1000)
    assert api_usage.current_period(spec, _utc(2026, 10, 31, 23, 59)) == "2026-10"
    assert api_usage.current_period(spec, _utc(2026, 11, 1, 0, 0)) == "2026-11"


# ---------------------------------------------------------------- status

def test_status_reports_percent_and_level(limit100):
    for _ in range(80):
        api_usage.record("google_books")
    row = next(r for r in api_usage.status()["apis"] if r["api"] == "google_books")
    assert (row["used"], row["limit"], row["percent"], row["level"]) == (80, 100, 80.0, "warning")
    for _ in range(15):
        api_usage.record("google_books")
    assert next(r for r in api_usage.status()["apis"] if r["api"] == "google_books")["level"] == "critical"


def test_status_is_ok_below_80(limit100):
    api_usage.record("google_books")
    assert next(r for r in api_usage.status()["apis"] if r["api"] == "google_books")["level"] == "ok"


def test_the_limit_is_configurable_and_defaults_to_1000():
    assert api_usage._specs()["google_books"].limit == config.GOOGLE_BOOKS_DAILY_LIMIT == 1000


# ---------------------------------------------------------------- persistence (survives a restart)

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    ApiUsage.__table__.create(engine)
    monkeypatch.setattr(api_usage, "engine", engine)
    monkeypatch.setattr(api_usage, "persistence_enabled", True)
    monkeypatch.setattr(api_usage, "_db_problem", None)
    return engine


def _flush():
    api_usage._writer.submit(lambda: None).result(timeout=10)   # one ordered writer: when this returns, earlier writes are done


def _stored(engine):
    with engine.connect() as conn:
        return {(r.api, r.period): r.count for r in conn.execute(select(ApiUsage))}


def test_counts_survive_a_restart(real_db, limit100, monkeypatch):
    for _ in range(3):
        api_usage.record("google_books")
    _flush()
    period = api_usage.current_period(api_usage._specs()["google_books"])
    assert _stored(real_db) == {("google_books", period): 3}

    monkeypatch.setattr(api_usage, "_counts", {})                # a new process: memory is empty
    assert _books_used() == 3                                    # resumed from the database
    api_usage.record("google_books")
    _flush()
    assert _stored(real_db) == {("google_books", period): 4}


def test_a_restart_near_the_limit_still_warns(real_db, limit100, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    period = api_usage.current_period(api_usage._specs()["google_books"])
    with real_db.begin() as conn:
        conn.execute(ApiUsage.__table__.insert().values(api="google_books", period=period, count=79))
    api_usage.record("google_books")                             # first request after the (simulated) restart: 80
    assert len(_warnings(caplog)) == 1


def test_a_broken_database_never_affects_counting_or_raises(tmp_path, monkeypatch, limit100):
    broken = create_engine(f"sqlite:///{tmp_path / 'no_table.db'}")     # the table was never created
    monkeypatch.setattr(api_usage, "engine", broken)
    monkeypatch.setattr(api_usage, "persistence_enabled", True)
    for _ in range(3):
        api_usage.record("google_books")                         # no exception
    _flush()
    assert _books_used() == 3                                    # still counted in memory
    assert api_usage.status()["persistence"].startswith("memory-only")


def test_record_does_not_wait_for_the_database(real_db, monkeypatch):
    """record() only queues the write: even a database call that blocks must not stall the caller."""
    import threading
    gate = threading.Event()
    monkeypatch.setattr(api_usage, "_persist", lambda *a: gate.wait(timeout=5))
    started = threading.Event()
    def call():
        api_usage.record("google_books")
        started.set()
    threading.Thread(target=call).start()
    assert started.wait(timeout=1)                               # returned while the writer is still blocked
    gate.set()


# ---------------------------------------------------------------- what actually counts as a Google Books request

def _books_client(monkeypatch, status=200, body=None):
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(status, json=body if body is not None else {"items": []})
    monkeypatch.setattr(books, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    return sent


def test_each_request_sent_to_google_is_counted(monkeypatch):
    sent = _books_client(monkeypatch)
    books.search_volumes("cozy mystery")
    books.search_volumes("fantasy")
    assert len(sent) == 2 and _books_used() == 2


def test_cache_hits_are_not_requests(monkeypatch):
    sent = _books_client(monkeypatch)
    books.search_volumes("cozy mystery")
    books.search_volumes("Cozy  Mystery")                        # same normalised query: served from the cache
    assert len(sent) == 1 and _books_used() == 1


def test_a_refused_request_still_counts_and_the_breaker_stops_further_ones(monkeypatch):
    sent = _books_client(monkeypatch, status=429, body={"error": {"message": "quota"}})
    with pytest.raises(books.BooksLookupError):
        books.search_volumes("cozy mystery")
    assert _books_used() == 1
    assert books.lookup_volumes("anything else") == []           # breaker is open: fallback behaviour is unchanged ...
    assert len(sent) == 1 and _books_used() == 1                 # ... and nothing more is sent or counted


def test_counting_never_changes_what_the_lookup_returns(monkeypatch):
    item = {"id": "1", "volumeInfo": {"title": "T", "authors": ["A"], "categories": ["Fiction / Mystery & Detective / Cozy / General"]}}
    _books_client(monkeypatch, body={"items": [item]})
    assert books.search_volumes("cozy") == [item]


def test_google_books_categories_are_collected_for_suggestions(monkeypatch):
    item = {"id": "1", "volumeInfo": {"title": "T", "authors": ["A"], "categories": ["Fiction / Mystery & Detective / Cozy / General"]}}
    _books_client(monkeypatch, body={"items": [item]})
    assert books.category_suggestions("coz") == []               # nothing seen yet
    books.search_volumes("cozy")
    assert books.category_suggestions("coz") == ["Cozy"]
    assert books.category_suggestions("mys") == ["Mystery & Detective"]
    assert books.category_suggestions("gener") == []             # "General" is dropped as meaningless


def test_one_log_line_per_real_request_shows_raw_and_kept_categories(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.books")
    items = [{"id": "1", "volumeInfo": {"title": "A", "categories": ["Fiction"]}},
             {"id": "2", "volumeInfo": {"title": "B", "categories": ["Fiction / Mystery & Detective / Cozy / General"]}},
             {"id": "3", "volumeInfo": {"title": "C"}}]
    _books_client(monkeypatch, body={"items": items})
    books.search_volumes("cozy mystery")
    books.search_volumes("cozy mystery")                                    # cache hit: no second line
    lines = [r.getMessage() for r in caplog.records if "google books categories" in r.getMessage()]
    assert len(lines) == 1
    assert "2/3 volumes had any" in lines[0]
    assert "'Fiction'" in lines[0] and "Fiction / Mystery & Detective / Cozy / General" in lines[0]   # raw, as Google sent it
    assert "kept=['Mystery & Detective', 'Cozy']" in lines[0]                                         # "Fiction"/"General" filtered


def test_generic_only_categories_log_an_empty_kept_list(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.books")
    _books_client(monkeypatch, body={"items": [{"id": "1", "volumeInfo": {"title": "A", "categories": ["Fiction"]}}]})
    books.search_volumes("anything")
    line = next(r.getMessage() for r in caplog.records if "google books categories" in r.getMessage())
    assert "raw=['Fiction'] kept=[]" in line
