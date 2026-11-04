"""近期反馈汇总：供 L1 few-shot、profile 修改提案与信源 trust 调整使用。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from secondbrain import db

# 参与个性化的反馈类型（"稍后读"只是阅读安排，不表达偏好；ADR 0013）
SIGNAL_KINDS = ("dismiss", "too_easy", "gained", "too_hard", "worthless")
POSITIVE_KINDS = ("gained",)
NEGATIVE_KINDS = ("dismiss", "worthless")
CALIBRATION_KINDS = ("too_easy", "too_hard")  # 校准样例：不代表喜欢或不喜欢
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
    """近 window_days 天内的个性化信号，按时间新到旧，最多 limit 条。
    只看每个条目的当前状态（最新一次反馈）：被覆盖或撤销的反馈不再作为信号。"""
    since = db.format_time(now - timedelta(days=window_days))
    marks = ",".join("?" * len(SIGNAL_KINDS))
    rows = conn.execute(
        f"""
        SELECT f.id, f.item_id, f.kind, f.note, f.ts, i.title, i.source_id
        FROM ({db.LATEST_FEEDBACK}) f JOIN items i ON i.id = f.item_id
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


def _pick(entries: list[FeedbackEntry], kinds: tuple[str, ...], cap: int) -> list[FeedbackEntry]:
    picked: list[FeedbackEntry] = []
    seen: set[int] = set()
    for e in entries:
        if e.kind in kinds and e.item_id not in seen and len(picked) < cap:
            seen.add(e.item_id)
            picked.append(e)
    return picked


def polarity_examples(
    entries: list[FeedbackEntry], per_polarity: int
) -> tuple[list[FeedbackEntry], list[FeedbackEntry]]:
    """(正例 = 有收获, 负例 = 不感兴趣 / 没价值)，各最多 per_polarity 条。
    entries 须按时间新到旧。"""
    positive = _pick(entries, POSITIVE_KINDS, per_polarity)
    return positive, _pick(entries, NEGATIVE_KINDS, per_polarity)


def calibration_examples(
    entries: list[FeedbackEntry], per_kind: int
) -> tuple[list[FeedbackEntry], list[FeedbackEntry]]:
    """(太浅, 太难) 的校准样例，各最多 per_kind 条。"""
    return _pick(entries, ("too_easy",), per_kind), _pick(entries, ("too_hard",), per_kind)


def _describe(e: FeedbackEntry) -> str:
    extra = [e.source_id]
    if e.content_type:
        extra.append(e.content_type)
    if e.topics:
        extra.append("topics: " + ", ".join(e.topics))
    note = f"；备注：{e.note}" if e.note else ""
    return f"- [{KIND_LABELS[e.kind]}] {e.title}（{'；'.join(extra)}）{note}"


def few_shot_block(entries: list[FeedbackEntry], per_polarity: int) -> str:
    """L1 system prompt 末尾的"最近反馈样例"段；没有任何样例时返回空串。"""
    if per_polarity <= 0:
        return ""
    positive, negative = polarity_examples(entries, per_polarity)
    too_easy, too_hard = calibration_examples(entries, per_polarity)
    if not (positive or negative or too_easy or too_hard):
        return ""
    lines = [
        "## 最近反馈样例",
        "",
        "以下是用户最近对推荐条目的真实反应，用来校准 relevance / novelty / quality 的判断；"
        "参考其中体现的偏好，不要机械地只推相似标题。",
    ]
    if positive:
        lines += ["", "用户有收获（正例）："] + [_describe(e) for e in positive]
    if negative:
        lines += ["", "用户不感兴趣 / 觉得没价值（反例）："] + [_describe(e) for e in negative]
    if too_easy:
        lines += ["", "用户觉得太浅（已掌握，同类内容 novelty 应低）："]
        lines += [_describe(e) for e in too_easy]
    if too_hard:
        lines += ["", "用户觉得太难（缺前置知识，不代表不感兴趣）："]
        lines += [_describe(e) for e in too_hard]
    return "\n".join(lines)


def too_hard_prerequisites(
    conn: sqlite3.Connection, now: datetime, window_days: int, max_chars: int
) -> str:
    """近期当前状态为"太难"的条目，其 L2 输出中的前置知识（去重，限 max_chars）。
    注入 L1 的 user 消息；没有时返回空串。"""
    if max_chars <= 0:
        return ""
    since = db.format_time(now - timedelta(days=window_days))
    rows = conn.execute(
        f"""
        SELECT a.data FROM ({db.LATEST_FEEDBACK}) f
        JOIN assessments a ON a.item_id = f.item_id AND a.stage = 'L2'
        WHERE f.kind = 'too_hard' AND f.ts >= ?
        ORDER BY f.id DESC, a.id DESC
        """,
        (since,),
    ).fetchall()
    names: list[str] = []
    for r in rows:
        for name in json.loads(r["data"]).get("prerequisites", []):
            if name and name.lower() not in {n.lower() for n in names}:
                names.append(name)
    if not names:
        return ""
    head = "## 用户近期觉得太难的内容所需前置知识\n\n"
    tail = "\n\n讲清这些前置知识的 explainer / survey 应提高 refresher_value。"
    budget = max_chars - len(head) - len(tail)
    kept: list[str] = []
    for name in names:
        if len("、".join([*kept, name])) > budget:
            break
        kept.append(name)
    return f"{head}{'、'.join(kept)}{tail}" if kept else ""
