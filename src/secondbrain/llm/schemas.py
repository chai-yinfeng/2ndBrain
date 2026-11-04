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
