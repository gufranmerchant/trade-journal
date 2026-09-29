from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.rate_limit import RateLimiter

client = TestClient(app)

FAKE_RESULT = {
    "keywords": [{"keyword": "cozy bakery mystery", "reason": "genre + setting search"}],
    "competitors": ["Death by Chocolate Cake"],
    "categories": ["Kindle eBooks > Mystery"],
}


def _reset_limiter(limit=main_module.KEYWORD_RESEARCH_LIMIT_PER_HOUR):
    # Isolates each test from the shared module-level limiter, which would
    # otherwise carry hits over between tests (and across the whole file).
    main_module._keyword_research_limiter = RateLimiter(limit=limit, window_seconds=3600)


def test_missing_topic_is_422():
    _reset_limiter()
    res = client.post("/tools/keyword-research", json={"topic": ""})
    assert res.status_code == 422


def test_successful_request_returns_ai_shape():
    _reset_limiter()
    with patch.object(main_module.keyword_research_module, "research_keywords", return_value=FAKE_RESULT):
        res = client.post("/tools/keyword-research", json={"topic": "a cozy mystery set in a bakery"})
    assert res.status_code == 200
    assert res.json() == FAKE_RESULT


def test_model_parse_failure_is_502_with_friendly_message():
    _reset_limiter()
    kw_module = main_module.keyword_research_module
    with patch.object(
        kw_module,
        "research_keywords",
        side_effect=kw_module.KeywordResearchError(kw_module.FRIENDLY_ERROR_MESSAGE),
    ):
        res = client.post("/tools/keyword-research", json={"topic": "topic"})
    assert res.status_code == 502
    assert res.json()["detail"] == kw_module.FRIENDLY_ERROR_MESSAGE


def test_rate_limit_blocks_after_the_per_ip_cap():
    _reset_limiter(limit=2)
    with patch.object(main_module.keyword_research_module, "research_keywords", return_value=FAKE_RESULT):
        assert client.post("/tools/keyword-research", json={"topic": "t"}).status_code == 200
        assert client.post("/tools/keyword-research", json={"topic": "t"}).status_code == 200
        blocked = client.post("/tools/keyword-research", json={"topic": "t"})
    assert blocked.status_code == 429


def test_rate_limit_is_checked_before_the_llm_call():
    # The limiter must reject before research_keywords is ever invoked -
    # that's the whole point of checking cost protection first.
    _reset_limiter(limit=0)
    with patch.object(main_module.keyword_research_module, "research_keywords") as mocked:
        res = client.post("/tools/keyword-research", json={"topic": "t"})
    assert res.status_code == 429
    mocked.assert_not_called()
