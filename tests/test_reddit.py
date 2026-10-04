"""Reddit integration. Fixtures follow Reddit's listing schema (data.children[].data); no test
touches the network (conftest.py). The structural tests at the bottom enforce the rules from
Reddit's Developer Terms: nothing from Reddit goes to a model, into logs, or into storage."""
import json
import logging
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import keyword_research, post_ideas, reddit

NOW = 1_800_000_000.0


def thread(tid, title, sub="CozyMystery", comments=10, score=50, age_hours=5.0, selftext="",
           over_18=False, stickied=False, removed=None, permalink=None, kind="t3"):
    return {"kind": kind, "data": {
        "id": tid, "title": title, "subreddit": sub, "num_comments": comments, "score": score,
        "created_utc": NOW - age_hours * 3600, "selftext": selftext, "over_18": over_18,
        "stickied": stickied, "removed_by_category": removed,
        "permalink": permalink if permalink is not None else f"/r/{sub}/comments/{tid}/slug/"}}


def listing(*threads):
    return {"data": {"children": list(threads)}}


class FakeReddit:
    """A MockTransport handler: token endpoint + per-community listings. Records every request."""

    def __init__(self, listings=None, token_status=200, listing_status=None):
        self.listings = listings or {}          # {(sub, kind): listing_json}
        self.token_status = token_status
        self.listing_status = listing_status or {}   # {sub: status}
        self.requests = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        if request.url.host == "www.reddit.com" and request.url.path == "/api/v1/access_token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "nope"})
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600, "token_type": "bearer"})
        m = re.match(r"^/r/([^/]+)/(hot|top|search)$", request.url.path)
        assert m, request.url
        sub, path = m.groups()
        if sub in self.listing_status:
            return httpx.Response(self.listing_status[sub], json={})
        kind = {"hot": "hot", "top": "top_month"}.get(path) or ("search_month" if request.url.params.get("t") == "month" else "search_year")
        return httpx.Response(200, json=self.listings.get((sub, kind), listing()))

    def listing_requests(self):
        return [r for r in self.requests if r.url.host == "oauth.reddit.com"]


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        f = FakeReddit(**kwargs)
        monkeypatch.setattr(reddit, "_client", httpx.Client(transport=httpx.MockTransport(f)))
        for name, value in (("REDDIT_CLIENT_ID", "idid1234"), ("REDDIT_CLIENT_SECRET", "secretsecret999"),
                            ("REDDIT_USERNAME", "marketer_dev")):
            monkeypatch.setattr(reddit.config, name, value)
        return f
    return install


# ------------------------------------------------------------------ switch + credentials

def test_without_credentials_nothing_is_fetched_and_no_request_is_made(monkeypatch):
    calls = []
    monkeypatch.setattr(reddit, "_client", httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(500))[1])))
    assert reddit.enabled() is False
    assert reddit.fetch_genre_corpus("cozy mystery") is None
    assert reddit.fetch_social_trending("a bakery", ["instagram"]) is None
    assert calls == []


def test_reddit_enabled_switch_turns_the_integration_off_even_with_credentials(fake, monkeypatch):
    f = fake()
    monkeypatch.setattr(reddit.config, "REDDIT_ENABLED", False)
    assert reddit.enabled() is False
    assert reddit.fetch_genre_corpus("cozy mystery") is None
    assert f.requests == []


def test_describe_config_never_reveals_the_secret(fake):
    fake()
    text = reddit.describe_config()
    assert "configured" in text and "secretsecret999" not in text and "idid1234" not in text


def test_user_agent_is_descriptive_includes_username_and_is_never_masked(fake):
    fake()
    assert reddit.user_agent() == "web:marketer-mirror:v1.0 (by /u/marketer_dev)"
    reddit.config.REDDIT_USERNAME = ""
    assert reddit.user_agent() == "web:marketer-mirror:v1.0"


# ------------------------------------------------------------------ OAuth + HTTP behaviour

def test_token_is_requested_with_basic_auth_and_client_credentials_and_reused(fake):
    f = fake(listings={("CozyMystery", "hot"): listing(thread("a1", "A thread"))})
    reddit.fetch_listing("CozyMystery", "hot")
    reddit.fetch_listing("CozyMystery", "top_month")
    token_requests = [r for r in f.requests if r.url.path == "/api/v1/access_token"]
    assert len(token_requests) == 1                        # one token for both listings
    req = token_requests[0]
    assert req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in req.content
    assert req.headers["user-agent"].startswith("web:marketer-mirror")
    listing_req = f.listing_requests()[0]
    assert listing_req.headers["authorization"] == "bearer tok-123"
    assert listing_req.headers["user-agent"] == "web:marketer-mirror:v1.0 (by /u/marketer_dev)"


def test_an_expired_token_is_refreshed(fake, monkeypatch):
    f = fake()
    reddit.fetch_listing("CozyMystery", "hot")
    monkeypatch.setattr(reddit, "_token_expires", time.time() - 1)
    reddit._cache.clear()
    reddit.fetch_listing("CozyMystery", "hot")
    assert len([r for r in f.requests if r.url.path == "/api/v1/access_token"]) == 2


def test_rejected_credentials_pause_all_lookups_without_further_requests(fake):
    f = fake(token_status=401)
    assert reddit.fetch_genre_corpus("cozy mystery") is None
    assert reddit._breaker_until > time.time()
    before = len(f.requests)
    assert reddit.fetch_genre_corpus("fantasy dragons") is None
    assert reddit.fetch_social_trending("a bakery", ["instagram"]) is None
    assert len(f.requests) == before


def test_rate_limit_response_pauses_lookups(fake):
    f = fake(listing_status={"CozyMystery": 429})
    reddit.fetch_many([("CozyMystery", "hot", None)])
    assert reddit._breaker_until > time.time()


def test_private_or_missing_communities_are_skipped_and_not_retried(fake):
    f = fake(listing_status={"Mystery": 404})
    out = reddit.fetch_many([("Mystery", "hot", None)])
    assert out == {}
    n = len(f.listing_requests())
    reddit.fetch_many([("Mystery", "top_month", None)])      # a different listing of the same dead community
    assert len(f.listing_requests()) == n                    # not asked again


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(200, json={"access_token": "t", "expires_in": 3600}) if r.url.host == "www.reddit.com" else httpx.Response(500),
    lambda r: httpx.Response(200, json={"access_token": "t", "expires_in": 3600}) if r.url.host == "www.reddit.com" else httpx.Response(200, text="<html>"),
])
def test_server_errors_and_garbage_yield_nothing_not_an_exception(monkeypatch, handler):
    monkeypatch.setattr(reddit, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    for name, value in (("REDDIT_CLIENT_ID", "id"), ("REDDIT_CLIENT_SECRET", "secret")):
        monkeypatch.setattr(reddit.config, name, value)
    assert reddit.fetch_genre_corpus("cozy mystery") is None
    assert reddit.fetch_social_trending("a bakery", ["instagram"]) is None


def test_timeout_yields_nothing(monkeypatch):
    def handler(r):
        if r.url.host == "www.reddit.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(reddit, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    for name, value in (("REDDIT_CLIENT_ID", "id"), ("REDDIT_CLIENT_SECRET", "secret")):
        monkeypatch.setattr(reddit.config, name, value)
    assert reddit.fetch_social_trending("a bakery", ["instagram"]) is None


def test_listings_are_cached_briefly_then_refetched(fake, monkeypatch):
    f = fake(listings={("CozyMystery", "hot"): listing(thread("a1", "A thread"))})
    reddit.fetch_listing("CozyMystery", "hot")
    reddit.fetch_listing("cozymystery", "hot")
    assert len(f.listing_requests()) == 1
    real_time = time.time
    monkeypatch.setattr(reddit.time, "time", lambda: real_time() + reddit.CACHE_TTL_SECONDS + 5)
    reddit.fetch_listing("CozyMystery", "hot")
    assert len(f.listing_requests()) == 2


# ------------------------------------------------------------------ parsing + the public whitelist

def test_listing_parsing_drops_nsfw_stickied_removed_and_malformed_threads():
    payload = listing(
        thread("ok", "A normal thread", selftext="body text"),
        thread("nsfw", "Adult thread", over_18=True),
        thread("pin", "Pinned announcement", stickied=True),
        thread("gone", "Removed thread", removed="moderator"),
        thread("t1", "A comment, not a thread", kind="t1"),
        thread("bad", "No permalink", permalink=""),
        thread("empty", "   "),
    )
    assert [t["id"] for t in reddit._parse_listing(payload, "CozyMystery")] == ["ok"]
    assert reddit._parse_listing({"unexpected": 1}, "x") == []


def test_public_threads_expose_only_title_metadata_and_link_never_body_text():
    [t] = reddit._parse_listing(listing(thread("a1", "A thread title", selftext="SECRET BODY WORDS", comments=7, score=3)), "CozyMystery")
    out = reddit.public(t)
    assert set(out) == {"title", "subreddit", "score", "comments", "created_utc", "url"}
    assert out["url"] == "https://www.reddit.com/r/CozyMystery/comments/a1/slug/"
    assert "SECRET BODY WORDS" not in json.dumps(out)
    assert "SECRET BODY WORDS" in t["_match"]      # kept in memory only, for string matching


def test_removed_or_deleted_post_bodies_are_not_kept_for_matching():
    [t] = reddit._parse_listing(listing(thread("a1", "Title", selftext="[removed]")), "x")
    assert "[removed]" not in t["_match"]


# ------------------------------------------------------------------ choosing communities

@pytest.mark.parametrize("topic, expected_first", [
    ("cozy mystery novels", "CozyMystery"),
    ("epic fantasy dragon riders", "Fantasy"),
    ("hard-boiled noir", "Mystery"),
    ("regency romance", "RomanceBooks"),
    ("hard science fiction space opera", "printSF"),
    ("keto cookbook for beginners", "cookbooks"),
])
def test_genre_topics_map_to_genre_communities(topic, expected_first):
    picked = reddit.pick_subreddits(topic)
    assert picked["genre"][0] == expected_first
    assert picked["general"] == ["books"] and picked["writer"] == []


def test_unmatched_topics_fall_back_to_general_and_writer_communities():
    assert reddit.pick_subreddits("quilting patterns") == {"genre": [], "general": ["books"], "writer": ["selfpublish"]}


def test_genre_communities_are_capped():
    assert len(reddit.pick_subreddits("cozy mystery fantasy romance horror historical")["genre"]) == reddit.MAX_GENRE_SUBREDDITS


def test_social_communities_follow_platform_and_audience_not_the_posts_subject():
    picked = reddit.pick_social_subreddits("bookkeeping tips for freelance designers", ["linkedin", "instagram"])
    assert picked[:2] == ["linkedin", "Instagram"]                 # platforms first
    assert "personalfinance" in picked or "smallbusiness" in picked  # audience theme
    assert len(picked) <= reddit.MAX_SOCIAL_SUBREDDITS
    assert reddit.pick_social_subreddits("zzz", []) == ["socialmedia"]


# ------------------------------------------------------------------ ranking

def test_a_lively_recent_thread_outranks_a_larger_old_one_and_caps_per_community():
    mk = lambda tid, c, h, sub="A": reddit._parse_listing(listing(thread(tid, f"T {tid}", sub=sub, comments=c, age_hours=h)), sub)[0]
    old_big = mk("old", 400, 24 * 20)
    fresh = mk("fresh", 120, 3)
    flood = [mk(f"f{i}", 100 - i, 4) for i in range(5)]
    ranked = reddit.rank_threads([old_big, fresh, *flood], limit=10, per_subreddit=3, now=NOW)
    titles = [t["title"] for t in ranked]
    assert titles[0] == "T fresh" and titles.index("T fresh") < titles.index("T old") if "T old" in titles else True
    assert len(ranked) == 3                                    # per-community cap
    assert all(set(t) == {"title", "subreddit", "score", "comments", "created_utc", "url"} for t in ranked)


def test_duplicate_threads_are_listed_once():
    t = reddit._parse_listing(listing(thread("same", "Dup")), "A")[0]
    assert len(reddit.rank_threads([t, dict(t)], limit=5)) == 1


# ------------------------------------------------------------------ matching real book titles, no AI

def book(title, author="Some Author", url="https://books.google.com/x"):
    return {"title": title, "author": author, "url": url}


def match(books_, *threads, topic="cozy mystery"):
    parsed = [reddit._parse_listing(listing(t), t["data"]["subreddit"])[0] for t in threads]
    from app.books import _topic_tokens
    return reddit.match_titles(books_, parsed, set(_topic_tokens(topic)))


def test_a_title_mentioned_in_a_thread_becomes_an_entry_with_count_and_links():
    out = match([book("The Library Murders", "Merryn Allingham")],
                thread("a", "Anyone read The Library Murders yet?", comments=30),
                thread("b", "Cozy recs", selftext="I loved The Library Murders and its bookshop setting", comments=5))
    [e] = out
    assert e["title"] == "The Library Murders" and e["author"] == "Merryn Allingham"   # verbatim from the Google list
    assert e["mentions"] == 2 and e["subreddits"] == ["CozyMystery"]
    assert [t["comments"] for t in e["threads"]] == [30, 5]                            # busiest thread first
    assert all(t["url"].startswith("https://www.reddit.com/r/CozyMystery/") for t in e["threads"])


def test_no_match_means_no_entries_the_section_is_omitted():
    assert match([book("The Library Murders")], thread("a", "What cozy should I read next?")) == []


def test_matching_is_on_whole_words_and_case_sensitive_for_ordinary_titles():
    b = [book("Murder at the Bakery", "J Writer")]
    assert match(b, thread("a", "murder at the bakery is a lovely premise")) == []         # generic lowercase phrase
    assert match(b, thread("b", "Murder at the Bakeryish things")) == []                   # not a whole word
    assert len(match(b, thread("c", "Has anyone read Murder at the Bakery?"))) == 1


def test_ampersands_apostrophes_and_subtitles_are_normalised():
    b = [book("Cupcake & Murder: A Dana Sweet Cozy Mystery Books 1-10", "Ann S. Marie"),
         book("Don't Look Back", "Jane Doe")]
    assert len(match(b, thread("a", "Cupcake and Murder is my comfort read"))) == 1
    assert len(match(b, thread("b", "Dont Look Back was so tense"))) == 1
    assert len(match(b, thread("c", "Don’t Look Back, loved it"))) == 1


def test_one_word_and_genre_word_titles_need_the_authors_surname_in_the_same_thread():
    one_word = [book("Outrageous", "Minerva Spencer")]
    assert match(one_word, thread("a", "Outrageous behaviour from the hero")) == []
    assert len(match(one_word, thread("b", "Minerva Spencer's Outrageous is fun"))) == 1
    generic = [book("The New Space Opera", "Gardner Dozois, Jonathan Strahan")]
    assert match(generic, thread("c", "Best New Space Opera of the year?"), topic="hard science fiction space opera") == []
    assert len(match(generic, thread("d", "Dozois edited The New Space Opera"), topic="hard science fiction space opera")) == 1


def test_short_titles_are_never_matched():
    assert match([book("Dune", "Frank Herbert")], thread("a", "Dune is great, Frank Herbert wrote it")) == []


def test_entries_are_ranked_by_mentions_then_comments_and_capped():
    books_ = [book(f"Quiet Village Mystery {i}", f"Author{i}") for i in range(8)]
    threads = [thread(f"t{i}{j}", f"Loved Quiet Village Mystery {i}", comments=i + j) for i in range(8) for j in range(i % 3 + 1)]
    out = match(books_, *threads)
    assert len(out) == reddit.MAX_RECOMMENDED
    assert [e["mentions"] for e in out] == sorted([e["mentions"] for e in out], reverse=True)


def test_post_body_text_is_used_for_matching_but_never_returned():
    out = match([book("The Library Murders", "M A")],
                thread("a", "Cozy recs please", selftext="PRIVATE-ISH BODY: try The Library Murders, it's great"))
    assert len(out) == 1 and "PRIVATE-ISH BODY" not in json.dumps(out)


# ------------------------------------------------------------------ block assembly

def corpus_from(fake_f, topic):
    return reddit.fetch_genre_corpus(topic)


def test_genre_block_trends_from_genre_communities_and_recommends_from_reader_communities_only(fake):
    f = fake(listings={
        ("CozyMystery", "hot"): listing(thread("h1", "What are you reading this week?", comments=80, age_hours=2)),
        ("CozyMystery", "top_month"): listing(thread("m1", "Favourite series?", comments=40, age_hours=100, selftext="I adore The Library Murders")),
        ("books", "search_month"): listing(thread("b1", "Cozy mystery recommendations", sub="books", comments=60, age_hours=30)),
        ("selfpublish", "search_month"): listing(thread("w1", "Writing a cozy mystery: The Library Murders as a comp", sub="selfpublish", comments=500, age_hours=5)),
    })
    corpus = reddit.fetch_genre_corpus("cozy mystery")
    assert corpus and corpus["subs"]["genre"][0] == "CozyMystery"
    block = reddit.build_keyword_block("cozy mystery", corpus, [book("The Library Murders", "Merryn Allingham")])
    titles = [t["title"] for t in block["trending"]]
    assert "What are you reading this week?" in titles and "Cozy mystery recommendations" in titles
    assert "Favourite series?" not in titles                          # top_month is for matching, not "trending"
    [rec] = block["recommended"]
    assert rec["mentions"] == 1 and rec["subreddits"] == ["CozyMystery"]  # the r/selfpublish mention is authors, not readers
    assert set(block) == {"trending", "recommended", "subreddits"}


def test_unmatched_topic_uses_general_and_writer_communities_for_trending_only(fake):
    f = fake(listings={("books", "search_month"): listing(thread("b1", "Quilting books?", sub="books", comments=9)),
                       ("selfpublish", "search_month"): listing(thread("w1", "Quilting book sales", sub="selfpublish", comments=4))})
    block = reddit.build_keyword_block("quilting patterns", reddit.fetch_genre_corpus("quilting patterns"), [])
    assert {t["subreddit"] for t in block["trending"]} == {"books", "selfpublish"}
    assert block["recommended"] == []


def test_a_writer_community_mention_is_never_a_reader_recommendation(fake):
    # For a topic with no genre community the fallback includes r/selfpublish (authors, not readers):
    # it can show up as a trending thread, but a book mentioned only there must not become
    # "readers are discussing this".
    fake(listings={("selfpublish", "search_month"): listing(
        thread("w1", "Comp titles: Quilting Basics Handbook worked for me", sub="selfpublish", comments=40, age_hours=4))})
    block = reddit.build_keyword_block("quilting patterns", reddit.fetch_genre_corpus("quilting patterns"),
                                       [book("Quilting Basics Handbook", "Pat Ternmaker")])
    assert [t["subreddit"] for t in block["trending"]] == ["selfpublish"]
    assert block["recommended"] == []


def test_nothing_real_means_no_block_at_all(fake):
    fake()   # every listing empty
    assert reddit.build_keyword_block("cozy mystery", reddit.fetch_genre_corpus("cozy mystery"), []) is None


def test_social_trending_is_current_activity_in_audience_communities_not_topic_filtered(fake):
    f = fake(listings={("Instagram", "hot"): listing(thread("i1", "Reels reach dropped again?", sub="Instagram", comments=200, age_hours=3)),
                       ("selfpublish", "hot"): listing(thread("s1", "Is Kindle Unlimited worth it?", sub="selfpublish", comments=90, age_hours=6))})
    out = reddit.fetch_social_trending("indie book marketing on a budget", ["instagram"])
    assert {t["title"] for t in out["threads"]} == {"Reels reach dropped again?", "Is Kindle Unlimited worth it?"}
    assert out["subreddits"] == ["Instagram", "selfpublish"]
    assert all("q" not in r.url.params for r in f.listing_requests())          # never searched by the post's topic


# ------------------------------------------------------------------ the rules from Reddit's terms, enforced

BODY_MARKER = "UNIQUE-BODY-MARKER-QZX"
TITLE_MARKER = "UNIQUE-TITLE-MARKER-QZX"


def _volumes():
    from tests.test_books import vol, COZY_DESC  # the Google Books fixtures
    return [vol("The Library Murders", ["Merryn Allingham"], COZY_DESC, ["Fiction / Mystery & Detective / Cozy"])]


def _model_reply():
    return MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps({
        "keywords": [{"keyword": "cozy bookshop mystery", "reason": "r"}],
        "competitors": ["Village cozy with a retired teacher and a dog, light tone"],
        "categories": ["Kindle eBooks > Literature & Fiction > Mystery"]})))])


def _reddit_world(fake):
    return fake(listings={
        ("CozyMystery", "hot"): listing(thread("h1", f"{TITLE_MARKER} hot thread", comments=50, age_hours=2,
                                               selftext=f"{BODY_MARKER} The Library Murders is wonderful")),
        ("Instagram", "hot"): listing(thread("i1", f"{TITLE_MARKER} social thread", sub="Instagram", comments=20, selftext=BODY_MARKER)),
    })


def test_end_to_end_keyword_research_adds_the_reddit_block_and_keeps_everything_else(fake):
    _reddit_world(fake)
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_model_reply()), \
         patch.object(keyword_research.books, "lookup_volumes", return_value=_volumes()):
        result = keyword_research.research_keywords("cozy mystery")
    assert [t["title"] for t in result["reddit"]["trending"]] == [f"{TITLE_MARKER} hot thread"]
    assert result["reddit"]["recommended"][0]["title"] == "The Library Murders"
    assert result["keywords"] and result["categories"] and result["competitors"][0]["kind"] == "book"


def test_reddit_text_never_reaches_the_model_keyword_research(fake):
    _reddit_world(fake)
    create = MagicMock(return_value=_model_reply())
    with patch.object(keyword_research.client.chat.completions, "create", create), \
         patch.object(keyword_research.books, "lookup_volumes", return_value=_volumes()):
        result = keyword_research.research_keywords("cozy mystery")
    assert TITLE_MARKER in json.dumps(result)                       # the data WAS fetched and used...
    sent = json.dumps(create.call_args.kwargs["messages"])
    assert TITLE_MARKER not in sent and BODY_MARKER not in sent    # ...but not one byte of it went to Groq
    assert json.loads(sent)[1]["content"] == "Book topic/title: cozy mystery"


def test_reddit_text_never_reaches_the_model_post_ideas(fake):
    _reddit_world(fake)
    reply = MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(
        {"ideas": [{"idea": "i", "rationale": "r"}], "platform_tags": {"instagram": [{"tag": "#t", "reason": "r"}]}})))])
    create = MagicMock(return_value=reply)
    with patch.object(post_ideas.client.chat.completions, "create", create):
        result = post_ideas.generate_post_ideas("a small bakery", ["instagram"])
    assert result["trending"]["threads"][0]["title"] == f"{TITLE_MARKER} social thread"
    sent = json.dumps(create.call_args.kwargs["messages"])
    assert TITLE_MARKER not in sent and BODY_MARKER not in sent


def test_reddit_content_is_never_logged(fake, caplog):
    _reddit_world(fake)
    with caplog.at_level(logging.DEBUG), \
         patch.object(keyword_research.client.chat.completions, "create", return_value=_model_reply()), \
         patch.object(keyword_research.books, "lookup_volumes", return_value=_volumes()):
        keyword_research.research_keywords("cozy mystery")
        reddit.fetch_social_trending("a small bakery", ["instagram"])
    assert TITLE_MARKER not in caplog.text and BODY_MARKER not in caplog.text
    assert "reddit fetch:" in caplog.text            # counts are logged, content is not


def test_reddit_failure_leaves_both_tools_working_without_the_sections(fake):
    fake(listing_status={"CozyMystery": 500, "books": 500, "selfpublish": 500, "Instagram": 500})
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_model_reply()), \
         patch.object(keyword_research.books, "lookup_volumes", return_value=_volumes()):
        result = keyword_research.research_keywords("cozy mystery")
    assert "reddit" not in result and result["keywords"]


def test_with_reddit_off_the_tool_output_is_unchanged(monkeypatch):
    with patch.object(keyword_research.client.chat.completions, "create", return_value=_model_reply()), \
         patch.object(keyword_research.books, "lookup_volumes", return_value=_volumes()):
        result = keyword_research.research_keywords("cozy mystery")
    assert "reddit" not in result


def test_reddit_module_has_no_path_to_a_model_database_or_files():
    # A structural guard: the module that handles Reddit data must stay free of anything that
    # could send it to a model or persist it. If this fails, the change needs review against
    # Reddit Developer Terms 7.2 / 7.3 first.
    source = Path(reddit.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", source, flags=re.M)
    assert not [m for m in imports if m.split(".")[0] in {"groq", "openai", "anthropic", "sqlalchemy", "sqlite3", "pickle", "shelve"}]
    assert not [m for m in imports if m in {"app.db", "app.models", "app.ai", "app.keyword_research", "app.post_ideas"}]
    assert not re.search(r"\bopen\(|write_text|write_bytes|\.commit\(", source)


def test_startup_hook_logs_both_integrations_redacted(caplog):
    # Regression: these lines used to be logged at import time, before main.py configured
    # logging, so they never appeared in the deploy logs.
    from fastapi.testclient import TestClient
    from app import main
    reddit.config.REDDIT_CLIENT_ID, reddit.config.REDDIT_CLIENT_SECRET = "idid1234", "secretsecret999"
    with caplog.at_level(logging.INFO), TestClient(main.app):
        pass
    assert "GOOGLE_BOOKS_API_KEY:" in caplog.text and "Reddit integration: configured" in caplog.text
    assert "secretsecret999" not in caplog.text and "idid1234" not in caplog.text


# ------------------------------------------------------------------ the feature flag is opt-in

@pytest.mark.parametrize("raw, expected", [
    (None, False), ("", False), ("  ", False), ("0", False), ("false", False), ("off", False), ("banana", False),
    ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
])
def test_env_flag_parsing_is_off_unless_explicitly_on(monkeypatch, raw, expected):
    from app import config
    if raw is None:
        monkeypatch.delenv("X_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_TEST_FLAG", raw)
    assert config._env_flag("X_TEST_FLAG") is expected


def test_the_shipped_default_is_off_and_credentials_alone_do_not_enable_reddit(monkeypatch):
    # A fresh interpreter with NO Reddit variables set: the default must be off...
    import subprocess, sys
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("REDDIT_")}
    code = ("from app import config, reddit; "
            "print(config.REDDIT_ENABLED, reddit.enabled()); "
            "config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = 'id', 'secret'; "
            "print(reddit.enabled())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(Path(reddit.__file__).parent.parent)).stdout.split()
    assert out == ["False", "False", "False"]       # default off; still off after credentials are added
    # ...and the template tells people the same
    template = (Path(reddit.__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^REDDIT_ENABLED=0\s*$", template, re.M)


# ------------------------------------------------------------------ nothing Reddit-related lives in the page HTML

STATIC = Path(reddit.__file__).parent / "static"


@pytest.mark.parametrize("page", ["keyword-research.html", "post-ideas.html"])
def test_tool_page_files_contain_no_reddit_markup_or_code(page):
    # The Reddit sections are built at runtime, only when a response carries Reddit data
    # (nav.js lazy-loads reddit-sections.js). The page files must stay exactly as they were
    # before the integration: no hidden containers, no render code.
    assert "reddit" not in (STATIC / page).read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("path", ["/marketer/books/keyword-research", "/marketer/social/post-ideas"])
def test_served_pages_have_no_reddit_trace_whether_the_flag_is_off_or_on(fake, monkeypatch, path):
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    monkeypatch.setattr(reddit.config, "REDDIT_ENABLED", False)
    off = client.get(path).text
    fake()                                              # credentials set ...
    monkeypatch.setattr(reddit.config, "REDDIT_ENABLED", True)
    on = client.get(path).text                          # ... and the flag on
    assert "reddit" not in off.lower() and "reddit" not in on.lower()
    import re as _re
    norm = lambda h: _re.sub(r"\?v=\d+", "?v=X", h)
    assert norm(off) == norm(on)                        # the flag never changes the served HTML


def test_reddit_sections_script_builds_the_dom_without_innerhtml_and_only_links_reddit():
    js = (STATIC / "js" / "reddit-sections.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)           # ignore the explanatory comments
    assert not re.search(r"\.innerHTML|insertAdjacentHTML|document\.write|outerHTML", code)
    assert 'https://www.reddit.com/' in js                       # the URL allow-list for thread links
    assert "textContent" in js


def test_nav_hook_loads_the_reddit_script_only_on_demand_for_the_two_tool_endpoints():
    nav = (STATIC / "js" / "nav.js").read_text(encoding="utf-8")
    assert '"/tools/keyword-research"' in nav and '"/tools/post-ideas"' in nav
    assert "/static/js/reddit-sections.js" in nav
    # the script tag is created inside loadRedditSections, never at page load
    assert nav.count('script.src = "/static/js/reddit-sections.js"') == 1 and 'createElement("script")' in nav
