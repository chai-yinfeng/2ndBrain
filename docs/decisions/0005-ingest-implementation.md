# 0005 — 采集层实现细节（M1）

- 状态：已接受
- 日期：2026-11-04

## 背景

roadmap 的目标目录结构只列出了 `sources/`、`extract/`、`db.py`、`models.py`。实现 M1 时需要几处计划里没写明的取舍：HTTP 访问的公共逻辑放哪里、抓取的编排放哪里、跨信源（arXiv 与 HF Daily Papers）重复的论文怎么处理、如何在礼貌抓取和总耗时之间折中。

## 决策

- **新增三个小模块**：
  - `net.py`：`HttpClient`，统一 UA、超时、重试（网络错误与 429/5xx，指数退避）、按 host 限速（线程安全），以及按 `content-length` 拒绝过大响应。
  - `urls.py`：URL 规范化与 `content_hash`。
  - `ingest.py`：采集编排（抓取 → 时间窗 → URL 去重 → 正文提取 → 入库）。M2 的 `pipeline/run.py` 会调用它。
- **并发**：信源之间用线程池并发（`fetch.concurrency`，默认 4）；同一 host 的请求按 `per_host_interval_seconds` 串行限速，arXiv API 另设 3 秒间隔。只有主线程写 SQLite。
- **跨信源去重**：HF Daily Papers 的条目 URL 统一写成 `https://arxiv.org/abs/<id>`，与 arXiv 信源天然按 `url_canonical` 去重；arXiv 条目去掉版本号。
- **`items.extract_method` 列**：记录正文来源（`feed / html / arxiv_html / pdf / summary / none`），用于排查提取失败、在 `sb fetch` 中统计"正文提取失败"。
- **正文提取顺序**：arXiv 条目先取 HTML 版，再取 PDF（前 `pdf_max_pages` 页，超过 `pdf_max_bytes` 不下载）；普通条目 feed 自带正文足够长（`min_feed_content_chars`）就直接用，否则抓原网页（trafilatura），都失败时退回 feed 文本或摘要。
- **`--limit N` 的口径**：每个信源先按时间窗过滤、按发布时间倒序，只取最新的 N 条再去重入库。因此每个信源每次新增 ≤ N，且以相同 N 重复运行时新增为 0；若按"新增 N 条"截断，条目多于 N 的信源（arXiv、OpenAI News 等）每次都会补抓更旧的条目，重复运行无法收敛。
- **跳过规则**：`enabled: false`、`type: youtube`（M5）、`needs_scraper: true` 的信源不发请求，`sb fetch` 打印跳过原因。
- **去重口径**：URL 规范化（https、host 小写、去跟踪参数 / fragment / 默认端口 / 尾斜杠、参数排序）+ `content_hash`（标题 + 正文归一化后的 sha256）。

## 后果

- 正面：网络细节集中在一处，测试只需替换 transport（`httpx.MockTransport`）即可全离线；同一篇论文只入库一次。
- 负面：跨信源重复的论文只归属于先处理的信源（M2 计算 trust 加权时需要注意，backlog 中的多信源聚合可以解决）；`content_hash` 包含标题，同一正文换了标题不会被判为重复。
