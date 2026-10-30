"""Google Lens searches through SerpApi's image upload API."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from httpx2 import AsyncClient, RequestError, Response, TimeoutException
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 500_000
_SERPAPI_REQUEST_LOG = re.compile(
    r"(https://serpapi\.com/search\.json\?[^\s\"']*?api_key=)[^&\s\"']+",
    re.IGNORECASE,
)


class _SerpApiLogFilter(logging.Filter):
    """Redact SerpApi's query credential from httpx request log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SERPAPI_REQUEST_LOG.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class GoogleAPIError(RuntimeError):
    """A provider failure safe to show without exposing request credentials."""


@asynccontextmanager
async def _api_client(proxy: str | None, timeout: float):
    client_logger = logging.getLogger("httpx2")
    log_filter = _SerpApiLogFilter()
    client_logger.addFilter(log_filter)
    try:
        async with AsyncClient(proxy=proxy, timeout=timeout) as client:
            yield client
    finally:
        client_logger.removeFilter(log_filter)


def _fit_image(image: bytes) -> bytes:
    """Keep ordinary uploads intact and fit larger images to the provider limit."""
    if len(image) <= MAX_UPLOAD_BYTES:
        return image
    try:
        with Image.open(io.BytesIO(image)) as source:
            frame = ImageOps.exif_transpose(source).convert("RGBA")
            frame.thumbnail((1600, 1600))
            rgb = Image.new("RGB", frame.size, "white")
            rgb.paste(frame, mask=frame.getchannel("A"))
        while True:
            for quality in (85, 70, 55):
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=quality, optimize=True)
                if len(output.getvalue()) <= MAX_UPLOAD_BYTES:
                    return output.getvalue()
            rgb = rgb.resize(
                (max(1, int(rgb.width * 0.8)), max(1, int(rgb.height * 0.8))),
                Image.Resampling.LANCZOS,
            )
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise GoogleAPIError("无法准备 SerpApi 上传图片") from None


def _response_json(response: Response) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise GoogleAPIError("SerpApi API Key 无效或无访问权限")
    if response.status_code == 429:
        raise GoogleAPIError("SerpApi 查询额度不足或请求过于频繁")
    if response.status_code >= 500:
        raise GoogleAPIError("SerpApi 服务暂时不可用")
    if response.status_code >= 300:
        raise GoogleAPIError("SerpApi 拒绝了搜索请求")
    try:
        payload = response.json()
    except ValueError:
        raise GoogleAPIError("SerpApi 返回了无效的 JSON 数据") from None
    if not isinstance(payload, dict):
        raise GoogleAPIError("SerpApi 返回了无效的结果结构")
    return payload


def _matches(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    metadata = payload.get("search_metadata", {})
    if not isinstance(metadata, dict):
        raise GoogleAPIError("SerpApi 返回了无效的结果结构")
    status = metadata.get("status")
    if status is not None and status != "Success":
        raise GoogleAPIError("SerpApi 未能完成 Google Lens 检索")
    # SerpApi documents Success + error as a successful, empty search. An
    # ordinary error must stop here, without spending another search credit.
    if "error" in payload and status != "Success":
        raise GoogleAPIError("SerpApi 未能完成 Google Lens 检索")
    if kind not in payload and status != "Success":
        raise GoogleAPIError("SerpApi 返回了无效的结果结构")
    matches = payload.get(kind, [])
    if not isinstance(matches, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("link"), str)
        or not item["link"].strip()
        for item in matches
    ):
        raise GoogleAPIError("SerpApi 返回了无效的结果结构")
    return matches


async def search_serpapi(
    image: bytes, api_key: str, timeout: float, proxy: str | None, visual: bool = False
) -> dict[str, Any]:
    """Upload once, then request exact matches and optionally similar images."""
    if not api_key.strip():
        raise GoogleAPIError("未配置 SerpApi API Key")
    if not image:
        raise GoogleAPIError("SerpApi 上传图片为空")
    upload = (
        image
        if len(image) <= MAX_UPLOAD_BYTES
        else await asyncio.to_thread(_fit_image, image)
    )
    filename, content_type = "image.jpg", "image/jpeg"
    if upload.startswith(b"\x89PNG\r\n\x1a\n"):
        filename, content_type = "image.png", "image/png"
    elif upload.startswith(b"RIFF") and upload[8:12] == b"WEBP":
        filename, content_type = "image.webp", "image/webp"
    try:
        async with _api_client(proxy, timeout) as client:
            uploaded = _response_json(
                await client.post(
                    "https://serpapi.com/image",
                    data={"api_key": api_key.strip()},
                    files={"image": (filename, upload, content_type)},
                )
            )
            if "error" in uploaded:
                raise GoogleAPIError("SerpApi 图片上传失败")
            image_id = uploaded.get("image_id")
            if not isinstance(image_id, str) or not image_id.strip():
                raise GoogleAPIError("SerpApi 未返回有效的图片 ID")
            params = {
                "engine": "google_lens",
                "image_id": image_id,
                "api_key": api_key.strip(),
                "hl": "en",
                "type": "exact_matches",
                "safe": "off",
            }
            payload = _response_json(
                await client.get(
                    "https://serpapi.com/search.json",
                    params=params,
                )
            )
            result: dict[str, Any] = {
                "exact_matches": _matches(payload, "exact_matches")
            }
            if visual:
                # Similar images are extra leads, so a failed second call must
                # never discard the exact matches already retrieved.
                try:
                    visual_payload = _response_json(
                        await client.get(
                            "https://serpapi.com/search.json",
                            params={**params, "type": "visual_matches"},
                        )
                    )
                    result["visual_matches"] = _matches(
                        visual_payload, "visual_matches"
                    )
                except GoogleAPIError:
                    result["visual_matches"] = []
            return result
    except TimeoutException:
        raise GoogleAPIError("SerpApi 请求超时") from None
    except RequestError:
        raise GoogleAPIError("SerpApi 网络请求失败") from None


async def search_x_serpapi(
    query: str, api_key: str, timeout: float, proxy: str | None
) -> list[dict[str, Any]]:
    """Search indexed X pages using a literal clue and fixed domain restrictions."""
    if not api_key.strip():
        raise GoogleAPIError("未配置 X 线索检索所需的 SerpApi Key")
    clue = " ".join(query.replace('"', " ").split())[:120]
    if not clue:
        return []
    try:
        async with _api_client(proxy, timeout) as client:
            payload = _response_json(
                await client.get(
                    "https://serpapi.com/search.json",
                    params={
                        "engine": "google",
                        "q": f'(site:x.com OR site:twitter.com) "{clue}"',
                        "api_key": api_key.strip(),
                        "num": 5,
                        "safe": "off",
                    },
                )
            )
            return _matches(payload, "organic_results")
    except TimeoutException:
        raise GoogleAPIError("X 线索检索超时") from None
    except RequestError:
        raise GoogleAPIError("X 线索检索网络请求失败") from None
