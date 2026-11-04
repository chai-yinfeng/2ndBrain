"""L0 规则过滤（零 token）：时间窗、最短正文、include / exclude 关键词。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from secondbrain.db import TIME_FORMAT
from secondbrain.models import L0Settings


def _item_time(row: sqlite3.Row) -> datetime | None:
    value = row["published_at"] or row["fetched_at"]
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC) if value else None


def l0_reason(row: sqlite3.Row, s: L0Settings, now: datetime) -> str | None:
    """返回排除原因；通过时返回 None。关键词只匹配标题与摘要（不区分大小写）。"""
    ts = _item_time(row)
    if ts is not None and ts < now - timedelta(days=s.max_age_days):
        return "超出时间窗"
    if (row["content_len"] or 0) < s.min_content_chars:
        return "正文过短"
    text = f"{row['title']} {row['summary'] or ''}".lower()
    for kw in s.exclude_keywords:
        if kw.lower() in text:
            return f"排除关键词：{kw}"
    if s.include_keywords and not any(kw.lower() in text for kw in s.include_keywords):
        return "未命中 include 关键词"
    return None
