"""保留原始画像；模型只读取兴趣、优先级与说明，不读取历史掌握度。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DomainInterest:
    domain: str
    priority: str = ""
    note: str = ""


@dataclass
class Profile:
    text: str
    domains: list[DomainInterest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        """历史表格保留在原文，发送模型时去除掌握度列。"""
        result = []
        excluded = None
        legacy_section = False
        for line in self.text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                legacy_section = heading.startswith("领域掌握度") or heading == "掌握度"
                if legacy_section:
                    result.extend(["## 关注领域", ""])
                    excluded = None
                    continue
            cells = _cells(line) if line.strip().startswith("|") else []
            if legacy_section and not cells:
                continue
            if "领域" in cells and "掌握度" in cells:
                excluded = cells.index("掌握度")
            elif not cells:
                excluded = None
            if excluded is not None and cells:
                cells = [value for index, value in enumerate(cells) if index != excluded]
                result.append("| " + " | ".join(cells) + " |")
            else:
                result.append(line)
        return "\n".join(result)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_profile(text: str) -> Profile:
    profile = Profile(text=text)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        header = _cells(line)
        if "领域" not in header or index + 1 >= len(lines):
            continue
        if not all(cell and set(cell) <= set("-: ") for cell in _cells(lines[index + 1])):
            continue
        for row in lines[index + 2:]:
            if not row.strip().startswith("|"):
                break
            values = dict(zip(header, _cells(row), strict=False))
            profile.domains.append(DomainInterest(
                domain=values.get("领域", ""),
                priority=values.get("优先级", ""),
                note=values.get("说明", ""),
            ))
        break
    return profile


def load_profile(path: Path) -> Profile:
    return parse_profile(path.read_text(encoding="utf-8"))
