"""概念复习调度（SM-2 简化版，ADR 0010）。复习状态保存在概念页的 frontmatter 中。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from secondbrain.profile.loader import MASTERY_LEVELS
from secondbrain.wiki.store import DEFAULT_EASE, Page, Wiki

GRADES = {
    "remembered": "还记得",
    "fuzzy": "模糊",
    "forgot": "忘了",
    "needs_refresh": "需要回炉",
    "mastered": "已扎实",
}
GRADE_HINTS = {
    "remembered": "回忆顺利：间隔拉长",
    "fuzzy": "有点模糊：间隔略增，扎实降为想回炉",
    "forgot": "忘了：间隔重置为 1 天，掌握度降一级",
    "needs_refresh": "想系统回炉：掌握度改为想回炉，立即到期",
    "mastered": "已经扎实：掌握度改为扎实，间隔拉长到至少一个月（可配置）",
}
DEFAULT_MASTERED_DAYS = 30
MIN_EASE, MAX_EASE = 1.3, 3.0
SOLID_AFTER_DAYS = 21  # "还记得"且间隔达到该天数时，想回炉 → 扎实
_RANK = {level: i for i, level in enumerate(MASTERY_LEVELS)}  # 陌生 < 接触过 < 想回炉 < 扎实


@dataclass(frozen=True)
class ReviewOutcome:
    slug: str
    grade: str
    interval_before: int
    interval_after: int
    ease_after: float
    mastery_before: str | None
    mastery_after: str | None
    next_due: date


def due_date(page: Page) -> date | None:
    last = page.meta.get("last_reviewed")
    if not last:
        return None
    last_day = last if isinstance(last, date) else date.fromisoformat(str(last))
    interval = page.meta.get("interval_days")
    # 间隔 0 表示立即到期（复习时点了"需要回炉"）
    return last_day + timedelta(days=1 if interval is None else int(interval))


def due_concepts(wiki: Wiki, day: date) -> list[Page]:
    """到期（next_due ≤ day）的概念页，最早到期的在前。"""
    due = [(d, p) for p in wiki.concepts() if (d := due_date(p)) is not None and d <= day]
    return [p for _, p in sorted(due, key=lambda x: (x[0], x[1].slug))]


def _step(mastery: str | None, delta: int) -> str | None:
    if mastery not in _RANK:
        return mastery
    rank = min(max(_RANK[mastery] + delta, 0), len(MASTERY_LEVELS) - 1)
    return MASTERY_LEVELS[rank]


def schedule(
    interval: int,
    ease: float,
    mastery: str | None,
    grade: str,
    mastered_days: int = DEFAULT_MASTERED_DAYS,
) -> tuple[int, float, str | None]:
    """返回 (新间隔天数, 新 ease, 新掌握度)。

    - 还记得：间隔 × ease（首次至少 2 天），ease +0.1；陌生 → 接触过；
      间隔达到 21 天时 想回炉 → 扎实。
    - 模糊：间隔 × 1.2（至少 1 天），ease −0.15；扎实 → 想回炉。
    - 忘了：间隔重置为 1 天，ease −0.2；掌握度降一级（陌生不再降）。
    - 需要回炉：掌握度 → 想回炉，间隔 0（立即到期），ease 不变。
    - 已扎实：掌握度 → 扎实，间隔 max(间隔 × ease, mastered_days)，ease 不变。
    ease 限制在 [1.3, 3.0]。
    """
    interval = max(int(interval or 1), 1)
    if grade == "remembered":
        new_interval = max(round(interval * ease), 2)
        new_ease = ease + 0.1
        new_mastery = "接触过" if mastery == "陌生" else mastery
        if new_mastery == "想回炉" and new_interval >= SOLID_AFTER_DAYS:
            new_mastery = "扎实"
    elif grade == "fuzzy":
        new_interval = max(round(interval * 1.2), 1)
        new_ease = ease - 0.15
        new_mastery = "想回炉" if mastery == "扎实" else mastery
    elif grade == "forgot":
        new_interval = 1
        new_ease = ease - 0.2
        new_mastery = _step(mastery, -1)
    elif grade == "needs_refresh":
        new_interval, new_ease, new_mastery = 0, ease, "想回炉"
    elif grade == "mastered":
        new_interval = max(round(interval * ease), mastered_days)
        new_ease, new_mastery = ease, "扎实"
    else:
        raise ValueError(f"未知的复习结果：{grade!r}")
    return new_interval, round(min(max(new_ease, MIN_EASE), MAX_EASE), 2), new_mastery


def review(
    wiki: Wiki, page: Page, grade: str, day: date, mastered_days: int = DEFAULT_MASTERED_DAYS
) -> ReviewOutcome:
    if grade not in GRADES:
        raise ValueError(f"未知的复习结果：{grade!r}")
    interval = int(page.meta.get("interval_days") or 1)
    ease = float(page.meta.get("ease") or DEFAULT_EASE)
    mastery = page.meta.get("mastery")
    new_interval, new_ease, new_mastery = schedule(interval, ease, mastery, grade, mastered_days)
    page.meta.update(
        interval_days=new_interval,
        ease=new_ease,
        mastery=new_mastery,
        last_reviewed=day.isoformat(),
        updated=day.isoformat(),
    )
    wiki.save_page(page)
    change = f"，掌握度 {mastery} → {new_mastery}" if new_mastery != mastery else ""
    line = f"[[{page.slug}]] {GRADES[grade]}：间隔 {interval} → {new_interval} 天"
    wiki.append_log(day, "review", page.title, [f"{line}，ease {new_ease}{change}"])
    return ReviewOutcome(
        page.slug,
        grade,
        interval,
        new_interval,
        new_ease,
        mastery,
        new_mastery,
        day + timedelta(days=new_interval),
    )
