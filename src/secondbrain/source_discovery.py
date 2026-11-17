"""只读的信源主页预览：发现 feed、文章列表或可手动浏览的主页。

不保存配置、不导入材料。调用者须展示 warnings / entries，明确确认后才保存 source。
网络读取使用现有 HttpClient 的 transport、超时和限速，但逐跳检查重定向并限流读取。
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import unquote, urldefrag, urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources import html_index, rss, sitemap

MAX_BYTES = 1_000_000
MAX_LINKS = 500
MAX_ENTRIES = 50
MAX_REQUESTS = 8
MAX_REDIRECTS = 3
_FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json"}
_ARTICLE_SECTIONS = {"blog", "posts", "post", "articles", "article", "news", "research", "p"}
_RESERVED = {
    "about", "contact", "login", "logout", "signin", "signup", "register", "account",
    "admin", "privacy", "terms", "feed", "rss", "atom", "search", "tags", "tag",
    "category", "categories", "archive", "archives", "page", "subscribe", "index",
    "faq", "help", "settings", "author", "authors", "careers", "jobs",
}
_SLUG = r"[\w~-]+(?:\.html)?"


class DiscoveryResult(TypedDict):
    homepage: str
    title: str
    method: str
    source: SourceConfig | None
    entries: list[RawEntry]
    warnings: list[str]


class _UnsafeURL(FetchError):
    pass


def _public_url(url: str, *, resolve: bool = False) -> str:
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or any(c.isspace() or ord(c) < 32 for c in url)
            or "\\" in url
            or "%" in host
        ):
            raise _UnsafeURL("仅支持无凭据的公开 HTTP(S) URL")
        host = host.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise _UnsafeURL("禁止访问本地或私有网络地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise _UnsafeURL("禁止访问本地或私有网络地址") from None
        else:
            if not address.is_global:
                raise _UnsafeURL("禁止访问本地或私有网络地址")
        if resolve:
            try:
                addresses = socket.getaddrinfo(
                    host, port or (443 if parts.scheme == "https" else 80)
                )
            except OSError as exc:
                raise FetchError("无法解析主页域名") from exc
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise _UnsafeURL("域名解析到本地或私有网络地址，已拒绝")
        netloc = f"[{host}]" if ":" in host else host
        if port and port != (443 if parts.scheme == "https" else 80):
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    except ValueError as exc:
        raise _UnsafeURL("URL 格式无效") from exc


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


class _Reader:
    def __init__(self, client: HttpClient, settings: FetchSettings) -> None:
        self.client = client
        self.remaining = MAX_REQUESTS
        self.max_bytes = min(MAX_BYTES, settings.content_max_chars * 4)

    def get(self, url: str, *, origin: tuple[str, str] | None = None) -> httpx.Response:
        for hop in range(MAX_REDIRECTS + 1):
            url = _public_url(url, resolve=True)
            if origin is not None and _origin(url) != origin:
                raise FetchError("列表或 sitemap 重定向到了其他站点，未自动采用")
            if self.remaining <= 0:
                raise FetchError("已达到主页发现请求上限")
            self.remaining -= 1
            self.client._throttle(urlsplit(url).hostname or "")
            try:
                # HttpClient.get 自动跟随重定向且仅按 Content-Length 限制大小；这里必须逐跳
                # 验证，并对实际解压后的字节计数，不能先下载完再检查。
                with self.client._client.stream("GET", url, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or hop == MAX_REDIRECTS:
                            raise FetchError("主页重定向次数过多或缺少目标")
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP {response.status_code}：公开预览不可用")
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > self.max_bytes:
                        raise FetchError(f"页面超过预览下载上限（{self.max_bytes} 字节）")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FetchError(f"页面超过预览下载上限（{self.max_bytes} 字节）")
                        chunks.append(chunk)
                    headers = {
                        key: value for key, value in response.headers.items()
                        if key not in {"content-encoding", "content-length"}
                    }
                    return httpx.Response(
                        response.status_code, headers=headers, content=b"".join(chunks),
                        request=response.request,
                    )
            except httpx.HTTPError as exc:
                raise FetchError("主页网络请求失败") from exc
        raise FetchError("主页重定向次数过多")


class _Page(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title = ""
        self.feeds: list[str] = []
        self.sitemaps: list[str] = []
        self.links: list[str] = []
        self.password = False
        self.limited = False
        self._title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._title = True
        if tag == "input" and (values.get("type") or "").lower() == "password":
            self.password = True
        if tag not in {"a", "link"} or not values.get("href"):
            return
        if len(self.links) + len(self.feeds) + len(self.sitemaps) >= MAX_LINKS:
            self.limited = True
            return
        url = urldefrag(urljoin(self.base, values["href"].strip())).url
        rel = (values.get("rel") or "").lower().split()
        kind = (values.get("type") or "").lower().split(";", 1)[0].strip()
        if tag == "link" and "alternate" in rel and kind in _FEED_TYPES:
            self.feeds.append(url)
        elif tag == "link" and "sitemap" in rel:
            self.sitemaps.append(url)
        elif tag == "a":
            self.links.append(url)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title += data[:500 - len(self.title)]


def _article_family(url: str, homepage: str) -> str | None:
    """只推断有明确文章前缀 / 日期路径的同源链接，不猜任意根目录 slug。"""
    try:
        url = _public_url(url)
    except _UnsafeURL:
        return None
    parts = urlsplit(url)
    if _origin(url) != _origin(homepage) or parts.query or url == homepage:
        return None
    segments = parts.path.strip("/").split("/")
    if any(unquote(s).lower().removesuffix(".html") in _RESERVED for s in segments):
        return None
    if any(not re.fullmatch(_SLUG, s) for s in segments):
        return None
    if len(segments) == 2 and segments[0].lower() in _ARTICLE_SECTIONS:
        return f"/{segments[0]}/"
    if len(segments) == 4 and re.fullmatch(r"20\d{2}/\d{2}/\d{2}", "/".join(segments[:3])):
        return "/DATE/"
    return None


def _pattern(urls: list[str], homepage: str, warnings: list[str]) -> str | None:
    families: dict[str, set[str]] = {}
    for url in urls:
        family = _article_family(url, homepage)
        if family:
            families.setdefault(family, set()).add(url)
    if len(families) != 1:
        if families:
            warnings.append("发现多个文章路径，无法可靠确定订阅范围；请手动浏览或明确配置范围。")
        return None
    family, links = next(iter(families.items()))
    if len(links) < 2:
        return None
    parts = urlsplit(homepage)
    prefix = re.escape(f"{parts.scheme}://{parts.netloc}")
    path = r"/20\d{2}/\d{2}/\d{2}/" if family == "/DATE/" else re.escape(family)
    blocked = "|".join(sorted(_RESERVED))
    return rf"^{prefix}{path}(?!(?i:{blocked})(?:\.html)?/?$){_SLUG}/?$"


def _source(homepage: str, method: str, url: str, pattern: str | None = None) -> SourceConfig:
    host = re.sub(r"[^a-z0-9]+", "-", urlsplit(homepage).hostname or "site").strip("-")
    source_id = f"{host}-{hashlib.sha256(homepage.encode()).hexdigest()[:8]}"
    return SourceConfig(
        id=source_id, type="rss" if method == "bookmark" else method,
        url=url, homepage=homepage, needs_scraper=method == "bookmark", url_pattern=pattern,
        max_links=MAX_ENTRIES if method == "html_index" else None,
    )


def _safe_entries(entries: list[RawEntry], base: str) -> list[RawEntry]:
    result: list[RawEntry] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            url = _public_url(urljoin(base, entry.url))
        except _UnsafeURL:
            continue
        if url not in seen:
            entry.url = url
            result.append(entry)
            seen.add(url)
        if len(result) >= MAX_ENTRIES:
            break
    return result

def _feed(data: bytes) -> bool:
    # feedparser 可从普通 HTML 中抓出链接；只有真正 feed 才交给现有 RSS parser。
    return bool(feedparser.parse(data).version)


def discover_source(url: str, client: HttpClient, fetch_settings: FetchSettings) -> DiscoveryResult:
    """预览 URL；method 为 rss / html_index / sitemap / bookmark / rejected。

    bookmark 也有可持久化 SourceConfig：needs_scraper=True，现有采集流程跳过它，主页
    仍可访问。rejected 的 source=None。任何结果均不写配置、数据库或稍后阅读清单。
    预览最多 8 次请求、每跳最多 3 次重定向、每响应 1 MB、500 链接、50 条候选。
    """
    result: DiscoveryResult = {
        "homepage": "", "title": "", "method": "rejected", "source": None,
        "entries": [], "warnings": [],
    }
    warnings = result["warnings"]
    try:
        homepage = _public_url(url)
    except _UnsafeURL as exc:
        warnings.append(str(exc))
        return result
    result["homepage"] = homepage
    result["title"] = urlsplit(homepage).hostname or homepage
    reader = _Reader(client, fetch_settings)
    try:
        response = reader.get(homepage)
    except _UnsafeURL as exc:
        warnings.append(str(exc))
        return result
    except FetchError as exc:
        warnings.append(str(exc))
    else:
        homepage = str(response.url)
        result["homepage"] = homepage
        if _feed(response.content):
            result.update(method="rss", source=_source(homepage, "rss", homepage),
                          entries=_safe_entries(rss.parse_feed(response.content), homepage))
            return result
        content_type = response.headers.get("content-type", "").lower()
        html_text = bool(re.search(
            r"<(?:!doctype\s+html|html\b|head\b|title\b|a\b|link\b)",
            response.text[:8192], re.I,
        ))
        if content_type and "html" not in content_type and not (
            content_type.startswith("text/") and html_text
        ):
            warnings.append("该地址不是公开 HTML 主页或 RSS/Atom，未自动枚举。")
        else:
            page = _Page(homepage)
            page.feed(response.text)
            result["title"] = " ".join(page.title.split()) or result["title"]
            if page.limited:
                warnings.append(f"仅检查前 {MAX_LINKS} 个链接。")
            if page.password:
                warnings.append("页面含登录表单；不登录、不执行脚本，无法确认公开文章列表。")
            else:
                for feed_url in dict.fromkeys(page.feeds[:3]):
                    try:
                        feed_response = reader.get(feed_url)
                        if not _feed(feed_response.content):
                            raise FetchError("声明的 feed 不是有效 RSS/Atom")
                        entries = _safe_entries(
                            rss.parse_feed(feed_response.content), str(feed_response.url)
                        )
                        result.update(
                            method="rss",
                            source=_source(homepage, "rss", str(feed_response.url)),
                            entries=entries,
                        )
                        return result
                    except FetchError as exc:
                        warnings.append(str(exc))
                pattern = _pattern(page.links, homepage, warnings)
                if pattern:
                    entries = html_index.parse_index(response.text, homepage, pattern, MAX_ENTRIES)
                    result.update(
                        method="html_index",
                        source=_source(homepage, "html_index", homepage, pattern),
                        entries=entries,
                    )
                    warnings.append("按明确文章路径推断列表范围；保存前请核对候选，未抓取文章正文。")
                    return result
                sitemap_urls = list(
                    dict.fromkeys(page.sitemaps + [urljoin(homepage, "/sitemap.xml")])
                )
                for sitemap_url in sitemap_urls[:2]:
                    try:
                        sitemap_response = reader.get(sitemap_url, origin=_origin(homepage))
                        candidates = sitemap.parse_sitemap(sitemap_response.content)
                        if len(candidates) > MAX_LINKS:
                            warnings.append(f"sitemap 仅检查前 {MAX_LINKS} 个链接。")
                        pattern = _pattern(
                            [e.url for e in candidates[:MAX_LINKS]], homepage, warnings
                        )
                        if not pattern:
                            continue
                        entries = _safe_entries(
                            sitemap.parse_sitemap(sitemap_response.content, pattern), homepage
                        )
                        result.update(
                            method="sitemap",
                            source=_source(homepage, "sitemap", str(sitemap_response.url), pattern),
                            entries=entries,
                        )
                        warnings.append(
                            "sitemap 仅提供候选 URL；未抓取正文，日期可能未知，请核对订阅范围。"
                        )
                        return result
                    except FetchError as exc:
                        warnings.append(str(exc))
    result.update(
        method="bookmark",
        source=_source(result["homepage"], "bookmark", result["homepage"]),
    )
    warnings.append(
        "仅支持保存主页并手动浏览：未可靠发现公开文章列表；不登录、不执行脚本；"
        "不会自动导入或加入稍后阅读。"
    )
    return result


class FollowResult(DiscoveryResult):
    saved_id: str | None


def follow_source(
    url: str,
    client: HttpClient,
    fetch_settings: FetchSettings,
    *,
    source_id: str | None = None,
) -> FollowResult:
    """显式确认入口：重新发现后保存信源配置，绝不导入候选或加入阅读清单。

    拒绝的 URL 返回 saved_id=None；非法 / 重复 id 或配置写入失败抛 ValueError /
    ConfigError。重复调用不会覆盖现有信源。
    """
    from secondbrain import config, ops, sources_file

    result: FollowResult = {
        **discover_source(url, client, fetch_settings),
        "saved_id": None,
    }
    source = result["source"]
    if source is None:
        return result
    if source_id is not None:
        source = SourceConfig.model_validate({**source.model_dump(), "id": source_id})
        result["source"] = source
    block = sources_file.dump_entry(source)

    def append(text: str) -> str:
        existing = config.parse_sources_text(text)
        if any(s.id == source.id for s in existing):
            raise ValueError(f"信源 id 已存在：{source.id}")
        return sources_file.append_entry(text, block)

    ops.edit_sources(append)
    result["saved_id"] = source.id
    return result
