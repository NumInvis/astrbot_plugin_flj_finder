"""SerpApi protocol and failure tests without live or paid API requests."""

import asyncio
import io
import logging
import sys
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import patch

import httpx2
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google_api import (
    MAX_UPLOAD_BYTES,
    GoogleAPIError,
    _fit_image,
    search_serpapi,
    search_x_serpapi,
)

KEY = "secret-api-key-never-in-errors"
MATCH = {"title": "Source", "link": "https://x.com/example/status/123"}


class GoogleAPITests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    async def test_upload_binary_image_and_stop_after_exact(self):
        requests = []

        async def handler(request):
            requests.append(request)
            if request.method == "POST":
                multipart = BytesParser(policy=default).parsebytes(
                    b"Content-Type: "
                    + request.headers["content-type"].encode()
                    + b"\r\n\r\n"
                    + await request.aread()
                )
                fields = {
                    part.get_param("name", header="content-disposition"): part
                    for part in multipart.iter_parts()
                }
                self.assertEqual(set(fields), {"image", "api_key"})
                self.assertEqual(fields["image"].get_payload(decode=True), b"jpeg data")
                self.assertEqual(
                    fields["api_key"].get_payload(decode=True), KEY.encode()
                )
                self.assertEqual(str(request.url), "https://serpapi.com/image")
                return httpx2.Response(200, json={"image_id": "uploaded-id"})
            self.assertEqual(request.url.path, "/search.json")
            self.assertEqual(
                dict(request.url.params),
                {
                    "engine": "google_lens",
                    "image_id": "uploaded-id",
                    "api_key": KEY,
                    "hl": "en",
                    "type": "exact_matches",
                    "safe": "off",
                },
            )
            return httpx2.Response(200, json={"exact_matches": [MATCH]})

        client = self.client(handler)
        with patch("google_api.AsyncClient", return_value=client) as factory:
            result = await search_serpapi(b"jpeg data", KEY, 15, "http://proxy.example")
        factory.assert_called_once_with(proxy="http://proxy.example", timeout=15)
        self.assertEqual(result, {"exact_matches": [MATCH]})
        self.assertEqual(len(requests), 2)
        self.assertTrue(client.is_closed)

    async def test_request_logs_redact_key_and_filter_is_removed(self):
        logger = logging.getLogger("httpx2")
        old_level = logger.level
        old_propagate = logger.propagate
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture()
        baseline_filters = tuple(logger.filters)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:

            async def transport(request):
                logger.info(
                    'HTTP Request: GET https://example.com/ordinary?q=ok "HTTP/1.1 200 OK"'
                )
                if request.method == "POST":
                    return httpx2.Response(200, json={"image_id": "id"})
                return httpx2.Response(200, json={"exact_matches": [MATCH]})

            client = self.client(transport)
            with patch("google_api.AsyncClient", return_value=client):
                self.assertEqual(
                    await search_serpapi(b"image", KEY, 15, None),
                    {"exact_matches": [MATCH]},
                )
            self.assertTrue(
                any("https://serpapi.com/search.json" in line for line in captured)
            )
            self.assertTrue(
                any("POST https://serpapi.com/image" in line for line in captured)
            )
            self.assertTrue(any("api_key=[REDACTED]" in line for line in captured))
            self.assertFalse(any(KEY in line for line in captured))
            self.assertTrue(
                any("https://example.com/ordinary?q=ok" in line for line in captured)
            )
            self.assertEqual(tuple(logger.filters), baseline_filters)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate

    async def test_empty_exact_does_not_request_visual_matches(self):
        for exact in (
            {"exact_matches": []},
            {
                "search_metadata": {"status": "Success"},
                "error": "Google hasn't returned any results for this query.",
            },
        ):
            with self.subTest(exact=exact):
                requests = []

                async def handler(request):
                    requests.append(request)
                    if request.method == "POST":
                        return httpx2.Response(200, json={"image_id": "same-id"})
                    self.assertEqual(request.url.params["image_id"], "same-id")
                    if request.url.params["type"] == "exact_matches":
                        return httpx2.Response(200, json=exact)
                    self.fail("Visual matching is not part of the X source workflow")

                client = self.client(handler)
                with patch("google_api.AsyncClient", return_value=client):
                    result = await search_serpapi(b"image", KEY, 15, None)
                self.assertEqual(result, {"exact_matches": []})
                self.assertEqual(len(requests), 2)
                self.assertTrue(client.is_closed)

    async def test_successfully_empty_exact_is_empty_result(self):
        async def handler(request):
            if request.method == "POST":
                return httpx2.Response(200, json={"image_id": "id"})
            return httpx2.Response(
                200,
                json={
                    "search_metadata": {"status": "Success"},
                    "error": "Google hasn't returned any results for this query.",
                },
            )

        client = self.client(handler)
        with patch("google_api.AsyncClient", return_value=client):
            self.assertEqual(
                await search_serpapi(b"image", KEY, 15, None), {"exact_matches": []}
            )

    async def test_x_clue_search_restricts_domains_and_uses_literal_query(self):
        async def handler(request):
            self.assertEqual(request.url.params["engine"], "google")
            self.assertEqual(request.url.params["safe"], "off")
            self.assertEqual(
                request.url.params["q"],
                '(site:x.com OR site:twitter.com) "Example Name"',
            )
            return httpx2.Response(200, json={"organic_results": [MATCH]})

        client = self.client(handler)
        with patch("google_api.AsyncClient", return_value=client):
            found = await search_x_serpapi('Example "Name"', KEY, 15, None)
        self.assertEqual(found, [MATCH])
        self.assertTrue(client.is_closed)

    async def test_failed_exact_never_falls_back_or_leaks_key(self):
        failures = [
            (401, {"error": KEY}, "API Key"),
            (403, {"error": KEY}, "API Key"),
            (429, {"error": KEY}, "额度"),
            (503, {"error": KEY}, "暂时不可用"),
            (400, {"error": KEY}, "拒绝"),
            (302, {"error": KEY}, "拒绝"),
            (200, {"error": KEY}, "未能完成"),
            (200, {"search_metadata": {"status": "Error"}, "error": KEY}, "未能完成"),
            (200, {"search_metadata": {"status": "Processing"}}, "未能完成"),
            (200, {"exact_matches": "invalid"}, "结构"),
            (200, {"exact_matches": [KEY]}, "结构"),
            (200, {"exact_matches": [{}]}, "结构"),
            (200, {"exact_matches": [{"title": "Missing link"}]}, "结构"),
            (200, {"exact_matches": [{"link": None}]}, "结构"),
            (200, {"exact_matches": [{"link": " "}]}, "结构"),
            (200, {"search_metadata": KEY}, "结构"),
            (200, {}, "结构"),
            (200, [KEY], "结构"),
        ]
        for status, payload, message in failures:
            with self.subTest(status=status, payload=payload):
                requests = []

                async def handler(request):
                    requests.append(request)
                    if request.method == "POST":
                        return httpx2.Response(200, json={"image_id": "id"})
                    return httpx2.Response(status, json=payload)

                client = self.client(handler)
                with patch("google_api.AsyncClient", return_value=client):
                    with self.assertRaisesRegex(GoogleAPIError, message) as caught:
                        await search_serpapi(b"image", KEY, 15, None)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))
                self.assertEqual(len(requests), 2)
                self.assertTrue(client.is_closed)

    async def test_upload_failure_never_searches(self):
        for payload in ({"error": KEY}, {}, {"image_id": 123}, {"image_id": ""}):
            with self.subTest(payload=payload):
                requests = []

                async def handler(request):
                    requests.append(request)
                    return httpx2.Response(200, json=payload)

                client = self.client(handler)
                with patch("google_api.AsyncClient", return_value=client):
                    with self.assertRaises(GoogleAPIError) as caught:
                        await search_serpapi(b"image", KEY, 15, None)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(requests), 1)
                self.assertTrue(client.is_closed)

    async def test_invalid_json_and_network_failures_are_safe(self):
        logger = logging.getLogger("httpx2")
        baseline_filters = tuple(logger.filters)
        for failure, message in (
            (None, "JSON"),
            (httpx2.ConnectError(KEY), "网络"),
            (httpx2.ReadTimeout(KEY), "超时"),
        ):
            with self.subTest(failure=type(failure).__name__):

                async def handler(request):
                    if failure is not None:
                        raise failure
                    return httpx2.Response(200, text=KEY)

                client = self.client(handler)
                with patch("google_api.AsyncClient", return_value=client):
                    with self.assertRaisesRegex(GoogleAPIError, message) as caught:
                        await search_serpapi(b"image", KEY, 15, None)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertTrue(client.is_closed)
                self.assertEqual(tuple(logger.filters), baseline_filters)

    async def test_cancellation_closes_client_without_visual_retry(self):
        searching = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            if request.method == "POST":
                return httpx2.Response(200, json={"image_id": "id"})
            searching.set()
            await asyncio.Event().wait()

        client = self.client(handler)
        logger = logging.getLogger("httpx2")
        baseline_filters = tuple(logger.filters)
        with patch("google_api.AsyncClient", return_value=client):
            task = asyncio.create_task(search_serpapi(b"image", KEY, 15, None))
            await searching.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(client.is_closed)
        self.assertEqual(len(requests), 2)
        self.assertEqual(tuple(logger.filters), baseline_filters)

    async def test_missing_key_and_empty_image_make_no_request(self):
        with patch("google_api.AsyncClient") as factory:
            for image, key in ((b"image", " "), (b"", KEY)):
                with self.assertRaises(GoogleAPIError):
                    await search_serpapi(image, key, 15, None)
        factory.assert_not_called()


class ImageUploadTests(unittest.TestCase):
    def test_small_image_bytes_are_preserved(self):
        data = b"already prepared JPEG"
        self.assertIs(_fit_image(data), data)

    def test_large_image_is_jpeg_under_provider_limit(self):
        image = Image.effect_noise((1600, 1600), 100).convert("RGB")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=100)
        self.assertGreater(len(output.getvalue()), MAX_UPLOAD_BYTES)
        fitted = _fit_image(output.getvalue())
        self.assertLessEqual(len(fitted), MAX_UPLOAD_BYTES)
        with Image.open(io.BytesIO(fitted)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertGreater(min(result.size), 1)

    def test_invalid_large_image_has_safe_error(self):
        with self.assertRaises(GoogleAPIError):
            _fit_image(b"invalid" * MAX_UPLOAD_BYTES)


if __name__ == "__main__":
    unittest.main()
