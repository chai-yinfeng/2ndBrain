"""profile.md 解析。掌握度表格式：| 领域 | 掌握度 | 优先级 | 说明 |。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MASTERY_LEVELS = ("陌生", "接触过", "想回炉", "扎实")
_REQUIRED_COLUMNS = ("领域", "掌握度")


@dataclass
class DomainMastery:
    domain: str
    mastery: str | None  # 归一化为 MASTERY_LEVELS 之一；无法识别时为 None
    raw_mastery: str
    priority: str = ""
    note: str = ""


@dataclass
class Profile:
    text: str
    domains: list[DomainMastery] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 掌握度表在 text.splitlines() 中的位置：[table_start, table_end)，含表头与分隔行
    table_start: int | None = None
    table_end: int | None = None
    header: list[str] = field(default_factory=list)

    @property
    def parsed(self) -> bool:
        return bool(self.domains)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(c and set(c) <= set("-: ") for c in cells)


def _normalize_mastery(raw: str) -> str | None:
    # 允许 "接触过（有专业背景）" 这类带补充说明的写法
    return next((level for level in MASTERY_LEVELS if raw.startswith(level)), None)


def parse_profile(text: str) -> Profile:
    profile = Profile(text=text)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        header = _cells(line)
        if not all(col in header for col in _REQUIRED_COLUMNS):
            continue
        if i + 1 >= len(lines) or not _is_separator(_cells(lines[i + 1])):
            continue
        idx = {name: header.index(name) for name in header}
        profile.table_start, profile.header = i, header
        profile.table_end = i + 2
        for row_no, row in enumerate(lines[i + 2 :], start=1):
            if not row.strip().startswith("|"):
                break
            profile.table_end = i + 2 + row_no
            cells = _cells(row)
            cells += [""] * (len(header) - len(cells))
            values = {name: cells[j] for name, j in idx.items()}
            raw = values["掌握度"]
            mastery = _normalize_mastery(raw)
            if mastery is None:
                profile.warnings.append(f"掌握度表第 {row_no} 行的掌握度无法识别：{raw!r}")
            profile.domains.append(
                DomainMastery(
                    domain=values["领域"],
                    mastery=mastery,
                    raw_mastery=raw,
                    priority=values.get("优先级", ""),
                    note=values.get("说明", ""),
                )
            )
        break
    if not profile.domains:
        profile.warnings.append(
            "未找到领域掌握度表（| 领域 | 掌握度 | 优先级 | 说明 |），将直接使用 profile 全文"
        )
    return profile


def load_profile(path: Path) -> Profile:
    return parse_profile(path.read_text(encoding="utf-8"))


def render_mastery(profile: Profile) -> str:
    """供 prompt 使用的掌握度摘要；解析失败时提示模型直接参考画像全文。"""
    if not profile.parsed:
        return "## 领域掌握度\n\n（未能解析掌握度表，请直接依据上面的画像全文判断。）"
    lines = [
        "## 领域掌握度（解析结果）",
        "",
        "取值：陌生 / 接触过 / 想回炉（熟悉但不扎实）/ 扎实。",
        "",
    ]
    for d in profile.domains:
        mastery = d.mastery or f"未识别（原文：{d.raw_mastery}）"
        priority = f"；优先级 {d.priority}" if d.priority else ""
        lines.append(f"- {d.domain}：{mastery}{priority}")
    return "\n".join(lines)


# 需要回炉的掌握度（"扎实"之外的都算）
WEAK_LEVELS = ("陌生", "接触过", "想回炉")


def _core_term(text: str) -> str:
    """ "MLA (Multi-head Latent Attention)" → "mla"：去掉括号内的补充说明。"""
    for bracket in ("(", "（"):
        text = text.split(bracket, 1)[0]
    return text.strip().lower()


def weak_prerequisites(profile: Profile, prerequisites: list[str]) -> list[str]:
    """前置概念中，出现在用户掌握度不扎实的领域（领域名或说明）里的那些。"""
    weak_text = " ".join(
        f"{d.domain} {d.note}".lower() for d in profile.domains if d.mastery in WEAK_LEVELS
    )
    result = []
    for prereq in prerequisites:
        core = _core_term(prereq)
        if len(core) >= 3 and core in weak_text:
            result.append(prereq)
    return result
