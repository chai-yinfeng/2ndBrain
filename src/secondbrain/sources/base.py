"""信源解析的公共工具。"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime


def clean_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


def struct_time_to_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser 的 *_parsed 字段（UTC struct_time）转 aware datetime。"""
    if not value:
        return None
    return datetime(*value[:6], tzinfo=UTC)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
