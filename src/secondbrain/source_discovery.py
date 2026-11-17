"""统一的信源适配与主页发现。

预览不保存配置或导入材料；follow_source 仅在明确确认后新增或更新配置。
网络读取复用现有 transport、超时和限速，逐跳检查公网并有界读取响应。
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Literal, TypedDict, cast
from urllib.parse import parse_qsl, unquote, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from secondbrain.models import FetchSettings, RawEntry, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources import FETCHERS, html_index, rss, sitemap
from secondbrain.sources.homepage import source_homepage

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
    status: Literal["ready", "empty", "failed", "manual", "rejected"]
    matched_source_id: str | None
    matched_from: Literal["followed", "catalog", "generic"]


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
        self.warnings: list[str] = []
        self.title = ""
        self.title_mode: str | None = None

    def get(
        self, url: str, *, origin: tuple[str, str] | None = None,
        params: dict[str, str | None] | None = None,
    ) -> httpx.Response:
        if params:
            url = str(httpx.URL(url).copy_merge_params(params))
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
                    buffered = httpx.Response(
                        response.status_code, headers=headers, content=b"".join(chunks),
                        request=response.request,
                    )
                    if self.title_mode == "feed":
                        feed = feedparser.parse(buffered.content)
                        if not feed.version:
                            raise FetchError("采集地址未返回有效 RSS/Atom")
                        self.title = str(feed.feed.get("title") or "")
                    elif self.title_mode == "html":
                        page = _Page(str(buffered.url))
                        page.feed(buffered.text)
                        self.title = " ".join(page.title.split())
                    return buffered
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
    scope = urlsplit(homepage).path.rstrip("/") + "/"
    if scope != "/" and parts.path.startswith(scope) and len(segments) == len(
        scope.strip("/").split("/")
    ) + 1:
        return scope
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
    parts = urlsplit(homepage)
    scope = parts.path.rstrip("/") + "/"
    scoped = {key: links for key, links in families.items() if key == scope}
    if scoped:
        families = scoped
    elif scope != "/":
        if families:
            warnings.append("候选文章不在当前主页路径范围内，未自动扩大订阅范围。")
        return None
    if len(families) != 1:
        if families:
            warnings.append(
                "发现多个文章路径，无法可靠确定订阅范围："
                + "、".join(sorted(families))
                + "；请明确配置范围。"
            )
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


_ADAPTER_FIELDS = (
    "type", "url", "query", "homepage", "needs_scraper", "url_pattern", "max_links", "use_lastmod",
)


def _normalized_location(url: str) -> str | None:
    try:
        parts = urlsplit(_public_url(url))
    except _UnsafeURL:
        return None
    # 不猜 www / HTTP 别名，也不丢 query；别名只能由显式配置或已检查的跳转证明。
    path = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda match: (
            chr(int(match[1], 16))
            if chr(int(match[1], 16)) in
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            else match[0].upper()
        ),
        parts.path,
    ).rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _locations(source: SourceConfig | str) -> set[str]:
    if isinstance(source, str):
        urls = [source]
    else:
        # RSS / sitemap 的域名根只是展示兜底，不是栏目身份。
        homepage = source.homepage
        if not homepage and source.type in {"html_index", "hf_papers", "youtube"}:
            homepage = source_homepage(source)
        if not homepage and source.type == "arxiv":
            query = source.query or ""
            homepage = (
                source_homepage(source) if re.fullmatch(r"cat:[\w.-]+", query)
                else "https://arxiv.org/search/?" + urlencode(
                    {"query": query, "searchtype": "all"}
                )
            )
        urls = [url for url in (source.url, homepage) if url]
    return {normalized for url in urls if (normalized := _normalized_location(url))}


def source_matches(left: SourceConfig | str, right: SourceConfig | str) -> bool:
    """按明确主页或采集地址匹配，保留栏目路径和查询条件；不以裸域名合并。"""
    if isinstance(left, SourceConfig) and isinstance(right, SourceConfig):
        if left.type == right.type == "arxiv" and left.query != right.query:
            return False
        if (
            left.type in {"sitemap", "html_index"} and right.type in {"sitemap", "html_index"}
            and left.url_pattern != right.url_pattern
        ):
            return False
    return bool(_locations(left) & _locations(right))


def _with_adapter(existing: SourceConfig, adapter: SourceConfig) -> SourceConfig:
    return SourceConfig.model_validate({
        **existing.model_dump(),
        **{key: getattr(adapter, key) for key in _ADAPTER_FIELDS},
    })


def _known_source(
    url: str,
) -> tuple[SourceConfig | None, list[SourceConfig]] | None:
    from secondbrain import config

    followed = [source for source in config.load_sources() if source_matches(source, url)]
    current = next(
        (source for source in followed if not source.needs_scraper),
        followed[0] if followed else None,
    )
    # 直接读完整 example；参考目录的 UI 隐藏状态不参与适配识别。
    catalog_path = config.config_dir() / "sources.example.yaml"
    if not catalog_path.is_file():
        catalog_path = config.CONFIG_DIR / "sources.example.yaml"
    catalog = config.load_sources(catalog_path) if catalog_path.is_file() else []
    adapters = [
        source for source in catalog if not source.needs_scraper and (
            source_matches(source, url)
            or any(source_matches(source, existing) for existing in followed)
        )
    ]
    if not adapters and (current is None or current.needs_scraper):
        return None
    return current, adapters


def _preview_source(
    source: SourceConfig, reader: _Reader, fetch_settings: FetchSettings,
) -> DiscoveryResult:
    homepage = source_homepage(source) or source.url or ""
    result: DiscoveryResult = {
        "homepage": homepage, "title": source.name or source.id, "method": source.type,
        "source": source, "entries": [], "warnings": [], "status": "failed",
        "matched_source_id": source.id, "matched_from": "generic",
    }
    try:
        if homepage:
            _public_url(homepage)
        if source.url:
            _public_url(source.url)
        if source.needs_scraper:
            result.update(method="bookmark", status="manual")
            result["warnings"].append("此来源需要手动浏览，尚未配置可用采集适配。")
            return result
        fetcher = FETCHERS.get(source.type)
        if fetcher is None:
            raise FetchError("该来源没有已注册的采集适配器")
        reader.title = ""
        reader.title_mode = (
            "feed" if source.type in {"rss", "youtube", "arxiv"}
            else "html" if source.type == "html_index" else None
        )
        entries = fetcher(source, cast(HttpClient, reader), fetch_settings)
        if reader.title and not source.name:
            source = source.model_copy(update={"name": reader.title[:200]})
            result.update(source=source, title=source.name or source.id)
        result["entries"] = _safe_entries(entries, source.url or homepage)
        result["status"] = "ready" if result["entries"] else "empty"
        if not result["entries"]:
            result["warnings"].append("采集适配成功，但本次没有候选条目。")
    except _UnsafeURL as exc:
        result["status"] = "rejected"
        result["warnings"].append(str(exc))
    except (FetchError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result["warnings"].append(str(exc))
    result["warnings"].extend(reader.warnings)
    reader.warnings.clear()
    return result


def preview_source(
    source: SourceConfig, client: HttpClient, fetch_settings: FetchSettings,
) -> DiscoveryResult:
    """使用已注册的真实适配器，只读预览；已知失败保留配置，不降级 bookmark。"""
    if source.needs_scraper:
        result = discover_source(source.homepage or source.url or "", client, fetch_settings)
        if result["source"] is not None:
            updated = _with_adapter(source, result["source"])
            if not updated.name and result["source"].name:
                updated = updated.model_copy(update={"name": result["source"].name})
            result.update(source=updated, matched_source_id=source.id)
        return result
    return _preview_source(source, _Reader(client, fetch_settings), fetch_settings)


def _known_preview(
    url: str, reader: _Reader, fetch_settings: FetchSettings,
) -> DiscoveryResult | None:
    known = _known_source(url)
    if known is None:
        return None
    current, adapters = known
    result: DiscoveryResult | None = None
    if current is not None and not current.needs_scraper:
        result = _preview_source(current, reader, fetch_settings)
        result["matched_from"] = "followed"
        if result["status"] in {"ready", "empty"}:
            return result
    diagnostics: list[str] = []
    for adapter in adapters:
        if current is not None and not current.needs_scraper and all(
            getattr(current, key) == getattr(adapter, key)
            for key in _ADAPTER_FIELDS if key != "homepage"
        ):
            continue
        preview = _preview_source(adapter, reader, fetch_settings)
        if preview["status"] in {"ready", "empty"}:
            if current is not None:
                repaired = _with_adapter(current, preview["source"] or adapter)
                if not repaired.name and preview["source"] is not None:
                    repaired = repaired.model_copy(update={"name": preview["source"].name})
                preview.update(
                    source=repaired, title=repaired.name or preview["title"],
                    matched_source_id=current.id, matched_from="followed",
                )
            else:
                preview["matched_from"] = "catalog"
            previous = result["warnings"] if result is not None else []
            preview["warnings"] = [*previous, *diagnostics, *preview["warnings"]]
            return preview
        if result is None and current is None:
            result = preview
            result["matched_from"] = "catalog"
        else:
            diagnostics.extend(
                f"参考目录适配 {adapter.id}（{adapter.type}）：{warning}"
                for warning in preview["warnings"]
            )
    if result is None:
        assert current is not None
        result = _preview_source(current, reader, fetch_settings)
        result.update(status="failed", matched_from="followed")
    result["warnings"].extend(diagnostics)
    return result


def _discover_source(
    url: str, client: HttpClient, fetch_settings: FetchSettings,
) -> DiscoveryResult:
    """预览 URL；method 为 rss / html_index / sitemap / bookmark / rejected。

    bookmark 也有可持久化 SourceConfig：needs_scraper=True，现有采集流程跳过它，主页
    仍可访问。rejected 的 source=None。任何结果均不写配置、数据库或稍后阅读清单。
    预览最多 8 次请求、每跳最多 3 次重定向、每响应 1 MB、500 链接、50 条候选。
    """
    result: DiscoveryResult = {
        "homepage": "", "title": "", "method": "rejected", "source": None,
        "entries": [], "warnings": [], "status": "rejected",
        "matched_source_id": None, "matched_from": "generic",
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
    known = _known_preview(homepage, reader, fetch_settings)
    if known is not None:
        return known
    try:
        response = reader.get(homepage)
    except _UnsafeURL as exc:
        warnings.append(str(exc))
        return result
    except FetchError as exc:
        warnings.append(str(exc))
        result["status"] = "failed"
    else:
        homepage = str(response.url)
        result["homepage"] = homepage
        known = _known_preview(homepage, reader, fetch_settings)
        if known is not None:
            return known
        if _feed(response.content):
            entries = _safe_entries(rss.parse_feed(response.content), homepage)
            title = str(feedparser.parse(response.content).feed.get("title") or result["title"])
            result.update(method="rss", source=_source(homepage, "rss", homepage),
                          entries=entries, title=title, status="ready" if entries else "empty")
            return result
        if re.search(r"<(?:[\w.-]+:)?(?:urlset|sitemapindex)\b", response.text[:8192]):
            try:
                candidates = sitemap.collect_sitemap(
                    reader, homepage, initial_response=response, max_entries=MAX_LINKS
                )
                warnings.extend(reader.warnings)
                reader.warnings.clear()
                pattern = _pattern(
                    [entry.url for entry in candidates], urljoin(homepage, "/"), warnings
                )
                if pattern:
                    entries = _safe_entries(
                        [entry for entry in candidates if re.search(pattern, entry.url)], homepage
                    )
                    result.update(
                        method="sitemap", source=_source(homepage, "sitemap", homepage, pattern),
                        entries=entries, status="ready" if entries else "empty",
                    )
                    return result
            except FetchError as exc:
                warnings.append(str(exc))
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
                            entries=entries, status="ready" if entries else "empty",
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
                        entries=entries, status="ready" if entries else "empty",
                    )
                    warnings.append("按明确文章路径推断列表范围；保存前请核对候选，未抓取文章正文。")
                    return result
                sitemap_urls = list(
                    dict.fromkeys(page.sitemaps + [urljoin(homepage, "/sitemap.xml")])
                )
                for sitemap_url in sitemap_urls[:2]:
                    try:
                        candidates = sitemap.collect_sitemap(
                            reader, sitemap_url, max_entries=MAX_LINKS, origin=_origin(homepage)
                        )
                        warnings.extend(reader.warnings)
                        reader.warnings.clear()
                        pattern = _pattern(
                            [e.url for e in candidates[:MAX_LINKS]], homepage, warnings
                        )
                        if not pattern:
                            continue
                        entries = _safe_entries(
                            [entry for entry in candidates if re.search(pattern, entry.url)],
                            homepage,
                        )
                        result.update(
                            method="sitemap",
                            source=_source(homepage, "sitemap", sitemap_url, pattern),
                            entries=entries, status="ready" if entries else "empty",
                        )
                        warnings.append(
                            "sitemap 仅提供候选 URL；未抓取正文，日期可能未知，请核对订阅范围。"
                        )
                        return result
                    except FetchError as exc:
                        warnings.append(str(exc))
    result.update(
        method="bookmark", status="failed" if result["status"] == "failed" else "manual",
        source=_source(result["homepage"], "bookmark", result["homepage"]),
    )
    warnings.append(
        "仅支持保存主页并手动浏览：未可靠发现公开文章列表；不登录、不执行脚本；"
        "不会自动导入或加入稍后阅读。"
    )
    return result


def discover_source(url: str, client: HttpClient, fetch_settings: FetchSettings) -> DiscoveryResult:
    """依次复用已关注、完整参考目录和通用发现；只返回候选，不写配置。"""
    from secondbrain import config

    result = _discover_source(url, client, fetch_settings)
    source = result["source"]
    if source is None:
        return result
    if result["matched_from"] == "generic":
        existing = next(
            (item for item in config.load_sources() if source_matches(item, source)
             or source_matches(item, url)),
            None,
        )
        if existing is not None:
            source = _with_adapter(existing, source)
            result.update(matched_source_id=existing.id, matched_from="followed")
    if result["status"] in {"ready", "empty", "manual"} and not source.name and result["title"]:
        source = source.model_copy(update={"name": result["title"][:200]})
    result["source"] = source
    return result


class FollowResult(DiscoveryResult):
    saved_id: str | None


def follow_source(
    url: str,
    client: HttpClient,
    fetch_settings: FetchSettings,
    *,
    source_id: str | None = None,
    name: str | None = None,
) -> FollowResult:
    """确认后新增或修复同一来源；人工字段和既有 ID 保持不变，不导入候选。"""
    from secondbrain import config, ops, sources_file

    result: FollowResult = {
        **discover_source(url, client, fetch_settings),
        "saved_id": None,
    }
    source = result["source"]
    if source is None or result["status"] in {"rejected", "failed"}:
        return result
    if source_id is not None:
        SourceConfig.model_validate({**source.model_dump(), "id": source_id})
    proposed_name = name.strip() if name is not None else None
    if proposed_name == "":
        raise ValueError("显示名称不能为空")
    if proposed_name is not None:
        SourceConfig.model_validate({**source.model_dump(), "name": proposed_name})
    if not source.name:
        source = SourceConfig.model_validate({
            **source.model_dump(),
            "name": proposed_name or result["title"] or source.id,
        })

    def save(text: str) -> str:
        nonlocal source
        existing = config.parse_sources_text(text)
        matched = next((item for item in existing if source_matches(item, source)), None)
        if matched is not None:
            if source_id is not None and source_id != matched.id:
                raise ValueError("该来源已关注；修改 ID 请使用正式编辑接口，不会复制来源")
            updated = (
                matched if source.needs_scraper and not matched.needs_scraper
                else _with_adapter(matched, source)
            )
            if not matched.name:
                updated = updated.model_copy(update={"name": proposed_name or source.name})
            source = updated
            changes = {
                key: getattr(updated, key) for key in _ADAPTER_FIELDS
                if getattr(updated, key) != getattr(matched, key)
            }
            if not matched.name and updated.name:
                changes["name"] = updated.name
            return sources_file.update_entry(text, matched.id, changes)
        if source_id is not None:
            source = SourceConfig.model_validate({**source.model_dump(), "id": source_id})
        if proposed_name is not None:
            source = source.model_copy(update={"name": proposed_name})
        if any(item.id == source.id for item in existing):
            raise ValueError(f"信源 id 已存在：{source.id}")
        return sources_file.append_entry(text, sources_file.dump_entry(source))

    ops.edit_sources(save)
    result.update(source=source, saved_id=source.id)
    return result
