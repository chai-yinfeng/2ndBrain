# 0019 — 手动添加的 DOI 解析（Semantic Scholar）与导入兜底

- 状态：已接受
- 日期：2026-11-06
- 补充：ADR 0018（手动添加文章）、ADR 0017（信源主页）

## 背景

- 用户添加 `https://dl.acm.org/doi/10.1145/2741948.2741964`（Borg 论文）时失败：ACM DL 对机器访问返回 403，或者返回 Cloudflare 验证页。原来的失败原因只有一句笼统的"无法获取正文"。
- 用户清空数据后，没抓取过的 RSS 信源（以及信源目录里的条目）都没有主页链接，因为 RSS 的主页只在抓取时从 channel `<link>` 记录。

## 决策

### 1. DOI → Semantic Scholar Graph API（新的外部 API）

- URL 中能识别出 DOI 时（`doi.org/10.x/…`、`dl.acm.org/doi/10.x/…`、`/doi/abs|pdf|full|epdf/10.x/…` 等），抽出 DOI，统一为小写、去掉末尾的 `.pdf`，然后请求：
  `GET https://api.semanticscholar.org/graph/v1/paper/DOI:<doi>?fields=title,authors,year,abstract,openAccessPdf,externalIds,url`
  - 不需要 key，走现有 `HttpClient`，沿用其超时与重试设置。
  - 429 也只重试有限次（`fetch.retries`），最后给出"Semantic Scholar 限流（HTTP 429），稍后再试"。不会死循环。
- 按返回结果分三种处理：
  1. 有 `externalIds.ArXiv`：改走现有的 arXiv 流程（API 元数据 + arxiv_html / pdf），规范 URL 为 arXiv abs 页，这样能和信源抓到的同一篇论文去重。
  2. 有 `openAccessPdf.url`：用现有的 PDF 提取，标题 / 作者 / 年份取 S2 的元数据。
  3. 都没有：以"标题 + 摘要"作为正文，`extract_method = abstract_only`，结果显示"新增（仅摘要）"。S2 没有摘要时只有标题，显示"新增（仅标题，S2 没有摘要）"。
- 规范 URL 为 `https://doi.org/<doi>`，同一篇论文的不同链接形式不会重复导入。
- S2 失败时，再尝试抓取页面本身，两边的原因合并显示，例如"Semantic Scholar 限流（HTTP 429）；页面：HTTP 403……"。
- 实测：未带 key 的 S2 请求偶尔会被 429 限流，重试一两次后通常能成功。

### 2. 具体的失败原因

| 情况 | 显示的原因 |
|---|---|
| HTTP 401 / 403 | 站点拒绝机器访问（被拦截） |
| HTTP 404 | 页面不存在 |
| HTTP 429 | 被限流 |
| 超时 | 站点没有及时响应 |
| 返回 Cloudflare 等机器人验证页 | 被拦截（按页面特征识别） |
| 提取到的正文短于 200 字符 | 不是文章页 |

所有失败原因都附上提示"可粘贴正文或上传 PDF"。

### 3. PDF 上传兜底

- `/add` 增加 PDF 上传框，CLI 增加 `sb add <url> --pdf FILE`：
  - 只用于单个 URL，与"粘贴正文"二选一；
  - 文件 ≤ 30MB，且必须以 `%PDF` 开头；
  - 用现有的 pypdf 提取。
- 生效时机：
  - 抓取失败、或只拿到摘要时，用上传的 PDF 或粘贴的正文作为正文（`manual_pdf` / `manual_text`）。标题优先用 S2 或页面的元数据。
  - 已存在、但之前只导入了摘要（`abstract_only`）的条目：再次添加时如果带了 PDF 或正文，就用它补全正文（`db.replace_content`）。其他已存在的条目仍然只更新状态与反馈（ADR 0018）。
- 上传依赖 `python-multipart`，它已经是现有依赖（FastAPI 的 Form 在用），无需新增依赖。

### 4. RSS 信源主页兜底

- 主页的优先级为：配置的 `homepage` > 已记录的 channel `<link>` > feed URL 的 `scheme://host/`。
  - 例如 `x.substack.com/feed`、`/feed.xml`、`/rss` 都兜底为站点根。
  - `feeds.*` 子域与 feedburner / feedblitz / feedpress / rss.app 这类托管域名不给兜底链接，因为站点根不是信源主页。
- "预览"成功并解析出 channel link 时，在结果里显示这个链接，并顺手写入 `sources.homepage`：只更新已有的 sources 行，不插入 items。
- 链接仍然只接受 http(s)。

## 后果

- 正面：
  - ACM 等拒绝机器访问的论文至少能以元数据 + 摘要导入；
  - 失败原因可以据此行动；
  - 没抓取过的 RSS 信源也能点开主页。
- 负面：
  - 多了一个外部 API。S2 的元数据可能缺失（如没有摘要），未带 key 时偶尔会被限流；
  - "不是文章页"按正文长度判断，可能误伤极短的文章（< 200 字符）。
