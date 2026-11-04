"""URL 规范化与内容指纹，用于去重。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "_hsenc",
    "_hsmi",
    "ref_src",
    "ref_url",
}


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in TRACKING_PARAMS


def canonicalize_url(url: str) -> str:
    """去跟踪参数与 fragment，scheme 统一为 https，host 小写，去默认端口与尾斜杠，参数排序。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    params = parse_qsl(parts.query, keep_blank_values=True)
    query = urlencode(sorted((k, v) for k, v in params if not _is_tracking(k)))
    return urlunsplit((scheme, host, path, query, ""))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def content_hash(title: str, body: str) -> str:
    """标题 + 正文（或摘要）归一化后的 sha256；标题参与，避免空正文互相碰撞。"""
    data = _normalize_text(title) + "\n" + _normalize_text(body)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
