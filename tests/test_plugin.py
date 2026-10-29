"""Exercise the plugin with real AstrBot messages and dispatch filters."""

import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.pipeline.waking_check.stage import (
    SessionPluginManager,
    WakingCheckStage,
    star_handlers_registry,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_flj_finder import main
from astrbot_plugin_flj_finder.search import SearchReport, SearchResult
from PIL import Image as PILImage


class RecordingEvent(AstrMessageEvent):
    """Record the platform send boundary without contacting a live account."""

    async def send(self, message):
        self.sent.append(message)


def make_event(
    parts=None,
    text="找鸡",
    session="10001",
    sender="20001",
    platform="aiocqhttp",
    message_type=MessageType.GROUP_MESSAGE,
):
    message = AstrBotMessage()
    message.type = message_type
    message.self_id = "123456"
    message.group_id = session if message_type == MessageType.GROUP_MESSAGE else ""
    message.session_id = session
    message.message_id = "42"
    message.sender = MessageMember(user_id=sender, nickname="Tester")
    message.message = parts if parts is not None else [Plain(text=text)]
    message.message_str = text
    message.raw_message = None
    event = RecordingEvent(
        text,
        message,
        PlatformMetadata(name=platform, description="Test adapter", id="test-bot"),
        session,
    )
    event.sent = []
    event.bot = SimpleNamespace(call_action=AsyncMock())
    return event


def sent_text(event):
    return [
        part.text
        for result in event.sent
        for part in result.chain
        if isinstance(part, Plain)
    ]


@pytest.mark.parametrize("provider_id", ["", "chosen-provider"])
@pytest.mark.parametrize("log_candidates", [False, True])
async def test_title_llm_uses_text_only_session_or_explicit_provider(
    plugin, provider_id, log_candidates
):
    import json

    plugin.config["title_llm_provider_id"] = provider_id
    plugin.config["log_title_candidates"] = log_candidates
    event = make_event()
    source = {"url": "https://source.example/photo", "title": "X ID: sample_01"}
    response = {
        "candidates": [
            {
                "handle": "sample_01",
                "source_index": 0,
                "evidence": source["title"],
                "confidence": 0.9,
                "platform": "X",
            }
        ]
    }
    plugin.context = SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="session-provider"),
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text=json.dumps(response))
        ),
    )
    with patch.object(main, "logger") as logger:
        candidates = await plugin._extract_title_accounts(event, [source])
    assert candidates[0]["platform"] == "X"
    args = plugin.context.llm_generate.await_args.kwargs
    assert args["chat_provider_id"] == (provider_id or "session-provider")
    assert args["contexts"] == []
    assert args["request_max_retries"] == 1
    assert "image_urls" not in args and "tools" not in args
    assert "sample_01" in args["prompt"]
    if log_candidates:
        logger.info.assert_called_once()
        assert "unverified text" in logger.info.call_args.args[0]
        assert "sample_01" in logger.info.call_args.args[1]
        assert "explicit_source_platform" in logger.info.call_args.args[1]
    else:
        logger.info.assert_not_called()
    if not provider_id:
        plugin.context.get_current_chat_provider_id.assert_awaited_once_with(
            event.unified_msg_origin
        )
    else:
        plugin.context.get_current_chat_provider_id.assert_not_awaited()


@pytest.fixture
def png_bytes():
    output = io.BytesIO()
    PILImage.new("RGBA", (16, 8), (255, 0, 0, 128)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def image(png_bytes):
    return Image.fromBase64(base64.b64encode(png_bytes).decode())


@pytest.fixture
def plugin():
    instance = main.FLJPlugin(
        SimpleNamespace(),
        {
            "cooldown_seconds": 15,
            "group_whitelist": ["10001", "10002"],
            "private_qq_whitelist": ["20001", "20002"],
        },
    )
    instance.service.search = AsyncMock(return_value=SearchReport())
    instance.service.close = AsyncMock()
    return instance


@pytest.mark.parametrize(
    "group_list,private_list,message_type,group,sender,platform,allowed",
    [
        ([], [], MessageType.GROUP_MESSAGE, "10001", "20001", "aiocqhttp", False),
        (["10001"], [], MessageType.GROUP_MESSAGE, "10001", "20001", "aiocqhttp", True),
        ([10001], [], MessageType.GROUP_MESSAGE, "10001", "20001", "aiocqhttp", True),
        (
            [" 10001 "],
            [],
            MessageType.GROUP_MESSAGE,
            "10001",
            "20001",
            "aiocqhttp",
            True,
        ),
        (
            [],
            ["20001"],
            MessageType.GROUP_MESSAGE,
            "10001",
            "20001",
            "aiocqhttp",
            False,
        ),
        (
            ["10001"],
            [],
            MessageType.FRIEND_MESSAGE,
            "10001",
            "20001",
            "aiocqhttp",
            False,
        ),
        (
            [],
            ["20001"],
            MessageType.FRIEND_MESSAGE,
            "10001",
            "20001",
            "aiocqhttp",
            True,
        ),
        ([], [20001], MessageType.FRIEND_MESSAGE, "10001", "20001", "aiocqhttp", True),
        (
            ["10001"],
            ["20001"],
            MessageType.GROUP_MESSAGE,
            "10001",
            "20001",
            "discord",
            False,
        ),
        (
            ["10001"],
            ["20001"],
            MessageType.OTHER_MESSAGE,
            "10001",
            "20001",
            "aiocqhttp",
            False,
        ),
        ("10001", [], MessageType.GROUP_MESSAGE, "10001", "20001", "aiocqhttp", False),
        (["10001"], [], MessageType.GROUP_MESSAGE, "1000", "20001", "aiocqhttp", False),
    ],
)
async def test_qq_whitelist_has_separate_group_and_private_scope(
    plugin,
    image,
    group_list,
    private_list,
    message_type,
    group,
    sender,
    platform,
    allowed,
):
    plugin.config.update(group_whitelist=group_list, private_qq_whitelist=private_list)
    event = make_event(
        [image],
        session=group,
        sender=sender,
        platform=platform,
        message_type=message_type,
    )
    await plugin.find_source(event)
    assert bool(event.sent) == allowed
    assert event.is_stopped() == allowed
    assert plugin.service.search.await_count == int(allowed)


async def test_denied_session_does_not_download_fetch_quote_or_use_request_state(
    plugin, monkeypatch
):
    plugin.config.update(group_whitelist=[], private_qq_whitelist=[])
    download = AsyncMock()
    monkeypatch.setattr(Image, "convert_to_base64", download)
    event = make_event([Reply(id="919")])
    await plugin.find_source(event)
    assert event.sent == [] and not event.is_stopped()
    download.assert_not_awaited()
    event.bot.call_action.assert_not_awaited()
    plugin.service.search.assert_not_awaited()
    assert not plugin._active and not plugin._cooldowns and not plugin._request_tasks


async def test_whitelist_revocation_takes_effect_before_cooldown_reply(plugin, image):
    await plugin.find_source(make_event([image]))
    plugin.config["group_whitelist"] = []
    second = make_event([image])
    await plugin.find_source(second)
    assert second.sent == [] and not second.is_stopped()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("找鸡", True),
        ("/找鸡", True),
        (" 找鸡 ", True),
        ("帮我找鸡", False),
        ("找鸡蛋", False),
        ("hello", False),
        ("找鸡 其他内容", False),
    ],
)
def test_registered_custom_filter_matches_only_command(text, expected):
    handler = next(
        entry
        for entry in star_handlers_registry.star_handlers_map.values()
        if entry.handler_module_path == main.__name__
        and entry.handler_name == "find_source"
    )
    assert len(handler.event_filters) == 1
    assert isinstance(handler.event_filters[0], main.FindSourceFilter)
    event = make_event(text=text)
    assert not event.is_at_or_wake_command
    assert all(item.filter(event, {}) for item in handler.event_filters) is expected


@pytest.mark.parametrize(
    "text, prefix, expected, quote_mention",
    [
        ("找鸡", "/", True, False),
        ("/找鸡", "/", True, False),
        ("!找鸡", "!", True, False),
        ("ordinary group text", "/", False, False),
        ("找鸡", "/", True, True),
        ("帮我找鸡", "/", False, True),
        (None, "/", False, True),
    ],
)
async def test_real_waking_stage_dispatches_raw_and_configured_prefix(
    monkeypatch, text, prefix, expected, quote_mention
):
    handler = next(
        entry
        for entry in star_handlers_registry.star_handlers_map.values()
        if entry.handler_module_path == main.__name__
        and entry.handler_name == "find_source"
    )
    monkeypatch.setattr(
        star_handlers_registry,
        "get_handlers_by_event_type",
        lambda *args, **kwargs: [handler],
    )
    monkeypatch.setattr(
        SessionPluginManager,
        "filter_handlers_by_session",
        AsyncMock(side_effect=lambda event, handlers: handlers),
    )
    stage = WakingCheckStage()
    stage.ctx = SimpleNamespace(
        astrbot_config={"admins_id": [], "wake_prefix": [prefix], "plugin_set": ["*"]}
    )
    stage.unique_session = False
    stage.ignore_bot_self_message = True
    stage.friend_message_needs_wake_prefix = False
    stage.ignore_at_all = False
    stage.disable_builtin_commands = False
    stage.no_permission_reply = False
    stage._umo_auto_name_recorder = MagicMock()
    if quote_mention:
        parts = [
            Reply(id="919", sender_id="777", chain=[Plain(text="找鸡")]),
            At(qq="777", name="Nickname"),
        ]
        if text is not None:
            parts.append(Plain(text=text))
        event = make_event(parts, text=f" @Nickname(777) {text or ''}")
    else:
        event = make_event(text=text)
    await stage.process(event)
    assert bool(event.get_extra("activated_handlers")) is expected
    assert event.is_wake is expected
    assert event.is_stopped() is not expected
    if expected and not quote_mention:
        assert event.message_str == "找鸡"


async def test_direct_image_takes_precedence_over_quoted_image(plugin, image):
    quoted = Image(file="https://example.org/quoted.png")
    event = make_event([Reply(id="11", chain=[quoted]), Plain(text="找鸡"), image])
    assert await plugin._find_image(event) is image
    event.bot.call_action.assert_not_called()


async def test_reply_chain_uses_real_image_without_remote_fetch(plugin, image):
    event = make_event(
        [Reply(id="11", chain=[Plain(text="photo"), image]), Plain(text="找鸡")]
    )
    found = await plugin._find_image(event)
    assert isinstance(found, Image)
    assert found.file == image.file
    event.bot.call_action.assert_not_called()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_bare_onebot_reply_fetch_preserves_bot_routing(plugin, wrapped):
    payload = {
        "message": [
            {"type": "text", "data": {"text": "photo"}},
            {
                "type": "image",
                "data": {
                    "url": "https://example.org/photo.png",
                    "file": "opaque-image-id",
                },
            },
        ]
    }
    event = make_event([Reply(id="919", chain=[]), Plain(text="找鸡")])
    event.bot.call_action.return_value = {"data": payload} if wrapped else payload
    found = await plugin._find_image(event)
    assert isinstance(found, Image)
    assert found.url == "https://example.org/photo.png"
    assert found.file == "https://example.org/photo.png"
    event.bot.call_action.assert_awaited_once_with(
        action="get_msg", message_id=919, self_id="123456"
    )


async def test_onebot_fetch_failure_becomes_missing_image(plugin):
    event = make_event([Reply(id="919")])
    event.bot.call_action.side_effect = RuntimeError("message unavailable")
    await plugin.find_source(event)
    assert "请发送 图片 + 找鸡" in sent_text(event)[0]
    assert "正在靓鸡甄选" not in sent_text(event)
    plugin.service.search.assert_not_called()
    assert not plugin._active


async def test_bare_reply_on_other_platform_does_not_call_onebot(plugin):
    event = make_event([Reply(id="919")], platform="discord")
    assert await plugin._find_image(event) is None
    event.bot.call_action.assert_not_called()


async def test_missing_image_explains_usage_without_consuming_cooldown(plugin):
    event = make_event()
    await plugin.find_source(event)
    assert len(sent_text(event)) == 1
    assert "请发送 图片 + 找鸡" in sent_text(event)[0]
    assert event.is_stopped()
    assert not plugin._cooldowns
    assert not plugin._active
    plugin.service.search.assert_not_called()


@pytest.mark.parametrize("verified", [True, False])
async def test_progress_and_success_png_require_selected_account_result(
    plugin, image, png_bytes, tmp_path, monkeypatch, verified
):
    success = tmp_path / "success.png"
    success.write_bytes(png_bytes)
    monkeypatch.setattr(main, "SUCCESS_IMAGE", success)
    plugin.service.search.return_value = SearchReport(
        results=[
            SearchResult(
                platform="X",
                account="@artist",
                name="Artist",
                url="https://x.com/artist/status/123",
                engine="google_lens",
                match_type="recovered" if verified else "clue",
                verified=verified,
                clue_strength=3 if not verified else 0,
            )
        ]
    )
    event = make_event([Plain(text="找鸡"), image])
    await plugin.find_source(event)
    assert sent_text(event)[0] == "正在靓鸡甄选"
    assert "平台：X" in sent_text(event)[1]
    assert "账号：@artist" in sent_text(event)[1]
    assert "https://x.com/artist/status/123" in sent_text(event)[1]
    assert sent_text(event)[1].splitlines()[0] == "🎯 靓鸡甄选结果"
    images = [
        part
        for result in event.sent
        for part in result.chain
        if isinstance(part, Image)
    ]
    assert len(images) == 1
    assert images[0].path == str(success.resolve())
    uploaded = plugin.service.search.await_args.args[0]
    assert uploaded == png_bytes
    with PILImage.open(io.BytesIO(uploaded)) as prepared:
        assert prepared.format == "PNG"
        assert prepared.mode == "RGBA"
    assert not plugin._active
    assert not plugin._request_tasks


@pytest.mark.parametrize(
    ("name", "account"),
    [("", "@artist"), ("Artist", "")],
    ids=["missing-name", "missing-account"],
)
async def test_verified_result_without_name_or_account_does_not_send_success_png(
    plugin, image, png_bytes, tmp_path, monkeypatch, name, account
):
    success = tmp_path / "success.png"
    success.write_bytes(png_bytes)
    monkeypatch.setattr(main, "SUCCESS_IMAGE", success)
    plugin.service.search.return_value = SearchReport(
        results=[
            SearchResult(
                platform="X",
                account=account,
                name=name,
                url="https://x.com/artist/status/123",
                verified=True,
            )
        ]
    )
    event = make_event([Plain(text="找鸡"), image])
    await plugin.find_source(event)
    images = [
        part
        for result in event.sent
        for part in result.chain
        if isinstance(part, Image)
    ]
    assert images == []


async def test_success_image_can_be_disabled(plugin, image, tmp_path, monkeypatch):
    success = tmp_path / "success.png"
    success.write_bytes(b"unused asset")
    monkeypatch.setattr(main, "SUCCESS_IMAGE", success)
    plugin.config["send_success_image"] = False
    plugin.service.search.return_value = SearchReport(
        results=[SearchResult(url="https://x.com/a/status/1", verified=True)]
    )
    event = make_event([image])
    await plugin.find_source(event)
    assert not any(
        isinstance(part, Image) for result in event.sent for part in result.chain
    )


async def test_search_failure_is_reported_and_releases_active_slot(plugin, image):
    plugin.service.search.side_effect = RuntimeError("upstream failed")
    event = make_event([image])
    await plugin.find_source(event)
    assert sent_text(event)[0] == "正在靓鸡甄选"
    assert "识图暂时失败" in sent_text(event)[1]
    assert not plugin._active
    assert not plugin._request_tasks


async def test_invalid_image_never_reaches_search(plugin):
    event = make_event([Image.fromBytes(b"not an image")])
    await plugin.find_source(event)
    assert sent_text(event)[0] == "正在靓鸡甄选"
    assert "无法读取这张图片" in sent_text(event)[1]
    plugin.service.search.assert_not_called()
    assert not plugin._active


async def test_cooldown_is_scoped_to_sender_and_session(plugin, image):
    first = make_event([image])
    repeated = make_event([image])
    other_group = make_event([image], session="10002")
    other_sender = make_event([image], sender="20002")
    for event in [first, repeated, other_group, other_sender]:
        await plugin.find_source(event)
    assert "秒后再试" in sent_text(repeated)[0]
    assert sent_text(other_group)[0] == "正在靓鸡甄选"
    assert sent_text(other_sender)[0] == "正在靓鸡甄选"
    assert plugin.service.search.await_count == 3


async def test_same_sender_inflight_is_rejected_and_terminate_closes_resources(
    plugin, image
):
    started = asyncio.Event()
    released = asyncio.Event()

    async def pending_search(upload):
        started.set()
        await released.wait()
        return SearchReport()

    plugin.service.search.side_effect = pending_search
    request = asyncio.create_task(plugin.find_source(make_event([image])))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        duplicate = make_event([image])
        await plugin.find_source(duplicate)
        assert sent_text(duplicate) == ["你的上一张图片还在甄选，请稍候。"]
        await plugin.terminate()
        result = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        plugin.service.close.assert_awaited_once()
        assert plugin._closing
        assert not plugin._active
        assert not plugin._cooldowns
        assert not plugin._request_tasks
    finally:
        released.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("phase", ["image_download", "quoted_message_lookup"])
async def test_terminate_cancels_request_before_search_starts(
    plugin, image, monkeypatch, phase
):
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_operation(*args, **kwargs):
        started.set()
        try:
            await release.wait()
            raise AssertionError("The unloaded request resumed")
        finally:
            cancelled.set()

    if phase == "image_download":
        event = make_event([image])
        monkeypatch.setattr(Image, "convert_to_base64", pending_operation)
        expected_sent = ["正在靓鸡甄选"]
    else:
        event = make_event([Reply(id="919", chain=[])])
        event.bot.call_action.side_effect = pending_operation
        expected_sent = []

    request = asyncio.create_task(plugin.find_source(event))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        assert plugin._request_tasks
        await plugin.terminate()
        outcome = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert cancelled.is_set()
        release.set()
        await asyncio.sleep(0)
        plugin.service.search.assert_not_called()
        plugin.service.close.assert_awaited_once()
        assert sent_text(event) == expected_sent
        assert not plugin._request_tasks
        assert not plugin._active

        after_unload = make_event([image])
        await plugin.find_source(after_unload)
        assert after_unload.sent == []
        plugin.service.search.assert_not_called()
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


def test_prepare_image_resizes_flattens_alpha_and_emits_jpeg():
    source = io.BytesIO()
    PILImage.new("RGBA", (3200, 800), (255, 0, 0, 0)).save(source, format="PNG")
    with PILImage.open(io.BytesIO(main.prepare_image(source.getvalue()))) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (1600, 400)
        assert all(channel >= 250 for channel in image.getpixel((100, 100)))


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_prepare_image_preserves_supported_original_bytes(image_format):
    source = io.BytesIO()
    PILImage.new("RGB", (510, 680), (17, 94, 146)).save(source, format=image_format)
    original = source.getvalue()
    assert main.prepare_image(original) == original


def test_prepare_image_corrects_rotated_original_before_upload():
    source = io.BytesIO()
    image = PILImage.new("RGB", (30, 50), (17, 94, 146))
    exif = image.getexif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif)
    original = source.getvalue()
    prepared = main.prepare_image(original)
    with PILImage.open(io.BytesIO(prepared)) as transformed:
        assert transformed.size == (50, 30)
        assert transformed.getexif().get(274, 1) == 1


@pytest.mark.parametrize(
    "data",
    [b"", b"not an image", b"x" * (main.MAX_IMAGE_BYTES + 1)],
    ids=["empty", "corrupt", "oversized"],
)
def test_prepare_image_rejects_empty_corrupt_and_oversized_uploads(data):
    with pytest.raises(ValueError):
        main.prepare_image(data)


def test_format_report_preserves_partial_engine_failure():
    text = main.format_report(SearchReport(errors={"baidu": "搜索超时"}))
    assert "不能据此判断图片没有出处" in text
    assert "这次没有找到可用的图片出处" not in text
    assert "baidu（搜索超时）" in text


def test_format_report_only_calls_completed_empty_search_not_found():
    assert "这次没有找到同图结果" in main.format_report(SearchReport())


@pytest.mark.parametrize("blocked", [False, True])
def test_format_report_distinguishes_exact_matches_without_x_source(blocked):
    report = SearchReport(
        exact_sources=[
            SearchResult(
                url="https://source.example/photo",
                title="PRIVATE_TITLE_SENTINEL",
                verified=True,
            )
        ],
        errors={"source_page": "部分精确匹配来源页面无法读取"} if blocked else {},
    )
    text = main.format_report(report)
    assert "已找到同图页面" in text
    assert "X 来源" in text
    assert ("来源页面无法读取" in text) == blocked
    assert "识图引擎未能完成" not in text
    assert "没有找到同图结果" not in text
    assert "source.example" not in text
    assert "PRIVATE_TITLE_SENTINEL" not in text


async def test_stage_logs_contain_counts_and_status_but_no_source_content(
    plugin, image
):
    plugin.service.search.return_value = SearchReport(
        exact_sources=[
            SearchResult(
                url="https://source.example/PRIVATE_PATH_SENTINEL?token=TOKEN_SENTINEL",
                title="PRIVATE_TITLE_SENTINEL",
                verified=True,
            )
        ],
        engine_results={"google_lens": 1},
        source_pages={"source.example": "HTTP 403"},
        errors={"source_page": "部分精确匹配来源页面无法读取"},
    )
    event = make_event([image])
    with patch.object(main, "logger") as logger:
        await plugin.find_source(event)
    logged = "\n".join(
        call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0]
        for call in logger.info.call_args_list
    )
    assert "exact_sources=1" in logged
    assert "x_candidates=0" in logged
    assert "source.example" in logged
    assert "HTTP 403" in logged
    assert "SENTINEL" not in logged
    assert "https://" not in logged
    assert "已找到同图页面" in sent_text(event)[1]
    assert not any(
        isinstance(part, Image) for result in event.sent for part in result.chain
    )


def test_format_report_never_returns_other_platforms_or_treats_them_as_success():
    text = main.format_report(
        SearchReport(
            results=[
                SearchResult(
                    platform="微博", url="https://weibo.com/example", verified=True
                ),
            ]
        )
    )
    assert "weibo.com" not in text
    assert "靓鸡甄选结果" not in text
    assert "没有找到同图结果" in text


def test_format_report_shows_only_selected_source_without_numbering():
    report = SearchReport(
        results=[
            SearchResult(
                platform="X",
                url="https://x.com/one/status/111",
                verified=True,
                engine="google_lens",
            ),
            SearchResult(
                platform="X",
                url="https://x.com/two/status/222",
                verified=True,
                engine="google_lens",
            ),
        ]
    )
    text = main.format_report(report)
    assert "https://x.com/one/status/111" in text
    assert "https://x.com/two" not in text
    assert "1. 平台" not in text
    assert "选择依据：" not in text


def test_format_report_selected_result_is_exactly_five_lines_and_hides_diagnostics():
    report = SearchReport(
        results=[
            SearchResult(
                platform="X",
                name="Ayame",
                account="@_ayameayame",
                url="https://x.com/_ayameayame",
                title="候选线索正文不显示",
                engine="google_lens",
                match_type="exact",
                verified=True,
                selection_reason="内部排序原因不显示",
            ),
        ],
        errors={"baidu": "搜索超时"},
        exact_sources=[SearchResult(title="PRIVATE_TITLE_SENTINEL", verified=True)],
        engine_results={"google_lens": 1, "baidu": 0},
        source_pages={"source.example": "HTTP 403"},
        x_candidate_count=1,
    )
    assert main.format_report(report).splitlines() == [
        "🎯 靓鸡甄选结果",
        "平台：X",
        "名字：Ayame",
        "账号：@_ayameayame",
        "来源：https://x.com/_ayameayame",
    ]


def test_format_report_unverified_source_uses_same_five_line_success_format():
    report = SearchReport(
        results=[
            SearchResult(
                platform="X",
                name="Ayame",
                account="@_ayameayame",
                url="https://x.com/_ayameayame",
                title="待核对的线索正文不显示",
                match_type="clue",
                verified=False,
                clue_strength=1,
            ),
        ]
    )
    assert main.format_report(report).splitlines() == [
        "🎯 靓鸡甄选结果",
        "平台：X",
        "名字：Ayame",
        "账号：@_ayameayame",
        "来源：https://x.com/_ayameayame",
    ]


async def test_ambiguous_clues_have_no_link_or_success_meme(plugin, image):
    plugin.service.search.return_value = SearchReport(ambiguous=True)
    event = make_event([image])
    await plugin.find_source(event)
    assert "暂时无法确定" in sent_text(event)[1]
    assert "https://x.com/" not in sent_text(event)[1]
    assert not any(
        isinstance(part, Image) for result in event.sent for part in result.chain
    )


def test_twitter_bridge_uses_current_active_plugin_instance_without_owning_it():
    first_api = SimpleNamespace(is_ready=True)
    second_api = SimpleNamespace(is_ready=True)
    metadata = SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(twitter_api=first_api)
    )
    context = SimpleNamespace(get_registered_star=lambda name: metadata)
    instance = main.FLJPlugin(context, {})
    assert instance._get_twitter_api() is first_api
    metadata.star_cls = SimpleNamespace(twitter_api=second_api)
    assert instance._get_twitter_api() is second_api
    metadata.activated = False
    assert instance._get_twitter_api() is None
