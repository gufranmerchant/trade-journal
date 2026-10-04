"""Bluesky display-only integration. Fixtures follow the AppView's searchPosts schema; no test touches
the network (conftest.py). The structural tests enforce the rules the module exists for: nothing from
Bluesky goes to a model, into logs, or into storage, and authors' opt-out labels are honoured."""
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import api_usage, bluesky, keyword_research

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
TEXT_MARKER = "BSKYTEXTMARKER"
HANDLE_MARKER = "markerhandle"
ROOT = Path(bluesky.__file__).resolve().parent.parent


def post(n, text="a cozy mystery recommendation", handle=None, likes=5, replies=0, labels=None, author_labels=None,
         reply=False, langs=("en",), uri=None):
    handle = handle or f"author{n}.bsky.social"
    record = {"text": text, "createdAt": "2026-10-03T10:00:00.000Z", "langs": list(langs)}
    if reply:
        record["reply"] = {"root": {}, "parent": {}}
    return {"uri": uri or f"at://did:plc:abc{n}/app.bsky.feed.post/3mwabc{n}", "cid": "x",
            "author": {"did": f"did:plc:abc{n}", "handle": handle, "displayName": "Secret Name", "avatar": "https://cdn/x.jpg",
                       "labels": author_labels or []},
            "record": record, "embed": {"secret": "embed"}, "likeCount": likes, "replyCount": replies, "repostCount": 1,
            "labels": labels or [], "indexedAt": "2026-10-03T10:00:01Z"}


class FakeBluesky:
    def __init__(self, posts=None, status=200):
        self.posts = posts or []
        self.status = status
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        return httpx.Response(200, json={"posts": self.posts, "cursor": "c"})


@pytest.fixture
def fake(monkeypatch):
    def install(posts=None, status=200, enable=True):
        f = FakeBluesky(posts, status)
        monkeypatch.setattr(bluesky, "_client", bluesky.make_client(httpx.MockTransport(f)))
        monkeypatch.setattr(bluesky.config, "BLUESKY_ENABLED", enable)
        return f
    return install


# ------------------------------------------------------------------ switch

def test_off_by_default_makes_no_request(fake):
    f = fake([post(1)], enable=False)
    assert bluesky.fetch_genre_posts("cozy mystery", NOW) is None and f.requests == []


def test_the_shipped_default_is_off():
    code = "from app import config; print(config.BLUESKY_ENABLED)"
    env = {k: v for k, v in __import__("os").environ.items() if k != "BLUESKY_ENABLED"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True).stdout.strip()
    assert out == "False"
    assert "BLUESKY_ENABLED=0" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_startup_log_says_which_state(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="app.bluesky")
    monkeypatch.setattr(bluesky.config, "BLUESKY_ENABLED", False)
    bluesky.log_configuration()
    assert "disabled" in caplog.text


# ------------------------------------------------------------------ what is kept

def test_keeps_a_normal_post_exactly_as_posted(fake):
    text = "Line one\n\nLine two <b>not html</b> & more \U0001F600 #booksky"
    fake([post(1, text=text + " cozy mystery")])
    out = bluesky.fetch_genre_posts("cozy mystery", NOW)
    assert out["posts"][0]["text"] == text + " cozy mystery"                      # untouched: newlines, markup, emoji


def test_authors_who_opted_out_of_logged_out_viewers_are_never_shown(fake):
    fake([post(1, author_labels=[{"val": "!no-unauthenticated"}]), post(2)])
    handles = [p["handle"] for p in bluesky.fetch_genre_posts("cozy mystery", NOW)["posts"]]
    assert handles == ["author2.bsky.social"]


@pytest.mark.parametrize("bad", [
    post(1, labels=[{"val": "porn"}]), post(1, labels=[{"val": "spam"}]), post(1, author_labels=[{"val": "spam"}]),
    post(1, author_labels=[{"val": "!no-unauthenticated"}]),
    post(1, reply=True), post(1, langs=("fr",)), post(1, text="nothing relevant here"),
    post(1, text="cozy fantasy and a mystery"),                                 # words present but not the phrase
    post(1, text="   "), post(1, handle="handle.invalid!"), post(1, uri="at://did:plc:x/app.bsky.feed.like/abc"),
])
def test_unusable_posts_are_dropped(fake, bad):
    fake([bad])
    assert bluesky.fetch_genre_posts("cozy mystery", NOW) is None


def test_posts_with_no_language_tag_are_kept(fake):
    p = post(1)
    del p["record"]["langs"]
    fake([p])
    assert bluesky.fetch_genre_posts("cozy mystery", NOW)


def test_malformed_entries_are_skipped_not_fatal(fake):
    fake([{"nonsense": True}, None, post(2)])
    assert [p["handle"] for p in bluesky.fetch_genre_posts("cozy mystery", NOW)["posts"]] == ["author2.bsky.social"]


def test_phrase_may_be_separated_by_punctuation_and_case(fake):
    fake([post(1, text="Loving this Cozy-Mystery series"), post(2, text="cozy,  mystery fans unite")])
    assert len(bluesky.fetch_genre_posts("cozy mystery", NOW)["posts"]) == 2


def test_public_exposes_only_the_whitelisted_fields(fake):
    fake([post(1)])
    p = bluesky.fetch_genre_posts("cozy mystery", NOW)["posts"][0]
    assert set(p) == {"text", "handle", "likes", "replies", "created_at", "url"}
    blob = repr(p)
    assert "Secret Name" not in blob and "did:plc" not in blob and "embed" not in blob and "avatar" not in blob
    assert p["url"] == "https://bsky.app/profile/author1.bsky.social/post/3mwabc1"


# ------------------------------------------------------------------ ranking

def test_most_engaged_first_one_per_author_no_duplicate_text_capped(fake):
    posts = [post(1, likes=3, text="cozy mystery one"), post(2, likes=50, text="cozy mystery two"),
             post(3, likes=10, replies=20, handle="same.bsky.social", text="cozy mystery three"),
             post(4, likes=60, handle="same.bsky.social", text="cozy mystery four"), post(5, likes=40, text="Cozy mystery fans: same words"),
             post(6, likes=39, text="cozy mystery fans: same   WORDS")]
    posts += [post(10 + i, likes=i, text=f"cozy mystery filler number {i}") for i in range(10)]
    fake(posts)
    out = bluesky.fetch_genre_posts("cozy mystery", NOW)["posts"]
    assert len(out) == bluesky.MAX_POSTS
    handles = [p["handle"] for p in out]
    assert len(set(handles)) == len(handles)
    # same.bsky.social has two posts (60 likes; 10 likes + 20 replies = 50 points): only the stronger one is kept
    assert [p["likes"] for p in out[:3]] == [60, 50, 40] and out[0]["handle"] == "same.bsky.social"
    assert all(p["handle"] != "same.bsky.social" or p["likes"] == 60 for p in out)
    assert "same   WORDS" not in repr(out)                                          # the 39-like near-duplicate of the 40-like post is gone
    texts = [" ".join(p["text"].lower().split()) for p in out]
    assert len(set(texts)) == len(texts)


# ------------------------------------------------------------------ the request

def test_request_is_an_anonymous_recent_top_english_search(fake):
    f = fake([post(1)])
    bluesky.fetch_genre_posts("cozy mystery novels", NOW)
    req = f.requests[0]
    params = dict(req.url.params)
    assert req.url.host == "api.bsky.app" and req.url.path == "/xrpc/app.bsky.feed.searchPosts"
    assert params["q"] == "cozy mystery"                                               # "novels" is dropped
    assert params["sort"] == "top" and params["lang"] == "en" and params["since"] == "2026-09-27T12:00:00Z"
    assert "authorization" not in req.headers and "MirrorJournal" in req.headers["user-agent"]


@pytest.mark.parametrize("topic,words", [
    ("cozy mystery novels", ["cozy", "mystery"]), ("Dark-Academia", ["dark", "academia"]), ("  ", []),
    ("one two three four five six seven eight", ["one", "two", "three", "four", "five", "six"]),
])
def test_query_words(topic, words):
    assert bluesky.query_words(topic) == words


def test_empty_topic_makes_no_request(fake):
    f = fake([post(1)])
    assert bluesky.fetch_genre_posts("   ", NOW) is None and f.requests == []


# ------------------------------------------------------------------ failure behaviour, cache, counting

@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_refusals_pause_lookups_and_yield_nothing(fake, status):
    f = fake(status=status)
    assert bluesky.fetch_genre_posts("cozy mystery", NOW) is None
    n = len(f.requests)
    assert bluesky.fetch_genre_posts("fantasy", NOW) is None and len(f.requests) == n   # breaker open: nothing more sent


def test_garbage_and_timeouts_yield_nothing_not_an_exception(monkeypatch, fake):
    fake()
    monkeypatch.setattr(bluesky, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    assert bluesky.fetch_genre_posts("cozy mystery", NOW) is None
    def slow(request):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(bluesky, "_client", httpx.Client(transport=httpx.MockTransport(slow)))
    assert bluesky.fetch_genre_posts("fantasy", NOW) is None


def test_results_are_cached_briefly_and_only_real_requests_are_counted(fake):
    f = fake([post(1)])
    bluesky.fetch_genre_posts("cozy mystery", NOW)
    bluesky.fetch_genre_posts("Cozy Mystery novels", NOW)
    assert len(f.requests) == 1
    assert next(r for r in api_usage.status()["apis"] if r["api"] == "bluesky")["used"] == 1


def test_the_cache_expires(fake, monkeypatch):
    f = fake([post(1)])
    bluesky.fetch_genre_posts("cozy mystery", NOW)
    key = "cozy mystery"
    monkeypatch.setitem(bluesky._cache, key, (bluesky._cache[key][0] - bluesky.CACHE_TTL_SECONDS - 1, bluesky._cache[key][1]))
    bluesky.fetch_genre_posts("cozy mystery", NOW)
    assert len(f.requests) == 2


# ------------------------------------------------------------------ the keyword tool

_RAW = '{"keywords": [{"keyword": "k", "reason": "r"}], "competitors": ["pattern"], "categories": ["c"]}'


def _groq():
    return patch.object(keyword_research.client.chat.completions, "create",
                        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=_RAW))]))


def test_keyword_research_gains_a_bluesky_block_only_with_usable_posts(fake):
    fake([post(1, text=f"{TEXT_MARKER} cozy mystery")])
    with _groq():
        result = keyword_research.research_keywords("cozy mystery")
    assert result["bluesky"]["query"] == "cozy mystery" and result["bluesky"]["posts"][0]["text"].startswith(TEXT_MARKER)
    assert set(result) >= {"keywords", "competitors", "categories", "bluesky"}


def test_no_posts_means_no_key(fake):
    fake([post(1, text="unrelated")])
    with _groq():
        assert "bluesky" not in keyword_research.research_keywords("cozy mystery")


def test_with_bluesky_off_the_output_is_unchanged_and_no_request_is_made(fake):
    f = fake([post(1)], enable=False)
    with _groq():
        result = keyword_research.research_keywords("cozy mystery")
    assert sorted(result) == ["categories", "competitors", "keywords"] and f.requests == []


def test_a_bluesky_failure_never_breaks_the_tool(fake, monkeypatch):
    fake()
    def boom(topic):
        raise RuntimeError("bluesky exploded")
    monkeypatch.setattr(bluesky, "fetch_genre_posts", boom)
    with _groq():
        result = keyword_research.research_keywords("cozy mystery")
    assert "bluesky" not in result and result["keywords"]


def test_bluesky_text_never_reaches_the_model(fake):
    fake([post(1, text=f"{TEXT_MARKER} cozy mystery", handle=f"{HANDLE_MARKER}.bsky.social")])
    with _groq() as create:
        keyword_research.research_keywords("cozy mystery")
    sent = repr(create.call_args)
    assert TEXT_MARKER not in sent and HANDLE_MARKER not in sent


def test_bluesky_content_is_never_logged(fake, caplog):
    fake([post(1, text=f"{TEXT_MARKER} cozy mystery", handle=f"{HANDLE_MARKER}.bsky.social")])
    with caplog.at_level(logging.DEBUG), _groq():
        keyword_research.research_keywords("cozy mystery")
    assert TEXT_MARKER not in caplog.text and HANDLE_MARKER not in caplog.text
    assert "bluesky fetch: HTTP 200, 1 usable posts" in caplog.text              # counts are logged, content is not


# ------------------------------------------------------------------ structural guards

def test_module_has_no_path_to_a_model_database_or_files():
    source = Path(bluesky.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", source, flags=re.M)
    assert not [m for m in imports if m.split(".")[0] in {"groq", "openai", "anthropic", "google", "sqlalchemy", "sqlite3", "pickle", "shelve"}]
    assert not [m for m in imports if m in {"app.db", "app.models", "app.ai", "app.keyword_research", "app.post_ideas", "app.books"}]
    assert not re.search(r"\bopen\(|write_text|write_bytes|\.commit\(", source)


def test_no_natural_language_or_sentiment_code_anywhere():
    # Pairing Bluesky text with a third-party NLP service is parked pending a decision on Bluesky's silence.
    for path in (ROOT / "app").glob("*.py"):
        assert "language.googleapis" not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("path", ["/marketer/books/keyword-research", "/marketer/social/post-ideas"])
def test_served_pages_have_no_bluesky_trace(path):
    from fastapi.testclient import TestClient
    from app import main
    assert "bluesky" not in TestClient(main.app).get(path).text.lower()


def test_page_files_contain_no_bluesky_markup():
    for page in ("keyword-research.html", "post-ideas.html"):
        assert "bluesky" not in (ROOT / "app" / "static" / page).read_text(encoding="utf-8").lower()


def test_section_script_uses_textcontent_and_only_links_to_bsky_app():
    js = (ROOT / "app" / "static" / "js" / "bluesky-section.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    assert not re.search(r"\.innerHTML|insertAdjacentHTML|document\.write|outerHTML", code)
    assert "https://bsky.app/profile/" in js and "textContent" in js


def test_nav_loads_the_bluesky_script_only_when_a_response_carries_the_data():
    nav = (ROOT / "app" / "static" / "js" / "nav.js").read_text(encoding="utf-8")
    assert re.search(r"if \(data\.bluesky\) \{\s*loadOptionalScript\(\"bluesky-section\.js\"", nav)
    assert "[data-bluesky-section]" in nav


def test_section_script_pluralises_replies_correctly():
    # a regression: a naive "+s" rendered "13 replys"
    js = (ROOT / "app" / "static" / "js" / "bluesky-section.js").read_text(encoding="utf-8")
    assert '"reply", "replies"' in js and '"like", "likes"' in js and "plural(" not in js
