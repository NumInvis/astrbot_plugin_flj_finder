"""Single-source decisions, including ambiguity and stable evidence ties."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from search import SearchResult, SearchService, _merge_result, choose_best
from x_recovery import RecoveryReport, XCandidate


def post(account="artist", tweet_id="123", **kwargs):
    return SearchResult(
        platform="X",
        account=f"@{account}",
        url=f"https://x.com/{account}/status/{tweet_id}",
        **kwargs,
    )


def test_confirmed_post_beats_profile_and_rich_unverified_clue():
    profile = SearchResult(platform="X", url="https://x.com/profile", verified=True)
    exact = post(verified=True)
    clue = post("other", clue_strength=3, source_hosts=("a.example", "b.example"))
    best, ambiguous = choose_best([profile, clue, exact])
    assert best.url == exact.url
    assert not ambiguous


def test_verified_source_relation_beats_unattributed_same_image():
    original = post("original", "456", verified=True, relation_verified=True)
    best, _ = choose_best([post(verified=True, engine="google_lens"), original])
    assert best.url == original.url
    assert "引用关系" in best.selection_reason


def test_attribution_and_google_order_resolve_confirmed_ties():
    first = post("one", "111", verified=True, engine="google_lens")
    second = post("two", "222", verified=True, engine="google_lens")
    assert choose_best([first, second])[0].url == first.url
    assert choose_best([second, first])[0].url == second.url
    signed = replace(second, clue_strength=3, source_hosts=("album.example",))
    assert choose_best([first, signed])[0].url == second.url


def test_complete_author_metadata_does_not_replace_engine_relevance_order():
    first = post("one", "111", verified=True, engine="google_lens")
    second = post(
        "two", "222", verified=True, engine="google_lens", name="Complete Name"
    )
    assert choose_best([first, second])[0].url == first.url


def test_explicit_attribution_beats_many_weak_mentions():
    signed = post(clue_strength=3, source_hosts=("a.example",))
    mentioned = post(
        "other", clue_strength=1, source_hosts=("b.example", "c.example", "d.example")
    )
    best, ambiguous = choose_best([mentioned, signed])
    assert best.url == signed.url
    assert not best.verified and not ambiguous


def test_equal_account_clues_are_ambiguous_and_never_forced_by_search_order():
    first = post("one", clue_strength=3, source_hosts=("a.example",))
    second = post("two", clue_strength=3, source_hosts=("b.example",))
    for items in ([first, second], [second, first]):
        assert choose_best(items) == (None, True)


def test_confirmed_unknown_account_is_a_fallback_but_keyword_hits_are_not():
    unknown = SearchResult(
        platform="X",
        account="@alias",
        url="https://x.com/alias",
        account_confirmed=True,
    )
    keyword = post("random")
    assert choose_best([keyword]) == (None, False)
    best, ambiguous = choose_best([keyword, unknown])
    assert best.account == "@alias" and not best.verified and not ambiguous
    assert "同名账号" in best.selection_reason
    assert "尚未确认同图" in best.selection_reason
    explicit = post("explicit", clue_strength=1)
    assert choose_best([unknown, explicit])[0].account == "@explicit"
    another = replace(unknown, account="@another", url="https://x.com/another")
    assert choose_best([unknown, another]) == (None, True)


def test_more_distinct_sources_can_resolve_clue_tie_without_duplicate_votes():
    first = post("one", clue_strength=2, source_hosts=("a.example", "a.example"))
    second = post("two", clue_strength=2, source_hosts=("b.example", "c.example"))
    assert choose_best([first, second])[0].url == second.url


def test_same_account_candidates_are_not_an_ambiguity():
    profile = SearchResult(
        platform="X", account="@artist", url="https://x.com/artist", clue_strength=3
    )
    detail = post(clue_strength=1)
    best, ambiguous = choose_best([detail, profile])
    assert best.url == profile.url and not ambiguous


def test_same_post_combines_verified_relation_and_compatible_source_signature():
    quoted = post(verified=True, relation_verified=True)
    attributed = post(verified=True, clue_strength=3, source_hosts=("album.example",))
    combined = _merge_result(quoted, attributed)
    assert combined.verified and combined.relation_verified
    assert combined.clue_strength == 3 and combined.source_hosts == ("album.example",)


def test_keyword_only_accounts_and_unknown_nickname_search_are_not_reliable_clues():
    assert choose_best(
        [post(), SearchResult(platform="X", url="https://x.com/search?q=nickname")]
    ) == (None, False)


def test_result_count_setting_is_removed_from_schema():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert "max_results" not in schema


@pytest.mark.asyncio
async def test_old_result_count_is_ignored_and_candidates_compared_before_slicing():
    service = SearchService({"engines": ["google_lens"], "max_results": 10})
    first = post("one", "111", verified=True, engine="google_lens")
    second = post("two", "222", verified=True, engine="google_lens")
    service._search_engine = AsyncMock(return_value=[first, second])

    async def enriched(result, image):
        return replace(result, relation_verified=result.account == "@two")

    service._source_account = AsyncMock(side_effect=enriched)
    report = await service.search(b"image")
    assert len(report.results) == 1 and report.results[0].url == second.url
    assert service._source_account.await_count == 2


@pytest.mark.asyncio
async def test_source_signature_can_resolve_against_actual_author_after_enrichment():
    service = SearchService({"engines": ["google_lens"]})
    service._search_engine = AsyncMock(
        return_value=[
            post("one", "111", verified=True, engine="google_lens"),
            post("old_handle", "222", verified=True, engine="google_lens"),
            SearchResult(
                url="https://album.example/photo", title="作者：@actual", verified=True
            ),
        ]
    )

    async def enriched(result, image):
        return (
            replace(result, account="@actual", url="https://x.com/actual/status/222")
            if result.account == "@old_handle"
            else result
        )

    service._source_account = AsyncMock(side_effect=enriched)
    report = await service.search(b"image")
    assert report.results[0].account == "@actual"


@pytest.mark.asyncio
async def test_account_signature_does_not_promote_an_unmatched_recent_post():
    service = SearchService({"engines": ["google_lens"]})
    service._search_engine = AsyncMock(
        return_value=[
            SearchResult(
                url="https://album.example/photo", title="作者：@artist", verified=True
            )
        ]
    )
    candidates = [
        XCandidate(
            "https://x.com/artist", clue_strength=3, source_hosts=("album.example",)
        ),
        XCandidate("https://x.com/artist/status/999", clue_strength=0),
    ]
    with patch(
        "search.recover_x",
        new=AsyncMock(return_value=RecoveryReport(candidates=candidates)),
    ):
        report = await service.search(b"image")
    assert len(report.results) == 1
    assert report.results[0].url == "https://x.com/artist"
    assert not report.results[0].verified


@pytest.mark.asyncio
async def test_direct_exact_match_can_resolve_verified_quote_without_guessing_retweet():
    quote = {"username": "@original", "tweet_id": "456", "author": "Original"}
    api = SimpleNamespace(
        get_tweet=AsyncMock(
            return_value={
                "status": True,
                "username": "artist",
                "tweet_id": "123",
                "screen_name": "Artist",
                "quote": quote,
                "retweet": {"retweeter_username": "someone_else"},
            }
        ),
        close=AsyncMock(),
    )
    service = SearchService({}, twitter_api_provider=lambda: api)
    result = post(verified=True, engine="google_lens")
    with patch("search.matching_quote", new=AsyncMock(return_value=quote)):
        found = await service._source_account(result, b"image")
    assert found.url == "https://x.com/original/status/456"
    assert found.relation_verified and found.verified
    api.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmatched_quote_keeps_parent_exact_match():
    api = SimpleNamespace(
        get_tweet=AsyncMock(
            return_value={
                "status": True,
                "username": "artist",
                "tweet_id": "123",
                "quote": {"username": "other", "tweet_id": "456"},
            }
        )
    )
    service = SearchService({}, twitter_api_provider=lambda: api)
    result = post(verified=True)
    with patch("search.matching_quote", new=AsyncMock(return_value=None)):
        found = await service._source_account(result, b"image")
    assert found.url == result.url and found.verified and not found.relation_verified
