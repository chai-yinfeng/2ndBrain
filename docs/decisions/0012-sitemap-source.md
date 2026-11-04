# 0012 — sitemap 信源：给没有 RSS 的博客用

- 状态：已接受
- 日期：2026-11-04
- 补充：ADR 0005（采集层、`--limit` 口径）；落地 roadmap Backlog "needs_scraper 信源的页面抓取支持"的一部分

## 背景

M1 起有 6 个信源因为没有 RSS 标为 `needs_scraper` 并跳过，其中 LMSYS（SGLang 社区博客）对用户很重要。2026-11 实测：

- `https://lmsys.org/sitemap.xml`：一个 urlset，143 个 URL；博客文章形如 `/blog/2026-11-02-hicache-distributed-serving`，**发布日期在 slug 里**；所有 `<lastmod>` 都是同一个站点构建时间，不能当发布时间；还包含 `/blog` 列表页、`/about` 等。
- `https://www.anthropic.com/sitemap.xml`：一个 urlset，589 个 URL，几乎都有 `<lastmod>`；`/news/<slug>` 282 个（日期不在 URL 里），另有 8 个新闻分类页（`/news/product` 等，没有 lastmod）。
- Anthropic 的 **lastmod 是"最后修改时间"，不是发布时间**：对 14 篇 lastmod 最新的新闻比较页面的 `datePublished`，7 篇一致或差 1–3 天，1 篇差 5 天，6 篇是老文章被刷新（差 1–13 个月，如 lastmod 2026-10-20、实际发布 2025-11-13）。lastmod 恒 ≥ 发布时间，所以可以用来选候选（时间窗内发布的文章不会漏），但入库时要以页面日期为准。trafilatura 从页面提取的日期与页面 JSON-LD 的 `datePublished` 一致。
- **Anthropic engineering 不能用 sitemap 接入**：sitemap 里只有 `/engineering` 列表页本身，没有 `/engineering/<slug>`；列表页上的文章链接全部指向 `/research/<slug>`，与研究文章混在同一前缀下（213 个），无法用 URL 区分。该信源保持 `needs_scraper`。
- Hazy Research、Meta AI 没有 sitemap（404）；DeepSeek 不在本次范围。

## 备选

1. **GitHub API**（LMSYS 博客源码在 GitHub 上）：只适用于 LMSYS，需要额外处理 Markdown / front matter，还受 API 限流。
2. **GitHub commits feed**：条目标题是提交信息而不是文章标题，一次提交可能改多篇或只改错字。
3. **列表页 HTML 抓取**：每个站点要写选择器，站点改版就失效。
4. **sitemap.xml（采用）**：标准格式，标准库 `xml.etree` 即可解析，同一套代码可复用到 Anthropic 及其他有 sitemap 的站点；不需要新依赖。

## 决策

- 新增信源类型 `type: sitemap`（`sources/sitemap.py`），`url` 填 sitemap 地址。新增两个只用于 sitemap 的配置字段：
  - `url_pattern`：正则（`re.search`），只保留匹配的 URL，用来排除列表页、About、分类页等。
  - `use_lastmod`（默认 false）：URL 中没有日期时，是否用 `<lastmod>`。
- **发布时间优先级**：URL 中的日期（内置识别 `YYYY-MM-DD` / `YYYY/MM/DD`，只看路径）→ 正文页提取的日期 → `<lastmod>`（仅 `use_lastmod: true`）。所有日期都经过 `plausible()`。
- **"最新 N"在抓正文页之前确定**：只能用 URL 日期或 lastmod 排序和过时间窗，之后完全沿用 `ingest.collect` 的流程（时间窗、每源最新 N 再去重、已入库 URL 不抓正文页，ADR 0005）。抓正文页时（`extract_content` 的 HTML 分支）用页面元数据补全标题 / 作者，并在 URL 无日期时用页面日期覆盖 lastmod。抓正文页失败时标题退回 URL slug。
- **日期未知的条目**不另设规则：普通信源当作近期条目，backfill 信源进入回补池并按 `backfill_min_content_chars` 判断是否为非文章页（与 RSS 相同）。
- **sitemap index 暂不支持**：实测两个站点都是单个 urlset，遇到 sitemapindex 时报错提示改填具体的 urlset 地址。
- 接入：`lmsys-sglang-blog`（URL 日期）、`anthropic-news`（`use_lastmod: true`，正则排除 8 个分类页）。

## 后果

- 正面：LMSYS 与 Anthropic News 可以采集；以后有 sitemap 的站点只需加配置。
- 负面：
  - 用 lastmod 选候选时，被刷新的老文章也会进入"最新 N"并入库（发布时间按页面日期记录为真实的旧日期），之后由 L0 时间窗过滤；它们占用 `--limit` 名额，但入库后不会再被抓取。
  - `url_pattern` 里的分类页排除列表是按 2026-11 的站点写的，站点新增分类页时需要更新（新分类页没有 lastmod，会被当作日期未知的近期条目入库）。
  - 发布时间只有日期精度（00:00 UTC）。
  - Anthropic engineering、Hazy Research、Meta AI、DeepSeek 仍待支持。
