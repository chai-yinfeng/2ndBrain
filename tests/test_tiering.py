from datetime import date

import pytest

from secondbrain.models import EstimateSettings, FilterSettings, ScoreSettings
from secondbrain.pipeline.tiering import (
    TOP_LANES,
    Candidate,
    apply_backlog,
    eligible_lanes,
    estimate_minutes,
    rank_score,
    select_for_l2,
    tier,
)

TODAY = date(2026, 11, 4)
FS = FilterSettings()  # daily_budget_minutes=105, lanes 3/10/2/1, deep_read_ratio=0.6


def cand(item_id: int, **kw) -> Candidate:
    base = dict(
        score=8.0,
        relevance=8,
        quality=7,
        novelty=7,
        refresher_value=2,
        content_type="frontier",
        role="core",
        est_minutes=10,
        has_l2=True,
    )
    return Candidate(item_id=item_id, **(base | kw))


def lanes_of(placements) -> dict[int, str]:
    return {p.item_id: p.lane for p in placements}


def top_minutes(placements) -> int:
    return sum(p.est_minutes for p in placements if p.lane in TOP_LANES)


def test_rank_score_formula_and_trust():
    s = ScoreSettings()
    # (0.45*8 + 0.3*6 + 0.25*max(4, 9)) / 1.0 = 7.65
    assert rank_score(8, 6, 4, 9, 3, s) == pytest.approx(7.65)
    assert rank_score(8, 6, 4, 9, 5, s) == pytest.approx(7.65 * 1.2)
    assert rank_score(8, 6, 4, 9, 1, s) == pytest.approx(7.65 * 0.8)


def test_estimate_minutes():
    s = EstimateSettings()
    assert estimate_minutes(12_000, False, s) == 10
    assert estimate_minutes(12_000, True, s) == 20
    assert estimate_minutes(10, False, s) == s.min_minutes
    assert estimate_minutes(10_000_000, True, s) == s.max_minutes


def test_total_minutes_within_budget():
    cands = [cand(i, score=9 - i * 0.1, est_minutes=25) for i in range(12)]
    placements = tier(cands, FS, TODAY)
    assert top_minutes(placements) <= FS.daily_budget_minutes
    # 25 分钟一条，105 分钟只放得下 4 条
    assert sum(p.lane in TOP_LANES for p in placements) == 4
    assert all(p.lane == "fyi" for p in placements if p.lane not in TOP_LANES)


def test_lane_caps_are_upper_bounds():
    cands = [cand(i, score=9, est_minutes=3) for i in range(20)]
    placements = tier(cands, FS, TODAY)
    counts = {lane: sum(p.lane == lane for p in placements) for lane in TOP_LANES}
    assert counts["must_read"] == 3
    assert counts["worth_it"] == 10
    ranks = sorted(p.rank for p in placements if p.lane == "must_read")
    assert ranks == [1, 2, 3]


def test_lanes_may_be_empty():
    assert tier([], FS, TODAY) == []
    placements = tier([cand(1, score=2, relevance=1, quality=2)], FS, TODAY)
    assert lanes_of(placements) == {1: "archived"}


def test_refresher_explainer_goes_to_refresher_not_must_read():
    explainer = cand(1, score=9.5, content_type="explainer", refresher_value=9, novelty=3)
    frontier = cand(2, score=8.0)
    placements = tier([explainer, frontier], FS, TODAY)
    assert lanes_of(placements) == {1: "refresher", 2: "must_read"}
    assert "must_read" not in eligible_lanes(explainer, FS.thresholds)


def test_refresher_does_not_consume_must_read_slots():
    refreshers = [
        cand(i, content_type="survey", refresher_value=9, novelty=2, est_minutes=5)
        for i in range(1, 3)
    ]
    frontiers = [cand(i, est_minutes=5) for i in range(10, 13)]
    placements = lanes_of(tier(refreshers + frontiers, FS, TODAY))
    assert sorted(i for i, lane in placements.items() if lane == "must_read") == [10, 11, 12]
    assert sorted(i for i, lane in placements.items() if lane == "refresher") == [1, 2]


def test_deep_read_flag():
    deep = cand(1, est_minutes=70)  # > 105 * 0.6 = 63
    (p,) = tier([deep], FS, TODAY)
    assert p.lane == "must_read" and p.is_deep_read
    (p,) = tier([cand(2, est_minutes=60)], FS, TODAY)
    assert not p.is_deep_read


def test_deep_read_that_does_not_fit_is_deferred():
    first = cand(1, score=9.5, est_minutes=50)
    deep = cand(2, score=9.0, est_minutes=70)
    small = cand(3, score=8.5, est_minutes=20)
    placements = {p.item_id: p for p in tier([first, deep, small], FS, TODAY)}
    assert placements[1].lane == "must_read"
    assert placements[2].lane == "deferred"
    assert placements[2].is_deep_read
    assert placements[2].deferred_from == TODAY.isoformat()
    assert placements[3].lane == "must_read"


def test_non_deep_item_that_does_not_fit_falls_to_fyi():
    placements = lanes_of(
        tier([cand(1, est_minutes=60), cand(2, score=7.9, est_minutes=50)], FS, TODAY)
    )
    assert placements == {1: "must_read", 2: "fyi"}


def test_deferred_items_are_considered_first():
    deferred = cand(1, score=7.6, est_minutes=70, deferred_from="2026-11-03")
    better = cand(2, score=9.5, est_minutes=50)
    placements = {p.item_id: p for p in tier([deferred, better], FS, TODAY)}
    assert placements[1].lane == "must_read"
    assert placements[1].deferred_from == "2026-11-03"
    assert placements[2].lane == "fyi"  # 预算只剩 35 分钟


def test_expired_deferred_items_go_to_fyi():
    old = cand(1, est_minutes=70, deferred_from="2026-10-30")  # 5 天 > backlog_max_days=3
    (p,) = tier([old], FS, TODAY)
    assert p.lane == "fyi"


def test_serendipity_rules():
    expansion = cand(1, role="expansion", quality=8, relevance=2, score=5)
    low_quality_expansion = cand(2, role="expansion", quality=6, relevance=9, score=9)
    off_topic = cand(3, quality=9, relevance=3, score=6.5)
    th = FS.thresholds
    assert eligible_lanes(expansion, th) == ["serendipity"]
    assert eligible_lanes(low_quality_expansion, th) == []
    assert "serendipity" in eligible_lanes(off_topic, th)
    placements = lanes_of(tier([expansion, low_quality_expansion, off_topic], FS, TODAY))
    assert sum(lane == "serendipity" for lane in placements.values()) == 1
    assert placements[2] == "fyi"  # expansion 只进意外栏


def test_items_without_l2_never_reach_top_lanes():
    (p,) = tier([cand(1, score=11, has_l2=False)], FS, TODAY)
    assert p.lane == "fyi"


def test_respects_slots_and_minutes_already_used_today():
    placements = tier(
        [cand(1, est_minutes=10), cand(2, est_minutes=10)],
        FS,
        TODAY,
        already_counts={"must_read": 2},
        already_minutes=90,
    )
    by_id = {p.item_id: p for p in placements}
    assert (by_id[1].lane, by_id[1].rank) == ("must_read", 3)
    assert by_id[2].lane == "fyi"  # 必读满了，剩余 5 分钟也放不下


def test_apply_backlog():
    must_reads = [(1, "2026-11-04"), (2, "2026-11-03"), (3, "2026-11-01"), (4, "2026-10-31")]
    kept, demoted = apply_backlog(must_reads, TODAY, max_days=3)
    assert kept == [2, 3]
    assert demoted == [4]


def test_select_for_l2_reserves_refresher_and_serendipity():
    fs = FilterSettings.model_validate({"l1": {"top_n": 4}})
    core = [cand(i, score=9 - i * 0.1, has_l2=False) for i in range(10)]
    seren = cand(100, role="expansion", quality=9, relevance=2, score=4, has_l2=False)
    refresh = cand(
        200, content_type="explainer", refresher_value=9, novelty=2, score=5, has_l2=False
    )
    chosen = select_for_l2(core + [seren, refresh], fs)
    assert len(chosen) == 4
    assert 100 in chosen and 200 in chosen
    assert chosen[2:] == [0, 1]


# ---------- lane_budget_reserve（ADR 0007） ----------


def _musts(n: int, minutes: int) -> list[Candidate]:
    return [cand(i, score=9.5 - i * 0.01, est_minutes=minutes) for i in range(n)]


def _refresher(item_id: int, minutes: int = 10) -> Candidate:
    return cand(
        item_id,
        content_type="explainer",
        refresher_value=9,
        novelty=2,
        score=6.5,
        est_minutes=minutes,
    )


def _serendipity(item_id: int, minutes: int = 8) -> Candidate:
    return cand(item_id, role="expansion", quality=9, relevance=2, score=5, est_minutes=minutes)


def test_reserve_keeps_budget_for_refresher_and_serendipity():
    placements = tier(_musts(3, 30) + [_refresher(100), _serendipity(200)], FS, TODAY)
    lanes = lanes_of(placements)
    # 105 − 15 − 10 = 80 分钟可用于必读：只放得下 2 条 30 分钟的
    assert sum(lane == "must_read" for lane in lanes.values()) == 2
    assert lanes[100] == "refresher"
    assert lanes[200] == "serendipity"
    assert top_minutes(placements) <= FS.daily_budget_minutes


def test_must_read_never_uses_reserved_minutes():
    placements = tier(_musts(5, 20) + [_refresher(100, 15), _serendipity(200, 10)], FS, TODAY)
    must_minutes = sum(p.est_minutes for p in placements if p.lane == "must_read")
    assert must_minutes <= FS.daily_budget_minutes - 15 - 10


def test_reserve_is_released_without_candidates():
    placements = tier(_musts(3, 30), FS, TODAY)
    assert sum(p.lane == "must_read" for p in placements) == 3  # 90 ≤ 105，保留额度已释放


def test_unused_reserve_flows_to_worth_it():
    # 温故条目只用掉 5 分钟，剩余的保留额度在温故栏分配完后释放给值得读
    worth = [cand(i, score=6.5, relevance=5, est_minutes=20) for i in range(10, 15)]
    placements = tier(_musts(2, 30) + [_refresher(100, 5)] + worth, FS, TODAY)
    lanes = lanes_of(placements)
    assert lanes[100] == "refresher"
    worth_minutes = sum(p.est_minutes for p in placements if p.lane == "worth_it")
    assert worth_minutes == 40  # 105 − 60 − 5 = 40，正好两条
    assert top_minutes(placements) <= FS.daily_budget_minutes


def test_reserve_can_be_disabled():
    fs = FilterSettings.model_validate({"lane_budget_reserve": {"refresher": 0, "serendipity": 0}})
    placements = tier(_musts(3, 30) + [_refresher(100, 20)], fs, TODAY)
    lanes = lanes_of(placements)
    assert sum(lane == "must_read" for lane in lanes.values()) == 3
    assert lanes[100] == "fyi"  # 90 + 20 > 105
