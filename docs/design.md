# 架构设计（初版，M0）

> 本文是初版草案，各 milestone 结束时同步更新。偏离计划的取舍见 `docs/decisions/`（掌握度 / 温故见 ADR 0003，时间预算见 ADR 0004）。

## 目标

从用户信任的信源自动采集内容 → LLM 分层过滤与总结 → 在每日时间预算内按 lane 推送到本地 Web UI（新进展 + 温故回炉 + 意外）→ 用户反馈驱动个性化 → "学了"的内容编译进知识 wiki → wiki 反过来为过滤层提供"已知 / 新颖 / 该复习了"信号，形成闭环。

## 数据流

```mermaid
flowchart LR
    S[Sources<br/>RSS/Atom · arXiv · HF Papers · YouTube<br/>role: core / expansion] --> I[Ingest<br/>抓取 · 正文提取 · 去重]
    S -. backfill 经典回补 M5 .-> BF[补课池<br/>每日限额释放]
    I --> L0[L0 规则<br/>零 token]
    L0 --> L1[L1 粗筛<br/>cheap 模型 · 批量<br/>content_type · refresher_value]
    L1 --> L2[L2 精读<br/>strong 模型 · 中文总结<br/>est_minutes · prerequisites]
    L2 --> T[Tiering<br/>时间预算 + lanes]
    BF --> T
    T --> MR[must_read / worth_it]
    T --> RF[refresher 温故]
    T --> SE[serendipity 意外]
    T --> FY[fyi / archived]
    MR --> W[Web UI<br/>预算进度条 · 积压 · 前置知识提示]
    RF --> W
    SE --> W
    W --> F[Feedback<br/>👍 👎 已读 稍后读 学了<br/>需要回炉 已扎实]
    F --> P[Profile<br/>掌握度表 + 反馈样例]
    P --> L1
    P --> L2
    F -- 学了 --> K[Wiki<br/>概念页 mastery · 复习间隔]
    K -- 已知 / 新颖信号 --> L1
    K -- 到期复习概念 --> RF
```

## 各层职责

| 层 | 职责 | 是否用 LLM |
|---|---|---|
| Sources | 读 `config/sources.yaml`：信源类型、地址 / 检索式、trust、role（core / expansion）、标签、时间窗、backfill | 否 |
| Ingest | 抓取（`net.py`：httpx，超时 / 重试 / 礼貌 UA / 按 host 限速；信源间线程池并发）、正文提取（HTML / PDF / 字幕）、URL 规范化 + content hash 去重（`urls.py`），由 `ingest.py` 编排写入 `items`；backfill 信源抓历史归档进补课池（M5）。见 ADR 0005 | 否 |
| L0 规则 | 时间窗、include / exclude 关键词、最短长度、已处理跳过 | 否 |
| L1 粗筛 | cheap 模型批量打分：`relevance / novelty / quality`、`content_type`、`refresher_value`、`topics[]` + 一句理由；结合 profile 掌握度：不扎实领域的好讲解加 refresher_value，扎实领域只推 frontier；分数 × trust 取 top-N | 是（判断） |
| L2 精读 | strong 模型读截断全文，输出中文 `tldr / key_points / why_for_you / scores`，以及 `est_minutes`（读懂并验证的时间）和 `prerequisites[]` | 是（判断 + 总结） |
| Tiering | 以 `daily_budget_minutes` 为主约束、各 lane 条数为上限，分到 `must_read / worth_it / refresher / serendipity / fyi / archived`；标记"深读"并可顺延；积压超期降级；允许为空 | 否 |
| Web UI | 本地 FastAPI + Jinja2 + HTMX：今日 digest（按 lane）、预算进度条、积压数量、est_minutes、前置知识提示、详情、历史、反馈按钮 | 否 |
| Feedback | 记录用户动作（含"需要回炉 / 已扎实"），供 profile 汇总、掌握度调整与 few-shot | 否 |
| Profile | 用户手写的 `profile.md`，含可解析的领域掌握度表；LLM 只提出修改 diff（含掌握度调整建议），用户确认后才写入 | 提案时用 |
| Wiki | "学了"的条目由 LLM 产出结构化页面操作、代码落盘；概念页记录 mastery 与复习间隔；`index.md` 作为已知知识注入 L1，到期概念进入温故栏 | 是（结构化输出） |

## 掌握度、温故与前置知识（ADR 0003）

- 掌握度四级：`陌生 / 接触过 / 想回炉（熟悉但不扎实）/ 扎实`。"熟悉"不等于"扎实"：只有"扎实"领域才降低入门讲解的权重。
- refresher lane 独立于 must_read，默认 1–2 条 / 天，来源：L1 中 refresher_value 高的 explainer / survey、M6 到期复习的概念（若有讲该概念的好文一起推）、M5 的经典回补池。
- L2 的 `prerequisites[]` 命中用户不扎实的概念时，UI 提示"建议先回炉：[[概念]]"。
- 复习调度（M6）：SM-2 简化版，按钮"还记得 / 模糊 / 忘了"调整 interval 与 mastery。

## 每日时间预算（ADR 0004）

- `daily_budget_minutes`（默认 105）为主约束；`lanes` 中的条数为上限。
- 单条 `est_minutes` > 预算 × `deep_read_ratio`（默认 0.6）标"深读"，可顺延到预算有余的日子。
- 未读 must_read 计入积压并在 UI 显示；超过 `backlog_max_days`（默认 3）天降级到 fyi。

## 数据模型草案

存储：SQLite 单文件 `data/brain.db`；wiki 为 `data/wiki/` 下的 Markdown。字段为草案，以实现时的 `db.py` 为准（M1 已实现 `sources`、`items`，schema 版本记录在 `PRAGMA user_version`）。

| 表 | 主要字段 | 引入 |
|---|---|---|
| `sources` | `id`, `type`, `url` / `query`, `trust`, `role`（core / expansion）, `tags`, `max_age_days`, `enabled`, `backfill` | M1 |
| `items` | `id`, `source_id`, `url`, `url_canonical`（唯一）, `content_hash`, `title`, `author`, `summary`, `content`, `extract_method`（feed / html / arxiv_html / pdf / summary / none）, `published_at`, `fetched_at`, `status`, `is_backfill` | M1（`is_backfill` M5） |
| `assessments` | `id`, `item_id`, `stage`（L1 / L2）, `model`, `relevance`, `novelty`, `quality`, `content_type`, `refresher_value`, `topics`（JSON）, `reason`, `tldr`, `key_points`（JSON）, `why_for_you`, `est_minutes`, `prerequisites`（JSON）, `created_at` | M2 |
| `digests` | `date`, `item_id`, `lane`（must_read / worth_it / refresher / serendipity / fyi / archived）, `rank`, `est_minutes`, `is_deep_read`, `deferred_from`（顺延来源日期） | M2 |
| `feedback` | `id`, `item_id`, `kind`（up / down / read / later / learned / needs_refresh / mastered）, `ts`, `note` | M3 |
| `llm_calls` | `id`, `ts`, `backend`, `model`, `stage`, `input_tokens`, `cache_hit_tokens`, `output_tokens`, `cost_usd`（按 config 价格估算） | M2 |

Wiki 概念页 frontmatter（M6）：`mastery`、`last_reviewed`、`interval_days`、`ease`（SM-2 简化版）、`sources`（来源条目）。

## LLM 后端

统一接口（M2）：`complete_json(messages, schema, tier="cheap"|"strong") -> (obj, usage)`。默认 DeepSeek（OpenAI 兼容），`fake` 后端用于离线测试。模型名、价格、预算与配额都写在 `config/settings.yaml`，便于更换。见 ADR 0002。

## 借鉴的原则

- **确定性预过滤**：能用规则解决的（时间窗、关键词、长度、去重）不花 token。
- **LLM 只做判断**：抓取、去重、排序、分级、落盘都是确定性代码；LLM 只输出结构化的打分、理由、估时和总结。
- **推荐必带理由**：每条推荐都有 `one_line_reason` / `why_for_you`，可解释、可反馈。
- **允许空**：某天没有值得推荐的内容就不推，配额与预算是上限而不是目标。
- **意外名额**：保留少量"高质量但低相关"的名额（`lanes.serendipity`，候选含 `role: expansion` 信源），防止信息茧房。
- **熟悉 ≠ 掌握**：不扎实的基础知识值得回炉，用独立的温故栏承载，不与新进展抢名额。
