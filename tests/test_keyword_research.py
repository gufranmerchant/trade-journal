from unittest.mock import MagicMock, patch

import httpx
import pytest
from groq import APIConnectionError, RateLimitError

from app import keyword_research


def _mock_response(content):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


def _fake_groq_request():
    return httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def test_empty_topic_raises_value_error():
    with pytest.raises(ValueError):
        keyword_research.research_keywords("")


def test_whitespace_only_topic_raises_value_error():
    with pytest.raises(ValueError):
        keyword_research.research_keywords("   ")


def test_topic_too_long_raises_value_error():
    with pytest.raises(ValueError):
        keyword_research.research_keywords("x" * (keyword_research.MAX_TOPIC_LENGTH + 1))


def test_research_keywords_returns_normalized_shape():
    raw = """{
        "keywords": [{"keyword": "cozy bakery mystery", "reason": "genre + setting search"}],
        "competitors": ["Death by Chocolate Cake"],
        "categories": ["Kindle eBooks > Mystery"]
    }"""
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = keyword_research.research_keywords("a cozy mystery set in a bakery")

    assert result == {
        "keywords": [{"keyword": "cozy bakery mystery", "reason": "genre + setting search"}],
        "competitors": ["Death by Chocolate Cake"],
        "categories": ["Kindle eBooks > Mystery"],
    }


def test_research_keywords_drops_malformed_entries():
    raw = """{
        "keywords": [{"keyword": "", "reason": "no keyword text, dropped"}, {"keyword": "real one"}],
        "competitors": ["", "  ", "Real Competitor"],
        "categories": []
    }"""
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = keyword_research.research_keywords("topic")

    assert result["keywords"] == [{"keyword": "real one", "reason": ""}]
    assert result["competitors"] == ["Real Competitor"]
    assert result["categories"] == []


def test_research_keywords_strips_think_block_and_fences():
    raw = (
        "<think>reasoning the model shouldn't show</think>"
        '```json\n{"keywords": [], "competitors": [], "categories": []}\n```'
    )
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response(raw)):
        result = keyword_research.research_keywords("topic")

    assert result == {"keywords": [], "competitors": [], "categories": []}


def test_malformed_json_raises_keyword_research_error_with_friendly_message():
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response("not json")):
        with pytest.raises(keyword_research.KeywordResearchError) as exc_info:
            keyword_research.research_keywords("topic")
    assert str(exc_info.value) == keyword_research.FRIENDLY_ERROR_MESSAGE


def test_groq_rate_limit_error_is_wrapped_with_friendly_message():
    error = RateLimitError(
        "Request too large for model in organization on output tokens per minute (OTPM)",
        response=httpx.Response(429, request=_fake_groq_request()),
        body=None,
    )
    with patch.object(keyword_research.client.chat.completions, "create", side_effect=error):
        with pytest.raises(keyword_research.KeywordResearchError) as exc_info:
            keyword_research.research_keywords("topic")
    assert str(exc_info.value) == keyword_research.FRIENDLY_ERROR_MESSAGE


def test_groq_connection_error_is_wrapped_with_friendly_message():
    error = APIConnectionError(request=_fake_groq_request())
    with patch.object(keyword_research.client.chat.completions, "create", side_effect=error):
        with pytest.raises(keyword_research.KeywordResearchError) as exc_info:
            keyword_research.research_keywords("topic")
    assert str(exc_info.value) == keyword_research.FRIENDLY_ERROR_MESSAGE


def test_normalize_dedupes_categories_and_competitors_case_insensitively():
    from app.keyword_research import _normalize
    out = _normalize({
        "keywords": [],
        "categories": ["Kindle eBooks > Mystery > Cozy", "kindle ebooks >  mystery > cozy", "Books > Thrillers"],
        "competitors": ["Cozy village mystery series", "Cozy village mystery series", ""],
    })
    assert out["categories"] == ["Kindle eBooks > Mystery > Cozy", "Books > Thrillers"]
    assert out["competitors"] == ["Cozy village mystery series"]


def _research_with_raw(raw):
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response(raw)):
        return keyword_research.research_keywords("topic")


def test_trailing_comma_before_closing_brace_and_bracket_is_forgiven():
    raw = (
        '{"keywords": [{"keyword": "cozy mystery", "reason": "genre match",},],'
        ' "competitors": ["a", "b",], "categories": ["c",],}'
    )
    assert _research_with_raw(raw) == {
        "keywords": [{"keyword": "cozy mystery", "reason": "genre match"}],
        "competitors": ["a", "b"],
        "categories": ["c"],
    }


def test_trailing_comma_repair_leaves_commas_inside_strings_alone():
    raw = '{"keywords": [{"keyword": "x ,} y ,]", "reason": "r",}], "competitors": [], "categories": []}'
    assert _research_with_raw(raw)["keywords"] == [{"keyword": "x ,} y ,]", "reason": "r"}]


@pytest.mark.parametrize("raw", [
    '{"keywords": [{"keyword": "a", "reason": "b"}, {"keyword": "c"',            # truncated mid-object
    '{"keywords": [], "competitors": [], "categories": [}',                      # wrong closer
    '{"keywords": [] "competitors": []}',                                        # missing comma
    '{"keywords": [], competitors: [], "categories": [],}',                      # unquoted key + trailing comma
])
def test_genuinely_broken_json_still_fails_with_friendly_error(raw):
    with pytest.raises(keyword_research.KeywordResearchError) as exc_info:
        _research_with_raw(raw)
    assert str(exc_info.value) == keyword_research.FRIENDLY_ERROR_MESSAGE


def test_competitor_prompt_is_pattern_only_and_genre_locked():
    # Real-title naming proved unreliable (invented titles under real authors'
    # names), so competitors are pattern descriptions only.
    prompt = keyword_research.SYSTEM_PROMPT
    assert "never name" in prompt and "any specific book title, series name or author" in prompt
    assert "GENRE-FIT RULES" in prompt and "SPECIFICITY RULES" in prompt
