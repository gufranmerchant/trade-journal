import httpx
import pytest

from app import books


def _refuse(request):
    raise httpx.ConnectError("network disabled in tests")


# Block the HTTP client for the WHOLE session, not per test. A per-test block is
# undone at teardown, and a lookup thread started by a test whose model call failed
# fast can still be running then - it would reach the real Google API (this happened
# intermittently). With the client itself replaced, such a straggler just gets a
# ConnectError. Tests that need the HTTP layer install their own MockTransport client.
books._client = httpx.Client(transport=httpx.MockTransport(_refuse))


@pytest.fixture(autouse=True)
def _reset_books_state(monkeypatch):
    """Fresh cache and closed quota breaker for every test."""
    books._cache.clear()
    monkeypatch.setattr(books, "_breaker_until", 0.0)
    yield
    books._cache.clear()
