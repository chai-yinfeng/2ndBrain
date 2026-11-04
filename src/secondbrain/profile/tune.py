"""信源 trust 调整建议：按每个信源条目的当前反馈统计正向比例（ADR 0013）。

有收获为正；不感兴趣 / 没价值为负；太浅 / 太难 / 稍后读不计。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from secondbrain import db
from secondbrain.models import PersonalizeSettings, SourceConfig

TUNE_POSITIVE = ("gained",)
TUNE_NEGATIVE = ("dismiss", "worthless")


@dataclass
class SourceStats:
    source_id: str
    trust: int
    positive: int = 0  # 当前状态为 有收获 的条目数
    negative: int = 0  # 当前状态为 不感兴趣 / 没价值 的条目数
    suggested: int | None = None  # 建议的新 trust；None 表示不调整
    reason: str = ""

    @property
    def samples(self) -> int:
        return self.positive + self.negative

    @property
    def ratio(self) -> float | None:
        return self.positive / self.samples if self.samples else None


def source_stats(
    conn: sqlite3.Connection,
    sources: list[SourceConfig],
    now: datetime,
    s: PersonalizeSettings,
) -> list[SourceStats]:
    """每个条目只看当前状态（时间窗内的最新一次反馈），避免重复点击放大权重。"""
    since = db.format_time(now - timedelta(days=s.tune_window_days))
    rows = conn.execute(
        f"""
        SELECT i.source_id, f.kind FROM ({db.LATEST_FEEDBACK}) f JOIN items i ON i.id = f.item_id
        WHERE f.ts >= ?
        """,
        (since,),
    ).fetchall()
    stats = {src.id: SourceStats(src.id, src.trust) for src in sources}
    for r in rows:
        st = stats.get(r["source_id"])
        if st is None:
            continue
        if r["kind"] in TUNE_NEGATIVE:
            st.negative += 1
        elif r["kind"] in TUNE_POSITIVE:
            st.positive += 1
    for st in stats.values():
        _suggest(st, s)
    return list(stats.values())


def _suggest(st: SourceStats, s: PersonalizeSettings) -> None:
    if st.samples < s.tune_min_samples:
        st.reason = f"样本不足（{st.samples} < {s.tune_min_samples}）"
        return
    ratio = st.ratio
    if ratio >= s.tune_raise_ratio and st.trust < 5:
        st.suggested, st.reason = st.trust + 1, f"正向比例 {ratio:.0%} ≥ {s.tune_raise_ratio:.0%}"
    elif ratio <= s.tune_lower_ratio and st.trust > 1:
        st.suggested, st.reason = st.trust - 1, f"正向比例 {ratio:.0%} ≤ {s.tune_lower_ratio:.0%}"
    else:
        st.reason = f"正向比例 {ratio:.0%}，维持"


_ID_LINE = re.compile(r"^\s*-\s+id:\s*([\w-]+)\s*(#.*)?$")
_TRUST_LINE = re.compile(r"^(\s*trust:\s*)(\d+)(.*)$")


def apply_trust_changes(yaml_text: str, changes: dict[str, int]) -> tuple[str, list[str]]:
    """只做行级替换：把各信源条目中的 `trust: N` 改为新值，保留注释与其余格式。
    返回 (新文本, 未找到 trust 行的信源 id)。"""
    lines = yaml_text.splitlines(keepends=True)
    current: str | None = None
    done: set[str] = set()
    for i, line in enumerate(lines):
        m = _ID_LINE.match(line)
        if m:
            current = m.group(1)
            continue
        t = _TRUST_LINE.match(line.rstrip("\n"))
        if t and current in changes and current not in done:
            ending = "\n" if line.endswith("\n") else ""
            lines[i] = f"{t.group(1)}{changes[current]}{t.group(3)}{ending}"
            done.add(current)
    return "".join(lines), sorted(set(changes) - done)
