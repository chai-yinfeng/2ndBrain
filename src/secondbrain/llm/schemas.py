"""L1 / L2 的结构化输出 schema（pydantic 校验模型输出）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

ContentType = Literal["frontier", "explainer", "survey", "tutorial", "opinion"]
REFRESHER_TYPES = ("explainer", "survey", "tutorial")

Score = Annotated[float, Field(ge=0, le=10)]


class Scores(BaseModel):
    relevance: Score
    novelty: Score
    quality: Score
    refresher_value: Score


class L1Assessment(Scores):
    id: int
    content_type: ContentType
    topics: list[str] = Field(default_factory=list)
    one_line_reason: str


class L1Response(BaseModel):
    items: list[L1Assessment]


class L2Response(BaseModel):
    tldr: str
    key_points: list[str]
    why_for_you: str
    est_minutes: int = Field(ge=1, le=600)
    prerequisites: list[str] = Field(default_factory=list)
    scores: Scores


# ---------- M4：profile 修改提案 ----------


class MasteryChange(BaseModel):
    """对掌握度表的一处修改。update / remove 用 row（1 起，对应输入中的行号），add 用 domain。"""

    action: Literal["update", "add", "remove"]
    row: int | None = None
    domain: str | None = None
    mastery: str | None = None
    priority: str | None = None
    note: str | None = None
    reason: str
    evidence: list[int]  # 反馈 id


class SectionEdit(BaseModel):
    """对其他段落的修改：在某个 ## 段落末尾追加一行，或删除其中完全相同的一行。"""

    section: str
    action: Literal["append", "remove"]
    text: str
    reason: str
    evidence: list[int]


class ProfileProposal(BaseModel):
    summary: str = ""
    mastery_changes: list[MasteryChange] = Field(default_factory=list)
    section_edits: list[SectionEdit] = Field(default_factory=list)
