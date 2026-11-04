"""近期反馈汇总：供 L1 few-shot、profile 修改提案与信源 trust 调整使用。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from secondbrain import db

# 参与个性化的反馈类型（"已读 / 稍后读"只是阅读状态，不表达偏好）
SIGNAL_KINDS = ("up", "down", "learned", "needs_refresh", "mastered")
POSITIVE_KINDS = ("up", "learned")
NEGATIVE_KINDS = ("down",)
KIND_LABELS = {k: db.FEEDBACK_KINDS[k] for k in SIGNAL_KINDS}


@dataclass
class FeedbackEntry:
    feedback_id: int
    item_id: int
    kind: str
    ts: str
    title: str
    source_id: str
    note: str | None = None
    content_type: str | None = None
    topics: list[str] = field(default_factory=list)

    def as_prompt_dict(self) -> dict:
        return {
            "id": self.feedback_id,
            "kind": self.kind,
            "title": self.title,
            "source": self.source_id,
            "content_type": self.content_type,
            "topics": self.topics,
            "note": self.note,
        }


def recent_feedback(
    conn: sqlite3.Connection, now: datetime, window_days: int, limit: int
) -> list[FeedbackEntry]:
    """近 window_days 天内的个性化信号，按时间新到旧，最多 limit 条。"""
    since = db.format_time(now - timedelta(days=window_days))
    marks = ",".join("?" * len(SIGNAL_KINDS))
    rows = conn.execute(
        f"""
        SELECT f.id, f.item_id, f.kind, f.note, f.ts, i.title, i.source_id
        FROM feedback f JOIN items i ON i.id = f.item_id
        WHERE f.kind IN ({marks}) AND f.ts >= ?
        ORDER BY f.ts DESC, f.id DESC
        LIMIT ?
        """,
        (*SIGNAL_KINDS, since, limit),
    ).fetchall()
    l1 = db.latest_assessments(conn, "L1")
    return [
        FeedbackEntry(
            feedback_id=r["id"],
            item_id=r["item_id"],
            kind=r["kind"],
            ts=r["ts"],
            title=r["title"],
            source_id=r["source_id"],
            note=r["note"],
            content_type=l1.get(r["item_id"], {}).get("content_type"),
            topics=l1.get(r["item_id"], {}).get("topics", []),
        )
        for r in rows
    ]


def polarity_examples(
    entries: list[FeedbackEntry], per_polarity: int
) -> tuple[list[FeedbackEntry], list[FeedbackEntry]]:
    """(正例, 负例)：每个条目只取最新的一次 👍 / 👎 / 学了。entries 须按时间新到旧。"""
    positive: list[FeedbackEntry] = []
    negative: list[FeedbackEntry] = []
    seen: set[int] = set()
    for e in entries:
        if e.kind not in POSITIVE_KINDS + NEGATIVE_KINDS or e.item_id in seen:
            continue
        seen.add(e.item_id)
        bucket = positive if e.kind in POSITIVE_KINDS else negative
        if len(bucket) < per_polarity:
            bucket.append(e)
    return positive, negative


def _describe(e: FeedbackEntry) -> str:
    extra = [e.source_id]
    if e.content_type:
        extra.append(e.content_type)
    if e.topics:
        extra.append("topics: " + ", ".join(e.topics))
    note = f"；备注：{e.note}" if e.note else ""
    return f"- [{KIND_LABELS[e.kind]}] {e.title}（{'；'.join(extra)}）{note}"


def few_shot_block(entries: list[FeedbackEntry], per_polarity: int) -> str:
    """L1 system prompt 末尾的"最近反馈样例"段；没有正 / 负反馈时返回空串。"""
    if per_polarity <= 0:
        return ""
    positive, negative = polarity_examples(entries, per_polarity)
    if not positive and not negative:
        return ""
    lines = [
        "## 最近反馈样例",
        "",
        "以下是用户最近对推荐条目的真实反应，用来校准 relevance / quality 的判断；"
        "参考其中体现的偏好，不要机械地只推相似标题。",
    ]
    if positive:
        lines += ["", "用户喜欢（👍 / 学了）："] + [_describe(e) for e in positive]
    if negative:
        lines += ["", "用户不喜欢（👎）："] + [_describe(e) for e in negative]
    return "\n".join(lines)
