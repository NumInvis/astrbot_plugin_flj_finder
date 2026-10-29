"""Offline contract tests using PicImageSearch's real response parsers."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PicImageSearch.model import (
    Ascii2DResponse,
    BaiDuResponse,
    GoogleLensExactMatchesResponse,
)

from google_api import GoogleAPIError
from search import (
    UNKNOWN_ACCOUNT,
    SearchResult,
    SearchService,
    _check_response,
    _EngineUnavailable,
    normalize_url,
    parse_results,
    public_url,
)
from x_recovery import RecoveryReport, XCandidate


class ParserTests(unittest.TestCase):
    def test_ascii2d_real_parser_keeps_post_and_explicit_account(self):
        html = """<html><body><div class="row item-box"><img src="/thumb.jpg">
        <div class="hash">abcdef</div><small>100x100 JPG</small>
        <div class="detail-box gray-link"><small>twitter</small>
        <a href="https://twitter.com/example/status/12345">Post title</a>
        <a href="https://twitter.com/example">Display name</a></div></div></body></html>"""
        response = Ascii2DResponse(html, "https://ascii2d.net/search/color/test")
        results = parse_results(response, "ascii2d")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://x.com/example/status/12345")
        self.assertEqual(results[0].account, "@example")
        self.assertEqual(results[0].name, "Display name")
        self.assertFalse(results[0].verified)

    def test_lens_exact_real_parser_has_match_evidence_without_inferred_author(self):
        html = """<div class="YxbOwd"><a class="ngTNl"
        href="https://x.com/i/status/12345"><span class="ZhosBf">Someone's name</span>
        </a></div>"""
        response = GoogleLensExactMatchesResponse(
            html, "https://www.google.com/search?udm=48"
        )
        results = parse_results(response, "google_lens", "exact")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].verified)
        self.assertEqual(results[0].account, UNKNOWN_ACCOUNT)
        self.assertEqual(results[0].name, "")

    def test_baidu_exact_and_visual_are_distinct(self):
        response = BaiDuResponse(
            {
                "same": {
                    "list": [
                        {
                            "url": "https://weibo.com/123456/postABC",
                            "image_src": "https://example.com/a.jpg",
                            "title": ["same image"],
                        }
                    ]
                },
                "data": {
                    "list": [
                        {
                            "fromUrl": "https://www.bilibili.com/video/BV1234567890",
                            "thumbUrl": "https://example.com/b.jpg",
                            "title": ["visual match"],
                        }
                    ]
                },
            },
            "https://graph.baidu.com/result",
        )
        results = parse_results(response, "baidu")
        self.assertEqual(results[0].account, "UID 123456")
        self.assertTrue(results[0].verified)
        self.assertEqual(results[1].account, UNKNOWN_ACCOUNT)
        self.assertFalse(results[1].verified)

    def test_x_domains_and_photo_suffix_normalize(self):
        self.assertEqual(
            normalize_url(
                "https://mobile.twitter.com/Test_User/status/123/photo/2?s=20#x"
            ),
            "https://x.com/Test_User/status/123",
        )

    def test_public_link_validation_and_lookalikes(self):
        for value in (
            "file:///etc/passwd",
            "http://localhost/x",
            "http://127.0.0.1/x",
            "http://10.0.0.2/x",
            "http://[::1]/x",
            "https://user:pass@x.com/test",
            "https://a.local/x",
            "https://x.com\\@127.0.0.1/x",
        ):
            with self.subTest(value=value):
                self.assertEqual(public_url(value), "")
        response = SimpleNamespace(
            raw=[
                SimpleNamespace(
                    url="https://x.com.evil.example/name/status/123",
                    title="",
                    author="",
                )
            ]
        )
        self.assertEqual(parse_results(response, "google_lens")[0].platform, "其他")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_account_survives_full_keyword_pool_and_final_selection(self):
        api = SimpleNamespace(
            is_ready=True,
            get_user_info=AsyncMock(
                return_value={
                    "status": True,
                    "user_name": "alias",
                    "screen_name": "Alias",
                }
            ),
            get_tweet=AsyncMock(return_value={"status": False}),
        )
        service = SearchService(
            {"engines": ["google_lens"], "serpapi_api_key": "test-key"},
            twitter_api_provider=lambda: api,
        )
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    url="https://source.example/photo",
                    title="alias - Gallery",
                    verified=True,
                )
            ]
        )
        extractor = AsyncMock(
            return_value=[
                {
                    "handle": "alias",
                    "platform": "unknown",
                    "source_url": "https://source.example/photo",
                }
            ]
        )
        with (
            patch("x_recovery._fetch_page", AsyncMock(return_value="")),
            patch(
                "search.search_x_serpapi",
                AsyncMock(
                    return_value=[
                        {"link": f"https://x.com/random{index}/status/{100 + index}"}
                        for index in range(8)
                    ]
                ),
            ),
        ):
            report = await service.search(b"image", title_extractor=extractor)
        self.assertEqual(report.x_candidate_count, 8)
        self.assertEqual(report.title_llm["checked"], 1)
        self.assertEqual(report.title_llm["found"], 1)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].account, "@alias")
        self.assertTrue(report.results[0].account_confirmed)
        self.assertFalse(report.results[0].verified)
        self.assertEqual(report.results[0].clue_strength, 0)

    async def test_llm_metadata_includes_unknown_platform_for_recovery_fallback(self):
        service = SearchService({"engines": ["google_lens"]})
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    url="https://source.example/photo",
                    title="X ID: sample_01",
                    source="Example",
                    verified=True,
                )
            ]
        )
        valid = {"handle": "sample_01", "platform": "X"}
        unknown = {"handle": "alias", "platform": "unknown"}
        extractor = AsyncMock(return_value=[valid, unknown])
        recovery = RecoveryReport(title_accounts_checked=1, title_accounts_found=1)
        with patch("search.recover_x", AsyncMock(return_value=recovery)) as recover:
            report = await service.search(b"image", title_extractor=extractor)
        self.assertEqual(extractor.await_args.args[0][0]["source"], "Example")
        self.assertEqual(
            recover.await_args.kwargs["title_candidates"], [valid, unknown]
        )
        self.assertEqual(
            report.title_llm,
            {
                "status": "completed",
                "returned": 2,
                "eligible": 2,
                "unknown": 1,
                "checked": 1,
                "found": 1,
            },
        )

    async def test_llm_failure_falls_back_to_rules_without_logging_response(self):
        service = SearchService({"engines": ["google_lens"]})
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    url="https://source.example/photo",
                    verified=True,
                )
            ]
        )
        with patch(
            "search.recover_x", AsyncMock(return_value=RecoveryReport())
        ) as recover:
            report = await service.search(
                b"image",
                title_extractor=AsyncMock(side_effect=ValueError("SECRET_SENTINEL")),
            )
        recover.assert_awaited_once()
        self.assertNotIn("title_candidates", recover.await_args.kwargs)
        self.assertEqual(report.title_llm["status"], "ValueError")
        self.assertNotIn("SECRET_SENTINEL", repr(report))

    async def test_direct_x_match_does_not_spend_llm_request(self):
        service = SearchService({"engines": ["google_lens"]})
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    platform="X",
                    url="https://x.com/sample/status/123",
                    verified=True,
                )
            ]
        )
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)
        extractor = AsyncMock()
        await service.search(b"image", title_extractor=extractor)
        extractor.assert_not_awaited()

    async def test_blocked_page_retains_exact_metadata_and_stage_counts(self):
        service = SearchService({"engines": ["google_lens"]})
        source = SearchResult(
            url="https://source.example/photo",
            title="A landscape photograph",
            verified=True,
        )
        service._search_engine = AsyncMock(return_value=[source])
        recovery = RecoveryReport(
            errors={"source_page": "部分精确匹配来源页面无法读取"},
            source_pages={"source.example": "HTTP 403"},
        )
        with patch("search.recover_x", AsyncMock(return_value=recovery)):
            report = await service.search(b"image")
        self.assertEqual(report.results, [])
        self.assertEqual(report.exact_sources, [source])
        self.assertEqual(report.engine_results, {"google_lens": 1})
        self.assertEqual(report.source_pages, {"source.example": "HTTP 403"})
        self.assertEqual(report.x_candidate_count, 0)

    async def test_empty_engine_records_zero_without_fabricating_sources(self):
        service = SearchService({"engines": ["google_lens"]})
        service._search_engine = AsyncMock(return_value=[])
        with patch("search.recover_x", AsyncMock()) as recover:
            report = await service.search(b"image")
        recover.assert_not_awaited()
        self.assertEqual(report.engine_results, {"google_lens": 0})
        self.assertEqual(report.exact_sources, [])
        self.assertEqual(report.source_pages, {})
        self.assertEqual(report.errors, {})

    async def test_exact_x_skips_recovery_and_non_x_is_never_returned(self):
        service = SearchService({"engines": ["google_lens"]})
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    platform="X", url="https://x.com/artist/status/123", verified=True
                ),
                SearchResult(
                    platform="微博", url="https://weibo.com/123/post", verified=True
                ),
            ]
        )
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)
        with patch("search.recover_x", new=AsyncMock()) as recover:
            report = await service.search(b"image")
        recover.assert_not_awaited()
        self.assertEqual([r.platform for r in report.results], ["X"])

    async def test_non_x_exact_sources_feed_recovery_with_paid_query_cap(self):
        borrowed = object()
        service = SearchService(
            {"engines": ["google_lens"], "serpapi_api_key": "test-key"},
            twitter_api_provider=lambda: borrowed,
        )
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    platform="其他",
                    url="https://example.org/post",
                    title="Artist - photo",
                    verified=True,
                ),
                SearchResult(
                    platform="其他", url="https://example.org/similar", verified=False
                ),
            ]
        )
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)

        async def recover(image, sources, api, web, proxy, timeout):
            self.assertEqual(image, b"image")
            self.assertIs(api, borrowed)
            self.assertEqual(
                sources,
                [{"url": "https://example.org/post", "title": "Artist - photo"}],
            )
            for _ in range(3):
                await web("Artist")
            return RecoveryReport(
                candidates=[
                    XCandidate(
                        "https://x.com/artist/status/123",
                        verified=True,
                        evidence="图像比对吻合",
                    ),
                    XCandidate("https://twitter.com/other", verified=False),
                    XCandidate("https://weibo.com/123/post", verified=True),
                ]
            )

        with (
            patch("search.recover_x", side_effect=recover),
            patch("search.search_x_serpapi", new=AsyncMock(return_value=[])) as web,
        ):
            report = await service.search(b"image")
        self.assertEqual(web.await_count, 2)
        self.assertEqual([r.platform for r in report.results], ["X"])
        self.assertEqual([r.verified for r in report.results], [True])
        self.assertEqual([r.match_type for r in report.results], ["recovered"])

    async def test_recovery_timeout_is_reported_without_returning_non_x(self):
        service = SearchService({"engines": ["google_lens"]})
        service.x_lookup_timeout = 0.01
        service._search_engine = AsyncMock(
            return_value=[
                SearchResult(
                    platform="其他", url="https://example.org/post", verified=True
                ),
            ]
        )

        async def slow(*args):
            await asyncio.sleep(2)

        with patch("search.recover_x", side_effect=slow):
            report = await service.search(b"image")
        self.assertEqual(report.results, [])
        self.assertIn("超时", report.errors["x_lookup"])

    async def test_explicit_handle_seed_is_prioritized_over_repeated_hosts(self):
        service = SearchService({"engines": ["google_lens"]})
        seeds = [
            SearchResult(
                url=f"https://example.org/page/{i}", title="Album - page", verified=True
            )
            for i in range(12)
        ]
        seeds.append(
            SearchResult(
                url="https://mirror.example.net/profile",
                title="昵称@artist - Twitter Profile",
                verified=True,
            )
        )
        service._search_engine = AsyncMock(return_value=seeds)
        with patch(
            "search.recover_x", new=AsyncMock(return_value=RecoveryReport())
        ) as recover:
            await service.search(b"image")
        sources = recover.await_args.args[1]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["title"], "昵称@artist - Twitter Profile")

    async def test_configured_google_api_key_skips_direct_google_requests(self):
        service = SearchService(
            {"engines": ["google_lens"], "serpapi_api_key": "test-key"}
        )
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)
        with (
            patch("search.Network") as direct,
            patch(
                "search.search_serpapi",
                new=AsyncMock(
                    return_value={
                        "exact_matches": [
                            {
                                "link": "https://x.com/example/status/12345",
                                "title": "Indexed post",
                            }
                        ]
                    }
                ),
            ) as api,
        ):
            report = await service.search(b"image")
        direct.assert_not_called()
        api.assert_awaited_once_with(b"image", "test-key", 25.0, None)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].account, "@example")
        self.assertTrue(report.results[0].verified)

    async def test_google_api_failure_is_not_a_successful_empty_search(self):
        service = SearchService(
            {"engines": ["google_lens"], "serpapi_api_key": "secret-key"}
        )
        with patch(
            "search.search_serpapi",
            new=AsyncMock(
                side_effect=GoogleAPIError("SerpApi 查询额度不足或请求过于频繁")
            ),
        ):
            report = await service.search(b"image")
        self.assertFalse(report.results)
        self.assertIn("额度不足", report.errors["google_lens"])
        self.assertNotIn("secret-key", str(report))

    async def test_lens_exact_only_uses_image_bytes_and_closes_owned_client(self):
        service = SearchService({"engines": ["google_lens"]})
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)
        calls = []
        lifecycle = []
        client = SimpleNamespace(event_hooks={"response": []})

        class FakeNetwork:
            async def __aenter__(self):
                lifecycle.append("open")
                return client

            async def __aexit__(self, *args):
                lifecycle.append("close")

        def lens_factory(*, client, search_type):
            async def search(*, file):
                calls.append((search_type, file))
                raw = (
                    []
                    if search_type == "exact_matches"
                    else [
                        SimpleNamespace(
                            url="https://x.com/example/status/123", title="Post"
                        )
                    ]
                )
                return SimpleNamespace(raw=raw)

            return SimpleNamespace(search=search)

        with (
            patch("search.Network", return_value=FakeNetwork()),
            patch("search.GoogleLens", side_effect=lens_factory),
        ):
            report = await service.search(b"image bytes")
        self.assertEqual(
            calls,
            [("exact_matches", b"image bytes")],
        )
        self.assertEqual(lifecycle, ["open", "close"])
        self.assertEqual(len(report.results), 0)

    async def test_timeout_cancellation_closes_owned_client(self):
        service = SearchService({"engines": ["google_lens"]})
        service.timeout = 0.02
        closed = []
        client = SimpleNamespace(event_hooks={"response": []})

        class FakeNetwork:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                closed.append(True)

        async def hanging_search(**kwargs):
            await asyncio.sleep(1)

        with (
            patch("search.Network", return_value=FakeNetwork()),
            patch(
                "search.GoogleLens", return_value=SimpleNamespace(search=hanging_search)
            ),
        ):
            report = await service.search(b"image")
        self.assertEqual(report.errors, {"google_lens": "搜索超时"})
        self.assertEqual(closed, [True])

    async def test_ranking_evidence_before_platform_and_status_dedup(self):
        service = SearchService(
            {"engines": ["google_lens", "baidu", "ascii2d"], "max_results": 3}
        )
        service._source_account = AsyncMock(side_effect=lambda result, *args: result)

        async def engine_results(engine, image):
            if engine == "google_lens":
                return [
                    SearchResult(
                        platform="X", url="https://x.com/i/status/123", engine=engine
                    ),
                    SearchResult(
                        platform="X",
                        account="@other",
                        url="https://x.com/other/status/222",
                        engine=engine,
                        match_type="exact",
                        verified=True,
                    ),
                ]
            if engine == "baidu":
                return [
                    SearchResult(
                        platform="微博",
                        account="UID 456",
                        url="https://weibo.com/456/post",
                        engine=engine,
                        match_type="exact",
                        verified=True,
                    )
                ]
            return [
                SearchResult(
                    platform="X",
                    account="@source",
                    url="https://x.com/source/status/123",
                    engine=engine,
                )
            ]

        service._search_engine = engine_results
        report = await service.search(b"image")
        self.assertEqual(
            [item.url for item in report.results],
            [
                "https://x.com/other/status/222",
            ],
        )
        self.assertEqual(report.errors, {})

    async def test_same_x_post_combines_exact_evidence_and_known_account(self):
        exact = SearchResult(
            platform="X",
            url="https://x.com/i/status/123",
            engine="google_lens",
            match_type="exact",
            verified=True,
        )
        visual = SearchResult(
            platform="X",
            url="https://x.com/artist/status/123",
            account="@artist",
            name="Artist Name",
            engine="ascii2d",
        )
        for order in (["google_lens", "ascii2d"], ["ascii2d", "google_lens"]):
            with self.subTest(order=order):
                service = SearchService({"engines": order})
                service._source_account = AsyncMock(
                    side_effect=lambda result, *args: result
                )

                async def engine_results(engine, image):
                    return [exact if engine == "google_lens" else visual]

                service._search_engine = engine_results
                report = await service.search(b"image")
                self.assertEqual(len(report.results), 1)
                result = report.results[0]
                self.assertTrue(result.verified)
                self.assertEqual(result.match_type, "exact")
                self.assertEqual(result.engine, "google_lens")
                self.assertEqual(result.account, "@artist")
                self.assertEqual(result.name, "Artist Name")
                self.assertEqual(result.url, "https://x.com/artist/status/123")

    async def test_partial_failure_preserves_results_and_redacts_exception(self):
        service = SearchService({"engines": ["google_lens", "baidu", "ascii2d"]})

        async def engine_results(engine, image):
            if engine == "google_lens":
                raise RuntimeError("https://name:secret@proxy.invalid api_key=private")
            if engine == "baidu":
                raise _EngineUnavailable("搜索服务返回 HTTP 403")
            return [
                SearchResult(
                    platform="X",
                    url="https://x.com/example/status/123",
                    engine=engine,
                    verified=True,
                )
            ]

        service._search_engine = engine_results
        report = await service.search(b"image")
        self.assertEqual(len(report.results), 1)
        self.assertEqual(set(report.errors), {"google_lens", "baidu"})
        self.assertNotIn("secret", str(report.errors))
        self.assertIn("403", report.errors["baidu"])

    async def test_engine_timeout_isolated(self):
        service = SearchService({"engines": ["google_lens", "baidu"]})
        service.timeout = 0.02

        async def engine_results(engine, image):
            if engine == "google_lens":
                await asyncio.sleep(1)
            return [
                SearchResult(
                    platform="X",
                    url="https://x.com/example/status/123",
                    engine=engine,
                    verified=True,
                )
            ]

        service._search_engine = engine_results
        report = await service.search(b"image")
        self.assertEqual(report.errors, {"google_lens": "搜索超时"})
        self.assertEqual(len(report.results), 1)

    async def test_empty_image_and_optional_saucenao(self):
        service = SearchService({"saucenao_api_key": "placeholder"})
        self.assertIn("saucenao", service.engines)
        report = await service.search(b"")
        self.assertEqual(report.errors, {"image": "图片内容为空"})
        await service.close()

    async def test_http_and_challenge_detection(self):
        with self.assertRaisesRegex(_EngineUnavailable, "429"):
            await _check_response(SimpleNamespace(status_code=429))
        response = SimpleNamespace(
            status_code=200,
            url="https://ascii2d.net/",
            headers={"content-type": "text/html"},
            text="<title>Just a moment...</title>",
            aread=AsyncMock(),
        )
        with self.assertRaisesRegex(_EngineUnavailable, "验证码"):
            await _check_response(response)

    async def test_google_http_200_javascript_gate_is_not_an_empty_search(self):
        response = SimpleNamespace(
            status_code=200,
            url="https://www.google.com/search?udm=26",
            headers={"content-type": "text/html; charset=UTF-8"},
            text="""<html><title>Google Search</title><noscript>
                <meta http-equiv="refresh" content="0;url=/httpservice/retry/enablejs?sei=test">
                <a href="/httpservice/retry/enablejs?sei=test">Enable JavaScript</a>
                </noscript><script>document.cookie="SG_SS=test"</script>
                <a href="https://support.google.com/websearch">Help</a></html>""",
            aread=AsyncMock(),
        )
        with self.assertRaisesRegex(_EngineUnavailable, "JavaScript.*未取得搜索结果"):
            await _check_response(response)

    async def test_google_rendered_captcha_is_a_failed_search(self):
        response = SimpleNamespace(
            status_code=200,
            url="https://www.google.com/search",
            headers={"content-type": "text/html"},
            text='<html><div id="recaptcha"></div>Our systems have detected unusual traffic</html>',
            aread=AsyncMock(),
        )
        with self.assertRaisesRegex(_EngineUnavailable, "人机验证"):
            await _check_response(response)

    async def test_google_result_page_noscript_hint_does_not_hide_results(self):
        response = SimpleNamespace(
            status_code=200,
            url="https://www.google.com/search?udm=48",
            headers={"content-type": "text/html"},
            text='<noscript><a href="/httpservice/retry/enablejs">Enable JavaScript</a></noscript>'
            + "".join(
                f'<a href="https://example.org/{index}">Match {index}</a>'
                for index in range(6)
            ),
            aread=AsyncMock(),
        )
        await _check_response(response)

    async def test_bilibili_owner_comes_only_from_public_video_metadata(self):
        service = SearchService({})
        result = SearchResult(
            platform="哔哩哔哩",
            url="https://www.bilibili.com/video/BV1234567890",
            title="Title not used as author",
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "code": 0,
                "data": {"owner": {"mid": 456, "name": "Uploader"}},
            },
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))

        class FakeNetwork:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        with patch("search.Network", return_value=FakeNetwork()):
            found = await service._source_account(result)
        self.assertEqual(found.account, "UID 456")
        self.assertEqual(found.name, "Uploader")
        self.assertFalse(found.verified)
        self.assertEqual(
            client.get.call_args.args[0],
            "https://api.bilibili.com/x/web-interface/view",
        )
        self.assertEqual(
            client.get.call_args.kwargs["params"], {"bvid": "BV1234567890"}
        )

    async def test_bilibili_lookup_failure_does_not_lose_source(self):
        service = SearchService({})
        result = SearchResult(
            platform="哔哩哔哩", url="https://www.bilibili.com/video/BV1234567890"
        )
        with patch("search.Network", side_effect=OSError("offline")):
            self.assertEqual(await service._source_account(result), result)

    async def test_x_public_oembed_adds_explicit_author_metadata(self):
        service = SearchService({})
        result = SearchResult(
            platform="X",
            url="https://x.com/i/status/20",
            match_type="exact",
            verified=True,
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"author_name": "jack", "author_url": "https://x.com/jack"},
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))

        class FakeNetwork:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        with patch("search.Network", return_value=FakeNetwork()):
            found = await service._source_account(result)
        self.assertEqual(found.name, "jack")
        self.assertEqual(found.account, "@jack")
        self.assertTrue(found.verified)
        self.assertEqual(found.url, "https://x.com/jack/status/20")
        self.assertEqual(client.get.call_args.args[0], "https://publish.x.com/oembed")
        self.assertEqual(
            client.get.call_args.kwargs["params"]["url"],
            "https://twitter.com/i/status/20",
        )
        self.assertFalse(client.get.call_args.kwargs["follow_redirects"])

    async def test_oembed_author_correction_clears_old_signature(self):
        service = SearchService({})
        result = SearchResult(
            platform="X",
            account="@wrong",
            url="https://x.com/wrong/status/123",
            verified=True,
            clue_strength=3,
            source_hosts=("album.example",),
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "author_name": "Actual",
                "author_url": "https://x.com/actual",
            },
        )

        class FakeNetwork:
            async def __aenter__(self):
                return SimpleNamespace(get=AsyncMock(return_value=response))

            async def __aexit__(self, *args):
                pass

        with patch("search.Network", return_value=FakeNetwork()):
            found = await service._source_account(result)
        self.assertEqual(found.url, "https://x.com/actual/status/123")
        self.assertEqual(found.account, "@actual")
        self.assertEqual(found.clue_strength, 0)
        self.assertEqual(found.source_hosts, ())

    async def test_x_direct_match_metadata_reuses_twitter_and_actual_author(self):
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "123",
                    "username": "real_artist",
                    "screen_name": "Artist",
                }
            ),
            close=AsyncMock(),
        )
        service = SearchService({}, twitter_api_provider=lambda: api)
        result = SearchResult(
            platform="X", url="https://x.com/old_handle/status/123", verified=True
        )
        with patch("search.Network") as network:
            found = await service._source_account(result)
        network.assert_not_called()
        api.close.assert_not_awaited()
        self.assertEqual(found.url, "https://x.com/real_artist/status/123")
        self.assertEqual(found.account, "@real_artist")
        self.assertEqual(found.name, "Artist")
        self.assertTrue(found.verified)

    async def test_x_metadata_from_another_status_does_not_change_account(self):
        api = SimpleNamespace(
            get_tweet=AsyncMock(
                return_value={
                    "status": True,
                    "tweet_id": "999",
                    "username": "unrelated",
                    "screen_name": "Unrelated",
                }
            )
        )
        service = SearchService({}, twitter_api_provider=lambda: api)
        result = SearchResult(
            platform="X",
            url="https://x.com/artist/status/123",
            account="@artist",
            verified=True,
        )
        with patch("search.Network", side_effect=OSError("offline")):
            found = await service._source_account(result)
        self.assertEqual(found, result)

    async def test_bilibili_article_uses_live_verified_author_fields(self):
        service = SearchService({})
        result = SearchResult(
            platform="哔哩哔哩", url="https://www.bilibili.com/read/cv9117807"
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "code": 0,
                "data": {"mid": 431699647, "author_name": "Uploader"},
            },
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))

        class FakeNetwork:
            async def __aenter__(self):
                return client

            async def __aexit__(self, *args):
                return None

        with patch("search.Network", return_value=FakeNetwork()):
            found = await service._source_account(result)
        self.assertEqual(found.name, "Uploader")
        self.assertEqual(found.account, "UID 431699647")
        self.assertEqual(
            client.get.call_args.args[0], "https://api.bilibili.com/x/article/viewinfo"
        )
        self.assertEqual(client.get.call_args.kwargs["params"], {"id": "9117807"})

    async def test_enrichment_does_not_fetch_arbitrary_source_or_weibo_login(self):
        service = SearchService({})
        for url in (
            "https://x.com.evil.example/artist/status/20",
            "https://weibo.com/3783157493/NDXAHaZHM",
        ):
            result = SearchResult(url=url)
            with patch("search.Network") as network:
                self.assertEqual(await service._source_account(result), result)
                network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
