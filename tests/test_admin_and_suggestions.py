import base64

import pytest
from fastapi.testclient import TestClient

from app import books, config, main, wikipedia
from app.rate_limit import RateLimiter

TOKEN = "correct-horse-battery-staple"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_admin_failures", RateLimiter(limit=10, window_seconds=900))
    monkeypatch.setattr(main, "_topic_suggestions_limiter", RateLimiter(limit=120, window_seconds=600))
    return TestClient(main.app)


def _basic(password, user="anyone"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


# ---------------------------------------------------------------- the admin page is protected

@pytest.mark.parametrize("path", ["/admin/api-usage", "/admin/api-usage.json"])
def test_admin_does_not_exist_when_no_token_is_configured(client, monkeypatch, path):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    assert client.get(path).status_code == 404
    assert client.get(path, headers=_basic("")).status_code == 404     # an empty password is not "no token"


@pytest.mark.parametrize("path", ["/admin/api-usage", "/admin/api-usage.json"])
def test_admin_requires_credentials(client, monkeypatch, path):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get(path, headers=_basic("wrong")).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer " + TOKEN}).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic !!!not-base64!!!"}).status_code == 401
    assert client.get(path + "?key=" + TOKEN).status_code == 401       # the secret is never accepted in a URL


def test_admin_html_page_with_the_right_password(client, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_BOOKS_DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    for _ in range(81):
        from app import api_usage
        api_usage.record("google_books")
    r = client.get("/admin/api-usage", headers=_basic(TOKEN))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]
    assert "Google Books API" in r.text and "81" in r.text and "100" in r.text and "Warning" in r.text
    assert TOKEN not in r.text


def test_admin_json_matches_the_counters(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    from app import api_usage
    api_usage.record("wikipedia")
    data = client.get("/admin/api-usage.json", headers=_basic(TOKEN)).json()
    wiki = next(a for a in data["apis"] if a["api"] == "wikipedia")
    assert wiki["used"] == 1 and wiki["limit"] is None
    assert data["warn_levels"] == [80, 95]


def test_admin_is_read_only(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    assert client.post("/admin/api-usage", headers=_basic(TOKEN)).status_code == 405


def test_repeated_wrong_passwords_lock_the_ip_out_even_for_the_right_one(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    for _ in range(10):
        assert client.get("/admin/api-usage", headers=_basic("guess")).status_code == 401
    assert client.get("/admin/api-usage", headers=_basic("guess")).status_code == 429
    assert client.get("/admin/api-usage", headers=_basic(TOKEN)).status_code == 429


def test_successful_logins_do_not_count_toward_the_lockout(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    for _ in range(30):
        assert client.get("/admin/api-usage", headers=_basic(TOKEN)).status_code == 200


# ---------------------------------------------------------------- topic suggestions endpoint

def test_blends_wikipedia_titles_with_google_books_categories_and_labels_the_source(client, monkeypatch):
    monkeypatch.setattr(wikipedia, "suggest", lambda q: ["Cozy mystery", "Cozy fantasy"])
    monkeypatch.setitem(books._category_counts, "Cozy", 5)
    body = client.get("/tools/topic-suggestions", params={"q": "coz"}).json()
    assert body["suggestions"] == [
        {"text": "Cozy mystery", "source": "wikipedia"}, {"text": "Cozy fantasy", "source": "wikipedia"}, {"text": "Cozy", "source": "books"}]


def test_books_terms_keep_their_reserved_slots_when_wikipedia_returns_a_full_list(client, monkeypatch):
    monkeypatch.setattr(wikipedia, "suggest", lambda q: [f"Genre {i}" for i in range(6)])
    for term in ("Genre A", "Genre B"):
        monkeypatch.setitem(books._category_counts, term, 3)
    texts = [s["text"] for s in client.get("/tools/topic-suggestions", params={"q": "gen"}).json()["suggestions"]]
    assert len(texts) == wikipedia.MAX_SUGGESTIONS and "Genre A" in texts and "Genre B" in texts


def test_a_books_term_that_duplicates_a_wikipedia_title_is_shown_once(client, monkeypatch):
    monkeypatch.setattr(wikipedia, "suggest", lambda q: ["Cozy mystery"])
    monkeypatch.setitem(books._category_counts, "cozy mystery", 9)
    suggestions = client.get("/tools/topic-suggestions", params={"q": "coz"}).json()["suggestions"]
    assert [s["text"].lower() for s in suggestions] == ["cozy mystery"]


def test_short_queries_return_nothing_without_calling_wikipedia(client, monkeypatch):
    def boom(q):
        raise AssertionError("must not be called")
    monkeypatch.setattr(wikipedia, "suggest", boom)
    for q in ("", "  ", "co"):
        assert client.get("/tools/topic-suggestions", params={"q": q}).json() == {"suggestions": []}


def test_over_the_rate_limit_it_quietly_returns_no_suggestions(client, monkeypatch):
    monkeypatch.setattr(main, "_topic_suggestions_limiter", RateLimiter(limit=2, window_seconds=600))
    monkeypatch.setattr(wikipedia, "suggest", lambda q: ["Cozy mystery"])
    codes = [client.get("/tools/topic-suggestions", params={"q": "cozy"}) for _ in range(4)]
    assert [r.status_code for r in codes] == [200] * 4
    assert [len(r.json()["suggestions"]) for r in codes] == [1, 1, 0, 0]


def test_wikipedia_being_down_gives_an_empty_list_not_an_error(client):
    r = client.get("/tools/topic-suggestions", params={"q": "cozy mystery"})     # the test session blocks the network
    assert r.status_code == 200 and r.json() == {"suggestions": []}


def test_oversized_queries_are_truncated_not_forwarded(client, monkeypatch):
    seen = []
    monkeypatch.setattr(wikipedia, "suggest", lambda q: seen.append(q) or [])
    client.get("/tools/topic-suggestions", params={"q": "x" * 5000})
    assert len(seen[0]) == main.MAX_SUGGESTION_QUERY_CHARS


def test_missing_q_is_a_validation_error_not_a_crash(client):
    assert client.get("/tools/topic-suggestions").status_code == 422
