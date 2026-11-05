"""信源解析的公共工具。"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta


class Fetched(list):
    """fetcher 的返回值：条目列表，另带从 feed 中读到的主页链接（RSS channel <link>）。"""

    homepage: str | None = None


def clean_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


# 早于该年份或晚于"现在 + 1 天"的发布时间视为异常（如 0001-01-01），按未知处理
MIN_PLAUSIBLE_YEAR = 1990


def plausible(dt: datetime | None, now: datetime | None = None) -> datetime | None:
    if dt is None:
        return None
    now = now or datetime.now(UTC)
    if dt.year < MIN_PLAUSIBLE_YEAR or dt > now + timedelta(days=1):
        return None
    return dt


def struct_time_to_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser 的 *_parsed 字段（UTC struct_time）转 aware datetime；异常值返回 None。"""
    if not value:
        return None
    try:
        return plausible(datetime(*value[:6], tzinfo=UTC))
    except ValueError:
        return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return plausible(dt if dt.tzinfo else dt.replace(tzinfo=UTC))
