from unittest.mock import MagicMock, patch

import pytest

from app import keyword_research


def _mock_response(content):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])


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


def test_malformed_json_raises_keyword_research_error():
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_mock_response("not json")):
        with pytest.raises(keyword_research.KeywordResearchError):
            keyword_research.research_keywords("topic")
