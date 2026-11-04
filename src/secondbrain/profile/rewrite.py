"""profile 修改提案：LLM 输出结构化提案 → 代码校验证据与规则 → 生成新文本与 unified diff。"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from secondbrain.llm.base import LLMBackend, Usage
from secondbrain.llm.schemas import MasteryChange, ProfileProposal, SectionEdit
from secondbrain.pipeline.run import PROMPTS_DIR
from secondbrain.profile.feedback import FeedbackEntry
from secondbrain.profile.loader import MASTERY_LEVELS, Profile, parse_profile

LEVEL_RANK = {level: i for i, level in enumerate(MASTERY_LEVELS)}  # 陌生 < 接触过 < 想回炉 < 扎实
UPGRADE_KINDS = {"mastered", "learned"}
DOWNGRADE_KINDS = {"needs_refresh"}


@dataclass
class UpdateResult:
    old_text: str
    new_text: str
    summary: str = ""
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def changed(self) -> bool:
        return self.old_text != self.new_text

    def diff(self, name: str = "profile.md") -> str:
        return "".join(
            difflib.unified_diff(
                self.old_text.splitlines(keepends=True),
                self.new_text.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 提案 ----------


def _sections(text: str) -> list[dict]:
    result, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            current = {"section": line[3:].strip(), "lines": []}
            result.append(current)
        elif current is not None and line.strip():
            current["lines"].append(line)
    return result


def proposal_messages(profile: Profile, feedback: list[FeedbackEntry]) -> list[dict[str, str]]:
    system = (PROMPTS_DIR / "profile_update.md").read_text(encoding="utf-8")
    table = [
        {
            "row": n,
            "domain": d.domain,
            "mastery": d.raw_mastery,
            "priority": d.priority,
            "note": d.note,
        }
        for n, d in enumerate(profile.domains, start=1)
    ]
    payload = {
        "table": table,
        "sections": _sections(profile.text),
        "feedback": [e.as_prompt_dict() for e in feedback],
    }
    user = "请根据反馈对 profile 提出修改提案，按规定格式输出 json：\n" + json.dumps(
        payload, ensure_ascii=False
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def propose(
    backend: LLMBackend, profile: Profile, feedback: list[FeedbackEntry]
) -> tuple[ProfileProposal, Usage]:
    return backend.complete_json(proposal_messages(profile, feedback), ProfileProposal, "strong")


# ---------- 校验与应用 ----------


def _evidence_problem(evidence: list[int], kinds: dict[int, str]) -> str | None:
    if not evidence:
        return "没有给出反馈证据"
    unknown = [i for i in evidence if i not in kinds]
    if unknown:
        return f"证据中的反馈 id 不存在：{unknown}"
    return None


def _mastery_problem(old: str | None, new: str, evidence_kinds: set[str]) -> str | None:
    if new not in LEVEL_RANK:
        return f"掌握度取值无效：{new!r}"
    if old is None or old == new:
        return None
    delta = LEVEL_RANK[new] - LEVEL_RANK[old]
    if delta > 0:
        if not evidence_kinds & UPGRADE_KINDS:
            return "升级缺少 已扎实 / 学了 证据"
        if delta > 1 and not (new == "扎实" and "mastered" in evidence_kinds):
            return "只能升一级（直接改为扎实需要 已扎实 证据）"
    else:
        if not evidence_kinds & DOWNGRADE_KINDS:
            return "降级缺少 需要回炉 证据"
        if delta < -1:
            return "每次只能降一级"
    return None


def _row_line(header: list[str], values: dict[str, str]) -> str:
    return "| " + " | ".join(values.get(col, "") for col in header) + " |"


def _apply_table(
    profile: Profile,
    changes: list[MasteryChange],
    kinds: dict[int, str],
    applied: list[str],
    rejected: list[str],
) -> list[str] | None:
    """返回新的表格行（表头与分隔行之后的数据行）；表格不存在时返回 None。"""
    if profile.table_start is None:
        if changes:
            rejected.extend(
                f"掌握度修改（{c.action}）：profile 中没有可解析的掌握度表" for c in changes
            )
        return None
    header = profile.header
    original = profile.text.splitlines()[profile.table_start + 2 : profile.table_end]
    rows = [
        {"领域": d.domain, "掌握度": d.raw_mastery, "优先级": d.priority, "说明": d.note}
        for d in profile.domains
    ]
    dirty: set[int] = set()  # 被修改过的行；未修改的行保留原文，避免无意义的 diff
    normalized = [d.mastery for d in profile.domains]
    removed: set[int] = set()
    for c in changes:
        label = f"掌握度 {c.action} {c.domain or f'第 {c.row} 行'}"
        problem = _evidence_problem(c.evidence, kinds)
        ev_kinds = {kinds[i] for i in c.evidence if i in kinds}
        if c.action in ("update", "remove"):
            if problem is None and (c.row is None or not 1 <= c.row <= len(rows)):
                problem = f"行号无效：{c.row}"
            if problem is None and c.row - 1 in removed:
                problem = f"第 {c.row} 行已被删除"
        if problem is None and c.action == "update" and c.mastery:
            problem = _mastery_problem(normalized[c.row - 1], c.mastery, ev_kinds)
        if problem is None and c.action == "add":
            if not c.domain or not c.mastery:
                problem = "新增行需要 domain 与 mastery"
            elif c.mastery not in LEVEL_RANK:
                problem = f"掌握度取值无效：{c.mastery!r}"
        if problem:
            rejected.append(f"{label}：{problem}")
            continue
        if c.action == "update":
            row = rows[c.row - 1]
            label = f"掌握度 update「{row['领域']}」"
            before = row["掌握度"]
            if c.mastery and c.mastery != normalized[c.row - 1]:
                row["掌握度"] = c.mastery
                normalized[c.row - 1] = c.mastery
            for col, value in (("优先级", c.priority), ("说明", c.note)):
                if value is not None:
                    row[col] = value
            dirty.add(c.row - 1)
            applied.append(f"{label}：{before} → {row['掌握度']}（{c.reason}；证据 {c.evidence}）")
        elif c.action == "remove":
            removed.add(c.row - 1)
            applied.append(
                f"掌握度 remove「{rows[c.row - 1]['领域']}」（{c.reason}；证据 {c.evidence}）"
            )
        else:
            rows.append(
                {
                    "领域": c.domain,
                    "掌握度": c.mastery,
                    "优先级": c.priority or "",
                    "说明": c.note or "",
                }
            )
            normalized.append(c.mastery)
            applied.append(
                f"掌握度 add「{c.domain}」：{c.mastery}（{c.reason}；证据 {c.evidence}）"
            )
    return [
        original[n] if n < len(original) and n not in dirty else _row_line(header, r)
        for n, r in enumerate(rows)
        if n not in removed
    ]


def _apply_section(
    lines: list[str], edit: SectionEdit, kinds: dict[int, str]
) -> tuple[list[str] | None, str | None]:
    problem = _evidence_problem(edit.evidence, kinds)
    if problem:
        return None, problem
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("## ") and line[3:].strip() == edit.section
        ),
        None,
    )
    if start is None:
        return None, f"找不到段落「{edit.section}」"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    text = edit.text.strip()
    if edit.action == "append":
        if not text.startswith(("-", "*")):
            text = f"- {text}"
        if any(line.strip() == text for line in lines[start:end]):
            return None, "该段已有完全相同的一行"
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        return lines[:insert_at] + [text] + lines[insert_at:], None
    for i in range(start + 1, end):
        if lines[i].strip() == text:
            return lines[:i] + lines[i + 1 :], None
    return None, "该段中没有完全相同的一行"


def apply_proposal(
    profile: Profile, proposal: ProfileProposal, feedback: list[FeedbackEntry]
) -> UpdateResult:
    """按规则校验提案并生成新 profile 文本；不合规的修改被拒绝并记录原因。"""
    kinds = {e.feedback_id: e.kind for e in feedback}
    result = UpdateResult(old_text=profile.text, new_text=profile.text, summary=proposal.summary)
    lines = profile.text.splitlines()
    table_rows = _apply_table(
        profile, proposal.mastery_changes, kinds, result.applied, result.rejected
    )
    if table_rows is not None:
        lines = lines[: profile.table_start + 2] + table_rows + lines[profile.table_end :]
    for edit in proposal.section_edits:
        new_lines, problem = _apply_section(lines, edit, kinds)
        label = f"段落「{edit.section}」{edit.action}：{edit.text.strip()}"
        if problem:
            result.rejected.append(f"{label}（{problem}）")
            continue
        lines = new_lines
        result.applied.append(f"{label}（{edit.reason}；证据 {edit.evidence}）")
    trailing = "\n" if profile.text.endswith("\n") else ""
    new_text = "\n".join(lines) + trailing
    # 只有确实改动了内容时才替换，避免仅因表格重排产生无意义的 diff
    result.new_text = new_text if result.applied else profile.text
    return result


def generate_update(
    backend: LLMBackend, profile_text: str, feedback: list[FeedbackEntry]
) -> UpdateResult:
    profile = parse_profile(profile_text)
    proposal, usage = propose(backend, profile, feedback)
    result = apply_proposal(profile, proposal, feedback)
    result.usage = usage
    return result


def save_profile(path: Path, new_text: str, history_dir: Path, now: datetime) -> Path:
    """先把当前版本备份到 history_dir/profile-<时间>.md，再写入新内容。返回备份路径。"""
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone().strftime("%Y%m%d-%H%M%S")
    backup = history_dir / f"profile-{stamp}.md"
    n = 2
    while backup.exists():
        backup = history_dir / f"profile-{stamp}-{n}.md"
        n += 1
    backup.write_text(path.read_text(encoding="utf-8") if path.exists() else "", encoding="utf-8")
    path.write_text(new_text, encoding="utf-8")
    return backup
