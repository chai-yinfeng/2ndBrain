"""profile 修改提案：LLM 输出结构化提案 → 代码校验证据与规则 → 生成新文本与 unified diff。"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from secondbrain.llm.base import LLMBackend, Usage
from secondbrain.llm.schemas import ProfileProposal, SectionEdit
from secondbrain.pipeline.run import PROMPTS_DIR
from secondbrain.profile.feedback import FeedbackEntry
from secondbrain.profile.loader import Profile, parse_profile


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
    payload = {
        "sections": _sections(profile.context),
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
