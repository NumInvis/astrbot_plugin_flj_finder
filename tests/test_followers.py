"""Follower-based selection only applies to unverified account candidates."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
import pytest

from search import SearchResult, SearchService, _merge_result, choose_best
from x_recovery import RecoveryReport, XCandidate


def account(handle, **kwargs):
    return SearchResult(
        platform="X", account=f"@{handle}", url=f"https://x.com/{handle}", **kwargs
    )


def test_most_followed_unverified_account_wins_even_over_a_stronger_clue():
    popular = account("popular", account_confirmed=True, followers=76824)
    signed = account("signed", clue_strength=3, followers=17)
    best, ambiguous = choose_best([signed, popular])
    assert best.account == "@popular" and not ambiguous and not best.verified
    assert "76,824" in best.selection_reason
    assert "未确认同图" in best.selection_reason
    assert (
        choose_best([popular, replace(signed, verified=True)])[0].account == "@signed"
    )


def test_follower_ties_return_one_and_missing_counts_are_not_zero():
    unknown = account("unknown", account_confirmed=True)
    zero = account("zero", account_confirmed=True, followers=0)
    assert choose_best([unknown, zero])[0].account == "@zero"
    tied = account("tied", account_confirmed=True, followers=0)
    best, ambiguous = choose_best([zero, tied])
    assert best.account == "@zero" and not ambiguous
    assert _merge_result(unknown, replace(unknown, followers=42)).followers == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, expected",
    [(76824, 76824), ("76,824", 76824), (0, 0), (True, None), (-1, None)],
)
async def test_followers_from_profile(value, expected):
    api = SimpleNamespace(
        get_user_info=AsyncMock(
            return_value={"status": True, "user_name": "artist", "followers": value}
        )
    )
    service = SearchService({}, twitter_api_provider=lambda: api)
    assert await service._account_followers("artist") == expected


@pytest.mark.asyncio
async def test_nitter_follower_counts_resolve_multiple_unknown_accounts():
    api = SimpleNamespace(
        nitter_url="https://nitter.example",
        get_user_info=AsyncMock(
            side_effect=lambda handle: {"status": True, "user_name": handle}
        ),
    )
    service = SearchService(
        {"engines": ["google_lens"]}, twitter_api_provider=lambda: api
    )
    service._search_engine = AsyncMock(
        return_value=[SearchResult(url="https://source.example/photo", verified=True)]
    )
    recovery = RecoveryReport(
        candidates=[
            XCandidate("https://x.com/riria", name="Small", account_confirmed=True),
            XCandidate("https://x.com/_x_riria", name="Large", account_confirmed=True),
        ]
    )
    requested = []

    def handle(request):
        requested.append(request.url.path)
        count = "76,824" if request.url.path == "/_x_riria" else "17"
        return httpx2.Response(
            200,
            text=f'<ul class="profile-statlist"><li class="following"><span class="profile-stat-num">999999</span></li><li class="followers"><span class="profile-stat-num">{count}</span></li></ul>',
        )

    with (
        patch("search.recover_x", AsyncMock(return_value=recovery)),
        patch(
            "search.AsyncClient",
            side_effect=lambda **kwargs: httpx2.AsyncClient(
                transport=httpx2.MockTransport(handle)
            ),
        ),
    ):
        report = await service.search(b"image")
    assert set(requested) == {"/riria", "/_x_riria"}
    assert len(report.results) == 1 and not report.ambiguous
    assert report.results[0].account == "@_x_riria"
    assert report.results[0].followers == 76824
    assert not report.results[0].verified


@pytest.mark.asyncio
async def test_follower_lookup_failure_keeps_count_unknown():
    api = SimpleNamespace(get_user_info=AsyncMock(side_effect=TimeoutError))
    service = SearchService({}, twitter_api_provider=lambda: api)
    assert await service._account_followers("artist") is None
