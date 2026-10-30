"""Bounded, read-only recovery of X links from exact image-match sources.

Source text supplies leads, never proof of authorship. Only matching post media
can upgrade a candidate to a confirmed image match. The Twitter client is borrowed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from httpx2 import AsyncClient, HTTPStatusError
from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError

_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_RESERVED = {
    "i",
    "home",
    "search",
    "explore",
    "intent",
    "share",
    "hashtag",
    "settings",
    "messages",
    "notifications",
    "login",
    "logout",
    "signup",
    "tos",
    "privacy",
    "about",
    "help",
    "compose",
    "download",
    "account",
    "communities",
    "jobs",
}
_HANDLE = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_@])")
_URL = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^\s<>\"']+", re.I)
_ATTRIBUTION_LABEL = re.compile(
    r"作者|来源|原帖|\b(?:author|source|original(?:\s+post)?)\b", re.I
)
_ATTRIBUTION_PREFIX = re.compile(
    r"(?:作者|来源|原帖|\b(?:author|source|original(?:\s+post)?)(?:\s+by)?)\s*[:：]?\s*$",
    re.I,
)
_MAX_PAGE = 512 * 1024
_MAX_MEDIA = 10 * 1024 * 1024


@dataclass
class XCandidate:
    url: str
    account: str = ""
    name: str = ""
    title: str = ""
    verified: bool = False
    evidence: str = ""
    clue_strength: int = 0
    source_hosts: tuple[str, ...] = ()
    relation_verified: bool = False
    account_confirmed: bool = False


@dataclass
class RecoveryReport:
    candidates: list[XCandidate] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    source_pages: dict[str, str] = field(default_factory=dict)
    title_accounts_checked: int = 0
    title_accounts_found: int = 0


def _merge_candidate(target: XCandidate, incoming: XCandidate) -> None:
    target.account_confirmed = target.account_confirmed or incoming.account_confirmed
    if incoming.clue_strength > target.clue_strength:
        target.clue_strength = incoming.clue_strength
        target.source_hosts = tuple(dict.fromkeys(incoming.source_hosts))
    elif incoming.clue_strength == target.clue_strength:
        target.source_hosts = tuple(
            dict.fromkeys((*target.source_hosts, *incoming.source_hosts))
        )
    if incoming.verified and (not target.verified or incoming.relation_verified):
        target.verified = True
        target.name = incoming.name or target.name
        target.evidence = incoming.evidence
    elif (
        not target.verified
        and incoming.evidence
        and incoming.evidence not in target.evidence.split("；")
    ):
        target.evidence = "；".join(
            (*target.evidence.split("；"), incoming.evidence)[-3:]
        ).strip("；")
    target.relation_verified = target.relation_verified or incoming.relation_verified
    target.title = target.title or incoming.title
    target.name = target.name or incoming.name


def canonical_x_url(value: str) -> str:
    """Accept an explicit account or post path on an exact X/Twitter host."""
    try:
        parsed = urlsplit(unescape(str(value)).strip().rstrip(".,;!，。；！)）"))
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return ""
        parts = parsed.path.strip("/").split("/")
        account = parts[0]
        if (
            not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account)
            or account.lower() in _RESERVED
        ):
            return ""
        if len(parts) == 1:
            return f"https://x.com/{account}"
        if len(parts) in {3, 5} and parts[1] == "status" and parts[2].isdigit():
            if len(parts) == 5 and not (
                parts[3] in {"photo", "video"} and parts[4].isdigit()
            ):
                return ""
            return f"https://x.com/{account}/status/{parts[2]}"
    except (ValueError, TypeError):
        pass
    return ""


def _source_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return parsed.hostname.lower().rstrip(".")
    except ValueError:
        pass
    return ""


def _text_account_clues(text: str, *, title: bool = False) -> list[tuple[str, int]]:
    """Use the same explicit attribution policy for cheap and full recovery."""
    text = unescape(str(text))[:32_000]
    handles = list(_HANDLE.finditer(text))[:3]
    profile_title = bool(
        title and len(handles) == 1 and re.search(r"\bTwitter\s+Profile\b", text, re.I)
    )
    clues: list[tuple[str, int]] = []
    for match in list(_URL.finditer(text))[:8]:
        url = canonical_x_url(match.group())
        if url:
            strength = (
                3
                if _ATTRIBUTION_PREFIX.search(
                    text[max(0, match.start() - 48) : match.start()]
                )
                else (2 if "/status/" in url else 1)
            )
            clues.append((url, strength))
    for match in handles:
        url = canonical_x_url(f"https://x.com/{match.group(1)}")
        if url:
            attributed = profile_title or bool(
                _ATTRIBUTION_PREFIX.search(
                    text[max(0, match.start() - 48) : match.start()]
                )
            )
            clues.append((url, 3 if attributed else 1))
    return clues


def collect_account_clues(
    sources: list[dict],
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Collect exact-source title attribution without any network requests."""
    accounts: dict[str, tuple[int, tuple[str, ...]]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        host = _source_host(str(source.get("url") or ""))
        for url, strength in _text_account_clues(
            str(source.get("title") or ""), title=True
        ):
            handle = urlsplit(url).path.strip("/").split("/")[0].lower()
            old_strength, old_hosts = accounts.get(handle, (0, ()))
            new_hosts = (host,) if host else ()
            if strength > old_strength:
                accounts[handle] = (strength, new_hosts)
            elif strength == old_strength:
                accounts[handle] = (
                    strength,
                    tuple(dict.fromkeys((*old_hosts, *new_hosts))),
                )
    return accounts


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.link_labels: dict[str, str] = {}
        self.text: list[str] = []
        self.titles: list[str] = []
        self._title = False
        self._hidden = 0
        self._link_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self._hidden += 1
        if tag == "title":
            self._title = True
        if tag == "a" and values.get("href"):
            self.links.append((values["href"] or "", values.get("class") or ""))
            self._link_href = values["href"] or ""
        if tag == "meta" and values.get("property") == "og:title":
            self.titles.append(values.get("content") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title = False
        if tag == "a":
            self._link_href = ""
        if tag in {"script", "style"}:
            self._hidden = max(0, self._hidden - 1)

    def handle_data(self, data: str) -> None:
        if self._title:
            self.titles.append(data)
        if not self._hidden:
            self.text.append(data)
            if self._link_href:
                self.link_labels[self._link_href] = (
                    self.link_labels.get(self._link_href, "") + data
                )[:180]


def _nickname(title: str) -> str:
    title = re.sub(r"\s+", " ", unescape(title)).strip()
    # Only a delimited attribution or source-title prefix is a nickname lead.
    match = re.match(
        r"^(.{2,48}?)\s+(?:on\s+(?:X|Twitter)|在\s*(?:X|Twitter))\b", title, re.I
    )
    if not match:
        match = re.match(r"^(.{2,40}?)\s+[|｜–—-]\s+\S", title)
    if not match:
        return ""
    clue = match.group(1).strip(' "“”「」')
    if (
        not clue
        or "@" in clue
        or "http" in clue.lower()
        or clue.lower()
        in {"x", "twitter", "home", "login", "sign in", "photo", "image"}
    ):
        return ""
    return clue


async def _public_url(url: str) -> bool:
    """Reject local targets and any hostname resolving to a non-public address."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or parsed.hostname.lower().rstrip(".")
            in {"localhost", "localhost.localdomain"}
        ):
            return False
        host = parsed.hostname.rstrip(".")
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        async with asyncio.timeout(1.5):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        return bool(addresses) and all(
            ipaddress.ip_address(address[4][0]).is_global for address in addresses
        )
    except (ValueError, OSError, TimeoutError):
        return False


async def _fetch_page(client: AsyncClient, url: str) -> str:
    async with asyncio.timeout(5):
        for redirects in range(3):
            if not await _public_url(url):
                raise ValueError("unsafe source")
            async with client.stream(
                "GET", url, follow_redirects=False, timeout=5
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirects == 2 or not response.headers.get("location"):
                        raise ValueError("redirect limit")
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    t in content_type
                    for t in ("text/html", "text/plain", "application/xhtml")
                ):
                    return ""
                size = response.headers.get("content-length", "")
                if size.isdigit() and int(size) > _MAX_PAGE:
                    raise ValueError("source too large")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > _MAX_PAGE:
                        raise ValueError("source too large")
                    data.extend(chunk)
                return bytes(data).decode("utf-8", errors="replace")
    return ""


def _same_image(original: bytes, candidate: bytes) -> bool:
    if not original or not candidate:
        return False
    if original == candidate:
        return True
    try:
        with (
            Image.open(BytesIO(original)) as first,
            Image.open(BytesIO(candidate)) as second,
        ):
            if any(
                i.width * i.height > 20_000_000 or i.width < 8 or i.height < 8
                for i in (first, second)
            ):
                return False
            if (
                abs((first.width / first.height) / (second.width / second.height) - 1)
                > 0.015
            ):
                return False
            a = first.convert("RGB").resize((32, 32), Image.Resampling.LANCZOS)
            b = second.convert("RGB").resize((32, 32), Image.Resampling.LANCZOS)
            difference = ImageStat.Stat(ImageChops.difference(a, b)).mean
            if max(difference) > 7:
                return False
            hashes = []
            for image in (a, b):
                pixels = list(image.convert("L").resize((9, 8)).getdata())
                hashes.append(
                    [
                        pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                        for y in range(8)
                        for x in range(8)
                    ]
                )
            return sum(x != y for x, y in zip(*hashes, strict=True)) <= 3
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return False


def _media_allowed(url: str, api: Any) -> bool:
    """Media may come from Twitter's CDN or the configured Nitter pic route."""
    try:
        value = urlsplit(url)
        if value.scheme not in {"http", "https"} or value.username or value.password:
            return False
        if value.hostname == "pbs.twimg.com" and value.port in {None, 80, 443}:
            return True
        base = urlsplit(str(getattr(api, "nitter_url", "") or ""))
        return bool(
            base.hostname
            and (value.scheme, value.hostname, value.port)
            == (base.scheme, base.hostname, base.port)
            and value.path.startswith(base.path.rstrip("/") + "/pic/")
        )
    except ValueError:
        return False


async def _matching_media(image: bytes, images: Any, api: Any) -> bool:
    if not isinstance(images, (list, tuple)):
        return False
    for media_url in images[:3]:
        if not isinstance(media_url, str) or not _media_allowed(media_url, api):
            continue
        content = await api.download_media(media_url, timeout=4, max_bytes=_MAX_MEDIA)
        if (
            isinstance(content, bytes)
            and len(content) <= _MAX_MEDIA
            and _same_image(image, content)
        ):
            return True
    return False


async def matching_quote(image: bytes, tweet: dict, api: Any) -> dict | None:
    """Return a quoted post only after independently matching its own media."""
    if api is None or not isinstance(tweet, dict):
        return None
    quoted = tweet.get("quote")
    if not isinstance(quoted, dict):
        return None
    username = str(quoted.get("username") or "").lstrip("@")
    tweet_id = str(quoted.get("tweet_id") or "")
    if not canonical_x_url(f"https://x.com/{username}/status/{tweet_id}"):
        return None
    try:
        async with asyncio.timeout(5):
            if await _matching_media(image, quoted.get("images"), api):
                return quoted
    except Exception:
        return None
    return None


async def recover_x(
    image: bytes,
    sources: list[dict],
    twitter_api: Any | None = None,
    search_web: Callable[[str], Awaitable[list[dict]]] | None = None,
    proxy: str | None = None,
    timeout: float = 20,
    title_candidates: list[dict] | None = None,
    extra_sources: list[dict] | None = None,
) -> RecoveryReport:
    report = RecoveryReport()
    clues: list[str] = []
    seen: set[str] = set()
    checked_posts: set[str] = set()
    timeline_done = False
    clue_hosts: dict[str, tuple[str, ...]] = {}

    def add(
        url: str,
        title: str,
        evidence: str,
        strength: int = 0,
        hosts: tuple[str, ...] = (),
        account_confirmed: bool = False,
    ) -> None:
        canonical = canonical_x_url(url)
        if not canonical:
            return
        existing = next(
            (
                candidate
                for candidate in report.candidates
                if candidate.url.lower() == canonical.lower()
            ),
            None,
        )
        if existing is not None:
            _merge_candidate(
                existing,
                XCandidate(
                    canonical,
                    title=title[:180],
                    evidence=evidence,
                    clue_strength=strength,
                    source_hosts=hosts,
                    account_confirmed=account_confirmed,
                ),
            )
            return
        if len(report.candidates) >= 8:
            # Reserve at most three verification slots without allowing a flood
            # of keyword posts to displace explicit account attribution.
            replaceable = [
                index
                for index, candidate in enumerate(report.candidates)
                if not candidate.verified
            ]
            post_count = sum(
                "/status/" in candidate.url for candidate in report.candidates
            )
            profile_slots = [
                index
                for index in replaceable
                if "/status/" not in report.candidates[index].url
            ]
            reserve_post_slot = (
                "/status/" in canonical and post_count < 3 and bool(profile_slots)
            )
            pool = profile_slots if reserve_post_slot else replaceable
            if not pool:
                return
            index = min(
                pool,
                key=lambda item: (
                    report.candidates[item].clue_strength,
                    report.candidates[item].account_confirmed,
                    -item,
                ),
            )
            if not reserve_post_slot and (strength, account_confirmed) <= (
                report.candidates[index].clue_strength,
                report.candidates[index].account_confirmed,
            ):
                return
            removed = report.candidates.pop(index)
            seen.discard(removed.url.lower())
        seen.add(canonical.lower())
        account = urlsplit(canonical).path.split("/")[1]
        report.candidates.append(
            XCandidate(
                canonical,
                f"@{account}",
                title=title[:180],
                evidence=evidence,
                clue_strength=strength,
                source_hosts=tuple(dict.fromkeys(hosts)),
                account_confirmed=account_confirmed,
            )
        )

    def extract(
        text: str, evidence: str, *, host: str = "", title: bool = False, weak: bool = False
    ) -> None:
        text = unescape(str(text))[:32_000]
        hosts = (host,) if host else ()
        for url, strength in _text_account_clues(text, title=title):
            # A visually similar page is only a lead: cap its weight at mention level.
            add(url, text[:180], evidence, min(strength, 1) if weak else strength, hosts)
        for handle in _HANDLE.findall(text)[:3]:
            if handle.lower() not in _RESERVED:
                clue_hosts[handle] = tuple(
                    dict.fromkeys((*clue_hosts.get(handle, ()), *hosts))
                )
                if len(clues) < 2 and handle.lower() not in {c.lower() for c in clues}:
                    clues.append(handle)
        nickname = _nickname(text)
        if nickname:
            clue_hosts[nickname] = tuple(
                dict.fromkeys((*clue_hosts.get(nickname, ()), *hosts))
            )
        if nickname and len(clues) < 2 and nickname not in clues:
            clues.append(nickname)

    async def keyword_search(client: AsyncClient, queried: set[str]) -> None:
        for clue in clues[:2]:
            if clue in queried:
                continue
            queried.add(clue)
            found_native = False
            nitter = str(getattr(twitter_api, "nitter_url", "") or "").rstrip("/")
            if nitter:
                try:
                    # The deployment's configured Nitter origin is trusted configuration.
                    # Do not follow its redirects or borrow/mutate the API's HTTP client.
                    async with asyncio.timeout(5):
                        async with client.stream(
                            "GET",
                            nitter + "/search",
                            params={"f": "tweets", "q": clue},
                            follow_redirects=False,
                            timeout=5,
                        ) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes(
                                chunk_size=64 * 1024
                            ):
                                if len(body) + len(chunk) > _MAX_PAGE:
                                    raise ValueError("search too large")
                                body.extend(chunk)
                    page = _Page()
                    page.feed(bytes(body).decode("utf-8", errors="replace"))
                    for href, classes in page.links[:500]:
                        if not ({"tweet-link", "username"} & set(classes.split())):
                            continue
                        parsed = urlsplit(urljoin(nitter + "/", href))
                        origin = urlsplit(nitter)
                        if (parsed.scheme, parsed.netloc) != (
                            origin.scheme,
                            origin.netloc,
                        ):
                            continue
                        canonical = canonical_x_url("https://x.com" + parsed.path)
                        if canonical:
                            add(
                                canonical,
                                clue,
                                "精确匹配来源线索经 X 关键词搜索所得（待核实）",
                                hosts=clue_hosts.get(clue, ()),
                            )
                            found_native = True
                except Exception:
                    report.errors["x_search"] = "X 关键词搜索暂不可用"
            if not found_native and search_web is not None:
                try:
                    async with asyncio.timeout(5):
                        results = await search_web(clue)
                    for result in results[:12]:
                        if isinstance(result, dict):
                            add(
                                str(result.get("link") or result.get("url") or ""),
                                str(result.get("title") or clue),
                                "精确匹配来源线索经 X 网页搜索所得（待核实）",
                                hosts=clue_hosts.get(clue, ()),
                            )
                except Exception:
                    report.errors["x_search"] = "X 网页搜索暂不可用"

    async def verify() -> None:
        nonlocal timeline_done
        if twitter_api is None or not getattr(twitter_api, "is_ready", True):
            return
        posts = [
            candidate for candidate in report.candidates if "/status/" in candidate.url
        ]
        if len(posts) < 3 and not timeline_done and len(checked_posts) < 3:
            profile = next(
                (
                    candidate
                    for candidate in report.candidates
                    if "/status/" not in candidate.url
                ),
                None,
            )
            if profile is not None and hasattr(twitter_api, "get_user_timeline_items"):
                timeline_done = True
                try:
                    async with asyncio.timeout(5):
                        timeline = await twitter_api.get_user_timeline_items(
                            profile.account[1:], since_id="", limit=3
                        )
                    for item in timeline[:3]:
                        if not isinstance(item, dict):
                            continue
                        tweet_id = str(item.get("tweet_id") or "")
                        username = str(
                            item.get("username") or profile.account[1:]
                        ).lstrip("@")
                        if tweet_id.isdigit():
                            add(
                                f"https://x.com/{username}/status/{tweet_id}",
                                profile.title,
                                "来源所列账号的近期帖子或转帖（待核实）",
                                hosts=profile.source_hosts,
                            )
                    posts = [
                        candidate
                        for candidate in report.candidates
                        if "/status/" in candidate.url
                    ]
                except Exception:
                    report.errors["x_timeline"] = "X 账号近期帖子暂不可用"
        for candidate in posts:
            if len(checked_posts) >= 3:
                break
            try:
                parts = urlsplit(candidate.url).path.strip("/").split("/")
                if parts[2] in checked_posts:
                    continue
                checked_posts.add(parts[2])
                async with asyncio.timeout(5):
                    tweet = await twitter_api.get_tweet(parts[0], parts[2])
                    if not isinstance(tweet, dict) or not tweet.get("status"):
                        continue
                    # A status ID can resolve even when the supplied path has
                    # the wrong author. Require the response identity before
                    # attributing matching media to an account.
                    tweet_id = str(tweet.get("tweet_id") or "")
                    username = str(tweet.get("username") or "").lstrip("@")
                    actual_url = canonical_x_url(
                        f"https://x.com/{username}/status/{tweet_id}"
                    )
                    if tweet_id != parts[2] or not actual_url:
                        continue
                    if username.lower() != parts[0].lower():
                        candidate.clue_strength = 0
                        candidate.source_hosts = ()
                    seen.discard(candidate.url.lower())
                    candidate.url = actual_url
                    candidate.account = f"@{username}"
                    seen.add(actual_url.lower())
                    candidate.name = str(tweet.get("screen_name") or "")[:100]
                    quoted = await matching_quote(image, tweet, twitter_api)
                    if quoted is not None:
                        quote_username = str(quoted["username"]).lstrip("@")
                        quote_url = canonical_x_url(
                            f"https://x.com/{quote_username}/status/{quoted['tweet_id']}"
                        )
                        seen.discard(candidate.url.lower())
                        candidate.url = quote_url
                        candidate.account = f"@{quote_username}"
                        candidate.name = str(
                            quoted.get("author") or quoted.get("screen_name") or ""
                        )[:100]
                        candidate.clue_strength = 0
                        candidate.source_hosts = ()
                        candidate.verified = True
                        candidate.relation_verified = True
                        candidate.evidence = "引用来源图像比对吻合"
                        seen.add(quote_url.lower())
                    elif await _matching_media(image, tweet.get("images"), twitter_api):
                        candidate.verified = True
                        candidate.evidence = "图像比对吻合"
            except Exception:
                report.errors["x_verify"] = "X 帖子图像核实暂不可用"

    async def query_title_accounts(platform: str) -> None:
        if twitter_api is not None and getattr(twitter_api, "is_ready", False):
            for item in (title_candidates or [])[:3]:
                if item.get("platform") != platform:
                    continue
                handle = str(item.get("handle") or "")
                url = canonical_x_url(f"https://x.com/{handle}")
                if not url:
                    continue
                report.title_accounts_checked += 1
                try:
                    profile = await asyncio.wait_for(
                        twitter_api.get_user_info(handle), timeout=3
                    )
                    if (
                        not isinstance(profile, dict)
                        or not profile.get("status")
                        or str(profile.get("user_name") or "").lstrip("@").casefold()
                        != handle.casefold()
                    ):
                        continue
                    report.title_accounts_found += 1
                    host = _source_host(str(item.get("source_url") or ""))
                    add(
                        url,
                        "",
                        "来源文字明示的 X 账号，已查询存在（同图未确认）"
                        if platform == "X"
                        else "来源文字的平台未确定候选，已查询到同名 X 账号（同图未确认）",
                        strength=1 if platform == "X" else 0,
                        hosts=(host,) if host else (),
                        account_confirmed=True,
                    )
                    candidate = next(
                        (
                            c
                            for c in report.candidates
                            if c.url.casefold() == url.casefold()
                        ),
                        None,
                    )
                    if candidate is not None:
                        candidate.name = str(profile.get("screen_name") or "")[:100]
                except Exception:
                    report.errors["title_account"] = "部分文字候选账号查询未完成"

    try:
        async with asyncio.timeout(max(0.01, timeout)):
            for source in sources[:10]:
                if isinstance(source, dict):
                    host = _source_host(str(source.get("url") or ""))
                    extract(
                        str(source.get("title") or ""),
                        "精确匹配来源标题明示的 X 线索（待核实）",
                        host=host,
                        title=True,
                    )
                    add(
                        str(source.get("url") or ""),
                        str(source.get("title") or ""),
                        "精确匹配来源明示的 X 链接（待核实）",
                        strength=2 if "/status/" in str(source.get("url") or "") else 1,
                        hosts=(host,) if host else (),
                    )
            for source in (extra_sources or [])[:6]:
                if not isinstance(source, dict):
                    continue
                host = _source_host(str(source.get("url") or ""))
                extract(
                    str(source.get("title") or ""),
                    "相似图来源标题明示的 X 线索（待核实）",
                    host=host,
                    title=True,
                    weak=True,
                )
                add(
                    str(source.get("url") or ""),
                    str(source.get("title") or ""),
                    "相似图来源明示的 X 链接（待核实）",
                    strength=1,
                    hosts=(host,) if host else (),
                )
            await query_title_accounts("X")
            async with AsyncClient(
                proxy=proxy, trust_env=False, timeout=5, follow_redirects=False
            ) as client:
                queried: set[str] = set()
                await keyword_search(client, queried)
                await verify()
                if not any(candidate.verified for candidate in report.candidates):
                    fetched_hosts: set[str] = set()
                    scan_queue = [(item, False) for item in sources[:10]] + [
                        (item, True) for item in (extra_sources or [])[:6]
                    ]
                    for source, weak in scan_queue:
                        if (
                            not isinstance(source, dict)
                            or not source.get("url")
                            or canonical_x_url(str(source["url"]))
                        ):
                            continue
                        try:
                            parsed_source = urlsplit(str(source["url"]))
                            host = (parsed_source.hostname or "").lower().rstrip(".")
                        except ValueError:
                            continue
                        if (
                            parsed_source.scheme not in {"http", "https"}
                            or not host
                            or host in fetched_hosts
                        ):
                            continue
                        if len(fetched_hosts) >= 3:
                            break
                        fetched_hosts.add(host)
                        report.source_pages[host] = "started"
                        try:
                            previous_urls = {
                                candidate.url for candidate in report.candidates
                            }
                            html = await _fetch_page(client, str(source["url"]))
                            report.source_pages[host] = "read" if html else "empty"
                            page = _Page()
                            page.feed(html)
                            for href, _ in page.links[:500]:
                                linked_url = canonical_x_url(
                                    urljoin(str(source["url"]), href)
                                )
                                strength = 2 if "/status/" in linked_url else 1
                                if not weak and _ATTRIBUTION_LABEL.search(
                                    page.link_labels.get(href, "")
                                ):
                                    strength = 3
                                add(
                                    linked_url,
                                    "",
                                    "相似图来源页面明示的 X 链接（待核实）"
                                    if weak
                                    else "精确匹配来源页面明示的 X 链接（待核实）",
                                    strength=strength if not weak else 1,
                                    hosts=(host,),
                                )
                            for title in page.titles[:3]:
                                extract(
                                    title,
                                    "相似图来源页面标题明示的 X 线索（待核实）"
                                    if weak
                                    else "精确匹配来源页面标题明示的 X 线索（待核实）",
                                    host=host,
                                    title=True,
                                    weak=weak,
                                )
                            extract(
                                " ".join(page.text)[:32_000],
                                "相似图来源页面明示的 X 账号线索（待核实）"
                                if weak
                                else "精确匹配来源页面明示的 X 账号线索（待核实）",
                                host=host,
                                weak=weak,
                            )
                            if any(
                                "/status/" in candidate.url
                                and candidate.url not in previous_urls
                                for candidate in report.candidates
                            ):
                                break
                        except Exception as exc:
                            report.source_pages[host] = (
                                f"HTTP {exc.response.status_code}"
                                if isinstance(exc, HTTPStatusError)
                                else type(exc).__name__
                            )
                            report.errors["source_page"] = (
                                "部分精确匹配来源页面无法读取"
                            )
                    await keyword_search(client, queried)
                    await verify()
                if not any(candidate.verified for candidate in report.candidates):
                    await query_title_accounts("unknown")
                    await verify()
    except TimeoutError:
        report.source_pages = {
            host: "timeout" if status == "started" else status
            for host, status in report.source_pages.items()
        }
        report.errors["x_recovery"] = "X 线索查找超时"
    unique: dict[str, XCandidate] = {}
    for candidate in report.candidates:
        key = candidate.url.lower()
        if key in unique:
            _merge_candidate(unique[key], candidate)
        else:
            unique[key] = candidate
    report.candidates = list(unique.values())
    if not report.candidates and clues:
        report.candidates.append(
            XCandidate(
                "https://x.com/search?q=" + quote(clues[0], safe=""),
                title=clues[0],
                evidence="仅有精确匹配来源昵称线索，可继续在 X 搜索（待核实）",
            )
        )
    return report
