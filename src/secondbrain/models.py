"""配置与采集数据的模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceType = Literal["rss", "arxiv", "hf_papers", "youtube"]


class SourceConfig(BaseModel):
    """`config/sources.yaml` 中的一个信源。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: SourceType
    url: str | None = None
    query: str | None = None
    trust: int = Field(default=3, ge=1, le=5)
    role: Literal["core", "expansion"] = "core"
    tags: list[str] = Field(default_factory=list)
    max_age_days: int = Field(default=7, ge=1)
    enabled: bool = True
    backfill: bool = False  # M5 实现，M1 只解析并入库
    needs_scraper: bool = False

    @model_validator(mode="after")
    def _check_location(self) -> SourceConfig:
        if self.type == "arxiv" and not self.query:
            raise ValueError("arxiv 信源需要 query")
        if self.type != "arxiv" and not self.url:
            raise ValueError(f"{self.type} 信源需要 url")
        return self


class FetchSettings(BaseModel):
    """settings 中的 `fetch` 段；缺省值即默认行为。"""

    user_agent: str = "secondbrain/0.1 (+https://github.com/chai-yinfeng/2ndBrain)"
    timeout_seconds: float = 20.0
    retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    concurrency: int = Field(default=4, ge=1)
    per_host_interval_seconds: float = Field(default=1.0, ge=0)
    host_intervals: dict[str, float] = Field(default_factory=dict)
    arxiv_max_results: int = Field(default=50, ge=1)
    min_feed_content_chars: int = Field(default=1500, ge=0)
    content_max_chars: int = Field(default=200_000, ge=1)
    pdf_max_pages: int = Field(default=30, ge=1)
    pdf_max_bytes: int = Field(default=20_000_000, ge=1)


@dataclass
class RawEntry:
    """信源解析出的一条原始条目（尚未提取正文、尚未入库）。"""

    url: str
    title: str
    author: str | None = None
    summary: str | None = None
    feed_content: str | None = None  # feed 自带的正文（HTML 或纯文本）
    published_at: datetime | None = None
    arxiv_id: str | None = None
