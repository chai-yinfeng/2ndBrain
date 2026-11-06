"""信源统计（只读，ADR 0018）：反馈分布与产出率。

反馈只影响 AI 判断（画像、few-shot、画像修改提案），不再用来建议或修改信源 trust；
trust 由用户手动设置。低产出停用建议保留，因为它只看产出、不看反馈。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from secondbrain import db
from secondbrain.models import PersonalizeSettings, SourceConfig

# /sources 上展示的反馈分布（各条目的当前状态）
DISTRIBUTION_KINDS = ("gained", "too_easy", "too_hard", "worthless", "dismiss", "later")


def feedback_distribution(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """各信源条目的当前反馈状态计数：source_id → {kind: 条数}（纯参考，不产生任何建议）。"""
    rows = conn.execute(
        f"""
        SELECT i.source_id, f.kind, COUNT(*) AS n
        FROM ({db.LATEST_FEEDBACK}) f JOIN items i ON i.id = f.item_id
        GROUP BY i.source_id, f.kind
        """
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["kind"] in DISTRIBUTION_KINDS:
            result.setdefault(r["source_id"], dict.fromkeys(DISTRIBUTION_KINDS, 0))[r["kind"]] = r[
                "n"
            ]
    return result


# ---------- 低产出信源（ADR 0016） ----------


@dataclass
class SourceYield:
    source_id: str
    items: int = 0
    l2: int = 0
    front: int = 0
    positive: int = 0
    negative: int = 0
    low_yield: bool = False


def source_yields(
    conn: sqlite3.Connection, sources: list[SourceConfig], now: datetime, s: PersonalizeSettings
) -> dict[str, SourceYield]:
    """近 yield_window_days 天的产出。低产出 = 已覆盖整个窗口（最早入库早于窗口起点，
    新加入的不算）、窗口内入库 ≥ low_yield_min_items、进入前排为 0、且仍启用。"""
    since = now - timedelta(days=s.yield_window_days)
    raw = db.source_yield(conn, since)
    result = {}
    for src in sources:
        r = raw.get(src.id)
        y = SourceYield(src.id)
        if r is not None:
            y.items, y.l2, y.front = r["items"], r["l2"], r["front"]
            y.positive, y.negative = r["positive"], r["negative"]
            covered = r["first_fetched"] is not None and r["first_fetched"] <= db.format_time(since)
            y.low_yield = (
                src.enabled and covered and y.items >= s.low_yield_min_items and y.front == 0
            )
        result[src.id] = y
    return result
