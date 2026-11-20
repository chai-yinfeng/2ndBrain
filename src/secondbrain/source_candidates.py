"""已下载 RSS 与人工选择页面之间的有限、可复核范围证据；不联网、不调用模型。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import replace
from itertools import islice
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from secondbrain.models import RawEntry
from secondbrain.sources.rss import parse_feed

MAX_ENTRIES = 50
MAX_SAMPLES = 10
MAX_PAGE_LINKS = 500


def _public_url(url: str, base: str = "") -> str | None:
    """仅做离线 URL 语法/字面公网地址检查；请求前仍须执行发现层的 DNS/逐跳守卫。"""
    try:
        if any(c.isspace() or ord(c) < 32 for c in url) or "\\" in unquote(url):
            return None
        if any(ord(c) < 32 for c in unquote(url)):
            return None
        parts = urlsplit(urljoin(base, url))
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
        scheme = parts.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or "%" in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # 拒绝单标签主机和浏览器可解释为私网 IP 的旧式数字地址。
            if "." not in host or all(
                re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in host.split(".")
            ):
                return None
        else:
            if not address.is_global:
                return None
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if scheme == "https" else 80):
            netloc += f":{port}"
        # path 大小写和完整 query 均有意义；只规范 host、尾斜杠、默认端口及 fragment。
        return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return None


def _section(homepage: str) -> str:
    parts = [unquote(part) for part in urlsplit(homepage).path.split("/") if part]
    if parts and parts[-1].casefold() in {"index", "index.html", "index.htm"}:
        parts.pop()
    return parts[-1].casefold() if parts else ""


def feed_scope_candidate(
    homepage: str, page_links: list[str], feed_url: str, data: bytes
) -> dict:
    """建议分类过滤或更广 feed，绝不将候选视为已获用户同意的替代来源。

    entries 是建议范围中的安全条目预览；无证据时为空。feed_total 是解析出的有链接
    条目总数；outside_count 是不在分类范围内（或未与页面样本重合）的条目数，含不安全
    条目。两个 URL 样本最多各 10 个，页面证据最多检查前 500 个链接。URL 不解析 DNS，
    不可用本函数替代实际请求的网络安全守卫。非 RSS/Atom 抛 FetchError。
    同名分类遗漏已命中的其他分类时 scope_confirmed=False；alternatives 同时给出更广
    feed 与更窄分类候选（不递归嵌套）。uncovered_page_urls 是分类未覆盖/尚未核对的
    页面候选链接样本，不能解释为这些链接一定属于其他栏目。
    """
    parsed = parse_feed(data)
    total = len(parsed)
    result = {
        "relation": "unverified",
        "scope_categories": [],
        "entries": [],
        "matched_urls": [],
        "page_sample": [],
        "feed_total": total,
        "outside_count": total,
        "note": "未找到页面文章 URL 重合或同名 feed 分类证据；不自动替换来源。",
        "scope_confirmed": False,
        "alternatives": [],
        "uncovered_page_urls": [],
    }
    home = _public_url(homepage)
    feed = _public_url(feed_url)
    if home is None or feed is None:
        result["note"] = "主页或 feed URL 不符合公开 HTTP(S) 地址要求；不提供替代建议。"
        return result

    page_urls: dict[str, None] = {}
    for link in islice(page_links, MAX_PAGE_LINKS):
        url = _public_url(link, homepage)
        if url is not None and url not in {home, feed}:
            page_urls.setdefault(url, None)
    result["page_sample"] = list(islice(page_urls, MAX_SAMPLES))

    safe_entries: list[RawEntry] = []
    seen: set[str] = set()
    for entry in parsed:
        url = _public_url(entry.url, feed_url)
        if url is not None and url not in {home, feed}:
            safe_entries.append(replace(entry, url=url))
            seen.add(url)
    matched = list(dict.fromkeys(entry.url for entry in safe_entries if entry.url in page_urls))
    result["matched_urls"] = matched[:MAX_SAMPLES]

    section = _section(home)
    category = next(
        (
            category
            for entry in safe_entries
            for category in entry.categories
            if section and category.casefold() == section
        ),
        None,
    )
    caution = "证据仅限本次 feed 与页面链接样本，不证明整站、历史或完整覆盖；须由用户确认后采用。"
    broad = None
    if matched:
        broad = {
            **result,
            "relation": "sample_overlap",
            "entries": safe_entries[:MAX_ENTRIES],
            "outside_count": total - sum(entry.url in page_urls for entry in safe_entries),
            "scope_confirmed": True,
            "uncovered_page_urls": [url for url in page_urls if url not in seen][:MAX_SAMPLES],
            "note": (
                f"本次 feed 有 {len(matched)} 个条目与页面文章 URL 重合。"
                "这是更广的综合 feed，可能混入其他栏目内容；未重合条目不等于已证实无关。"
                f"{caution}"
            ),
        }
        broad.pop("alternatives")
    if category is not None:
        scoped = [
            entry
            for entry in safe_entries
            if any(value.casefold() == category.casefold() for value in entry.categories)
        ]
        scoped_urls = {entry.url for entry in scoped}
        scoped_matches = sum(url in page_urls for url in scoped_urls)
        conflicts = [url for url in matched if url not in scoped_urls]
        uncovered = [url for url in page_urls if url not in scoped_urls]
        overlap = (
            f"该分类有 {scoped_matches} 个条目与页面 URL 重合。"
            if scoped_matches
            else "该分类暂无页面文章 URL 重合证据，仅有同名分类证据。"
        )
        if conflicts:
            overlap += (
                f"页面中另有 {len(conflicts)} 个重合 URL 未标注目标分类（其他分类或无分类），"
                "按同名分类过滤会遗漏这些页面内容，不能视为网页等价。"
            )
        if uncovered:
            overlap += (
                f"页面样本有 {len(uncovered)} 个候选链接未被该分类覆盖，"
                "其中未出现在本次 feed 中的链接尚无法核对；该过滤可能遗漏页面内容。"
            )
        result.update(
            relation="category_filter",
            scope_categories=[category],
            entries=scoped[:MAX_ENTRIES],
            outside_count=total - len(scoped),
            uncovered_page_urls=uncovered[:MAX_SAMPLES],
            scope_confirmed=not conflicts,
            note=(f"页面路径栏目与 feed 分类“{category}”同名；这是更窄的分类候选。"
                  f"{overlap}{caution}"),
        )
        if broad is not None:
            narrow = {key: value for key, value in result.items() if key != "alternatives"}
            result["alternatives"] = [broad, narrow]
    elif broad is not None:
        result.update(broad)
    elif not total:
        result["note"] = "这是空 RSS/Atom，没有可核对的文章或分类证据；不自动替换来源。"
    return result
