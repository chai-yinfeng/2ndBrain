"""L1 粗筛：cheap 模型批量打分。system 前缀固定（profile + 规则）以利用上下文缓存。"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from secondbrain.llm.schemas import L1Assessment


def interleave_by_source(rows: list[sqlite3.Row], limit: int | None) -> list[sqlite3.Row]:
    """按信源轮询取条目（信源按 trust 降序，信源内按时间新到旧），避免单个高产信源占满名额。"""
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:  # rows 已按时间倒序
        groups[row["source_id"]].append(row)
    order = sorted(groups, key=lambda sid: (-groups[sid][0]["trust"], sid))
    picked: list[sqlite3.Row] = []
    depth = 0
    while any(depth < len(groups[sid]) for sid in order):
        for sid in order:
            if depth < len(groups[sid]):
                picked.append(groups[sid][depth])
                if limit is not None and len(picked) >= limit:
                    return picked
        depth += 1
    return picked


def l1_messages(system: str, batch: list[sqlite3.Row], item_chars: int) -> list[dict[str, str]]:
    items = [
        {
            "id": row["id"],
            "title": row["title"],
            "source": row["source_id"],
            "text": (row["content"] or row["summary"] or "")[:item_chars],
        }
        for row in batch
    ]
    user = "请评估以下条目，按规定格式输出 json：\n" + json.dumps(
        {"items": items}, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def match_results(batch: list[sqlite3.Row], results: list[L1Assessment]) -> dict[int, L1Assessment]:
    """按 id 对齐模型输出；模型多给或少给的条目被忽略（少给的条目留待下次）。"""
    wanted = {row["id"] for row in batch}
    return {r.id: r for r in results if r.id in wanted}
