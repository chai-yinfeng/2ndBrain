"""L0 规则过滤（零 token）：时间窗、最短正文、include / exclude 关键词。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from secondbrain.db import TIME_FORMAT
from secondbrain.models import L0Settings


def _item_time(row: sqlite3.Row) -> datetime | None:
    value = row["published_at"] or row["fetched_at"]
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC) if value else None


def l0_reason(
    row: sqlite3.Row, s: L0Settings, now: datetime, ignore_age: bool = False
) -> str | None:
    """返回排除原因；通过时返回 None。关键词只匹配标题与摘要（不区分大小写）。

    ignore_age：回补池条目不受时间窗限制。视频：Shorts 与时长不足的排除；
    没有字幕的视频不按正文长度排除（保留标题 / 简介供 L1 判断，但不会进入 L2）。"""
    if "/shorts/" in (row["url"] or ""):
        return "Shorts"
    duration = row["duration_seconds"]
    if duration is not None and duration < s.min_video_minutes * 60:
        return "视频过短"
    ts = _item_time(row)
    if not ignore_age and ts is not None and ts < now - timedelta(days=s.max_age_days):
        return "超出时间窗"
    if row["extract_method"] != "no_transcript" and (row["content_len"] or 0) < s.min_content_chars:
        return "正文过短"
    text = f"{row['title']} {row['summary'] or ''}".lower()
    for kw in s.exclude_keywords:
        if kw.lower() in text:
            return f"排除关键词：{kw}"
    if s.include_keywords and not any(kw.lower() in text for kw in s.include_keywords):
        return "未命中 include 关键词"
    return None
