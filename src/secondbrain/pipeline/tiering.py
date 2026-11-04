"""分级（纯函数，无 IO）：排序分、阅读时间估计、L2 选择、按时间预算分 lane、积压处理。

规则见 ADR 0003 / 0004 与 docs/design.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from secondbrain.llm.schemas import REFRESHER_TYPES
from secondbrain.models import EstimateSettings, FilterSettings, ScoreSettings, Thresholds

# 预算分配时各 lane 的处理顺序：必读优先，温故与意外名额少、放在值得读之前避免被挤掉。
LANE_ORDER = ("must_read", "refresher", "serendipity", "worth_it")
TOP_LANES = frozenset(LANE_ORDER)


def rank_score(
    relevance: float,
    quality: float,
    novelty: float,
    refresher_value: float,
    trust: int,
    s: ScoreSettings,
) -> float:
    """(w_rel·relevance + w_q·quality + w_v·max(novelty, refresher_value)) / Σw
    × (1 + trust_weight·(trust − 3))，结果约在 0–12。"""
    total_w = s.w_relevance + s.w_quality + s.w_value
    base = (
        s.w_relevance * relevance
        + s.w_quality * quality
        + s.w_value * max(novelty, refresher_value)
    ) / total_w
    return round(base * (1 + s.trust_weight * (trust - 3)), 3)


def estimate_minutes(content_chars: int, is_paper: bool, s: EstimateSettings) -> int:
    minutes = content_chars / s.chars_per_minute * (s.paper_multiplier if is_paper else 1.0)
    return int(min(s.max_minutes, max(s.min_minutes, round(minutes))))


@dataclass(frozen=True)
class Candidate:
    item_id: int
    score: float
    relevance: float
    quality: float
    novelty: float
    refresher_value: float
    content_type: str
    role: str
    est_minutes: int
    has_l2: bool = False
    deferred_from: str | None = None  # ISO 日期：首次被顺延的日期


@dataclass(frozen=True)
class Placement:
    item_id: int
    lane: str  # TOP_LANES | fyi | archived | deferred
    rank: int
    est_minutes: int
    is_deep_read: bool
    deferred_from: str | None = None


def is_refresher(c: Candidate, th: Thresholds) -> bool:
    return (
        c.content_type in REFRESHER_TYPES
        and c.refresher_value >= th.refresher_value
        and c.refresher_value >= c.novelty
    )


def is_serendipity(c: Candidate, th: Thresholds) -> bool:
    if c.quality < th.serendipity_quality:
        return False
    return c.role == "expansion" or c.relevance <= th.serendipity_max_relevance


def eligible_lanes(c: Candidate, th: Thresholds) -> list[str]:
    """候选可进入的前排 lane。只有经过 L2 的条目才能进入前排；expansion 只进意外栏。"""
    if not c.has_l2:
        return []
    if c.role == "expansion":
        return ["serendipity"] if is_serendipity(c, th) else []
    lanes: list[str] = []
    if is_refresher(c, th):
        lanes.append("refresher")  # 温故条目不占必读名额
    elif c.score >= th.must_read and c.relevance >= th.must_read_relevance:
        lanes.append("must_read")
    if is_serendipity(c, th):
        lanes.append("serendipity")
    if c.score >= th.worth_it:
        lanes.append("worth_it")
    return lanes


def select_for_l2(candidates: list[Candidate], fs: FilterSettings) -> list[int]:
    """从 L1 结果中选出进入 L2 的条目：先为意外 / 温故栏预留名额，其余按排序分取满 top_n。"""
    th, top_n = fs.thresholds, fs.l1.top_n
    chosen: list[int] = []

    def take(pool: list[Candidate], n: int) -> None:
        for c in pool:
            if len(chosen) >= top_n or n <= 0:
                return
            if c.item_id not in chosen:
                chosen.append(c.item_id)
                n -= 1

    seren = sorted((c for c in candidates if is_serendipity(c, th)), key=lambda c: -c.quality)
    refresh = sorted(
        (c for c in candidates if c.role != "expansion" and is_refresher(c, th)),
        key=lambda c: -c.refresher_value,
    )
    take(seren, fs.lanes.serendipity)
    take(refresh, fs.lanes.refresher)
    core = [c for c in candidates if c.role != "expansion"]
    take(sorted(core, key=lambda c: -c.score), top_n)
    return chosen


def _days_between(earlier: str, today: date) -> int:
    return (today - date.fromisoformat(earlier)).days


def tier(
    candidates: list[Candidate],
    fs: FilterSettings,
    today: date,
    already_counts: dict[str, int] | None = None,
    already_minutes: int = 0,
) -> list[Placement]:
    """按时间预算（主约束）与 lane 条数上限分级。

    - 顺延过的深读条目最先考虑；超过 backlog_max_days 仍未排上的不再顺延，降为 fyi。
    - 单条 est_minutes > 预算 × deep_read_ratio 为"深读"；深读条目放不进剩余预算时标记 deferred。
    - 非深读条目放不进预算时落到 fyi。lane 允许为空。
    - already_counts / already_minutes：当天先前 run 已占用的名额与时间（同一天多次 run 共享预算）。
    - lane_budget_reserve（ADR 0007）：温故 / 意外栏各自保留一段预算，其他 lane 不能占用；
      该栏没有合格候选（或名额已满）时不保留；该栏分配结束后，未用完的保留额度释放回总预算。
    """
    th = fs.thresholds
    budget = fs.daily_budget_minutes
    deep_limit = budget * fs.deep_read_ratio
    caps = fs.lanes.model_dump()
    counts = {lane: 0 for lane in LANE_ORDER} | dict(already_counts or {})
    left = budget - already_minutes
    placed: dict[int, Placement] = {}

    def expired(c: Candidate) -> bool:
        return c.deferred_from is not None and (
            _days_between(c.deferred_from, today) > fs.backlog_max_days
        )

    # 仍在保留中的额度：lane → 分钟
    reserved = {
        lane: minutes
        for lane, minutes in fs.lane_budget_reserve.items()
        if minutes > 0
        and lane in counts
        and counts[lane] < caps[lane]
        and any(lane in eligible_lanes(c, th) and not expired(c) for c in candidates)
    }

    def available(lane: str) -> int:
        """该 lane 可用的预算：总剩余减去其他 lane 仍在保留的额度。"""
        return left - sum(m for other, m in reserved.items() if other != lane)

    def try_place(c: Candidate, lane: str) -> bool:
        nonlocal left
        if counts[lane] >= caps[lane]:
            return False
        deep = c.est_minutes > deep_limit
        if c.est_minutes <= available(lane):
            counts[lane] += 1
            left -= c.est_minutes
            placed[c.item_id] = Placement(
                c.item_id, lane, counts[lane], c.est_minutes, deep, c.deferred_from
            )
            return True
        if deep:
            placed[c.item_id] = Placement(
                c.item_id, "deferred", 0, c.est_minutes, True, c.deferred_from or today.isoformat()
            )
            return True
        return False

    by_score = sorted(candidates, key=lambda c: -c.score)
    # 1) 顺延条目优先：放进第一个可用的 lane
    for c in by_score:
        if c.deferred_from is None or expired(c):
            continue
        for lane in LANE_ORDER:
            if lane in eligible_lanes(c, th) and try_place(c, lane):
                break
    # 2) 其余条目按 lane 顺序、分数从高到低填充
    for lane in LANE_ORDER:
        for c in by_score:
            if c.item_id in placed or expired(c) or lane not in eligible_lanes(c, th):
                continue
            if counts[lane] >= caps[lane]:
                break
            try_place(c, lane)
        reserved.pop(lane, None)  # 该栏分配结束，释放未用完的保留额度
    # 3) 剩下的进 fyi / archived
    rest = [c for c in by_score if c.item_id not in placed]
    ranks = {"fyi": 0, "archived": 0}
    for c in rest:
        lane = "fyi" if c.score >= th.fyi or expired(c) else "archived"
        ranks[lane] += 1
        placed[c.item_id] = Placement(
            c.item_id, lane, ranks[lane], c.est_minutes, c.est_minutes > deep_limit, c.deferred_from
        )
    return [placed[c.item_id] for c in by_score]


def apply_backlog(
    must_reads: list[tuple[int, str]], today: date, max_days: int
) -> tuple[list[int], list[int]]:
    """must_reads：此前各日 digest 中仍未读的必读 (item_id, 日期)。
    返回 (仍在积压中的 item_id, 超过 max_days 需降为 fyi 的 item_id)。当天的不算积压。"""
    kept, demoted = [], []
    for item_id, day in must_reads:
        age = _days_between(day, today)
        if age <= 0:
            continue
        (demoted if age > max_days else kept).append(item_id)
    return kept, demoted
