"""Display-name enrichment for the selected profile and recovery result."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from search import SearchResult, SearchService
from x_recovery import RecoveryReport, XCandidate


def result(**kwargs):
    return SearchResult(
        platform="X", account="@_ayameayame", url="https://x.com/_ayameayame", **kwargs
    )


def twitter_api():
    return SimpleNamespace(
        get_user_info=AsyncMock(
            return_value={
                "status": True,
                "user_name": "_AyameAyame",
                "screen_name": "Ayame",
            }
        ),
        close=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_profile_result_gets_name_from_borrowed_twitter_api():
    api = twitter_api()
    service = SearchService(
        {"engines": ["google_lens"]}, twitter_api_provider=lambda: api
    )
    service._search_engine = AsyncMock(
        return_value=[result(verified=True, engine="google_lens")]
    )
    report = await service.search(b"image")
    assert report.results[0].name == "Ayame"
    api.get_user_info.assert_awaited_once_with("_ayameayame")
    api.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_account_clue_also_gets_profile_name_without_claiming_match():
    api = twitter_api()
    service = SearchService(
        {"engines": ["google_lens"]}, twitter_api_provider=lambda: api
    )
    service._search_engine = AsyncMock(
        return_value=[
            SearchResult(
                url="https://example.org/photo",
                title="作者：@_ayameayame",
                verified=True,
            )
        ]
    )
    recovery = RecoveryReport(
        candidates=[
            XCandidate(
                "https://x.com/_ayameayame",
                clue_strength=3,
                source_hosts=("example.org",),
            )
        ]
    )
    with patch("search.recover_x", new=AsyncMock(return_value=recovery)):
        report = await service.search(b"image")
    assert report.results[0].name == "Ayame"
    assert not report.results[0].verified
    api.get_user_info.assert_awaited_once_with("_ayameayame")


@pytest.mark.asyncio
async def test_profile_lookup_runs_only_for_selected_unknown_name():
    api = twitter_api()
    service = SearchService(
        {"engines": ["google_lens"]}, twitter_api_provider=lambda: api
    )
    other = SearchResult(platform="X", url="https://x.com/other", verified=True)
    service._search_engine = AsyncMock(return_value=[result(verified=True), other])
    report = await service.search(b"image")
    assert len(report.results) == 1
    api.get_user_info.assert_awaited_once_with("_ayameayame")
    await service._profile_name(replace(report.results[0], name="Already Known"))
    assert api.get_user_info.await_count == 1


@pytest.mark.asyncio
async def test_explicit_known_handle_is_usable_with_authorless_status_url():
    api = twitter_api()
    service = SearchService({}, twitter_api_provider=lambda: api)
    original = replace(
        result(verified=True), url="https://x.com/i/status/123", name=" "
    )
    found = await service._profile_name(original)
    assert found.name == "Ayame" and found.url == original.url
    api.get_user_info.assert_awaited_once_with("_ayameayame")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [
        {"status": False, "user_name": "_AyameAyame", "screen_name": "Ayame"},
        {"status": True, "user_name": "other", "screen_name": "Unrelated"},
        {"status": True, "user_name": "_AyameAyame", "screen_name": ""},
        {"status": True, "user_name": "_AyameAyame", "screen_name": None},
        None,
    ],
)
async def test_failed_or_wrong_profile_preserves_existing_result(profile):
    api = twitter_api()
    api.get_user_info.return_value = profile
    service = SearchService({}, twitter_api_provider=lambda: api)
    original = result(verified=True)
    assert await service._profile_name(original) == original


@pytest.mark.asyncio
async def test_profile_request_failure_is_safe_and_cancellation_propagates():
    api = twitter_api()
    api.get_user_info.side_effect = TimeoutError("secret proxy credentials")
    service = SearchService({}, twitter_api_provider=lambda: api)
    original = result(verified=True)
    assert await service._profile_name(original) == original
    api.get_user_info.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await service._profile_name(original)
