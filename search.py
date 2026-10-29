"""Reverse image search and conservative extraction of public source accounts."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from httpx2 import AsyncClient
from PicImageSearch import Ascii2D, BaiDu, GoogleLens, Network, SauceNAO
from pyquery import PyQuery

if __package__:
    from .google_api import GoogleAPIError, search_serpapi, search_x_serpapi
    from .x_recovery import (
        canonical_x_url,
        collect_account_clues,
        matching_quote,
        recover_x,
    )
else:
    from google_api import GoogleAPIError, search_serpapi, search_x_serpapi
    from x_recovery import (
        canonical_x_url,
        collect_account_clues,
        matching_quote,
        recover_x,
    )

UNKNOWN_ACCOUNT = "未提供"
TITLE_LLM_TIMEOUT = 30
logger = logging.getLogger(__name__)
_ENGINES = ("google_lens", "baidu", "ascii2d", "saucenao")
_X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
_WEIBO_HOSTS = {
    "weibo.com",
    "www.weibo.com",
    "m.weibo.com",
    "weibo.cn",
    "www.weibo.cn",
    "m.weibo.cn",
}
_BILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "space.bilibili.com",
    "t.bilibili.com",
}
_X_RESERVED = {
    "home",
    "explore",
    "search",
    "i",
    "intent",
    "hashtag",
    "share",
    "settings",
    "messages",
    "notifications",
    "login",
    "signup",
}


@dataclass(frozen=True)
class SearchResult:
    platform: str = "其他"
    account: str = UNKNOWN_ACCOUNT
    name: str = ""
    url: str = ""
    title: str = ""
    engine: str = ""
    match_type: str = "visual"
    verified: bool = False
    clue_strength: int = 0
    source_hosts: tuple[str, ...] = ()
    relation_verified: bool = False
    selection_reason: str = ""
    source: str = ""
    account_confirmed: bool = False
    followers: int | None = None


@dataclass
class SearchReport:
    results: list[SearchResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    ambiguous: bool = False
    # Exact image-match pages are metadata, separate from selected X sources.
    exact_sources: list[SearchResult] = field(default_factory=list)
    engine_results: dict[str, int] = field(default_factory=dict)
    source_pages: dict[str, str] = field(default_factory=dict)
    x_candidate_count: int = 0
    title_llm: dict[str, str | int] = field(default_factory=dict)


class _EngineUnavailable(RuntimeError):
    pass


def public_url(value: Any) -> str:
    """Accept public HTTP links, excluding credentials, local hosts and IP ranges."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
        ):
            return ""
        if any(c in value for c in "\r\n\\") or host.endswith(
            (".localhost", ".local", ".internal")
        ):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
                return ""
        parsed.port  # Reject invalid ports.
        return urlunsplit(
            (parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
        )
    except (TypeError, ValueError):
        return ""


def _platform_account(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if host in _X_HOSTS:
        if (
            parts
            and parts[0].lower() == "intent"
            and len(parts) > 1
            and parts[1] == "user"
        ):
            uid = parse_qs(parsed.query).get("user_id", [""])[0]
            return "X", f"UID {uid}" if uid.isdigit() else UNKNOWN_ACCOUNT
        if (
            parts
            and parts[0].lower() not in _X_RESERVED
            and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0])
        ):
            return "X", f"@{parts[0]}"
        return "X", UNKNOWN_ACCOUNT
    if host in _WEIBO_HOSTS:
        if len(parts) >= 2 and parts[0] in {"u", "profile"} and parts[1].isdigit():
            return "微博", f"UID {parts[1]}"
        if len(parts) >= 2 and parts[0] == "n":
            return "微博", f"@{parts[1]}"
        if parts and parts[0].isdigit() and host != "m.weibo.cn":
            return "微博", f"UID {parts[0]}"
        if (
            host in {"weibo.com", "www.weibo.com"}
            and len(parts) == 1
            and parts[0] not in {"login", "signup", "newlogin", "tv", "hot", "search"}
        ):
            return "微博", f"@{parts[0]}"
        return "微博", UNKNOWN_ACCOUNT
    if host in _BILI_HOSTS:
        if host == "space.bilibili.com" and parts and parts[0].isdigit():
            return "哔哩哔哩", f"UID {parts[0]}"
        return "哔哩哔哩", UNKNOWN_ACCOUNT
    return "其他", UNKNOWN_ACCOUNT


def normalize_url(value: Any) -> str:
    url = public_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/") or "/"
    if host in _X_HOSTS:
        # A status ID identifies the same post across usernames and Twitter/X domains.
        status = re.search(r"/(?:status|statuses)/(\d+)(?:/|$)", path)
        if status:
            prefix = path.split("/")[1]
            author = (
                prefix
                if prefix.lower() not in _X_RESERVED
                and re.fullmatch(r"[A-Za-z0-9_]{1,15}", prefix)
                else "i"
            )
            return f"https://x.com/{author}/status/{status[1]}"
        return urlunsplit(
            (
                "https",
                "x.com",
                path,
                parsed.query if path.startswith("/intent/") else "",
                "",
            )
        )
    if host in _WEIBO_HOSTS or host in _BILI_HOSTS:
        return urlunsplit(("https", parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _result_key(result: SearchResult) -> str:
    if result.platform == "X" and (match := re.search(r"/status/(\d+)", result.url)):
        return f"x:status:{match[1]}"
    return result.url


def _rank(result: SearchResult) -> tuple[int, ...]:
    return (
        int(result.verified),
        int(result.platform == "X" and bool(re.search(r"/status/\d+", result.url))),
        int(result.relation_verified),
        result.clue_strength,
        len(set(result.source_hosts)),
        int(result.engine == "google_lens"),
    )


def choose_best(results: list[SearchResult]) -> tuple[SearchResult | None, bool]:
    """Pick one evidenced source, or a uniquely supported account clue."""
    candidates = [item for item in results if item.platform == "X"]
    confirmed = [item for item in candidates if item.verified]
    if confirmed:
        best = max(confirmed, key=_rank)
        reason = (
            "引用关系及被引用帖配图已核实"
            if best.relation_verified
            else "同图匹配；来源明确署名与该账号一致"
            if best.clue_strength >= 3
            else "同图匹配；优先返回具体帖子"
            if "/status/" in best.url
            and any("/status/" not in item.url for item in confirmed)
            else "Google 完全匹配；同级按引擎返回顺序选择"
            if best.engine == "google_lens"
            else "帖子配图比对吻合"
            if best.match_type == "recovered"
            else "引擎提供同图匹配证据"
        )
        return replace(best, selection_reason=best.selection_reason or reason), False
    groups: dict[str, list[SearchResult]] = {}
    for item in candidates:
        account = _platform_account(item.url)[1]
        if (item.clue_strength > 0 or item.account_confirmed) and account.startswith(
            "@"
        ):
            groups.setdefault(account.casefold(), []).append(item)
    if not groups:
        return None, False
    counted = [
        item
        for items in groups.values()
        for item in items
        if item.followers is not None
    ]
    if counted:
        best = max(
            counted,
            key=lambda item: (
                item.followers,
                item.clue_strength,
                len(set(item.source_hosts)),
            ),
        )
        return replace(
            best,
            selection_reason=f"未确认同图；在已取得关注者数的候选中最多（{best.followers:,}）",
        ), False
    ranked = []
    for items in groups.values():
        strength = max(item.clue_strength for item in items)
        hosts = {
            host
            for item in items
            if item.clue_strength == strength
            for host in item.source_hosts
        }
        ranked.append(
            (
                (strength, len(hosts)),
                max(
                    items,
                    key=lambda item: (
                        item.clue_strength,
                        len(set(item.source_hosts)),
                        "/status/" in item.url,
                    ),
                ),
            )
        )
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None, True
    best = ranked[0][1]
    reason = {
        0: "来源文字候选在 X 存在同名账号，尚未确认同图",
        3: "同图来源明确署名的账号，尚未确认同图",
        2: "同图来源直接提供的帖子链接，尚未确认同图",
    }.get(best.clue_strength, "同图来源明确提及的账号，尚未确认同图")
    return replace(best, selection_reason=reason), False


def _merge_result(first: SearchResult, second: SearchResult) -> SearchResult:
    """Keep stronger match evidence and compatible metadata for the same source."""
    winner, other = (second, first) if _rank(second) > _rank(first) else (first, second)
    winner_known = bool(winner.account and winner.account != UNKNOWN_ACCOUNT)
    other_known = bool(other.account and other.account != UNKNOWN_ACCOUNT)
    if (
        winner_known
        and other_known
        and winner.account.casefold() != other.account.casefold()
    ):
        return winner
    account = winner.account if winner_known else other.account
    url = winner.url
    if (
        _platform_account(url)[1] == UNKNOWN_ACCOUNT
        and _platform_account(other.url)[1] != UNKNOWN_ACCOUNT
    ):
        url = other.url
    strength = max(winner.clue_strength, other.clue_strength)
    hosts = tuple(
        sorted(
            {
                host
                for item in (winner, other)
                if item.clue_strength == strength
                for host in item.source_hosts
            }
        )
    )
    return replace(
        winner,
        account=account,
        name=winner.name or other.name,
        url=url,
        clue_strength=strength,
        source_hosts=hosts,
        account_confirmed=first.account_confirmed or second.account_confirmed,
        followers=winner.followers if winner.followers is not None else other.followers,
    )


def _make_result(
    item: Any, url: str, engine: str, match_type: str
) -> SearchResult | None:
    url = normalize_url(url)
    if not url:
        return None
    platform, account = _platform_account(url)
    author_url = public_url(getattr(item, "author_url", ""))
    if account == UNKNOWN_ACCOUNT and author_url:
        author_platform, author_account = _platform_account(author_url)
        if author_platform == platform:
            account = author_account
    return SearchResult(
        platform=platform,
        account=account,
        name=str(getattr(item, "author", "") or "")[:100],
        url=url,
        title=str(getattr(item, "title", "") or "")[:4096],
        engine=engine,
        match_type=match_type,
        verified=match_type == "exact",
        source=str(getattr(item, "source", "") or "")[:120],
    )


def parse_results(
    response: Any, engine: str, match_type: str = "visual"
) -> list[SearchResult]:
    """Flatten engine result links without inferring identities from images or titles."""
    results: list[SearchResult] = []
    groups = [(getattr(response, "raw", []), match_type)]
    if engine == "baidu":
        groups.insert(0, (getattr(response, "exact_matches", []), "exact"))
    for items, kind in groups:
        for item in items:
            links = [getattr(item, "url", "")]
            # Ascii2D includes author profile links in url_list: keep its primary
            # post when present and only use the list when the primary is absent.
            if not links[0] and engine == "ascii2d":
                links.extend(
                    getattr(link, "href", "") for link in getattr(item, "url_list", [])
                )
            if engine == "saucenao":
                if float(getattr(item, "similarity", 0) or 0) < 60:
                    continue
                links.extend(getattr(item, "ext_urls", []))
            for url in links:
                result = _make_result(item, url, engine, kind)
                if result:
                    results.append(result)
    return results


async def _check_response(response: Any) -> None:
    if response.status_code >= 400:
        raise _EngineUnavailable(f"搜索服务返回 HTTP {response.status_code}")
    if "/sorry/" in str(response.url) or "/captcha" in str(response.url):
        raise _EngineUnavailable("搜索服务要求验证码")
    await response.aread()
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        page = response.text.lower()
        host = urlsplit(str(response.url)).hostname
        if host in {"google.com", "www.google.com", "lens.google.com"}:
            if "retry/enablejs" in page and len(PyQuery(response.text)("a")) <= 5:
                raise _EngineUnavailable(
                    "Google 要求 JavaScript 验证，服务器未取得搜索结果；请检查 Google 访问环境"
                )
            if any(
                marker in page
                for marker in (
                    'id="captcha-form"',
                    'id="recaptcha"',
                    "our systems have detected unusual traffic",
                )
            ):
                raise _EngineUnavailable("Google 要求人机验证，服务器未取得搜索结果")
        if any(
            marker in page
            for marker in (
                "<title>just a moment",
                "<title>百度安全验证",
                "<title>安全验证",
                "<title>captcha",
            )
        ):
            raise _EngineUnavailable("搜索服务要求验证码或安全验证")


class SearchService:
    def __init__(self, config: Mapping[str, Any], twitter_api_provider=None):
        selected = config.get("engines", ["google_lens", "baidu", "ascii2d"])
        if isinstance(selected, str):
            selected = [part.strip() for part in selected.split(",") if part.strip()]
        self.engines = list(dict.fromkeys(selected))
        self.api_key = str(config.get("saucenao_api_key", "") or "").strip()
        self.serpapi_api_key = str(config.get("serpapi_api_key", "") or "").strip()
        if self.api_key and "saucenao" not in self.engines:
            self.engines.append("saucenao")
        self.proxy = str(config.get("proxy", "") or "").strip() or None
        self.timeout = max(1.0, float(config.get("search_timeout", 25)))
        self.x_lookup_timeout = max(
            5, min(30, float(config.get("x_lookup_timeout", 20)))
        )
        self.twitter_api_provider = twitter_api_provider
        self._semaphore = asyncio.Semaphore(3)

    async def _search_engine(self, engine: str, image: bytes) -> list[SearchResult]:
        if engine not in _ENGINES:
            raise _EngineUnavailable("不支持的搜索引擎")
        if engine == "saucenao" and not self.api_key:
            raise _EngineUnavailable("未配置 SauceNAO API key")
        async with self._semaphore:
            if engine == "google_lens" and self.serpapi_api_key:
                try:
                    payload = await search_serpapi(
                        image, self.serpapi_api_key, self.timeout, self.proxy
                    )
                except GoogleAPIError as exc:
                    raise _EngineUnavailable(str(exc)) from exc
                results = []
                for item in payload.get("exact_matches", []):
                    result = _make_result(
                        SimpleNamespace(
                            title=item.get("title", ""), source=item.get("source", "")
                        ),
                        item.get("link", ""),
                        engine,
                        "exact",
                    )
                    if result:
                        results.append(result)
                return results
            async with Network(proxies=self.proxy, timeout=self.timeout) as client:
                client.event_hooks["response"].append(_check_response)
                if engine == "google_lens":
                    exact = await GoogleLens(
                        client=client, search_type="exact_matches"
                    ).search(file=image)
                    return parse_results(exact, engine, "exact")
                if engine == "baidu":
                    return parse_results(
                        await BaiDu(client=client).search(file=image), engine
                    )
                if engine == "ascii2d":
                    return parse_results(
                        await Ascii2D(client=client).search(file=image), engine
                    )
                response = await SauceNAO(
                    client=client, api_key=self.api_key, numres=10, minsim=60
                ).search(file=image)
                if response.status is not None and response.status < 0:
                    raise _EngineUnavailable(
                        "SauceNAO 拒绝请求，请检查 API key 或查询额度"
                    )
                return parse_results(response, engine, "similarity")

    async def _source_account(
        self, result: SearchResult, image: bytes | None = None
    ) -> SearchResult:
        """Read author metadata using the borrowed Twitter API or official endpoints."""
        if result.account != UNKNOWN_ACCOUNT and result.name and image is None:
            return result
        url = public_url(result.url)
        if not url:
            return result
        parsed = urlsplit(url)
        endpoint = ""
        params: dict[str, str] = {}
        kind = ""
        if parsed.hostname in _X_HOSTS:
            if status := re.search(r"/status/(\d+)(?:/|$)", parsed.path):
                try:
                    api = (
                        self.twitter_api_provider()
                        if self.twitter_api_provider
                        else None
                    )
                    if api is not None:
                        username = parsed.path.strip("/").split("/")[0]
                        tweet = await asyncio.wait_for(
                            api.get_tweet(username, status[1]), timeout=4
                        )
                        author = str(tweet.get("username") or "").lstrip("@")
                        actual = canonical_x_url(
                            f"https://x.com/{author}/status/{status[1]}"
                        )
                        if (
                            tweet.get("status")
                            and str(tweet.get("tweet_id")) == status[1]
                            and actual
                        ):
                            result = replace(
                                result,
                                url=actual,
                                account=f"@{author}",
                                name=str(tweet.get("screen_name") or "")[:100],
                                clue_strength=result.clue_strength
                                if _platform_account(result.url)[1].casefold()
                                == f"@{author}".casefold()
                                else 0,
                                source_hosts=result.source_hosts
                                if _platform_account(result.url)[1].casefold()
                                == f"@{author}".casefold()
                                else (),
                            )
                            if image is not None and tweet.get("quote"):
                                try:
                                    quote = await asyncio.wait_for(
                                        matching_quote(image, tweet, api), timeout=1.5
                                    )
                                    if quote:
                                        quote_author = str(quote["username"]).lstrip(
                                            "@"
                                        )
                                        return replace(
                                            result,
                                            url=canonical_x_url(
                                                f"https://x.com/{quote_author}/status/{quote['tweet_id']}"
                                            ),
                                            account=f"@{quote_author}",
                                            name=str(
                                                quote.get("author")
                                                or quote.get("screen_name")
                                                or ""
                                            )[:100],
                                            verified=True,
                                            match_type="recovered",
                                            relation_verified=True,
                                            clue_strength=0,
                                            source_hosts=(),
                                            selection_reason="引用关系及被引用帖配图已核实",
                                        )
                                except Exception:
                                    pass
                            return result
                except Exception:
                    pass
                endpoint = "https://publish.x.com/oembed"
                params = {
                    "url": f"https://twitter.com/i/status/{status[1]}",
                    "omit_script": "true",
                }
                kind = "x"
        elif parsed.hostname in _BILI_HOSTS:
            if video := re.search(r"/video/(BV[A-Za-z0-9]{10})(?:/|$)", parsed.path):
                endpoint = "https://api.bilibili.com/x/web-interface/view"
                params = {"bvid": video[1]}
                kind = "video"
            elif article := re.search(r"/read/cv(\d+)(?:/|$)", parsed.path):
                endpoint = "https://api.bilibili.com/x/article/viewinfo"
                params = {"id": article[1]}
                kind = "article"
        if not endpoint:
            return result
        try:
            async with Network(
                proxies=self.proxy, timeout=min(6, self.timeout)
            ) as client:
                response = await client.get(
                    endpoint,
                    params=params,
                    follow_redirects=False,
                )
                response.raise_for_status()
                payload = response.json()
                if kind == "x":
                    author_url = public_url(payload.get("author_url", ""))
                    platform, account = _platform_account(author_url)
                    if platform == "X" and account != UNKNOWN_ACCOUNT:
                        same_author = (
                            _platform_account(result.url)[1].casefold()
                            == account.casefold()
                        )
                        return replace(
                            result,
                            account=account,
                            name=str(payload.get("author_name", "") or "")[:100],
                            url=canonical_x_url(
                                f"https://x.com/{account.lstrip('@')}/status/{status[1]}"
                            )
                            or result.url,
                            clue_strength=result.clue_strength if same_author else 0,
                            source_hosts=result.source_hosts if same_author else (),
                        )
                    return result
                data = payload.get("data", {}) if payload.get("code") == 0 else {}
                owner = data.get("owner", {}) if kind == "video" else data
                mid = str(owner.get("mid", ""))
                if mid.isdigit():
                    return replace(
                        result,
                        account=f"UID {mid}",
                        name=str(
                            owner.get("name" if kind == "video" else "author_name", "")
                            or ""
                        )[:100],
                    )
        except Exception:
            # Missing account metadata must not discard an otherwise usable source.
            pass
        return result

    async def search(
        self,
        image: bytes,
        title_extractor: Callable[[list[dict]], Awaitable[list[dict]]] | None = None,
    ) -> SearchReport:
        report = SearchReport()
        if not image:
            report.errors["image"] = "图片内容为空"
            return report

        async def run(engine: str) -> tuple[str, list[SearchResult], str]:
            try:
                results = await asyncio.wait_for(
                    self._search_engine(engine, image), timeout=self.timeout
                )
                return engine, results, ""
            except TimeoutError:
                return engine, [], "搜索超时"
            except _EngineUnavailable as exc:
                return engine, [], str(exc)
            except Exception:
                # Exception strings can contain proxy credentials or SauceNAO URLs.
                return engine, [], "搜索请求或结果解析失败"

        collected = await asyncio.gather(*(run(engine) for engine in self.engines))
        unique: dict[str, SearchResult] = {}
        for engine, results, error in collected:
            report.engine_results[engine] = len(results)
            if error:
                report.errors[engine] = error
            for result in results:
                key = _result_key(result)
                unique[key] = (
                    _merge_result(unique[key], result) if key in unique else result
                )
        report.exact_sources = [item for item in unique.values() if item.verified]
        exact_sources = [
            {"url": item.url, "title": item.title}
            for item in unique.values()
            if item.verified and item.platform != "X"
        ]
        account_clues = collect_account_clues(exact_sources)

        def with_clues(result: SearchResult) -> SearchResult:
            # An author's signature supports the account; it does not make an
            # arbitrary keyword/timeline post a source for this image.
            if not result.verified:
                return result
            account = result.account
            if account == UNKNOWN_ACCOUNT:
                account = _platform_account(result.url)[1]
            strength, hosts = account_clues.get(account.lstrip("@").casefold(), (0, ()))
            if strength > result.clue_strength:
                return replace(result, clue_strength=strength, source_hosts=hosts)
            if strength == result.clue_strength and strength:
                return replace(
                    result,
                    source_hosts=tuple(sorted(set(hosts) | set(result.source_hosts))),
                )
            return result

        exact_x = [
            with_clues(item)
            for item in unique.values()
            if item.platform == "X" and item.verified
        ]
        # Inspect a bounded shortlist before selecting the single displayed source.
        chosen = sorted(exact_x, key=_rank, reverse=True)[:3]
        if not chosen:
            source_items = sorted(
                (
                    item
                    for item in unique.values()
                    if item.verified and item.platform != "X"
                ),
                key=lambda item: bool(
                    re.search(
                        r"(?<![A-Za-z0-9_@])@[A-Za-z0-9_]{1,15}(?![A-Za-z0-9_])",
                        item.title,
                    )
                ),
                reverse=True,
            )
            sources = []
            source_hosts = set()
            for item in source_items:
                host = urlsplit(item.url).hostname
                if host not in source_hosts:
                    source_hosts.add(host)
                    sources.append({"url": item.url, "title": item.title})
                    if item.source:
                        sources[-1]["source"] = item.source
                    if len(sources) == 6:
                        break
            if sources:
                title_candidates = []
                if title_extractor is not None:
                    try:
                        extracted = await asyncio.wait_for(
                            title_extractor(sources), TITLE_LLM_TIMEOUT
                        )
                        title_candidates = [
                            item
                            for item in extracted
                            if item["platform"] in {"X", "unknown"}
                        ][:3]
                        report.title_llm = {
                            "status": "completed",
                            "returned": len(extracted),
                            "eligible": len(title_candidates),
                            "unknown": sum(
                                item["platform"] == "unknown" for item in extracted
                            ),
                        }
                    except Exception as exc:
                        report.title_llm = {"status": type(exc).__name__}
                        report.errors["title_llm"] = (
                            "标题文字分析未完成，已继续规则检索"
                        )
                queries = 0

                async def search_web(query):
                    nonlocal queries
                    if queries >= 2:
                        return []
                    queries += 1
                    return await search_x_serpapi(
                        query, self.serpapi_api_key, self.x_lookup_timeout, self.proxy
                    )

                try:
                    api = (
                        self.twitter_api_provider()
                        if self.twitter_api_provider
                        else None
                    )
                    recovery = await asyncio.wait_for(
                        recover_x(
                            image,
                            sources,
                            api,
                            search_web if self.serpapi_api_key else None,
                            self.proxy,
                            self.x_lookup_timeout,
                            **(
                                {"title_candidates": title_candidates}
                                if title_candidates
                                else {}
                            ),
                        ),
                        # Recovery returns its partial clues when its own budget
                        # expires; leave time to close its client and return them.
                        timeout=self.x_lookup_timeout + 1,
                    )
                    report.errors.update(recovery.errors)
                    report.source_pages.update(recovery.source_pages)
                    if title_extractor is not None:
                        report.title_llm.update(
                            checked=recovery.title_accounts_checked,
                            found=recovery.title_accounts_found,
                        )
                    for candidate in recovery.candidates:
                        result = _make_result(
                            SimpleNamespace(title=candidate.title),
                            candidate.url,
                            "x_lookup",
                            "recovered" if candidate.verified else "clue",
                        )
                        if result and result.platform == "X":
                            chosen.append(
                                replace(
                                    result,
                                    name=candidate.name,
                                    verified=candidate.verified,
                                    title=candidate.evidence or candidate.title,
                                    clue_strength=candidate.clue_strength,
                                    source_hosts=candidate.source_hosts,
                                    relation_verified=candidate.relation_verified,
                                    account_confirmed=candidate.account_confirmed,
                                )
                            )
                except TimeoutError:
                    report.errors["x_lookup"] = "X 来源追查超时"
                except GoogleAPIError as exc:
                    report.errors["x_lookup"] = str(exc)
                except Exception:
                    report.errors["x_lookup"] = "X 来源追查暂时失败"

        async def enrich(result: SearchResult) -> SearchResult:
            if result.engine == "x_lookup":
                return result
            try:
                return await asyncio.wait_for(
                    self._source_account(result, image), timeout=6
                )
            except TimeoutError:
                return result

        merged: dict[str, SearchResult] = {}
        for result in await asyncio.gather(*(enrich(result) for result in chosen)):
            result = with_clues(result)
            key = _result_key(result)
            merged[key] = (
                _merge_result(merged[key], result) if key in merged else result
            )
        report.x_candidate_count = len(merged)
        if not any(item.verified for item in merged.values()):
            accounts = list(
                dict.fromkeys(
                    _platform_account(item.url)[1].casefold()
                    for item in merged.values()
                    if item.clue_strength > 0 or item.account_confirmed
                )
            )
            accounts = [account for account in accounts if account.startswith("@")]
            if len(accounts) > 1:
                counts = dict(
                    zip(
                        accounts,
                        await asyncio.gather(
                            *(
                                self._account_followers(account[1:])
                                for account in accounts
                            )
                        ),
                        strict=True,
                    )
                )
                logger.info("FLJ candidate followers: %s", counts)
                merged = {
                    key: replace(
                        item,
                        followers=counts.get(_platform_account(item.url)[1].casefold()),
                    )
                    for key, item in merged.items()
                }
        best, report.ambiguous = choose_best(list(merged.values()))
        if best is not None:
            best = await self._profile_name(best)
        report.results = [best] if best is not None else []
        return report

    async def _account_followers(self, handle: str) -> int | None:
        """Read candidate popularity without treating it as image evidence."""

        def count(value):
            if type(value) is int:
                return value if value >= 0 else None
            if isinstance(value, str) and re.fullmatch(
                r"[0-9][0-9,\s]*", value.strip()
            ):
                return int(re.sub(r"[,\s]", "", value))
            return None

        try:
            api = self.twitter_api_provider() if self.twitter_api_provider else None
            if api is None:
                return None
            profile = await asyncio.wait_for(api.get_user_info(handle), timeout=3)
            if (
                not isinstance(profile, dict)
                or not profile.get("status")
                or str(profile.get("user_name") or "").lstrip("@").casefold()
                != handle.casefold()
            ):
                return None
            for key in ("followers", "followers_count"):
                value = count(profile.get(key))
                if value is not None:
                    return value
            nitter = str(getattr(api, "nitter_url", "") or "").rstrip("/")
            if nitter:
                # Nitter's origin comes from the Twitter plugin's configuration.
                async with AsyncClient(
                    proxy=getattr(api, "proxy", None) or self.proxy,
                    trust_env=False,
                    timeout=4,
                    follow_redirects=False,
                ) as client:
                    async with asyncio.timeout(4):
                        response = await client.get(nitter + "/" + handle)
                        response.raise_for_status()
                page = PyQuery(response.text)
                return count(
                    page(".profile-statlist .followers .profile-stat-num").text()
                )
        except Exception as exc:
            logger.warning(
                "FLJ follower lookup failed for @%s: %s", handle, type(exc).__name__
            )
        return None

    async def _profile_name(self, result: SearchResult) -> SearchResult:
        """Fill a selected account's display name without changing its evidence."""
        if result.name.strip() or result.platform != "X":
            return result
        account = (
            result.account
            if re.fullmatch(r"@[A-Za-z0-9_]{1,15}", result.account)
            else _platform_account(result.url)[1]
        )
        if not account.startswith("@"):
            return result
        try:
            api = self.twitter_api_provider() if self.twitter_api_provider else None
            if api is None:
                return result
            profile = await asyncio.wait_for(api.get_user_info(account[1:]), timeout=4)
            if (
                isinstance(profile, dict)
                and profile.get("status")
                and str(profile.get("user_name") or "").lstrip("@").casefold()
                == account[1:].casefold()
                and isinstance(profile.get("screen_name"), str)
                and profile["screen_name"].strip()
            ):
                return replace(result, name=profile["screen_name"].strip()[:100])
            logger.warning("FLJ X profile lookup returned no matching display name")
        except Exception as exc:
            logger.warning("FLJ X profile lookup failed: %s", type(exc).__name__)
        return result

    async def close(self) -> None:
        """Clients are scoped to searches and already closed by their contexts."""
