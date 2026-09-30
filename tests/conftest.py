import pytest

from app import books


@pytest.fixture(autouse=True)
def _no_real_books_network(monkeypatch):
    """No test may reach the real Google Books API: lookups fail (the same path a
    quota error takes) unless a test installs its own fake. Also resets the
    module's cache and quota breaker so tests can't leak state into each other."""
    def _blocked(query):
        raise books.BooksLookupError("network disabled in tests")

    monkeypatch.setattr(books, "_fetch_volumes", _blocked)
    books._cache.clear()
    monkeypatch.setattr(books, "_breaker_until", 0.0)
    yield
    books._cache.clear()
