# 0020 — 信源目录 homepage、html_index 信源与论文导入增强（M12）

- 状态：已接受
- 日期：2026-11-06
- 补充：ADR 0012（sitemap 信源）、ADR 0017（信源主页）、ADR 0018 / 0019（手动添加）

## 背景

- 信源目录中的主页链接是推导出来的。RSS 兜底到站点根，往往不是人实际浏览的博客列表页。
- sglang.io 的博客在更新，但它的 sitemap 只列了 3 篇，也没有 RSS。
- 手动导入论文时有几个问题：
  - USENIX 页面只拿到约 3000 字的摘要页；
  - S2 未带 key 时常被 429 限流；
  - 同一篇论文先后用 arXiv 链接和 DOI / ACM 链接导入时，只有 S2 知道 arXiv id 才会合并，否则产生两条记录。

## 决策

### 1. 信源目录写明确的 `homepage`

`config/sources.example.yaml` 的每个条目都在 `url` / `query` 之后写 `homepage`，指向人实际浏览的博客列表页或频道页。全部用一次性脚本实际请求核对过，结果见 PR / progress。

拿不准的两处：

- `meta-ai`：对机器请求返回 400，人工浏览可以打开，保留 `https://ai.meta.com/blog/`。
- `lesswrong-curated`：`/curated` 会重定向到 `/recommendations`，写最终地址。

### 2. 新信源类型 `html_index`

- 配置：`url`（列表页）、`url_pattern`（必填，匹配**绝对化之后**的文章链接）、可选 `max_links`。
- 抓取流程：
  1. 用标准库 `html.parser` 解析列表页中所有 `<a href>`，`urljoin` 成绝对地址并去掉 `#fragment`；
  2. 按正则过滤，去重时保持页面顺序；
  3. 取前 `max_links` 个；之后按 ADR 0005 的口径在每个信源中取最新 `fetch_limit` 条；
  4. 逐篇抓正文（复用 `extract_content` 与 `apply_page_meta`）。
- **发布日期的取值顺序**：
  1. 列表页上该链接自带的结构化日期：`<a>` 属性中的 `YYYY-MM-DD`（如 sglang.io 的 `data-post-date`），或 `<a>` 内 `<time datetime>`；
  2. 正文页 meta 的日期；
  3. 都没有时为未知，入库后按抓取时间参与时间窗，详情页显示"发布 未知"。
- **偏离说明**：任务原本的建议是正文页 meta 优先。但实测 sglang.io 的文章页（前端渲染）没有结构化日期，trafilatura 的启发式日期会猜错，例如把 2026-10-19 的文章猜成 2025-02-24。列表页上的结构化日期更可靠，所以列表日期优先（`RawEntry.keep_date`）。"链接附近的文字日期"（如 "Oct 19"）不解析：格式多样、容易误配，这里取最简单可靠的方案。
- 预览、主页兜底（列表页本身）、产出率统计都与其他类型一致。`/sources` 的添加表单也接受它的 `url_pattern`。
- 目录新增 `sglang-blog`：`https://www.sglang.io/blog`，正则为 `^https://www\.sglang\.io/blog/[^/?#]+$`。它和 lmsys 是两个都在更新的博客。

### 3. 论文导入增强（手动添加）

#### a. citation_pdf_url

- 页面有 `<meta name="citation_pdf_url">` 时（USENIX、ACM 和大多数出版社都有），下载该 PDF 并用 pypdf 提取。PDF 文本比网页正文长时采用 PDF（`extract_method = pdf`）。
- PDF 被拦截时保留网页正文，再走后续兜底。
- 其他 `citation_title / citation_author / citation_publication_date / citation_doi / citation_arxiv_id` 用来补充元数据和论文标识。

#### b. Crossref 兜底

- S2 失败（含 429），或 S2 没有摘要时，查询 `https://api.crossref.org/works/<doi>`：
  - 取 title、author（given + family）、出版日期（published-print → published-online → issued）；
  - abstract 是 JATS XML，去标签后再去掉开头的 "Abstract"；
  - `link` 中 content-type 含 pdf 的链接也会尝试下载。
- 只带通用 UA，**不提供邮箱**，因此不进入 polite pool。

#### c. S2 限流与 key

- `HttpClient` 对 429 / 5xx 读取 `Retry-After`（秒）：等待 `max(指数退避, Retry-After)`，但不超过 `fetch.retry_after_max_seconds`（10 秒）；重试次数仍是 `fetch.retries`。
- 批量导入时，同一 host 的请求间隔由已有的 `per_host_interval_seconds`（默认 1 秒）保证，S2 请求之间至少间隔约 1 秒，没有另加机制。
- 可选的 `S2_API_KEY`（环境变量或 .env）存在时带上 `x-api-key` 头。`/ops` 设置区可以设置或清除它，复用 M10 的组件与规则：不回显，写 .env 时原子写入、权限 600。

#### d. 跨标识去重（schema v9）

- `items` 新增 `doi`、`arxiv_id` 两列。旧行按规范 URL 回填：`arxiv.org/abs/…` 回填 arxiv_id，`doi.org/…` 回填 doi。
- 标识的来源：
  - arXiv 信源与手动导入都从 arXiv API 的 `arxiv:doi` 读取 doi；
  - S2、Crossref、citation meta 的结果写入两列；
  - DOI 一律转为小写。
- 手动导入时按 **规范 URL → doi → arxiv_id** 的顺序查重。URL 本身带的标识在抓取前就查；抓取后拿到的标识再查一次。任一命中即视为"已存在"。
- 命中"已存在"时：
  - 如果新正文比已有的更完整（已有的是 `abstract_only`，或不到新正文的一半），就替换正文，结果显示"已存在（补全正文）"；
  - 补上缺失的 doi / arxiv_id。
- 最后一道兜底：手动条目之间规范化标题（小写、去标点、合并空白）完全相同时，只提示"可能重复：#id"，不自动合并。

#### e. "正文不完整"标记

- 判定条件（满足其一）：
  - `extract_method = abstract_only`；
  - 来源是 manual，正文短于 **1500 字符**，且是论文类（有 doi 或 arxiv_id，或提取方式为 pdf / arxiv_html，或 URL 是 arXiv / .pdf）。
- 1500 字符大约是一篇长摘要加引言开头的长度。真正的论文全文通常有几万字符。
- 展示与补全：
  - 详情页和稍后读列表显示"正文不完整"徽标；
  - 详情页提供"粘贴正文 / 上传 PDF"表单，处理逻辑与 /add 的兜底相同，提交后替换正文；
  - `sb items incomplete` 列出这些条目。

## 后果

- 正面：
  - 信源目录的链接直达博客页；
  - 能接入没有 RSS 的列表页博客；
  - 论文导入能拿到全文的情况明显增多；
  - 同一篇论文不再因链接形式不同而重复。
- 负面：
  - 多了 Crossref 这个外部 API；
  - html_index 依赖列表页的静态 HTML，前端完全渲染的列表页（没有 `<a href>`）无法使用；
  - 列表页日期优先，意味着站点列表上的日期写错时会沿用错误日期。
- DB：迁移到 **v9**。
- 依赖：无新增。
