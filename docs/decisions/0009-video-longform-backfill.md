# 0009 — 视频字幕、长内容 map-reduce 与经典回补（M5）

- 状态：已接受
- 日期：2026-11-04

## 背景

M5 引入 YouTube 讲座与长内容，并实现 ADR 0004 中的经典回补。需要决定：字幕以什么形式存储并保留时间戳；视频时长从哪里来；超长内容如何精读；回补条目如何与常规条目隔离、如何每天限量释放。

## 决策

### 字幕

- 抓取顺序：`youtube-transcript-api`（新增依赖，直接读取字幕，不需要浏览器）→ 失败时退回 `yt-dlp`（新增依赖，只调用 `extract_info(download=False)` 取字幕轨道 URL，再下载 json3 字幕，**不下载视频**）。
- 语言：英文、中文；先人工字幕，再自动字幕。都没有时标记 `extract_method = no_transcript`。
- 限流：所有字幕请求共享一个全局最小间隔（`fetch.transcript_interval_seconds`，默认 3 秒）；HTTP 错误按 `transcript_retries` 退避重试；被 YouTube 判定为 `RequestBlocked` / `IpBlocked` 时不重试，只退回 yt-dlp 一次，仍失败就如实记录。不使用代理，不伪造 cookie。
- **存储**：字幕按约 `transcript_paragraph_seconds`（默认 30）秒合并成段，每段以 `[mm:ss]`（≥ 1 小时为 `[h:mm:ss]`）开头，作为普通文本存入 `items.content`。这样 L1 / L2 / map-reduce / 页面展示都不需要额外分支，时间戳直接随文本进入 prompt。不单独建 JSON 列。
- **时长**：频道 RSS 不含时长，用字幕最后一段的结束时间近似，存入新列 `items.duration_seconds`。L0 排除 Shorts（URL 含 `/shorts/`）和短于 `filter.l0.min_video_minutes`（默认 5）的视频。
- 没有字幕的视频仍进入 L1（按标题与简介判断），但不进入 L2；L0 不按正文长度排除它们；页面显示"无字幕"。
- 视频要点：L2 prompt 要求每条 key_point 以正文中的时间戳开头；展示时由代码解析时间戳，生成 `https://www.youtube.com/watch?v=ID&t=SECONDS` 链接（不存储链接）。

### 长内容 map-reduce

- 待读文本（论文取 abstract / introduction / conclusion，其余取全文）超过 `filter.l2.mapreduce_threshold_chars`（默认 96000，即单次输入上限）时：按行切块（`chunk_chars` 12000、`chunk_overlap_chars` 500、最多 `max_chunks` 20）→ cheap 模型逐块输出 `ChunkNotes`（摘要 + 带时间戳的要点，stage = L2_MAP）→ strong 模型基于拼接的提要输出标准 L2 结果（stage = L2，`text_kind = chunk_notes`）。
- map 阶段不带 profile（只做客观提要，system 前缀固定便于缓存）；个性化判断留给汇总阶段。
- 费用预估同时计入 map 与 reduce，沿用 `run.max_cost_usd`。

### 按需精读

- `sb deep <item_id> [--force]` 与 Web "精读"按钮共用 `pipeline.run.deep_one`：只写 L2 评估，不改变条目在 digest 中的位置和状态。

### 经典回补

- `backfill: true` 的信源：fetch 时，时间窗外的 feed 条目也入库，标记新列 `items.is_backfill = 1`（每源最多 `fetch.backfill_max_per_source`；带 `--limit` 时再受其限制）。只用 feed 自带的历史，不写爬虫。
- 回补条目不参与常规 L0 / L1 / L2 竞争。每次 run 在常规 L2 之后：
  1. 当天已放入温故栏（含顺延）的回补条目数未达 `backfill.daily_limit`（默认 1）时才处理；
  2. 已粗筛的回补池（状态 `backfill_l1`）不足 `backfill.screen_size`（默认 10）时，先对新回补条目做 L0（忽略时间窗）+ L1 粗筛；
  3. 按优先级挑选：explainer / survey / tutorial 优先，其次 topics 命中"接触过 / 想回炉"的领域，再按 refresher_value 与排序分；
  4. 选中的做 L2（已有 L2 的复用），然后和常规条目一起分级，但**只能进温故栏**，仍受时间预算与 `lane_budget_reserve` 约束；
  5. 没排上的回到回补池（`backfill_l1`），保留已有 L2，以后的日子再试。
- 默认把 karpathy-github、lilian-weng、colah 设为 `backfill: true`。

## 后果

- 正面：视频与长文复用同一条流水线；时间戳全程以文本保留，实现简单；回补节奏可控，不挤占当天的新内容。
- 负面：
  - `content_max_chars`（默认 20 万字符）会截断超长字幕（约 3.5 小时以上的讲座），截断部分不参与精读；
  - map 阶段逐块串行，3 小时级视频的精读需要数分钟；
  - 时长靠字幕近似，没有字幕的视频不能按时长过滤；
  - YouTube 的限流策略可能变化，抓字幕失败时只能如实记录。
