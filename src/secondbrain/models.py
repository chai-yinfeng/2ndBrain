"""配置与采集数据的模型。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceType = Literal["rss", "arxiv", "hf_papers", "youtube", "sitemap"]


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
    url_pattern: str | None = None  # sitemap：只保留匹配该正则的 URL（re.search）
    use_lastmod: bool = False  # sitemap：URL 无日期时按 <lastmod> 选最新条目（页面日期优先）

    @model_validator(mode="after")
    def _check_location(self) -> SourceConfig:
        if self.type == "arxiv" and not self.query:
            raise ValueError("arxiv 信源需要 query")
        if self.type != "arxiv" and not self.url:
            raise ValueError(f"{self.type} 信源需要 url")
        if self.type != "sitemap" and (self.url_pattern is not None or self.use_lastmod):
            raise ValueError("url_pattern / use_lastmod 只用于 sitemap 信源")
        if self.url_pattern is not None:
            try:
                re.compile(self.url_pattern)
            except re.error as exc:
                raise ValueError(f"url_pattern 不是有效的正则：{exc}") from exc
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
    transcript_interval_seconds: float = Field(default=3.0, ge=0)  # 两次字幕请求的最小间隔
    transcript_retries: int = Field(default=2, ge=0)  # 字幕请求遇到 HTTP 错误时的重试次数
    transcript_paragraph_seconds: int = Field(default=30, ge=5)  # 字幕按约 N 秒合并为一段
    backfill_max_per_source: int = Field(default=100, ge=0)  # 每次 fetch 每源最多回补条数
    # 回补池中发布时间未知的条目，正文短于该值视为非文章页（如 FAQ、About），不入库
    backfill_min_content_chars: int = Field(default=2000, ge=0)
    transcript_max_chars: int = Field(
        default=400_000, ge=1
    )  # 视频字幕的长度上限（代替 content_max_chars）


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
    video_id: str | None = None  # YouTube 视频 id
    # sitemap 条目：抓正文页时用页面元数据补全标题 / 作者 / 发布时间（ADR 0012）
    page_meta: bool = False


# ---------- M2：LLM 与过滤 / 分级设置 ----------


class LLMSettings(BaseModel):
    backend: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    # 两级都默认 deepseek-flash（ADR 0014）；strong 可在 settings 中改回 deepseek-v4-pro
    models: dict[Literal["cheap", "strong"], str] = Field(
        default_factory=lambda: {"cheap": "deepseek-flash", "strong": "deepseek-flash"}
    )
    timeout_seconds: float = 120.0
    max_retries: int = Field(default=3, ge=0)  # 网络错误 / 429 / 5xx，由 openai SDK 指数退避
    concurrency: int = Field(default=4, ge=1)  # 同时进行的 LLM 调用数
    # 推理模型（如 deepseek-v4-pro）的 reasoning token 也计入 max_tokens（ADR 0011）；
    # strong 的 8192 对 flash 同样无害（只按实际生成计费）
    max_tokens: dict[Literal["cheap", "strong"], int] = Field(
        default_factory=lambda: {"cheap": 4096, "strong": 8192}
    )
    max_tokens_cap: int = Field(default=16384, ge=1)  # 输出被截断时重试翻倍的上限


class ModelPrice(BaseModel):
    """USD / 1M tokens（高峰价）。"""

    input_cache_hit: float
    input_cache_miss: float
    output: float


class PricingSchedule(BaseModel):
    peak_weekdays_utc: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # 周一 = 0
    peak_hours_utc: list[tuple[int, int]] = Field(default_factory=lambda: [(1, 4), (6, 10)])
    off_peak_multiplier: float = 0.5


class L0Settings(BaseModel):
    max_age_days: int = Field(default=30, ge=1)
    min_content_chars: int = Field(default=300, ge=0)
    include_keywords: list[str] = Field(default_factory=list)  # 非空时标题/摘要须至少命中一个
    exclude_keywords: list[str] = Field(default_factory=list)  # 标题/摘要命中任一即排除
    min_video_minutes: float = Field(default=5, ge=0)  # 短于该时长的视频排除（Shorts 一律排除）


class L1Settings(BaseModel):
    batch_size: int = Field(default=10, ge=1)
    item_chars: int = Field(default=4000, ge=200)  # 每条送入 L1 的正文字符数（约 1k token）
    top_n: int = Field(default=15, ge=0)  # 进入 L2 的条目数


class L2Settings(BaseModel):
    max_input_chars: int = Field(default=96_000, ge=1000)  # 约 24k token
    section_max_chars: int = Field(default=24_000, ge=500)  # 论文单个章节的上限
    # 长内容 map-reduce：待读文本超过阈值时分块，cheap 模型逐块提要，strong 模型汇总
    mapreduce_threshold_chars: int = Field(default=96_000, ge=1000)
    chunk_chars: int = Field(default=12_000, ge=1000)
    chunk_overlap_chars: int = Field(default=500, ge=0)
    max_chunks: int = Field(default=20, ge=1)


class ScoreSettings(BaseModel):
    """排序分 = (w_rel·relevance + w_q·quality + w_v·max(novelty, refresher_value)) / Σw
    × (1 + trust_weight·(trust − 3))。"""

    w_relevance: float = 0.45
    w_quality: float = 0.30
    w_value: float = 0.25
    trust_weight: float = 0.1


class Thresholds(BaseModel):
    must_read: float = 7.5
    must_read_relevance: float = 6.0
    worth_it: float = 6.0
    refresher_value: float = 7.0
    serendipity_quality: float = 7.5
    serendipity_max_relevance: float = 5.0
    fyi: float = 4.0


class EstimateSettings(BaseModel):
    """未经 L2 的条目按正文长度估计阅读时间。"""

    chars_per_minute: int = Field(default=1200, ge=1)
    paper_multiplier: float = 2.0
    min_minutes: int = 3
    max_minutes: int = 180


class LaneCaps(BaseModel):
    must_read: int = 3
    worth_it: int = 10
    refresher: int = 2
    serendipity: int = 1


class RunSettings(BaseModel):
    fetch_limit: int = Field(default=20, ge=1)
    max_cost_usd: float = 0.5  # 单次 run 估算费用上限，超过则不调用 L2


class PersonalizeSettings(BaseModel):
    """M4：反馈汇总、few-shot、profile 修改提案、信源 trust 调整。"""

    feedback_window_days: int = Field(default=30, ge=1)  # 汇总近 N 天的反馈
    summary_max_items: int = Field(default=50, ge=1)  # 送入 profile 提案的反馈上限
    few_shot_per_polarity: int = Field(default=5, ge=0)  # L1 中正 / 负样例各最多几条
    min_feedback_for_update: int = Field(default=1, ge=1)  # 少于该数不提议修改 profile
    tune_window_days: int = Field(default=90, ge=1)
    tune_min_samples: int = Field(default=5, ge=1)  # 信源有反馈的条目数少于该值不给建议
    tune_raise_ratio: float = Field(default=0.7, ge=0, le=1)  # 正向比例 ≥ 该值建议 trust +1
    tune_lower_ratio: float = Field(default=0.3, ge=0, le=1)  # 正向比例 ≤ 该值建议 trust −1
    # 近期"太难"条目的前置知识注入 L1 user 消息的长度上限（0 关闭；ADR 0013）
    too_hard_prereq_max_chars: int = Field(default=600, ge=0)


class BackfillSettings(BaseModel):
    """经典回补（ADR 0004 / 0009）：每天从回补池选少量条目进温故栏。"""

    daily_limit: int = Field(default=1, ge=0)
    screen_size: int = Field(default=10, ge=1)  # 回补池中保持多少条已粗筛的候选


class WikiSettings(BaseModel):
    """M6：知识 wiki（ADR 0010）。"""

    ingest_max_chars: int = Field(default=12_000, ge=500)  # 正文短于该值才整篇送入；否则用 L2 要点
    index_max_chars: int = Field(default=8_000, ge=500)  # 送入 ingest 的 index.md 上限
    related_pages: int = Field(default=5, ge=0)  # 送入 ingest 的候选相关页面数
    related_page_chars: int = Field(default=2_000, ge=200)  # 每个相关页面的截断长度
    known_max_chars: int = Field(default=2_000, ge=0)  # L1 "已知知识"段的上限（0 关闭）
    review_minutes: int = Field(default=5, ge=1)  # 概念复习在时间预算中的估计耗时
    review_slots: int = Field(default=1, ge=0)  # 每天最多放入温故栏的到期概念数
    recent_days: int = Field(default=30, ge=1)  # 为到期概念找"近期相关条目"的时间窗
    first_review_days: int = Field(default=3, ge=1)  # 新建概念页的首次复习间隔（ADR 0013）
    mastered_interval_days: int = Field(default=30, ge=1)  # 复习时点"已扎实"后的最短间隔


class FilterSettings(BaseModel):
    """settings 中与流水线相关的全部配置（由 config.pipeline_settings 组装）。"""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    pricing_schedule: PricingSchedule = Field(default_factory=PricingSchedule)
    daily_budget_minutes: int = 105
    lanes: LaneCaps = Field(default_factory=LaneCaps)
    # 为温故 / 意外栏保留的预算（分钟），其他 lane 不能占用；无合格候选时释放（ADR 0007）
    lane_budget_reserve: dict[str, int] = Field(
        default_factory=lambda: {"refresher": 15, "serendipity": 10}
    )
    deep_read_ratio: float = 0.6
    backlog_max_days: int = 3
    run: RunSettings = Field(default_factory=RunSettings)
    l0: L0Settings = Field(default_factory=L0Settings)
    l1: L1Settings = Field(default_factory=L1Settings)
    l2: L2Settings = Field(default_factory=L2Settings)
    score: ScoreSettings = Field(default_factory=ScoreSettings)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    estimate: EstimateSettings = Field(default_factory=EstimateSettings)
    personalize: PersonalizeSettings = Field(default_factory=PersonalizeSettings)
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)
    wiki: WikiSettings = Field(default_factory=WikiSettings)
