"""L1 / L2 的结构化输出 schema（pydantic 校验模型输出）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from secondbrain.llm.base import format_validation_error

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


# ---------- M5：长内容 map-reduce ----------


class ChunkPoint(BaseModel):
    t: str | None = None  # 时间戳 mm:ss / h:mm:ss（视频）；没有则为空
    text: str


class ChunkNotes(BaseModel):
    summary: str
    points: list[ChunkPoint] = Field(default_factory=list)


# ---------- M6：wiki ingest / lint ----------

PageType = Literal["concept", "entity", "paper"]


class PageCreate(BaseModel):
    slug: str
    type: PageType
    title: str
    summary: str  # 一行摘要，写入 index.md
    aliases: list[str] = Field(default_factory=list)
    body: str  # Markdown 正文（## 小节，可含 [[slug]]）


class PageUpdate(BaseModel):
    """对已有页面的受限修改。"""

    slug: str
    action: Literal["append_section", "replace_section", "add_links", "add_aliases"]
    section: str | None = None  # append_section / replace_section 的小节标题
    text: str | None = None
    links: list[str] = Field(default_factory=list)  # add_links：要链接的 slug
    aliases: list[str] = Field(default_factory=list)  # add_aliases


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


def _coerce_op(op):
    """可安全修正的偏差：类型 / 动作大小写与空白、别名 / 链接写成字符串、缺 slug 时用标题、
    缺一行摘要时留空。其余问题交给 schema 校验。"""
    if not isinstance(op, dict):
        return op
    op = dict(op)
    for key in ("type", "action"):
        if isinstance(op.get(key), str):
            op[key] = op[key].strip().lower()
    for key in ("aliases", "links"):
        if key in op:
            op[key] = _as_list(op[key])
    if not op.get("slug") and op.get("title"):
        op["slug"] = op["title"]
    if "title" in op and op.get("summary") is None:
        op["summary"] = ""
    return op


class WikiPlan(BaseModel):
    """ingest 的编辑计划。宽容解析（ADR 0011）：单个操作不合法时只丢弃该操作，
    原因记在 dropped 中（由 apply_plan 报告），不让整份计划作废。"""

    summary: str = ""
    creates: list[PageCreate] = Field(default_factory=list)
    updates: list[PageUpdate] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        dropped = list(_as_list(data.get("dropped")))
        if not isinstance(data.get("summary", ""), str):
            data["summary"] = str(data["summary"])
        for key, model in (("creates", PageCreate), ("updates", PageUpdate)):
            ops = data.get(key)
            if isinstance(ops, dict):
                ops = [ops]
            if ops is None:
                ops = []
            if not isinstance(ops, list):
                dropped.append(f"{key}：应为列表，已忽略")
                ops = []
            kept = []
            for i, op in enumerate(ops):
                try:
                    kept.append(model.model_validate(_coerce_op(op)))
                except ValidationError as exc:
                    dropped.append(f"{key}[{i}]：{format_validation_error(exc)}")
            data[key] = kept
        data["dropped"] = dropped
        return data


class LintIssue(BaseModel):
    pages: list[str]
    description: str


class LintFindings(BaseModel):
    issues: list[LintIssue] = Field(default_factory=list)


# ---------- M8：深度速读与补前置（ADR 0015） ----------


class SectionToRead(BaseModel):
    name: str
    reason: str


class PrereqNeed(BaseModel):
    name: str
    why: str  # 为什么这篇需要它


class DeepSummary(BaseModel):
    problem: str  # 问题与动机
    method: list[str] = Field(default_factory=list)  # 核心方法：关键算法步骤 / 公式
    results: list[str] = Field(default_factory=list)  # 关键结果与数字
    limitations: list[str] = Field(default_factory=list)  # 局限与争议
    relation_to_known: str = ""  # 与用户已有知识的关系
    sections_to_read: list[SectionToRead] = Field(default_factory=list)
    prerequisites: list[PrereqNeed] = Field(default_factory=list)


class PrereqExplanation(BaseModel):
    name: str
    explanation: str  # 约 300–500 字，讲到"读懂这篇需要的程度"


class PrereqExplanations(BaseModel):
    items: list[PrereqExplanation] = Field(default_factory=list)
