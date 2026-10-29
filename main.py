"""AstrBot command for reverse image source searches."""

import asyncio
import base64
import binascii
import io
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from .search import (
    TITLE_LLM_TIMEOUT,
    UNKNOWN_ACCOUNT,
    SearchReport,
    SearchService,
    choose_best,
)
from .title_llm import (
    SYSTEM_PROMPT,
    build_title_prompt,
    format_candidate_diagnostics,
    parse_title_candidates,
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUCCESS_IMAGE = Path(__file__).parent / "assets" / "success.png"


class FindSourceFilter(filter.CustomFilter):
    """Recognize command text even when QQ adds an @ to a quoted reply."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        text = "".join(
            part.text for part in event.get_messages() if isinstance(part, Plain)
        ).strip()
        if text in {"找鸡", "/找鸡"}:
            return True
        for prefix in sorted(cfg.get("wake_prefix", []), key=len, reverse=True):
            if prefix and text.startswith(prefix):
                return text[len(prefix) :].strip() == "找鸡"
        return False


def prepare_image(data: bytes) -> bytes:
    """Validate uploads, preserving originals when no transformation is needed."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片需小于 10 MB。")
    try:
        with PILImage.open(io.BytesIO(data)) as source:
            if source.width * source.height > 30_000_000:
                raise ValueError("图片像素过大，请缩小后再试。")
            if (
                source.format in {"JPEG", "PNG", "WEBP"}
                and max(source.size) <= 1600
                and getattr(source, "n_frames", 1) == 1
                and source.getexif().get(274, 1) == 1
            ):
                source.load()
                return data
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((1600, 1600))
            background = PILImage.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            output = io.BytesIO()
            background.save(output, format="JPEG", quality=92)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, PILImage.DecompressionBombError) as exc:
        raise ValueError("无法读取这张图片，请发送 JPG、PNG 或 WebP 图片。") from exc


def format_report(report: SearchReport) -> str:
    """Render the selected X source as a compact five-line message."""
    best, ambiguous = choose_best(report.results)
    results = [best] if best is not None else []
    if not results:
        if report.ambiguous or ambiguous:
            text = "找到了多个证据相当的 X 账号，暂时无法确定最相关来源。可以换原图或更清晰的图片再试。"
        elif report.exact_sources and "source_page" in report.errors:
            text = "已找到同图页面，但部分来源页面无法读取，暂未取得可用的 X 来源。"
        elif report.exact_sources:
            text = "已找到同图页面，但尚未取得可用的 X 来源。"
        elif report.errors:
            text = "本次未取得可用的 X 来源，部分检索或来源追查未能完成，不能据此判断图片没有出处。"
        else:
            text = "这次没有找到同图结果。可以换原图或更清晰的图片再试。"
    else:
        text = "🎯 靓鸡甄选结果"
        item = results[0]
        text = (
            f"{text}\n"
            f"平台：{item.platform}\n"
            f"名字：{item.name or '未提供'}\n"
            f"账号：{item.account or '未提供'}\n"
            f"来源：{item.url}"
        )
    if not results and report.errors:
        text += "\n\n未完成的检索或来源追查：" + "、".join(
            f"{engine}（{reason}）" for engine, reason in report.errors.items()
        )
    return text


class FLJPlugin(Star):
    """Find indexed image sources from direct and quoted image messages."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = SearchService(config, self._get_twitter_api)
        self._active: set[tuple[str, str]] = set()
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._request_tasks: set[asyncio.Task] = set()
        self._closing = False

    def _get_twitter_api(self):
        getter = getattr(self.context, "get_registered_star", None)
        metadata = getter("astrbot_plugin_twitter") if getter else None
        if metadata is None or not metadata.activated:
            return None
        api = getattr(metadata.star_cls, "twitter_api", None)
        return api if api is not None and api.is_ready else None

    async def _extract_title_accounts(
        self, event: AstrMessageEvent, sources: list[dict]
    ):
        """Parse source text with the session's provider, without images or history."""
        provider_id = str(self.config.get("title_llm_provider_id", "") or "").strip()
        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=build_title_prompt(sources),
            system_prompt=SYSTEM_PROMPT,
            contexts=[],
            request_max_retries=1,
        )
        candidates = parse_title_candidates(response.completion_text, sources)
        if self.config.get("log_title_candidates", False):
            logger.info(
                "FLJ title candidates (unverified text): %s",
                format_candidate_diagnostics(candidates, sources),
            )
        return candidates

    async def _find_image(self, event: AstrMessageEvent) -> Image | None:
        messages = event.get_messages()
        direct = next((part for part in messages if isinstance(part, Image)), None)
        if direct is not None:
            return direct
        replies = [part for part in messages if isinstance(part, Reply)]
        for reply in replies:
            for part in reply.chain or []:
                if isinstance(part, Image):
                    return part
        if event.get_platform_name() == "aiocqhttp":
            for reply in replies:
                try:
                    response = await asyncio.wait_for(
                        event.bot.call_action(
                            action="get_msg",
                            message_id=int(reply.id),
                            self_id=event.get_self_id(),
                        ),
                        timeout=10,
                    )
                    payload = response.get("data", response)
                    for part in payload.get("message", []):
                        if isinstance(part, dict) and part.get("type") == "image":
                            data = part.get("data", {})
                            location = data.get("url") or data.get("file")
                            if location:
                                return Image(file=location, url=data.get("url"))
                except Exception as exc:
                    logger.warning(
                        "FLJ quoted image lookup failed: %s", type(exc).__name__
                    )
        return None

    @filter.custom_filter(FindSourceFilter)
    async def find_source(self, event: AstrMessageEvent):
        """Search the image attached to, or quoted by, this command."""
        if event.get_platform_name() != "aiocqhttp":
            return
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            identity = event.get_group_id()
            allowed = self.config.get("group_whitelist", [])
        elif event.get_message_type() == MessageType.FRIEND_MESSAGE:
            identity = event.get_sender_id()
            allowed = self.config.get("private_qq_whitelist", [])
        else:
            return
        identity = str(identity).strip()
        if (
            not identity.isascii()
            or not identity.isdecimal()
            or not isinstance(allowed, (list, tuple))
            or identity not in {str(value).strip() for value in allowed}
        ):
            return
        event.stop_event()
        if self._closing:
            return
        task = asyncio.create_task(self._handle_request(event))
        self._request_tasks.add(task)
        try:
            await task
        finally:
            self._request_tasks.discard(task)

    async def _handle_request(self, event: AstrMessageEvent):
        if self._closing:
            return
        key = (event.unified_msg_origin, event.get_sender_id())
        now = time.monotonic()
        cooldown = max(0, int(self.config.get("cooldown_seconds", 15)))
        self._cooldowns = {
            identity: timestamp
            for identity, timestamp in self._cooldowns.items()
            if now - timestamp < cooldown
        }
        if key in self._active:
            await event.send(event.plain_result("你的上一张图片还在甄选，请稍候。"))
            return
        if key in self._cooldowns:
            remaining = max(1, int(cooldown - (now - self._cooldowns[key])) + 1)
            await event.send(event.plain_result(f"请在 {remaining} 秒后再试。"))
            return
        if len(self._active) >= 2:
            await event.send(event.plain_result("甄选队列已满，请稍后再试。"))
            return
        self._active.add(key)
        try:
            image = await self._find_image(event)
            if image is None:
                await event.send(
                    event.plain_result(
                        "请发送 图片 + 找鸡，或引用一张图片后发送 找鸡。"
                    )
                )
                return
            await event.send(event.plain_result("正在靓鸡甄选"))
            self._cooldowns[key] = time.monotonic()
            try:
                encoded = await asyncio.wait_for(image.convert_to_base64(), timeout=20)
                if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
                    raise ValueError("图片需小于 10 MB。")
                data = base64.b64decode(encoded, validate=True)
                upload = await asyncio.to_thread(prepare_image, data)
            except (ValueError, binascii.Error) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "图片数据无效。"
                await event.send(event.plain_result(message))
                return
            except Exception as exc:
                logger.warning("FLJ image download failed: %s", type(exc).__name__)
                await event.send(
                    event.plain_result("图片读取失败，可能已过期，请重新发送图片。")
                )
                return
            try:
                extractor = None
                if self.config.get("use_title_llm", True) and callable(
                    getattr(self.context, "llm_generate", None)
                ):

                    async def extractor(sources):
                        return await self._extract_title_accounts(event, sources)

                search = (
                    self.service.search(upload, title_extractor=extractor)
                    if extractor is not None
                    else self.service.search(upload)
                )
                report = await asyncio.wait_for(
                    search,
                    timeout=max(5, int(self.config.get("search_timeout", 30)))
                    + max(5, min(30, int(self.config.get("x_lookup_timeout", 20))))
                    + (TITLE_LLM_TIMEOUT if extractor is not None else 0)
                    + 10,
                )
            except TimeoutError:
                await event.send(event.plain_result("识图引擎响应超时，请稍后再试。"))
                return
            except Exception as exc:
                logger.error("FLJ search failed: %s", type(exc).__name__)
                await event.send(
                    event.plain_result("识图暂时失败，请检查插件配置和服务器网络。")
                )
                return
            logger.info(
                "FLJ search stages: engine_results=%s, exact_sources=%d, "
                "source_pages=%s, x_candidates=%d, title_llm=%s",
                report.engine_results,
                len(report.exact_sources),
                report.source_pages,
                report.x_candidate_count,
                report.title_llm,
            )
            logger.info(
                "FLJ search report: results=%d, ambiguous=%s, errors=%s",
                len(report.results),
                report.ambiguous,
                report.errors,
            )
            for result in report.results:
                logger.info(
                    "FLJ selected source: match=%s, verified=%s, reason=%s",
                    result.match_type,
                    result.verified,
                    result.selection_reason,
                )
            await event.send(event.plain_result(format_report(report)))
            selected, _ = choose_best(report.results)
            if (
                self.config.get("send_success_image", True)
                and selected is not None
                and selected.name.strip()
                and selected.account
                and selected.account != UNKNOWN_ACCOUNT
                and SUCCESS_IMAGE.is_file()
            ):
                await event.send(
                    event.chain_result([Image.fromFileSystem(str(SUCCESS_IMAGE))])
                )
        finally:
            self._active.discard(key)

    async def terminate(self):
        self._closing = True
        tasks = list(self._request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.service.close()
        self._active.clear()
        self._cooldowns.clear()
