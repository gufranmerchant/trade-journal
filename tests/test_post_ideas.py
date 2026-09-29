from unittest.mock import MagicMock, patch

import httpx
import pytest
from groq import APIConnectionError, RateLimitError

from app import post_ideas


def _mock_response(content):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


def _fake_groq_request():
    return httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def test_empty_topic_raises_value_error():
    with pytest.raises(ValueError):
        post_ideas.generate_post_ideas("", ["linkedin"])


def test_whitespace_only_topic_raises_value_error():
    with pytest.raises(ValueError):
        post_ideas.generate_post_ideas("   ", ["linkedin"])


def test_topic_too_long_raises_value_error():
    with pytest.raises(ValueError):
        post_ideas.generate_post_ideas("x" * (post_ideas.MAX_TOPIC_LENGTH + 1), ["linkedin"])


def test_no_platforms_raises_value_error():
    with pytest.raises(ValueError):
        post_ideas.generate_post_ideas("topic", [])


def test_unknown_platform_raises_value_error():
    with pytest.raises(ValueError):
        post_ideas.generate_post_ideas("topic", ["myspace"])


def test_platforms_are_normalized_case_insensitively_and_deduped():
    assert post_ideas._normalize_platforms(["LinkedIn", "linkedin", " Instagram "]) == ["linkedin", "instagram"]


def test_platforms_are_returned_in_canonical_order_regardless_of_input_order():
    assert post_ideas._normalize_platforms(["medium", "linkedin", "tiktok"]) == ["linkedin", "tiktok", "medium"]


def test_generate_post_ideas_returns_normalized_shape():
    raw = """{
        "ideas": [
            {"idea": "Behind-the-scenes bake video", "rationale": "process content performs well"}
        ],
        "platform_tags": {
            "linkedin": [{"tag": "smallbusinessgrowth", "reason": "SEO term for owners"}],
            "instagram": [{"tag": "#sourdough", "reason": "core niche hashtag"}]
        }
    }"""
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = post_ideas.generate_post_ideas("a bakery launching sourdough", ["linkedin", "instagram"])

    assert result == {
        "ideas": [{"idea": "Behind-the-scenes bake video", "rationale": "process content performs well"}],
        "platform_tags": {
            "linkedin": [{"tag": "smallbusinessgrowth", "reason": "SEO term for owners"}],
            "instagram": [{"tag": "#sourdough", "reason": "core niche hashtag"}],
        },
    }


def test_generate_post_ideas_only_includes_requested_platforms():
    # Even if the model hallucinates an extra platform key, the response
    # shape must be driven by what the user actually asked for.
    raw = """{
        "ideas": [],
        "platform_tags": {
            "linkedin": [{"tag": "term", "reason": "reason"}],
            "tiktok": [{"tag": "#extra", "reason": "not requested"}]
        }
    }"""
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = post_ideas.generate_post_ideas("topic", ["linkedin"])

    assert list(result["platform_tags"].keys()) == ["linkedin"]


def test_generate_post_ideas_drops_malformed_entries():
    raw = """{
        "ideas": [{"idea": "", "rationale": "dropped, no idea text"}, {"idea": "real idea"}],
        "platform_tags": {"linkedin": [{"tag": "", "reason": "dropped"}, {"tag": "real tag"}]}
    }"""
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = post_ideas.generate_post_ideas("topic", ["linkedin"])

    assert result["ideas"] == [{"idea": "real idea", "rationale": ""}]
    assert result["platform_tags"]["linkedin"] == [{"tag": "real tag", "reason": ""}]


def test_generate_post_ideas_caps_ideas_at_three():
    raw = """{
        "ideas": [
            {"idea": "one"}, {"idea": "two"}, {"idea": "three"}, {"idea": "four"}
        ],
        "platform_tags": {"linkedin": []}
    }"""
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = post_ideas.generate_post_ideas("topic", ["linkedin"])

    assert len(result["ideas"]) == 3


def test_generate_post_ideas_strips_think_block_and_fences():
    raw = (
        "<think>reasoning the model shouldn't show</think>"
        '```json\n{"ideas": [], "platform_tags": {"linkedin": []}}\n```'
    )
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = post_ideas.generate_post_ideas("topic", ["linkedin"])

    assert result == {"ideas": [], "platform_tags": {"linkedin": []}}


def test_malformed_json_raises_post_ideas_error_with_friendly_message():
    with patch.object(post_ideas.client.chat.completions, "create", return_value=_mock_response("not json")):
        with pytest.raises(post_ideas.PostIdeasError) as exc_info:
            post_ideas.generate_post_ideas("topic", ["linkedin"])
    assert str(exc_info.value) == post_ideas.FRIENDLY_ERROR_MESSAGE


def test_groq_rate_limit_error_is_wrapped_with_friendly_message():
    error = RateLimitError(
        "Request too large for model in organization on output tokens per minute (OTPM)",
        response=httpx.Response(429, request=_fake_groq_request()),
        body=None,
    )
    with patch.object(post_ideas.client.chat.completions, "create", side_effect=error):
        with pytest.raises(post_ideas.PostIdeasError) as exc_info:
            post_ideas.generate_post_ideas("topic", ["linkedin"])
    assert str(exc_info.value) == post_ideas.FRIENDLY_ERROR_MESSAGE


def test_groq_connection_error_is_wrapped_with_friendly_message():
    error = APIConnectionError(request=_fake_groq_request())
    with patch.object(post_ideas.client.chat.completions, "create", side_effect=error):
        with pytest.raises(post_ideas.PostIdeasError) as exc_info:
            post_ideas.generate_post_ideas("topic", ["linkedin"])
    assert str(exc_info.value) == post_ideas.FRIENDLY_ERROR_MESSAGE


def test_reuses_the_verified_keyword_research_model():
    from app import keyword_research
    assert post_ideas.MODEL is keyword_research.MODEL
