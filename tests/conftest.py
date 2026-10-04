import httpx
import pytest

from app import books, reddit


def _refuse(request):
    raise httpx.ConnectError("network disabled in tests")


# Block the HTTP clients for the WHOLE session, not per test. A per-test block is
# undone at teardown, and a lookup thread started by a test whose model call failed
# fast can still be running then - it would reach the real Google / Reddit APIs (this
# happened intermittently with Google Books). With the client itself replaced, such a
# straggler just gets a ConnectError. Tests that need the HTTP layer install their own
# MockTransport client.
books._client = httpx.Client(transport=httpx.MockTransport(_refuse))
reddit._client = httpx.Client(transport=httpx.MockTransport(_refuse))


@pytest.fixture(autouse=True)
def _reset_external_state(monkeypatch):
    """Fresh caches, closed breakers, no cached tokens and no Reddit credentials for every
    test (so Reddit is off unless a test turns it on)."""
    books._cache.clear()
    reddit._cache.clear()
    reddit._dead_subs.clear()
    monkeypatch.setattr(books, "_breaker_until", 0.0)
    monkeypatch.setattr(reddit, "_breaker_until", 0.0)
    monkeypatch.setattr(reddit, "_token", None)
    monkeypatch.setattr(reddit, "_token_expires", 0.0)
    for name, value in (("REDDIT_CLIENT_ID", ""), ("REDDIT_CLIENT_SECRET", ""),
                        ("REDDIT_USERNAME", ""), ("REDDIT_ENABLED", True)):
        monkeypatch.setattr(reddit.config, name, value)
    yield
    books._cache.clear()
    reddit._cache.clear()
