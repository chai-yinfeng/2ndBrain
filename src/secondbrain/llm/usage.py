"""LLM 用量记录与费用估算。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from secondbrain.db import format_time
from secondbrain.llm.base import Usage
from secondbrain.models import ModelPrice, PricingSchedule


def is_peak(ts: datetime, schedule: PricingSchedule) -> bool:
    utc = ts.astimezone(UTC)
    if utc.weekday() not in schedule.peak_weekdays_utc:
        return False
    return any(start <= utc.hour < end for start, end in schedule.peak_hours_utc)


def full_price(usage: Usage, pricing: dict[str, ModelPrice]) -> float:
    """高峰（全价）美元；价格表中没有的模型（如 fake）计 0。"""
    price = pricing.get(usage.model)
    if price is None:
        return 0.0
    return (
        usage.cache_hit_tokens * price.input_cache_hit
        + usage.cache_miss_tokens * price.input_cache_miss
        + usage.completion_tokens * price.output
    ) / 1_000_000


def estimate_cost(
    usage: Usage, pricing: dict[str, ModelPrice], schedule: PricingSchedule, ts: datetime
) -> tuple[float, bool]:
    """返回 (估算美元, 是否高峰)。价格表中没有的模型（如 fake）计 0。"""
    peak = is_peak(ts, schedule)
    cost = full_price(usage, pricing)
    return (cost if peak else cost * schedule.off_peak_multiplier), peak


def record_call(
    conn: sqlite3.Connection,
    backend: str,
    stage: str,
    usage: Usage,
    ts: datetime,
    pricing: dict[str, ModelPrice],
    schedule: PricingSchedule,
) -> float:
    cost, peak = estimate_cost(usage, pricing, schedule, ts)
    conn.execute(
        """
        INSERT INTO llm_calls (ts, backend, stage, model, prompt_tokens, cache_hit_tokens,
                               cache_miss_tokens, completion_tokens, cost_usd, is_peak)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            format_time(ts),
            backend,
            stage,
            usage.model,
            usage.prompt_tokens,
            usage.cache_hit_tokens,
            usage.cache_miss_tokens,
            usage.completion_tokens,
            cost,
            int(peak),
        ),
    )
    return cost


def summarize(conn: sqlite3.Connection, since: datetime | None) -> list[sqlite3.Row]:
    where, params = ("WHERE ts >= ?", [format_time(since)]) if since else ("", [])
    return conn.execute(
        f"""
        SELECT stage, model, COUNT(*) AS calls,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(cache_hit_tokens) AS cache_hit_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(cost_usd) AS cost_usd
        FROM llm_calls {where}
        GROUP BY stage, model
        ORDER BY stage, model
        """,
        params,
    ).fetchall()
