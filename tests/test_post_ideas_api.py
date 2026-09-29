from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.rate_limit import RateLimiter

client = TestClient(app)

FAKE_RESULT = {
    "ideas": [{"idea": "Behind-the-scenes bake video", "rationale": "process content performs well"}],
    "platform_tags": {"linkedin": [{"tag": "smallbusinessgrowth", "reason": "SEO term for owners"}]},
}


def _reset_limiter(limit=main_module.POST_IDEAS_LIMIT_PER_HOUR):
    # Isolates each test from the shared module-level limiter, which would
    # otherwise carry hits over between tests (and across the whole file).
    main_module._post_ideas_limiter = RateLimiter(limit=limit, window_seconds=3600)


def test_missing_topic_is_422():
    _reset_limiter()
    res = client.post("/tools/post-ideas", json={"topic": "", "platforms": ["linkedin"]})
    assert res.status_code == 422


def test_missing_platforms_is_422():
    _reset_limiter()
    res = client.post("/tools/post-ideas", json={"topic": "a bakery", "platforms": []})
    assert res.status_code == 422


def test_unknown_platform_is_422():
    _reset_limiter()
    res = client.post("/tools/post-ideas", json={"topic": "a bakery", "platforms": ["myspace"]})
    assert res.status_code == 422


def test_successful_request_returns_ai_shape():
    _reset_limiter()
    with patch.object(main_module.post_ideas_module, "generate_post_ideas", return_value=FAKE_RESULT):
        res = client.post("/tools/post-ideas", json={"topic": "a bakery launching sourdough", "platforms": ["linkedin"]})
    assert res.status_code == 200
    assert res.json() == FAKE_RESULT


def test_model_parse_failure_is_502_with_friendly_message():
    _reset_limiter()
    pi_module = main_module.post_ideas_module
    with patch.object(
        pi_module,
        "generate_post_ideas",
        side_effect=pi_module.PostIdeasError(pi_module.FRIENDLY_ERROR_MESSAGE),
    ):
        res = client.post("/tools/post-ideas", json={"topic": "topic", "platforms": ["linkedin"]})
    assert res.status_code == 502
    assert res.json()["detail"] == pi_module.FRIENDLY_ERROR_MESSAGE


def test_rate_limit_blocks_after_the_per_ip_cap():
    _reset_limiter(limit=2)
    with patch.object(main_module.post_ideas_module, "generate_post_ideas", return_value=FAKE_RESULT):
        payload = {"topic": "t", "platforms": ["linkedin"]}
        assert client.post("/tools/post-ideas", json=payload).status_code == 200
        assert client.post("/tools/post-ideas", json=payload).status_code == 200
        blocked = client.post("/tools/post-ideas", json=payload)
    assert blocked.status_code == 429


def test_rate_limit_is_checked_before_the_llm_call():
    # The limiter must reject before generate_post_ideas is ever invoked -
    # that's the whole point of checking cost protection first.
    _reset_limiter(limit=0)
    with patch.object(main_module.post_ideas_module, "generate_post_ideas") as mocked:
        res = client.post("/tools/post-ideas", json={"topic": "t", "platforms": ["linkedin"]})
    assert res.status_code == 429
    mocked.assert_not_called()
