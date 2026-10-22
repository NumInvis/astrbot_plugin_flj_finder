"""Offline recovery contracts: evidence, network boundaries, and borrowed clients."""

import asyncio
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from x_recovery import (
    _MAX_PAGE,
    _fetch_page,
    _public_url,
    _same_image,
    canonical_x_url,
    collect_account_clues,
    matching_quote,
    recover_x,
)


def picture(color="navy", reverse=False):
    image = Image.new("RGB", (96, 72), color)
    draw = ImageDraw.Draw(image)
    for index in range(8):
        x = index * 12
        draw.rectangle(
            (x, index * 3, x + 5, 60 - index * 2),
            fill="white" if not reverse else "black",
        )
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class UrlTests(unittest.TestCase):
    def test_cheap_title_clues_scan_every_supplied_seed(self):
        sources = [
            {"url": f"https://source{index}.example/p", "title": "Landscape"}
            for index in range(12)
        ]
        sources.append({"url": "https://last.example/p", "title": "作者：@lastartist"})
        self.assertEqual(
            collect_account_clues(sources)["lastartist"], (3, ("last.example",))
        )

    def test_account_clue_strength_distinguishes_attribution_mentions_and_links(self):
        clues = collect_account_clues(
            [
                {"url": "https://one.example/p", "title": "作者：@Artist"},
                {"url": "https://two.example/p", "title": "来源 @artist"},
                {"url": "https://one.example/again", "title": "Reposted by @Artist"},
                {
                    "url": "https://three.example/p",
                    "title": "悠宝三岁@tusiww - Twitter Profile - TwStalker",
                },
                {"url": "https://four.example/p", "title": "RT @aggregator"},
                {
                    "url": "https://five.example/p",
                    "title": "See https://x.com/post_author/status/123",
                },
                {
                    "url": "https://six.example/p",
                    "title": "原帖：https://x.com/original/status/456",
                },
            ]
        )
        self.assertEqual(clues["artist"], (3, ("one.example", "two.example")))
        self.assertEqual(clues["tusiww"], (3, ("three.example",)))
        self.assertEqual(clues["aggregator"][0], 1)
        self.assertEqual(clues["post_author"][0], 2)
        self.assertEqual(clues["original"][0], 3)

    def test_nickname_and_email_do_not_fabricate_account_clues(self):
        self.assertEqual(
            collect_account_clues(
                [
                    {"url": "https://one.example/p", "title": "Artist on X: drawing"},
                    {
                        "url": "https://two.example/p",
                        "title": "Contact alice@example.com",
                    },
                ]
            ),
            {},
        )

    def test_only_exact_hosts_accounts_and_posts_are_accepted(self):
        self.assertEqual(
            canonical_x_url("https://twitter.com/Alice/status/123/photo/1?s=20"),
            "https://x.com/Alice/status/123",
        )
        self.assertEqual(
            canonical_x_url("https://mobile.twitter.com/alice"), "https://x.com/alice"
        )
        for value in (
            "https://x.com.evil.example/alice",
            "https://evil.example/?x=https://x.com/alice",
            "https://x.com@evil.example/alice",
            "https://alice@x.com/alice",
            "https://x.com/search?q=alice",
            "https://x.com/intent/tweet",
            "https://x.com/i/status/123",
            "https://x.com/home",
            "https://x.com/accountnamewaytoolong",
            "file://x.com/alice",
            "https://x.com/alice/status/no",
            "https://x.com/alice/followers",
            "https://x.com:123/alice",
        ):
            with self.subTest(value=value):
                self.assertEqual(canonical_x_url(value), "")

    def test_image_evidence_is_conservative(self):
        source = picture()
        self.assertTrue(_same_image(source, source))
        self.assertFalse(_same_image(source, picture("red", reverse=True)))
        self.assertFalse(_same_image(source, b"not an image"))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Recovery tests never make live source-page requests. Boundary tests
        # exercise the actual fetcher separately with MockTransport.
        patcher = patch("x_recovery._fetch_page", AsyncMock(return_value=""))
        self.default_fetch = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_same_candidate_merges_strength_and_distinct_source_hosts(self):
        sources = [
            {"url": "https://one.example/p", "title": "RT @Artist"},
            {"url": "https://two.example/p", "title": "作者：@artist"},
            {
                "url": "https://one.example/again",
                "title": "Artist (@artist) - Twitter Profile",
            },
        ]
        report = await recover_x(picture(), sources)
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate.clue_strength, 3)
        self.assertEqual(set(candidate.source_hosts), {"one.example", "two.example"})
        self.assertFalse(candidate.relation_verified)
        self.assertEqual(
            collect_account_clues(sources)["artist"],
            (candidate.clue_strength, candidate.source_hosts),
        )

    async def test_page_links_receive_status_or_explicit_original_strength(self):
        self.default_fetch.return_value = '<a href="https://x.com/post/status/123">View</a><a href="https://x.com/author/status/456">查看原帖</a>'
        report = await recover_x(
            picture(), [{"url": "https://page.example/p", "title": "Image"}]
        )
        self.assertEqual(
            [candidate.clue_strength for candidate in report.candidates], [2, 3]
        )
        self.assertTrue(
            all(
                candidate.source_hosts == ("page.example",)
                for candidate in report.candidates
            )
        )

    async def test_keyword_results_keep_derived_strength_zero(self):
        search = AsyncMock(return_value=[{"link": "https://x.com/found/status/123"}])
        report = await recover_x(
            picture(),
            [{"url": "https://page.example/p", "title": "Artist on X: artwork"}],
            search_web=search,
        )
        self.assertEqual(report.candidates[0].clue_strength, 0)
        self.assertEqual(report.candidates[0].source_hosts, ("page.example",))

    async def test_matching_quoted_image_selects_quote_author_before_parent_media(self):
        source = picture()
        quoted = {
            "username": "original",
            "tweet_id": "456",
            "author": "Original Artist",
            "images": ["https://pbs.twimg.com/media/original.png"],
        }
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "username": "quoter",
                    "tweet_id": "123",
                    "screen_name": "Quoting User",
                    "quote": quoted,
                    "images": ["https://pbs.twimg.com/media/parent.png"],
                }
            ),
            download_media=AsyncMock(return_value=source),
            close=AsyncMock(),
        )
        report = await recover_x(
            source, [{"url": "https://x.com/quoter/status/123"}], twitter_api=api
        )
        candidate = report.candidates[0]
        self.assertEqual(candidate.url, "https://x.com/original/status/456")
        self.assertEqual(candidate.account, "@original")
        self.assertEqual(candidate.name, "Original Artist")
        self.assertTrue(candidate.verified)
        self.assertTrue(candidate.relation_verified)
        self.assertEqual(candidate.clue_strength, 0)
        self.assertEqual(candidate.source_hosts, ())
        self.assertIn("引用来源", candidate.evidence)
        api.get_tweet.assert_awaited_once_with("quoter", "123")
        self.assertEqual(api.download_media.await_count, 1)
        api.close.assert_not_awaited()

    async def test_unmatched_quote_cannot_inherit_matching_parent_image(self):
        source = picture()
        quoted = {
            "username": "unrelated",
            "tweet_id": "456",
            "author": "Other Artist",
            "images": ["https://pbs.twimg.com/media/quote.png"],
        }

        async def media(url, **kwargs):
            return picture("red", reverse=True) if "quote.png" in url else source

        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "username": "parent",
                    "tweet_id": "123",
                    "screen_name": "Parent Artist",
                    "quote": quoted,
                    "images": ["https://pbs.twimg.com/media/parent.png"],
                }
            ),
            download_media=AsyncMock(side_effect=media),
        )
        report = await recover_x(
            source, [{"url": "https://x.com/parent/status/123"}], twitter_api=api
        )
        candidate = report.candidates[0]
        self.assertTrue(candidate.verified)
        self.assertFalse(candidate.relation_verified)
        self.assertEqual(candidate.account, "@parent")
        self.assertEqual(candidate.name, "Parent Artist")

    async def test_retweeter_metadata_is_not_an_original_post_identity(self):
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "username": "parent",
                    "tweet_id": "123",
                    "retweet": {"retweeter_username": "other"},
                    "images": ["https://pbs.twimg.com/media/parent.png"],
                }
            ),
            download_media=AsyncMock(return_value=picture()),
        )
        report = await recover_x(
            picture(), [{"url": "https://x.com/parent/status/123"}], twitter_api=api
        )
        self.assertEqual(report.candidates[0].account, "@parent")
        self.assertTrue(report.candidates[0].verified)
        self.assertFalse(report.candidates[0].relation_verified)

    async def test_strong_old_signature_does_not_transfer_to_canonical_author(self):
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "username": "actual",
                    "tweet_id": "123",
                    "images": ["https://pbs.twimg.com/media/post.png"],
                }
            ),
            download_media=AsyncMock(return_value=picture()),
        )
        report = await recover_x(
            picture(),
            [
                {
                    "url": "https://source.example/p",
                    "title": "原帖：https://x.com/wrong/status/123",
                }
            ],
            twitter_api=api,
        )
        candidate = report.candidates[0]
        self.assertTrue(candidate.verified)
        self.assertEqual(candidate.account, "@actual")
        self.assertEqual(candidate.clue_strength, 0)
        self.assertEqual(candidate.source_hosts, ())

    async def test_weak_mentions_do_not_add_strong_attribution_support_hosts(self):
        sources = [{"url": "https://source.example/p", "title": "作者：@artist"}] + [
            {"url": f"https://weak{index}.example/p", "title": "RT @artist"}
            for index in range(5)
        ]
        report = await recover_x(picture(), sources)
        self.assertEqual(report.candidates[0].clue_strength, 3)
        self.assertEqual(report.candidates[0].source_hosts, ("source.example",))
        self.assertEqual(
            collect_account_clues(sources)["artist"], (3, ("source.example",))
        )

    async def test_no_x_or_clue_returns_empty_without_fabricating_account(self):
        with patch(
            "x_recovery._fetch_page", AsyncMock(return_value="<p>Landscape picture</p>")
        ):
            report = await recover_x(
                picture(), [{"url": "https://public.example/p", "title": "A landscape"}]
            )
        self.assertEqual(report.candidates, [])

    async def test_explicit_source_page_link_is_canonical_and_unverified(self):
        html = '<a href="https://twitter.com/artist/status/123?s=20">original</a>'
        with patch("x_recovery._fetch_page", AsyncMock(return_value=html)) as fetch:
            report = await recover_x(
                picture(), [{"url": "https://public.example/p", "title": "Image"}]
            )
        fetch.assert_awaited_once()
        self.assertEqual(report.candidates[0].url, "https://x.com/artist/status/123")
        self.assertEqual(report.candidates[0].account, "@artist")
        self.assertFalse(report.candidates[0].verified)
        self.assertIn("来源页面", report.candidates[0].evidence)

    async def test_title_handle_needs_no_credentials_and_still_checks_source_page(self):
        with patch("x_recovery._fetch_page", AsyncMock(return_value="")) as fetch:
            report = await recover_x(
                picture(),
                [
                    {
                        "url": "https://public.example/p",
                        "title": "Artist (@alice) illustration",
                    }
                ],
            )
        fetch.assert_awaited_once()
        self.assertEqual(report.candidates[0].account, "@alice")
        self.assertFalse(report.candidates[0].verified)

    async def test_chinese_adjacent_handle_supplies_account_and_search_clue(self):
        search = AsyncMock(return_value=[])
        report = await recover_x(
            picture(),
            [{"title": "悠宝三岁@tusiww - Twitter Profile - TwStalker"}],
            search_web=search,
        )
        self.assertEqual(report.candidates[0].url, "https://x.com/tusiww")
        self.assertEqual(report.candidates[0].account, "@tusiww")
        search.assert_awaited_once_with("tusiww")

    async def test_email_is_not_a_handle_clue(self):
        search = AsyncMock(return_value=[])
        report = await recover_x(
            picture(), [{"title": "Contact alice@example.com"}], search_web=search
        )
        self.assertEqual(report.candidates, [])
        search.assert_not_awaited()

    async def test_delimited_nickname_search_finds_post_without_inventing_handle(self):
        search = AsyncMock(
            return_value=[
                {
                    "link": "https://twitter.com/actual_handle/status/456",
                    "title": "Snow Artist on X",
                    "snippet": "picture",
                }
            ]
        )
        report = await recover_x(
            picture(),
            [
                {
                    "url": "https://public.example/p",
                    "title": "Snow Artist on X: lovely drawing",
                }
            ],
            search_web=search,
        )
        search.assert_awaited_once_with("Snow Artist")
        self.assertEqual(report.candidates[0].account, "@actual_handle")
        self.assertFalse(report.candidates[0].verified)

    async def test_nickname_from_source_title_prefix_searches_x(self):
        search = AsyncMock(
            return_value=[{"link": "https://x.com/artist", "title": "Artist"}]
        )
        report = await recover_x(
            picture(),
            [
                {
                    "url": "https://public.example/p",
                    "title": "小画师 - Illustration community",
                }
            ],
            search_web=search,
        )
        search.assert_awaited_once_with("小画师")
        self.assertEqual(report.candidates[0].url, "https://x.com/artist")

    async def test_search_failure_has_static_error_and_nickname_search_link(self):
        search = AsyncMock(
            side_effect=RuntimeError("secret credential and internal endpoint")
        )
        with patch("x_recovery._fetch_page", AsyncMock(return_value="")):
            report = await recover_x(
                picture(),
                [
                    {
                        "url": "https://public.example/p",
                        "title": "Snow Artist on X: drawing",
                    }
                ],
                search_web=search,
            )
        self.assertEqual(report.candidates[0].account, "")
        self.assertEqual(
            report.candidates[0].url, "https://x.com/search?q=Snow%20Artist"
        )
        self.assertNotIn("secret", str(report.errors))
        self.assertFalse(report.candidates[0].verified)

    async def test_source_page_handle_and_title_can_supply_search_clue(self):
        search = AsyncMock(return_value=[])
        with patch(
            "x_recovery._fetch_page",
            AsyncMock(
                return_value="<title>Artist on X: artwork</title><p>Twitter @realartist</p>"
            ),
        ):
            report = await recover_x(
                picture(),
                [{"url": "https://public.example/p", "title": "Drawing"}],
                search_web=search,
            )
        self.assertIn(
            "@realartist", [candidate.account for candidate in report.candidates]
        )
        self.assertLessEqual(search.await_count, 2)

    async def test_unrelated_post_image_is_not_verified(self):
        api = SimpleNamespace(
            is_ready=True,
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "123",
                    "username": "artist",
                    "screen_name": "Artist",
                    "images": ["https://pbs.twimg.com/media/art.jpg"],
                }
            ),
            download_media=AsyncMock(return_value=picture("red", reverse=True)),
            close=AsyncMock(),
        )
        report = await recover_x(
            picture(),
            [{"url": "https://x.com/artist/status/123", "title": "Artist"}],
            twitter_api=api,
        )
        self.assertFalse(report.candidates[0].verified)
        api.close.assert_not_awaited()
        self.assertEqual(
            api.download_media.await_args.kwargs["max_bytes"], 10 * 1024 * 1024
        )

    async def test_matching_post_media_is_verified_without_closing_borrowed_client(
        self,
    ):
        source = picture()
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "123",
                    "username": "artist",
                    "screen_name": "Artist",
                    "images": ["https://pbs.twimg.com/media/art.png"],
                }
            ),
            download_media=AsyncMock(return_value=source),
            close=AsyncMock(),
            aclose=AsyncMock(),
        )
        report = await recover_x(
            source,
            [{"url": "https://x.com/artist/status/123", "title": "Artist"}],
            twitter_api=api,
        )
        self.assertTrue(report.candidates[0].verified)
        self.assertEqual(report.candidates[0].evidence, "图像比对吻合")
        self.assertEqual(report.candidates[0].name, "Artist")
        api.close.assert_not_awaited()
        api.aclose.assert_not_awaited()

    async def test_wrong_status_prefix_is_replaced_with_returned_real_author(self):
        source = picture()
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "123",
                    "username": "real_author",
                    "screen_name": "Real Artist",
                    "images": ["https://pbs.twimg.com/media/art.png"],
                }
            ),
            download_media=AsyncMock(return_value=source),
        )
        report = await recover_x(
            source, [{"url": "https://x.com/wrong_author/status/123"}], twitter_api=api
        )
        candidate = report.candidates[0]
        self.assertTrue(candidate.verified)
        self.assertEqual(candidate.url, "https://x.com/real_author/status/123")
        self.assertEqual(candidate.account, "@real_author")
        self.assertEqual(candidate.name, "Real Artist")

    async def test_mismatched_returned_status_id_is_never_verified(self):
        source = picture()
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "456",
                    "username": "real_author",
                    "screen_name": "Real Artist",
                    "images": ["https://pbs.twimg.com/media/art.png"],
                }
            ),
            download_media=AsyncMock(return_value=source),
        )
        report = await recover_x(
            source, [{"url": "https://x.com/wrong_author/status/123"}], twitter_api=api
        )
        self.assertFalse(report.candidates[0].verified)
        self.assertEqual(
            report.candidates[0].url, "https://x.com/wrong_author/status/123"
        )
        self.assertEqual(report.candidates[0].name, "")
        api.download_media.assert_not_awaited()

    async def test_incomplete_or_invalid_returned_author_cannot_verify_image(self):
        for username in ("", "search", "not/an/account", "name_too_long_for_x"):
            api = SimpleNamespace(
                get_tweet=AsyncMock(
                    return_value={
                        "status": True,
                        "tweet_id": "123",
                        "username": username,
                        "images": ["https://pbs.twimg.com/media/art.png"],
                    }
                ),
                download_media=AsyncMock(return_value=picture()),
            )
            report = await recover_x(
                picture(), [{"url": "https://x.com/artist/status/123"}], twitter_api=api
            )
            self.assertFalse(report.candidates[0].verified)
            api.download_media.assert_not_awaited()

    async def test_full_profile_candidate_cap_still_admits_searched_post(self):
        sources = [
            {"title": "@one @two @three"},
            {"title": "@four @five @six"},
            {"title": "@seven @eight"},
        ]
        search = AsyncMock(return_value=[{"link": "https://x.com/actual/status/987"}])
        report = await recover_x(picture(), sources, search_web=search)
        self.assertEqual(len(report.candidates), 8)
        self.assertIn(
            "https://x.com/actual/status/987",
            [candidate.url for candidate in report.candidates],
        )

    async def test_full_profile_candidate_cap_still_admits_timeline_post(self):
        sources = [
            {"title": "@one @two @three"},
            {"title": "@four @five @six"},
            {"title": "@seven @eight"},
        ]
        api = SimpleNamespace(
            get_user_timeline_items=AsyncMock(
                return_value=[{"tweet_id": "987", "username": "actual"}]
            ),
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "987",
                    "username": "actual",
                    "images": [],
                }
            ),
        )
        report = await recover_x(picture(), sources, twitter_api=api)
        self.assertEqual(len(report.candidates), 8)
        self.assertIn(
            "https://x.com/actual/status/987",
            [candidate.url for candidate in report.candidates],
        )
        api.get_tweet.assert_awaited_once_with("actual", "987")

    async def test_full_profiles_reserve_three_posts_without_losing_strong_profile(
        self,
    ):
        sources = [
            {"title": "悠宝三岁@tusiww - Twitter Profile - TwStalker"},
            {"title": "@one @two @three"},
            {"title": "@four @five @six"},
            {"title": "@seven"},
        ]
        search = AsyncMock(
            return_value=[
                {"link": f"https://x.com/noise/status/{index}"} for index in range(12)
            ]
        )
        report = await recover_x(picture(), sources, search_web=search)
        self.assertEqual(len(report.candidates), 8)
        self.assertEqual(
            sum("/status/" in candidate.url for candidate in report.candidates), 3
        )
        profile = next(
            candidate
            for candidate in report.candidates
            if candidate.account == "@tusiww"
        )
        self.assertEqual(profile.clue_strength, 3)

    async def test_strong_profile_survives_more_than_eight_native_search_posts(self):
        body = "".join(
            f'<a class="tweet-link" href="/noise/status/{index}"></a>'
            for index in range(20)
        )

        def handler(request):
            return httpx2.Response(200, text=body)

        api = SimpleNamespace(nitter_url="http://nitter:8080", is_ready=False)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        with patch("x_recovery.AsyncClient", return_value=client):
            report = await recover_x(
                picture(),
                [
                    {
                        "url": "https://twstalker.example/tusiww",
                        "title": "悠宝三岁@tusiww - Twitter Profile - TwStalker",
                    }
                ],
                twitter_api=api,
            )
        self.assertEqual(len(report.candidates), 8)
        profile = next(
            candidate
            for candidate in report.candidates
            if candidate.account == "@tusiww"
        )
        self.assertEqual(profile.clue_strength, 3)
        self.assertEqual(profile.source_hosts, ("twstalker.example",))

    async def test_explicit_page_post_replaces_weaker_keyword_post_at_capacity(self):
        search = AsyncMock(
            return_value=[
                {"link": f"https://x.com/noise/status/{index}"} for index in range(12)
            ]
        )
        self.default_fetch.return_value = (
            '<a href="https://x.com/original/status/987">View</a>'
        )
        report = await recover_x(
            picture(),
            [{"url": "https://source.example/p", "title": "Artist on X: image"}],
            search_web=search,
        )
        self.assertEqual(len(report.candidates), 8)
        original = next(
            candidate
            for candidate in report.candidates
            if candidate.account == "@original"
        )
        self.assertEqual(original.clue_strength, 2)
        self.assertEqual(original.source_hosts, ("source.example",))

    async def test_later_strong_profile_replaces_weak_profile_at_capacity(self):
        sources = [
            {"title": "@one @two @three"},
            {"title": "@four @five @six"},
            {"title": "@seven @eight"},
            {"title": "悠宝三岁@tusiww - Twitter Profile - TwStalker"},
        ]
        report = await recover_x(picture(), sources)
        self.assertEqual(len(report.candidates), 8)
        self.assertEqual(
            next(
                candidate
                for candidate in report.candidates
                if candidate.account == "@tusiww"
            ).clue_strength,
            3,
        )

    async def test_aggregator_title_does_not_hide_original_source_page_post(self):
        source = picture()
        api = SimpleNamespace(
            get_user_timeline_items=AsyncMock(return_value=[]),
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "987",
                    "username": "original",
                    "images": ["https://pbs.twimg.com/media/art.png"],
                }
            ),
            download_media=AsyncMock(return_value=source),
        )
        self.default_fetch.return_value = (
            '<a href="https://twitter.com/original/status/987">Original</a>'
        )
        report = await recover_x(
            source,
            [{"title": "Reposted by @aggregator", "url": "https://public.example/p"}],
            twitter_api=api,
        )
        original = next(
            candidate
            for candidate in report.candidates
            if candidate.account == "@original"
        )
        self.assertTrue(original.verified)
        api.get_user_timeline_items.assert_awaited_once()
        api.get_tweet.assert_awaited_once_with("original", "987")

    async def test_unmatched_title_search_post_continues_to_original_page_post(self):
        source = picture()
        search = AsyncMock(return_value=[{"link": "https://x.com/other/status/123"}])

        async def tweet(username, tweet_id):
            return {
                "status": True,
                "tweet_id": tweet_id,
                "username": username,
                "images": [f"https://pbs.twimg.com/media/{tweet_id}.png"],
            }

        async def media(url, **kwargs):
            return source if "987.png" in url else picture("red", reverse=True)

        api = SimpleNamespace(
            get_tweet=AsyncMock(side_effect=tweet),
            download_media=AsyncMock(side_effect=media),
        )
        self.default_fetch.return_value = (
            '<a href="https://x.com/original/status/987">Original</a>'
        )
        report = await recover_x(
            source,
            [{"title": "Artist on X: art", "url": "https://public.example/p"}],
            twitter_api=api,
            search_web=search,
        )
        self.assertEqual(api.get_tweet.await_count, 2)
        self.assertEqual(
            [call.args[1] for call in api.get_tweet.await_args_list], ["123", "987"]
        )
        self.assertTrue(
            next(
                candidate
                for candidate in report.candidates
                if candidate.account == "@original"
            ).verified
        )

    async def test_page_fallback_keeps_global_detail_budget_and_no_duplicate_ids(self):
        source = picture()
        search = AsyncMock(
            return_value=[
                {"link": f"https://x.com/other/status/{index}"} for index in range(1, 4)
            ]
        )
        api = SimpleNamespace(get_tweet=AsyncMock(return_value={"status": False}))
        self.default_fetch.return_value = (
            '<a href="https://x.com/original/status/987">Original</a>'
        )
        report = await recover_x(
            source,
            [{"title": "Artist on X: art", "url": "https://public.example/p"}],
            twitter_api=api,
            search_web=search,
        )
        self.assertEqual(api.get_tweet.await_count, 3)
        self.assertEqual(
            {call.args[1] for call in api.get_tweet.await_args_list}, {"1", "2", "3"}
        )
        self.assertIn(
            "https://x.com/original/status/987",
            [candidate.url for candidate in report.candidates],
        )
        self.assertFalse(any(candidate.verified for candidate in report.candidates))

    async def test_untrusted_image_url_is_never_downloaded(self):
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "123",
                    "username": "artist",
                    "images": [
                        "http://127.0.0.1/private",
                        "https://pbs.twimg.com.evil.example/a",
                    ],
                }
            ),
            download_media=AsyncMock(return_value=picture()),
        )
        report = await recover_x(
            picture(), [{"url": "https://x.com/artist/status/123"}], twitter_api=api
        )
        api.download_media.assert_not_awaited()
        self.assertFalse(report.candidates[0].verified)

    async def test_known_handle_timeline_and_details_are_bounded(self):
        api = SimpleNamespace(
            get_user_timeline_items=AsyncMock(
                return_value=[{"tweet_id": str(i)} for i in range(10)]
            ),
            get_tweet=AsyncMock(return_value={"status": True, "images": []}),
        )
        report = await recover_x(
            picture(),
            [{"title": "@artist", "url": "https://public.example/p"}],
            twitter_api=api,
        )
        api.get_user_timeline_items.assert_awaited_once_with(
            "artist", since_id="", limit=3
        )
        self.assertEqual(api.get_tweet.await_count, 3)
        self.assertTrue(all(not candidate.verified for candidate in report.candidates))

    async def test_timeline_repost_keeps_actual_post_author(self):
        api = SimpleNamespace(
            get_user_timeline_items=AsyncMock(
                return_value=[
                    {"tweet_id": "789", "username": "actual_author", "is_retweet": True}
                ]
            ),
            get_tweet=AsyncMock(return_value={"status": True, "images": []}),
        )
        report = await recover_x(picture(), [{"title": "@reposter"}], twitter_api=api)
        api.get_tweet.assert_awaited_once_with("actual_author", "789")
        self.assertEqual(
            report.candidates[1].url, "https://x.com/actual_author/status/789"
        )

    async def test_bounded_pages_queries_and_invalid_result_filter(self):
        search = AsyncMock(
            return_value=[
                {"link": "https://x.com/search?q=anything"},
                {"link": "https://evil.example/alice"},
            ]
        )
        sources = [
            {"url": f"https://public.example/{i}", "title": f"Name {i} on X: art"}
            for i in range(8)
        ]
        with patch("x_recovery._fetch_page", AsyncMock(return_value="")) as fetch:
            report = await recover_x(picture(), sources, search_web=search)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(report.candidates[0].account, "")

    async def test_duplicate_source_hosts_do_not_crowd_out_alternate_original_link(
        self,
    ):
        self.default_fetch.side_effect = [
            RuntimeError("403 forbidden"),
            '<a href="https://x.com/original/status/987">Original</a>',
        ]
        sources = [
            {"url": f"https://blocked.example/page/{index}", "title": "Image"}
            for index in range(4)
        ] + [{"url": "https://alternate.example/post", "title": "Image"}]
        report = await recover_x(picture(), sources)
        self.assertEqual(self.default_fetch.await_count, 2)
        self.assertEqual(
            [call.args[1] for call in self.default_fetch.await_args_list],
            ["https://blocked.example/page/0", "https://alternate.example/post"],
        )
        self.assertEqual(report.candidates[0].url, "https://x.com/original/status/987")

    async def test_source_page_attempts_stay_at_three_distinct_hosts(self):
        sources = [
            {"url": f"https://source{index}.example/page", "title": "Image"}
            for index in range(8)
        ]
        await recover_x(picture(), sources)
        self.assertEqual(self.default_fetch.await_count, 3)

    async def test_nitter_keyword_search_uses_configured_origin_then_web_fallback(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(
                200,
                text='<a class="tweet-link" href="/actual/status/555#m"></a><a class="username" href="/actual">@actual</a>',
            )

        api = SimpleNamespace(nitter_url="http://nitter:8080", is_ready=False)
        search = AsyncMock(return_value=[])
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        with patch("x_recovery.AsyncClient", return_value=client):
            report = await recover_x(
                picture(),
                [{"title": "Artist on X: art"}],
                twitter_api=api,
                search_web=search,
            )
        self.assertEqual(
            str(requests[0].url), "http://nitter:8080/search?f=tweets&q=Artist"
        )
        self.assertEqual(report.candidates[0].url, "https://x.com/actual/status/555")
        search.assert_not_awaited()

    async def test_cancellation_is_preserved(self):
        with patch(
            "x_recovery._fetch_page", AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await recover_x(picture(), [{"url": "https://public.example/p"}])

    async def test_overall_timeout_returns_static_status(self):
        async def slow_fetch(*args):
            await asyncio.sleep(1)
            return ""

        with patch("x_recovery._fetch_page", side_effect=slow_fetch):
            report = await recover_x(
                picture(), [{"url": "https://public.example/p"}], timeout=0.01
            )
        self.assertEqual(report.errors, {"x_recovery": "X 线索查找超时"})


class QuoteMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_quote_match_returns_unchanged_quote_without_closing_api(self):
        quoted = {
            "username": "original",
            "tweet_id": "456",
            "author": "Artist",
            "images": ["https://pbs.twimg.com/media/post.png"],
        }
        api = SimpleNamespace(
            download_media=AsyncMock(return_value=picture()), close=AsyncMock()
        )
        matched = await matching_quote(picture(), {"quote": quoted}, api)
        self.assertIs(matched, quoted)
        self.assertEqual(
            api.download_media.await_args.kwargs["max_bytes"], 10 * 1024 * 1024
        )
        self.assertFalse(hasattr(api, "quote"))
        api.close.assert_not_awaited()

    async def test_public_quote_match_rejects_invalid_identity_and_untrusted_media(
        self,
    ):
        cases = [
            {
                "username": "search",
                "tweet_id": "456",
                "images": ["https://pbs.twimg.com/media/post.png"],
            },
            {
                "username": "original",
                "tweet_id": "invalid",
                "images": ["https://pbs.twimg.com/media/post.png"],
            },
            {
                "username": "original",
                "tweet_id": "456",
                "images": ["http://127.0.0.1/private"],
            },
        ]
        for quoted in cases:
            api = SimpleNamespace(download_media=AsyncMock(return_value=picture()))
            self.assertIsNone(await matching_quote(picture(), {"quote": quoted}, api))
            api.download_media.assert_not_awaited()

    async def test_public_quote_mismatch_does_not_inherit_parent_image(self):
        quoted = {
            "username": "original",
            "tweet_id": "456",
            "images": ["https://pbs.twimg.com/media/quote.png"],
        }
        api = SimpleNamespace(
            download_media=AsyncMock(return_value=picture("red", reverse=True))
        )
        self.assertIsNone(
            await matching_quote(
                picture(),
                {"quote": quoted, "images": ["https://pbs.twimg.com/media/parent.png"]},
                api,
            )
        )

    async def test_public_quote_media_downloads_are_capped_at_three(self):
        quoted = {
            "username": "original",
            "tweet_id": "456",
            "images": [
                f"https://pbs.twimg.com/media/{index}.png" for index in range(4)
            ],
        }
        api = SimpleNamespace(
            download_media=AsyncMock(return_value=picture("red", reverse=True))
        )
        self.assertIsNone(await matching_quote(picture(), {"quote": quoted}, api))
        self.assertEqual(api.download_media.await_count, 3)

    async def test_public_quote_match_preserves_cancellation(self):
        quoted = {
            "username": "original",
            "tweet_id": "456",
            "images": ["https://pbs.twimg.com/media/post.png"],
        }
        api = SimpleNamespace(
            download_media=AsyncMock(side_effect=asyncio.CancelledError)
        )
        with self.assertRaises(asyncio.CancelledError):
            await matching_quote(picture(), {"quote": quoted}, api)


class NetworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_ip_and_mixed_dns_answers_are_rejected(self):
        for url in (
            "http://127.0.0.1/a",
            "http://[::1]/",
            "http://169.254.169.254/",
            "file:///tmp/a",
            "http://localhost/",
            "http://user@8.8.8.8/",
        ):
            self.assertFalse(await _public_url(url))
        loop = asyncio.get_running_loop()
        mixed = [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=mixed)):
            self.assertFalse(await _public_url("https://public.example/a"))

    async def test_redirect_target_revalidated_and_blocked_before_request(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(
                302, headers={"location": "http://127.0.0.1/private"}
            )

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler)
        ) as client:
            with patch(
                "x_recovery._public_url", AsyncMock(side_effect=[True, False])
            ) as validate:
                with self.assertRaises(ValueError):
                    await _fetch_page(client, "https://public.example/p")
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            validate.await_args_list[1].args[0], "http://127.0.0.1/private"
        )

    async def test_redirect_loop_is_bounded(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(302, headers={"location": "/again"})

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler)
        ) as client:
            with patch("x_recovery._public_url", AsyncMock(return_value=True)):
                with self.assertRaises(ValueError):
                    await _fetch_page(client, "https://public.example/p")
        self.assertEqual(len(requests), 3)

    async def test_oversized_page_is_rejected(self):
        def handler(request):
            return httpx2.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"x" * (_MAX_PAGE + 1),
            )

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler)
        ) as client:
            with patch("x_recovery._public_url", AsyncMock(return_value=True)):
                with self.assertRaises(ValueError):
                    await _fetch_page(client, "https://public.example/p")


if __name__ == "__main__":
    unittest.main()
