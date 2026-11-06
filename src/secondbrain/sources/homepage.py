"""信源主页链接（/sources 上信源名的链接，ADR 0017）。纯函数。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, quote_plus, urlsplit

from secondbrain.models import SourceConfig

_ARXIV_CAT = re.compile(r"\bcat:([A-Za-z-]+\.[A-Za-z-]+)")


def safe_url(url: str | None) -> str | None:
    """只接受 http / https 且有 host 的链接（拒绝 javascript: 等 scheme）。"""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url.strip()


# 明显的 feed 托管域名：站点根不是信源主页，不给兜底链接
_FEED_HOSTS = ("feedburner.com", "feedblitz.com", "feedpress.me", "rss.app")


def feed_root(url: str | None) -> str | None:
    """RSS 的兜底主页：feed URL 的 scheme://host/（如 x.substack.com/feed → x.substack.com/）。
    feeds.* 子域与 feedburner 等托管域名返回 None。"""
    parts = urlsplit(url or "")
    host = parts.hostname or ""
    if (
        not host
        or host.startswith("feeds.")
        or any(host == h or host.endswith("." + h) for h in _FEED_HOSTS)
    ):
        return None
    return safe_url(f"{parts.scheme}://{parts.netloc}/")


def source_homepage(source: SourceConfig, detected: str | None = None) -> str | None:
    """配置的 homepage 优先；否则按类型推导：rss 用抓取 / 预览时从 feed 读到的 channel <link>，
    没有时用 feed URL 的站点根（托管域名除外）；sitemap 用站点根，arxiv 用分类列表
    （没有 cat: 时用搜索），hf_papers 与 youtube 用固定页面。"""
    if source.homepage:
        return safe_url(source.homepage)
    url = source.url or ""
    if source.type == "rss":
        return safe_url(detected) or feed_root(url)
    if source.type == "html_index":
        return safe_url(url)  # 列表页本身就是人浏览的页面
    if source.type == "sitemap":
        parts = urlsplit(url)
        return safe_url(f"{parts.scheme}://{parts.netloc}/") if parts.netloc else None
    if source.type == "arxiv":
        m = _ARXIV_CAT.search(source.query or "")
        if m:
            return f"https://arxiv.org/list/{m.group(1)}/recent"
        return f"https://arxiv.org/search/?query={quote_plus(source.query or '')}&searchtype=all"
    if source.type == "hf_papers":
        return "https://huggingface.co/papers"
    if source.type == "youtube":
        channel = parse_qs(urlsplit(url).query).get("channel_id", [None])[0]
        return f"https://www.youtube.com/channel/{quote_plus(channel)}" if channel else None
    return None
