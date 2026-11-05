# Progress

每个 milestone 完成时追加一条：做了什么、验收结果、遗留问题。

## 待决

- **默认 data 目录是否重置（M2 遗留）**：推荐默认：不重置，保留已有数据，不做覆盖。（已按默认处理）
- **温故 / 意外是否预留预算（M2 遗留）**：推荐默认：预留。tiering 新增 `lane_budget_reserve`（默认 refresher 15 分钟、serendipity 10 分钟）；该栏没有合格候选时释放回总预算，must_read 不能占用（ADR 0007）。（已按默认处理）
- **真实 DeepSeek 下的 profile 提案质量（M4 遗留）**：推荐默认：暂不验证，等用户积累真实反馈后由用户在 UI 中触发。（已按默认处理）
- **Web"生成修改提案"没有费用确认（M4 遗留）**：推荐默认：保持（单次约几美分），在按钮旁显示"将调用 strong 模型"提示（M5 已加，"精读"按钮同样提示）。（已按默认处理）
- **超长视频字幕被截断（M5 遗留）**：推荐默认：视频单独使用 `fetch.transcript_max_chars`（默认 40 万字符），由 map-reduce 处理。（已按默认处理，M6 已实现）

## 已处理（原待决）

- **`needs_scraper` 信源的页面抓取支持**：原 6 个信源没有可用 RSS，放入 roadmap Backlog。2026-11-04：LMSYS、Anthropic news 与 Anthropic engineering 已通过新的 `type: sitemap` 接入（ADR 0012）；engineering 文章已迁到 `/research/` 下，信源改名为 `anthropic-research`，混入的研究文章由 L1 过滤。Hazy Research、Meta AI（均无 sitemap）、DeepSeek 仍在 Backlog。
- **发布时间异常的条目（M5 遗留）**：早于 1990 年、晚于当前 + 1 天（如 0001-01-01）的发布时间按未知处理；回补信源中时间未知且正文短于 `fetch.backfill_min_content_chars`（2000）的条目视为非文章页，不入库（M6 已实现）。默认 data 中已入库的 item 271（lilian-weng "FAQ"）：用户同意删除，已删除（删前备份 `data/brain.db.bak-before-del271`）。该 feed 每次都包含这一页，但会被上述非文章页规则拦截：临时目录实测新增 52 条，FAQ 被跳过。
- **"GPU 性能与 kernel"的掌握度**：用户确认为"接触过"，已更新 `data/profile.md`。
- **定时任务**：用户同意，已安装每日本地 07:00 运行（EST 下为 UTC 12:00，非高峰时段）。
- **SSH 推送**：用户终端中 SSH 正常，只是 agent 的 shell 无法使用 key；合并改走 `gh` + HTTPS。

## M0 — 仓库与骨架（2026-11-04）

**做了什么**
- 初始化仓库（main）与 remote `git@github.com:chai-yinfeng/2ndBrain.git`，`.gitignore`。
- `pyproject.toml`（uv，src 布局，包 `secondbrain`，入口 `sb`）与 `uv.lock`；依赖 typer、httpx、pyyaml、pydantic，dev 组 pytest、ruff；pytest 注册 `live` marker 并默认跳过。
- `src/secondbrain/config.py`：项目根 / `config/` / `data/` 定位（`SB_DATA_DIR` 可覆盖）、手写 `.env` 解析（不覆盖已有环境变量）、settings 读取（正式文件不存在时退回 example）、LLM 后端解析。
- `src/secondbrain/cli.py`：`sb --help`、`sb doctor`。
- `config/settings.example.yaml`、`sources.example.yaml`、`profile.example.md`，`.env.example`。
- 文档：`CLAUDE.md`、`README.md`、`docs/design.md`、`docs/roadmap.md`、本文件、ADR 0001 / 0002。

**验收结果**
- `uv sync`、`uv run sb --help`、`uv run sb doctor`（未设置 key 时警告、返回 0）、`uv run pytest -q`、`uv run ruff check .` 均通过；首个提交已推送到 `origin/main`。

**遗留问题**
- `DEEPSEEK_API_KEY` 待用户在 `.env` 中填写（M2 真实调用前需要）。
- 价格表为 2026-11 记录的 DeepSeek 官方价，后续变动需手动更新 `config/settings*.yaml`。

## M0 补充 — 回炉机制、时间预算与信源种子（2026-11-04，分支 `m0-roadmap-refresher`）

**做了什么**（纯文档 + 配置，无功能代码）
- 新增 ADR 0003（掌握度 `陌生 / 接触过 / 想回炉 / 扎实`、L1 `content_type` / `refresher_value`、独立温故栏、L2 `prerequisites[]`、M6 SM-2 简化复习）与 ADR 0004（L2 `est_minutes`、每日时间预算为主约束、深读标记与顺延、积压降级、`backfill` 经典回补）。
- `docs/roadmap.md`：M1–M6 按上述机制增补条目与验收；M1 信源 schema 增加 `role`、`backfill`、`needs_scraper`，写入种子清单。
- `docs/design.md`：数据流图加入 lanes（含 refresher）、时间预算、补课池与 wiki 到期复习回路；数据模型增加相应字段。
- `config/settings.example.yaml`：新增 `daily_budget_minutes: 105`、`lanes`（must_read 3 / worth_it 10 / refresher 2 / serendipity 1）、`deep_read_ratio: 0.6`、`backlog_max_days: 3`；原 `tiers` 与 `serendipity_slots` 合并进 `lanes`。
- `config/sources.example.yaml`：完整种子清单（46 个，其中 41 个启用；5 个标 `needs_scraper`；视频源 `enabled: false` 待 M5）。
- `config/profile.example.md`：改为通用模板（背景、领域掌握度表、不想看的内容、内容偏好、时间预算）。
- 个人 profile 只写在本地 `data/profile.md`（gitignore，不提交）。

**验收结果**
- `uv run pytest -q`、`uv run ruff check .` 通过；`git check-ignore data/profile.md` 确认个人 profile 被忽略。

**遗留问题**
- 种子清单中大量 URL 标 `# TODO verify`，部分 YouTube `channel_id` 为占位符 `TODO`，由 M1 / M5 实测确认。
- 本地个人 profile 中有一项掌握度标注为待确认，需用户在 `data/profile.md` 中确认。

## M1 — 采集层（2026-11-04，分支 `m1-ingest`）

**做了什么**
- 新增依赖：feedparser（RSS/Atom 与 arXiv Atom 解析）、trafilatura（网页正文提取）、pypdf（PDF 文本）。
- `models.py`：`SourceConfig`（含 `role`、`backfill`、`needs_scraper`，未知字段报错）、`FetchSettings`、`RawEntry`。
- `config.py`：`sources.yaml` 不存在时退回 `sources.example.yaml`；信源校验（id 唯一、arxiv 需 query、其余需 url）；`fetch` 设置段。
- `net.py`：`HttpClient`（礼貌 UA、超时、429/5xx 与网络错误指数退避重试、按 host 限速、过大响应拒绝）。
- `sources/`：`rss.py`、`arxiv.py`（官方 API，去版本号）、`hf_papers.py`（JSON API，URL 统一为 arXiv abs 页以跨信源去重）。
- `extract/`：`html.py`（trafilatura）、`pdf.py`（前 `pdf_max_pages` 页）。arXiv 先取 HTML 版再退回 PDF；普通条目 feed 正文足够长就直接用，否则抓原网页。
- `urls.py`：URL 规范化 + `content_hash`；`db.py`：`sources`、`items` 两表，`PRAGMA user_version` 迁移。
- `ingest.py`：信源间线程池并发抓取，单信源失败隔离；只有主线程写库。
- CLI：`sb sources list`、`sb fetch [--source ID] [--limit N]`、`sb items list [--since 2d] [--source ID]`。
- 测试：`tests/test_sources.py`、`test_extract.py`、`test_dedupe.py`（`httpx.MockTransport` + `tests/fixtures/`，PDF 在测试中生成），全部离线。
- 实现取舍见 ADR 0005（新增模块、并发与限速、跨信源去重、`extract_method` 列、`--limit` 口径）。

**验收结果**
- `uv run pytest -q`：52 passed；`uv run ruff check .`：通过。
- `uv run sb fetch --limit 20`（空库）：新增 216 条，失败 0 个信源，跳过 12 个（6 个 `needs_scraper`、5 个视频源与 gwern 未启用），耗时约 100 秒。
- 再次 `uv run sb fetch --limit 20`：新增 0 条，耗时约 9 秒。
- 正文提取：arxiv_html 72、feed 72、html 69、pdf 3；本次没有退回摘要的条目。

**信源实测（2026-11）**
- 修正 URL：vllm-blog → `https://vllm.ai/blog/rss.xml`（原 `blog.vllm.ai/feed.xml` 重定向后 404）；martin-kleppmann → `https://martin.kleppmann.com/feed.rss`（原 `feed.xml` 404）。
- 改为 `needs_scraper`：lmsys-sglang-blog（`lmsys.org` 常见 feed 路径均 404，页面无 feed 链接）。原本已标 `needs_scraper` 的 hazy-research、anthropic-engineering（2026-11-04 改名 `anthropic-research`）、anthropic-news、meta-ai、deepseek-news 实测确认无 RSS。
- 改为 `enabled: false`：gwern（gwern.net 的 feed 指向 substack，2021-06 后未更新）。
- 其余 rss / arxiv / hf_papers 信源均实测可用，已去掉 `# TODO verify`；本次新增 0 条的信源（horace-he、tri-dao、aphyr-jepsen、karpathy-github、lilian-weng、chip-huyen、colah、eugene-yan、qwen-blog、alignment-forum、hillel-wayne）是时间窗内没有新文章，不是故障。

**遗留问题**
- 6 个 `needs_scraper` 信源（含 Anthropic、Meta、DeepSeek 官方和 LMSYS）需要页面抓取支持，暂不采集。
- 视频源的 `channel_id` 仍待 M5 确认（两个为占位符 `TODO`）。
- 跨信源重复的论文只归属先处理的信源（见 ADR 0005）。
- `sb items list | head` 在管道提前关闭时返回码为 1（Click 对 EPIPE 的默认处理，无报错输出）。

## M2 — LLM 后端 + 分层过滤 + 分级（2026-11-04，分支 `m2-filter`）

**做了什么**
- 新增依赖 openai（DeepSeek 的 OpenAI 兼容 API）。
- `llm/`：
  - `base.py`：统一接口 `complete_json`，`Usage`，`LLMError`。
  - `deepseek.py`：JSON 模式；SDK 对网络错误 / 429 / 5xx 退避重试；pydantic 校验失败重试一次；错误信息中的密钥替换为 `***`。
  - `fake.py`：按关键词与标题哈希确定性打分。
  - `usage.py`：`llm_calls` 表，费用按价格表估算，非高峰时段半价。
  - `schemas.py`：L1 / L2 输出 schema。
  - `prompts/l1_system.md`、`l2_system.md`。
- `profile/loader.py`：解析领域掌握度表，掌握度带补充说明（如"接触过（……）"）也能识别；解析失败时给出警告，退回 profile 全文。`data/profile.md` 不存在时才从 example 复制，已存在时从不覆盖。
- `pipeline/`：
  - `l0_rules.py`：时间窗、最短正文、include / exclude 关键词、已处理跳过。
  - `l1_screen.py`：按信源轮询选候选、批量打分；模型漏掉的条目单独补评一次。
  - `l2_deep.py`：论文取 abstract + introduction + conclusion。
  - `tiering.py`：纯函数，负责排序分、时间预算、lanes、深读 / 顺延、积压。
  - `run.py`：编排；L2 前预估费用，超过 `run.max_cost_usd` 就停止；第一个调用先单独执行以预热上下文缓存。
- `db.py`：迁移 v2，新增 `assessments`、`digests`、`llm_calls` 三张表。
- CLI：`sb run [--limit N] [--no-fetch]`、`sb digest show [--date]`、`sb usage [--since 7d]`。
- settings：`llm` 补充超时 / 重试 / 并发 / max_tokens；新增 `pricing_schedule`、`run`、`filter`（l0 / l1 / l2 / score / thresholds / estimate）。
- 新增测试 `test_tiering.py`、`test_pipeline_fake.py`、`test_llm_deepseek_parse.py`，并补充 `test_cli.py`；测试用的 profile 是自造的 `tests/fixtures/profile.md`。
- 实现取舍见 ADR 0006；排序分公式与分级规则写入 `docs/design.md`。

**验收结果**
- `uv run pytest -q`：96 passed；`uv run ruff check .`：通过。
- fake 全链路（临时 `SB_DATA_DIR`，profile 从 example 生成）：L1 30 / L2 15，必读 3、值得读 6、温故 2、意外 1、速览 18，排了 99 / 105 分钟，费用 $0。
- live（DeepSeek，UTC 高峰时段）：
  - 默认 data 目录的第一次 run 发生在修复之前：L1 30 条只完成 21 条（模型漏条）；估时偏高，一篇博客被估成 90 分钟，导致只有必读 2 条和顺延 2 条；费用 $0.2209。
  - 修复（漏条补评、缓存预热、L2 prompt 增加估时锚点）后，在重置过的 scratch 副本上重跑：L1 30 / 30，L1 缓存命中 32k / 40k token；必读 3、值得读 1、温故 1、意外 1、顺延 1、速览 21、归档 2；排了 102 / 105 分钟；费用 $0.2370（L1 $0.0132，L2 $0.2238）。

**遗留问题**
- 默认 data 目录中今天的 digest 来自修复前的那次 run。未做重置（需要清空 M2 的三张表并把条目状态改回 new，属于覆盖数据，等待确认）。
- L2 占费用的约 95%；单次约 $0.22（高峰），非高峰约减半。
- 时间预算常被 1–2 篇长文占满，此时温故 / 意外 / 值得读可能为空，这是"预算为主约束"的预期行为；若希望这些 lane 有保底，需要另行决定是否为其预留预算。

## M3 — 本地 Web UI + 反馈采集 + 定时运行（2026-11-04，分支 `m3-web`）

**做了什么**
- 先处理 M2 遗留的两个决策（见"待决"）：默认 data 不重置；tiering 新增 `lane_budget_reserve`（ADR 0007，默认 refresher 15 / serendipity 10 分钟），补了 5 个单测。design.md 注明同一天多次 `sb run` 共享当日预算。
- 新增依赖：fastapi、uvicorn、jinja2、python-multipart（表单解析）。
- `web/`：
  - `app.py`：`/`、`/item/{id}`、`/history`、`POST /feedback`。
  - `templates/`：服务端渲染的中文页面。
  - `static/`：vendored 的 htmx 2.0.4 和 `style.css`，支持浅色 / 深色模式。
- `db.py`：
  - 迁移 v3：新增 `feedback` 表与 `items.read_at`。
  - 新增反馈读写、详情、历史等查询。
  - 积压改为只统计未读的必读。
- `profile/loader.py`：新增 `weak_prerequisites`，用于"建议先回炉"提示。
- `schedule.py` 与 CLI：
  - `sb serve [--host 127.0.0.1] [--port 8000]`。
  - `sb schedule install [--time HH:MM] [--dry-run]`、`sb schedule uninstall [--dry-run]`。默认本地 07:00，美东冬令时 = UTC 12:00、夏令时 = UTC 11:00，都在非高峰时段。
- README：日常使用、定时运行、cron 替代方案、HTMX 版本与来源。
- 测试：`tests/test_web.py`（TestClient、反馈校验、已读与积压、前置知识提示、定时 dry-run 与 UTC 换算），以及 `tests/test_tiering.py` 新增的预留预算单测。

**验收结果**
- `uv run pytest -q tests/test_web.py`：19 passed；`uv run pytest -q`：120 passed；`uv run ruff check .`：通过。
- `sb serve --port 8000`（默认 data，启动时迁移到 schema v3）：
  - 首页 `data-item-id` 计数 43（digest 51 条，其中 8 条归档不展示）。
  - `POST /feedback item_id=1&kind=up` 返回 200。
  - `data-budget="102/105"`。
  - `/history`、`/item/1` 返回 200；非法 kind 返回 400；不存在的 item 返回 404。
- 验收写入的那条反馈（feedback.id = 1，item 1，kind up）已从默认 data 中删除，feedback 表恢复为空。
- `sb schedule install --dry-run`：打印 plist 与将执行的 `launchctl load -w ...`；没有写入 `~/Library/LaunchAgents`，也没有调用 launchctl。

**遗留问题**
- 非 dry-run 的 install / uninstall（真正写 plist、调用 launchctl）按要求没有执行过。
- 浏览器实际点击验证待主会话进行。

## M4 — 个性化与记忆（2026-11-04，分支 `m4-profile`）

**做了什么**
- `profile/feedback.py`：近期反馈汇总（窗口、条数可配），以及 L1 few-shot 段。few-shot 接在 L1 system prompt 的 profile + 规则之后，没有正负反馈时省略。
- `profile/rewrite.py`：
  - 输入：profile 与反馈。
  - 提案：strong 模型输出结构化提案（schema `ProfileProposal`）。
  - 校验：代码按证据与掌握度规则校验。
  - 输出：生成新文本与 unified diff，未修改的表格行保留原文；写入前备份。
  - 提示词：`llm/prompts/profile_update.md`；fake 后端支持该 schema。
- `profile/tune.py`：按信源统计 👍 / 👎 / 学了 的比例，给出 trust 调整建议；YAML 用行级替换，保留注释。
- `profile/loader.py`：记录掌握度表在文本中的位置，供重建表格。
- CLI：`sb profile update [--dry-run|--apply]`（反馈不足时不调用 LLM）、`sb sources tune [--apply]`。
- Web：`/profile` 页面，可查看、编辑保存（自动备份）、生成提案（HTMX）并应用；应用时校验 sha，生成提案后 profile 被改过则返回 409。导航栏新增"画像"。
- settings 新增 `personalize` 段；`.gitignore` 加入 `config/sources.yaml`、`config/settings.yaml`；CLAUDE.md 目录约定写明这两个文件不提交。
- 规则取舍见 ADR 0008。
- 测试：新增 `tests/test_profile.py`（22 个），使用 fixture profile、临时 SB_DATA_DIR 与临时 CONFIG_DIR。

**验收结果**
- `uv run pytest -q tests/test_profile.py`：22 passed；`uv run pytest -q`：142 passed；`uv run ruff check .`：通过。
- 默认 data 上运行 `sb profile update --dry-run`：输出"无足够反馈，不提议修改（…共 0 条…）"，没有调用 LLM，llm_calls 中无 PROFILE 记录。
- 临时 SB_DATA_DIR（example profile + 复制的 brain.db + 构造的 6 条反馈）+ `SB_LLM_BACKEND=fake`：
  - `--dry-run` 打印 3 处修改的 diff：新增 2 行掌握度，"不想看的内容"段追加 1 行。
  - `--apply` 写入后，`profile_history/` 下有 1 个备份，内容与原 example 一致。
- 默认 data 上运行 `sb sources tune`：46 个信源都没有反馈，提示样本不足，没有建议。

**遗留问题**
- 段落修改只支持追加 / 删除整行；更大的改写需要在 `/profile` 手动编辑。
- 真实 DeepSeek 的提案质量还没验证：默认 data 没有反馈，也不能改用户的真实 profile。

## M5 — 视频讲座、长内容与经典回补（2026-11-04，分支 `m5-longform`）

**做了什么**
- 新增依赖：youtube-transcript-api（直接读取字幕）、yt-dlp（前者失败时退回，只取字幕、不下载视频）。
- `sources/youtube.py`：解析频道 RSS（video id、Shorts 链接、简介）。`sources.example.yaml` 中 5 个视频频道的 channel_id 已查实；默认启用 karpathy-youtube（max_age_days 放宽到 730）和 gpu-mode，其余仍为 false。
- `extract/transcript.py`：
  - 字幕：先人工字幕后自动字幕，限英文 / 中文。
  - 限流：全局最小间隔，HTTP 错误退避重试；被封禁时只退回 yt-dlp 一次，不用代理和 cookie。
  - 存储：每约 30 秒合并为一段 `[mm:ss]` 文本，存入 content。
  - 时间戳工具：解析、生成跳转链接。
- `db.py` 迁移 v4：新增 `items.is_backfill`、`items.duration_seconds`；条目状态新增 `backfill_l1`。
- L0：排除 Shorts 和短于 `min_video_minutes` 的视频；无字幕视频不按正文长度排除，也不进入 L2。
- `pipeline/l2_deep.py`：
  - 长内容 map-reduce：分块后 cheap 模型逐块提要（L2_MAP），strong 模型汇总（L2）；单条精读时各块并发。
  - 费用预估同时覆盖 map 与 reduce。
  - 视频要点带时间戳。
- `pipeline/run.py`：
  - 重构为 `_Runner`，L1 / L2 逻辑复用。
  - 新增回补步骤：每天 `backfill.daily_limit` 条，先粗筛回补池，再按优先级选出做 L2，只进温故栏，没排上的回到池中。
  - 新增 `deep_one`，供 `sb deep` 和 Web 使用。
- `sb deep <item_id> [--force]`；`sb fetch` 输出回补条数和无字幕原因；`sb run` 输出回补统计。
- Web：
  - 速览条目和详情页增加"精读"按钮（HTMX，旁注"将调用 strong 模型"）。
  - 视频要点渲染为时间戳链接。
  - 显示"无字幕 / 经典回补"标记。
  - `/profile` 的"生成修改提案"旁增加费用提示。
- karpathy-github、lilian-weng、colah 设为 `backfill: true`。
- 设计取舍见 ADR 0009。
- 测试：新增 `tests/test_youtube.py`（12）、`test_longform.py`（9）、`test_backfill.py`（7）。

**验收结果**
- `uv run pytest -q tests/test_youtube.py tests/test_longform.py tests/test_backfill.py`：28 passed；`uv run pytest -q`：170 passed；`uv run ruff check .`：通过。
- `sb fetch --source karpathy-youtube --limit 2`（真实网络）：新增 2 条，youtube-transcript-api 直接拿到字幕，没有被限流。
  - item 217 "How I use LLMs"：时长 2h11m，13.1 万字符。
  - item 218 "Deep Dive into LLMs like ChatGPT"：时长 3h31m，因 `content_max_chars` 截断为 20 万字符。
- `sb deep 217`（真实 DeepSeek，UTC 高峰）：
  - 12 段 map + 1 次汇总，耗时 3:05（map 串行）；
  - 6 条带 `[mm:ss]` 的要点，每条附 `watch?v=…&t=…` 链接；
  - 预估 $0.040，实际 $0.0568。
- 之后把 map 改为并发，并调高输出 token 的预估。`sb deep 218`：18 段 map + 汇总，耗时 1:44，预估 $0.103，实际 $0.0722。
- `sb fetch --source lilian-weng`（回补）：新增 53 条，全部进入回补池，耗时 52 秒；未触发 L2。

**遗留问题**
- 超过约 3.5 小时的字幕会被 `fetch.content_max_chars`（20 万）截断，后半段不参与精读；如需完整，可以单独为视频放宽上限。
- 频道 RSS 只有最近 15 个视频，看不到 Karpathy 早期的系列课程。
- lilian-weng 的 feed 中有一条发布时间为 0001-01-01 的条目（疑似站点的非文章页面），已进入回补池，L1 会自行判断。
- 默认 data 中的回补池（53 条）会在下次 `sb run` 时开始粗筛（每次最多 10 条）。

## M6 — 知识 Wiki（LLM-Wiki 闭环）（2026-11-04，分支 `m6-wiki`）

**做了什么**
- M5 遗留：视频字幕单独上限 `transcript_max_chars`；发布时间合理性检查（`sources/base.plausible`）；回补中时间未知的短页不入库。
- 新增依赖 markdown（Web 渲染 wiki 页面）。
- `wiki/store.py`：布局与初始化、slug 规范化与路径校验、frontmatter 读写、index 重建、log 追加、正文的受限编辑。
- `wiki/ingest.py`：结构化提案 `WikiPlan`（prompt `wiki_ingest.md`），由代码校验后落盘；`wiki_ingests` 表保证幂等；新概念的掌握度取自 profile。
- `wiki/lint.py`：断链、缺失页、孤立页、缺字段、index 不一致；`--llm` 检查矛盾。
- `wiki/review.py`：SM-2 简化版、到期列表、复习写回。
- `wiki/loop.py`：L1"已知知识"、到期概念的相关条目、前置知识与概念页匹配。
- `pipeline/run.py`：L1 的 user 消息附"已知知识"；到期概念作为只进温故栏的候选参与分级，写入 `digest_concepts`。
- `db.py` 迁移 v5：新增 `wiki_ingests`、`digest_concepts`。
- CLI：`sb wiki ingest [ID] [--pending] [--force]`、`sb wiki lint [--llm]`、`sb wiki due [--date]`、`sb wiki review <slug> <结果>`；`sb run` 会使用 wiki；`digest show` 显示概念复习。
- Web：`/wiki`、`/wiki/page/<slug>`、复习按钮（HTMX）；首页温故栏显示概念复习条目及相关条目；"建议先回炉"链接到 wiki 页面；导航栏新增 Wiki。
- 规则取舍见 ADR 0010。
- 测试：新增 `tests/test_wiki.py`（16）；`test_backfill.py` 与 `test_youtube.py` 补充了 M5 遗留项的测试。

**验收结果**
- `uv run pytest -q tests/test_wiki.py`：16 passed；`uv run pytest -q`：190 passed；`uv run ruff check .`：通过。
- 默认 data：
  - `sb wiki ingest --pending`：报告 0 条待 ingest，并初始化了 `data/wiki` 骨架（空 index、log）。
  - `sb wiki lint`：0 项问题。
- 真实 DeepSeek 演示：
  - 环境：临时 SB_DATA_DIR（复制 brain.db，profile 用 example），为 item 217 插入一条"学了"反馈。
  - `sb wiki ingest --pending`：新建 4 个概念页（token、context-window、tool-use、reasoning-model）和 1 个 paper 页（how-i-use-llms）。
  - 费用：预估 $0.0085，实际 $0.0160（非高峰；输出 7654 token，之后已把输出预估调到 8000）。
  - `sb wiki lint`：1 项（paper 页是孤立页，属预期）。
  - `sb wiki due`：今天 0 个，明天 4 个。

**遗留问题**
- 名字匹配是子串匹配（"已知知识"、相关页面、前置知识），可能漏配或误配。
- 受限编辑无法删除段落，需要手动在 Obsidian 或编辑器里改。

## M6 修复 — wiki ingest 的 schema 校验失败（2026-11-04，分支 `m6-wiki-ingest-fix`）

**问题**：在 main 上复跑真实 ingest 演示时失败（`模型输出 2 次未通过 schema 校验：1 validation error for WikiPlan`，花费 $0.0172）。

**根因**：
- 复现时 `finish_reason = length`，`completion_tokens = 4096`，其中 `reasoning_tokens = 3298`。deepseek-v4-pro 是推理模型，推理 token 也计入 `max_tokens`，所以正文 JSON 被截断。
- 推理长度每次不同，因此结果不稳定。原来的重试参数完全相同，仍会被截断。

**修复**（ADR 0011）：
- strong 模型的 `max_tokens` 默认提高到 8192。
- 遇到截断时，重试把 `max_tokens` 翻倍（上限 `max_tokens_cap` 16384），并要求输出更精简。
- 校验失败重试时，把"字段路径: 原因"反馈给模型；最终错误信息里也带字段路径。
- WikiPlan 宽容解析：可安全修正的偏差直接修正，单个非法操作只丢弃该操作并在拒绝列表中报告。
- 测试：
  - 新增 fixture `wiki_plan_truncated.json`、`wiki_plan_malformed.json`，均为脱敏后只保留结构的失败样例；
  - `test_llm_deepseek_parse.py` 新增 4 个测试，`test_wiki.py` 新增 3 个测试。

**验收**：
- `uv run pytest -q`：197 passed；`uv run ruff check .`：通过。
- 在同一个临时目录 `m6live.D3CQ` 中真实运行 `sb wiki ingest --pending`：
  - 一次成功，completion 3773 token，没有截断；
  - 新建 4 个概念页和 1 个 paper 页；
  - 费用 $0.0076（非高峰）；
  - `sb wiki lint`：0 项。

## Backlog — sitemap 信源（2026-11-04，分支 `backlog-sitemap-source`）

**做了什么**（ADR 0012）
- `sources/sitemap.py`：新信源类型 `type: sitemap`。用标准库 `xml.etree` 解析 urlset（不支持 sitemap index，遇到时报错）；按 `url_pattern` 正则筛选 URL；发布时间优先级为 URL 中的日期 → 正文页日期 → `<lastmod>`（仅 `use_lastmod: true`），都经过 `plausible()`。
- `models.py`：`SourceType` 加 `sitemap`；`SourceConfig` 加 `url_pattern`、`use_lastmod`（只允许用于 sitemap，正则在加载时校验）；`RawEntry` 加 `page_meta`。
- `extract/html.py`：`extract_page_meta`（trafilatura 提取标题 / 作者 / 日期）；`ingest.extract_content` 在 HTML 分支对 sitemap 条目补全标题 / 作者，URL 无日期时用页面日期覆盖 lastmod。其余流程（时间窗、每源最新 N 再去重、已入库 URL 不抓正文页、日期未知条目的非文章页判断）沿用 `ingest.collect`，没有另设规则。
- `config/sources.example.yaml`：`lmsys-sglang-blog`、`anthropic-news` 改为 `type: sitemap`，去掉 `needs_scraper`。`anthropic-engineering` 当时保持 `needs_scraper`（后续改为 `anthropic-research`，见下）。
- 测试：`tests/test_sitemap.py`（22 个离线 + 1 个 live），fixture `sitemap_dated.xml`、`sitemap_lastmod.xml`、`sitemap_article.html`。

**实测（2026-11）**
- LMSYS：sitemap 143 个 URL，`url_pattern` 保留 137 篇带日期的文章（排除首页、`/blog` 列表页、About 等）；lastmod 全部是同一个构建时间。
- Anthropic：sitemap 589 个 URL。对 lastmod 最新的 14 篇新闻比较页面的 `datePublished`：7 篇一致或差 1–3 天，1 篇差 5 天，6 篇是老文章被刷新（最早实际发布于 2025-09-12）。结论：lastmod 只能用来选候选，不能当发布时间；页面日期（trafilatura 提取，与 JSON-LD 一致）可信。
- Anthropic engineering 的文章在 sitemap 中只出现在 `/research/<slug>` 下，与研究文章混在一起，sitemap 无法单独接入。

**验收结果**
- `uv run pytest -q`：219 passed（原 197 + 新增 22）；`uv run ruff check .`、`uv run ruff format --check .`：通过；`uv run pytest -q -m live tests/test_sitemap.py`：1 passed。
- 临时目录（`SB_LLM_BACKEND=fake`）真实运行 `sb fetch --source <id> --limit 5`：
  - lmsys-sglang-blog：新增 5（"HiCache Across Multi-Parallelism and Distributed Serving" 2026-11-02 等，标题与日期正确）；再跑一次新增 0、重复 5。
  - anthropic-news：新增 5（"Expanding our partnership with GSA …" 2026-10-27 等，日期为页面日期而非 lastmod）；再跑一次新增 0、重复 5。`--limit 20` 时再新增 2 条，其中 1 条是 lastmod 被刷新的老文章（页面日期 2026-09-01），之后由 L0 时间窗过滤。

**遗留问题**
- ~~Anthropic engineering 仍为 `needs_scraper`~~：已按用户选择改为经 `/research/` 接入（见下）。
- Hazy Research、Meta AI（无 sitemap）、DeepSeek 仍在 Backlog。
- `anthropic-news` 的 `url_pattern` 里排除了 8 个分类页，站点新增分类页时需要更新。

**后续：`anthropic-research`（2026-11-04，分支 `backlog-anthropic-research`）**
- 用户选择用 `/research/` 前缀接入 engineering 文章。`config/sources.example.yaml` 中 `anthropic-engineering` 改名为 `anthropic-research`（内容已不只是 engineering；默认 DB 中该 id 从未入库过条目，改名不影响数据）：`type: sitemap`、`use_lastmod: true`、`url_pattern: '^https://www\.anthropic\.com/research/[^/]+$'`，tags 改为 `[research, harness, agents]`，trust / role / max_age_days 不变，去掉 `needs_scraper`。
- 实测 sitemap 中 `/research` 下 213 个 URL 全部有 lastmod：1 个 `/research` 列表页、5 个 `/research/team/*` 团队页由正则排除，其余 207 个单段 slug 都是文章页。
- 临时目录（`SB_LLM_BACKEND=fake`）`sb fetch --source anthropic-research --limit 5`：新增 5（"Progress on provable inference for large language models" 2026-11-02 等；另外 4 条是 lastmod 被批量刷新的老文章，页面日期 2026-03-23 至 2026-10-01，与页面 `datePublished` 一致）；再跑一次新增 0、重复 5。

## M7 — 反馈语义与 Web 运维（2026-11-04，分支 `m7-web-ops`）

**做了什么**（ADR 0013）
- 文章反馈：
  - 改为两组：读之前（稍后读 / 不感兴趣）、读完后（太浅 / 有收获 / 太难 / 没价值）；
  - 每个条目的当前状态 = 最新一次点击，再点高亮按钮记录 `clear` 撤销；读完后四种写 `read_at`；
  - 新页面 `/later`，首页显示"稍后再看：N 条"；积压排除稍后读 / 不感兴趣。
- 个性化：
  - L1 few-shot：正例有收获，反例不感兴趣 / 没价值，校准样例太浅 / 太难；
  - 近期"太难"条目的 L2 前置知识注入 L1 user 消息（`personalize.too_hard_prereq_max_chars`，默认 600）；
  - profile 证据规则：升级需要太浅，有收获只用于 陌生 → 接触过，降级需要太难，每次一级；
  - sources tune：有收获为正，不感兴趣 / 没价值为负。
- wiki：
  - `--pending` = 当前状态为有收获；
  - 新概念页从"接触过"起步，首次复习间隔 `wiki.first_review_days`（3 天）；
  - 复习新增"需要回炉"（立即到期）、"已扎实"（间隔 ≥ `wiki.mastered_interval_days`，30 天）；
  - 原文目录移到 `data/wiki/raw`。
- 运维：
  - `ops.py`：`run_once` / `ingest_items` / `apply_trust`，CLI 与 Web 共用；`data/run.lock` 用 `fcntl.flock`；
  - `web/tasks.py`：后台线程任务，HTMX 2 秒轮询；
  - 首页"编进 wiki"，`/ops`（用量、立即运行、高峰提示、trust 建议），`/wiki` 体检按钮。
- 信源管理 `/sources`：
  - 列表含最近抓取结果（schema v6 `sources.last_fetch_*`）、入库数、正 / 负反馈数；
  - 试抓预览后添加，启用 / 停用，删除二次确认；
  - 写 `config/sources.yaml`：先从 example 复制，原子写，注释不保留。
- 知识图谱：`/wiki/graph` 与 `/wiki/graph.json`，前端用 vendored 的 Cytoscape.js 3.34.4。版本、来源与 sha256 见 design.md / ADR 0013。没有新增 Python 依赖。
- 测试：
  - 新增 `test_feedback.py`（11）、`test_ops.py`（9）、`test_sources_web.py`（5）、`test_graph.py`（4），`test_wiki.py` 新增 1 个（raw 目录）；
  - 因语义变更修改了 `test_profile.py`、`test_web.py`、`test_wiki.py` 中的旧测试（反馈类型改名、证据规则、新概念页掌握度、raw 路径）。

**验收结果**
- `uv run pytest -q`：251 passed；`uv run ruff check .`、`uv run ruff format --check .`：通过。
- roadmap M7 的验收命令在 `mktemp -d` 临时目录中全部实际运行过（fake LLM，端口 8765）：
  - 首页 5 条条目都带两组按钮；
  - `POST /feedback kind=gained` 返回"已记录：有收获"，`read_at` 已设置；
  - `/later`、`/ops`、`/sources` 正常；
  - 后台 wiki ingest 状态为 done；
  - `/wiki/graph.json` 返回 3 个节点、2 条边；
  - 持锁时 `sb run --no-fetch` 被拒绝，exit=1。
- 浏览器中手动检查：首页按钮、后台 ingest 进度与结果链接、图谱渲染（无控制台错误）、`/ops`、`/sources` 都正常。

**遗留问题**
- 后台任务只在内存中，Web 服务重启后进行中的任务丢失；Web 中 run 与 ingest 并发时依靠 SQLite 锁等待。
- `/sources` 写回会丢失 sources.yaml 的注释（已在页面与 README 注明）。
- `profile/loader.mastery_for` 已无调用方，保留未删；`wiki.ingest.apply_plan` 的 `profile` 参数保留但不再使用。
- 已有页面中旧的 `../../../raw/<id>.md` 链接在 Obsidian 中会失效（默认 wiki 为空，无影响）。

## 默认模型改为 deepseek-flash（2026-11-04，分支 `flash-default`）

- 用户判断 deepseek-v4-pro 性价比低：贵很多，但没有明显更聪明。因此 cheap / strong 两级默认都改为 `deepseek-flash`（ADR 0014），涉及 `config/settings.example.yaml` 与 `LLMSettings` 的默认值。两级机制和 v4-pro 的价格条目都保留，可在 `config/settings.yaml` 中切回。
- `max_tokens` 保持不变（cheap 4096 / strong 8192）。`llm_calls` 表不记录 reasoning_tokens，无法从已有数据判断 flash 是否返回推理 token。已有数据中 flash 的最大 completion 为 3771（L2_MAP），v4-pro 的 L2 最大为 7324。8192 的上限和截断后的翻倍重试对 flash 同样适用。
- 测试：`test_llm_deepseek_parse.py` 的分层测试改为显式配置 strong = v4-pro（默认两级相同，已经无法检验分层），并新增一个默认值测试。

## M8 — 深度速读与前置知识（2026-11-04，分支 `m8-deep-summary`）

**做了什么**（ADR 0015）
- 预算：
  - 深读条目按 `deep_summary_minutes`（15）占用预算，速读时间也放不下才顺延；
  - schema v7 新增 `digests.budget_minutes`（旧行取 est_minutes）与 `item_ai` 缓存表；
  - 首页"今日已排"、`sb digest show`、同日多次 run 都按占用预算计算；卡片显示"速读约 15 分钟 · 全文约 90 分钟"。
- 深度速读：
  - `pipeline/summary.py` 读全文，长内容 cheap 分块（SUMMARY_MAP）后由 strong 汇总（SUMMARY），视频保留时间戳；
  - 结果缓存，重新生成需要确认；
  - 入口：`sb summary`、item 页与深读卡片上的按钮（后台任务）。
- 补前置：
  - 有速读时只用速读的前置知识，否则用 L2 的（名字轻量归一后去重；最初的合并做法在真实运行中约一半是近重复，已改）；wiki 已有的给链接与掌握度；
  - 没有的用一次 strong 调用（PREREQ）生成讲解并缓存，同时列出库中最多 3 条讲解类文章；
  - "存为概念页"不调用 LLM；
  - 入口：`sb prereq`、item 页按钮。
- 费用提示改为按当前模型价格估算的"非高峰 – 高峰"区间（替换按 v4-pro 写的"约几美分"）。
- 测试：
  - 新增 `test_summary.py`（12）、`test_budget.py`（3），`test_tiering.py` 新增 2 个；
  - 因语义变更修改了 `test_tiering.py` 中的 2 个旧测试，以及 `top_minutes` 辅助函数。

**验收结果**
- `uv run pytest -q`：269 passed；`uv run ruff check .`、`uv run ruff format --check .`：通过。
- roadmap M8 的验收命令已在 `mktemp -d` 临时目录中全部实际运行（fake LLM，端口 8765）。
- 默认数据副本的对比（fake 后端，源文件只读复制）：
  - 今天（2026-11-04）再跑 `sb run --no-fetch`：main 与本分支结果相同，都是顺延 7 条、今日已排 102 / 105。原因是当天先前 run 已排的 2 条必读按旧规则占了 102 分钟（迁移后旧行的 budget_minutes = est_minutes），剩余 3 分钟放不下 15 分钟的速读。
  - 模拟第二天（2026-11-05，同一份副本）：

    | | 顺延 | 前排 | 今日已排 |
    |---|---|---|---|
    | main | 6 | 必读 2 / 温故 2 / 值得读 2 | 103 / 105 |
    | 本分支 | 1 | 必读 3 / 值得读 3 / 温故 1 / 意外 1 | 103 / 105 |

    本分支中 6 篇 80–110 分钟的论文各按 15 分钟进入前排。

**遗留问题**
- 升级当天先前 run 已占用的预算按全文计，第二天起才生效。
- AI 讲解未经人工核对。
- 深度速读按全文输入，费用高于 L2，但都是按需触发。
- 补前置在 Web 中是同步请求（一次调用，约半分钟）。

## M9 — 手动运行与信源产出率（2026-11-05，分支 `m9-manual-run`）

**做了什么**（ADR 0016）
- 运行方式开关：
  - `/ops` 显示当前模式（每天 HH:MM 自动 / 手动）以及 LaunchAgent 是否已加载；
  - "改为手动 / 开启每日自动"按钮需要确认，并提示所选时间是否落在高峰；有运行在进行时拒绝改为手动；
  - `schedule.status / install / uninstall` 由 CLI 与 Web 共用；
  - 测试通过 `SB_LAUNCH_AGENTS_DIR`（conftest 中 autouse）指向临时目录，不碰真实的 LaunchAgents 与 launchctl。
- 首页"开始今日过滤"：
  - 今天还没有 digest 时显示按钮、耗时与费用区间，可勾选不限时间窗；
  - 点击后在后台运行并轮询，完成后整页刷新；
  - 锁被占用时显示"正在运行"；今天已运行过时只留"再运行一次"。
- 不限时间窗：`sb run --all-time` 与 Web 勾选项。collect 不截断（每源仍取 fetch_limit 条），L0 对新条目 ignore_age。
- 速览与归档折叠为 `<details>`，折叠区域内有反馈按钮。
- 信源产出率：
  - `/sources` 显示近 30 天的入库、精读、前排、正 / 负；
  - 满足条件的信源标记"低产出，建议停用"，并给出停用按钮；
  - `sb sources tune` 只读列出低产出信源。
- 测试：新增 `tests/test_manual_run.py`（9 个）；conftest 新增 autouse fixture，隔离 LaunchAgents 目录。没有修改旧测试。

**验收结果**
- `uv run pytest -q`：279 passed；`uv run ruff check .`、`uv run ruff format --check .`：通过。
- roadmap M9 的验收命令已在 `mktemp -d` 临时目录中全部实际运行（fake LLM，端口 8765，临时 LaunchAgents 目录）。
- 默认数据副本对比（`sqlite3 .backup`，fake 后端，`sb run --no-fetch`）：
  - **按副本原样**：带与不带 `--all-time` 的结果相同，都是 L0 检查 0 条、顺延 2 条、今日已排 105 / 105。原因是副本里没有待处理的常规新条目（57 条 new 都在回补池），`--all-time` 只影响本次的新条目。
  - **把 30 条曾因"超出时间窗"被 L0 排除的条目重置为 new 后再对比**：
    - 不带 `--all-time`：L0 排除 30 条（超出时间窗 30），L1 送入 0 条；
    - 带 `--all-time`：L0 排除 0 条，L1 送入 30 条，L2 选中 15 条，分级为顺延 3、速览 29。当天预算在副本里已经用满，所以新条目都进了速览。

**遗留问题**
- 不限时间窗的首次运行：L1 在高峰时段约 $0.5，可能触发 `run.max_cost_usd` 而跳过 L2（详见 ADR 0016），建议非高峰运行或临时调高上限。
- "今天是否已运行"以 digest 为准，没有产出的运行不算。
- 只有使用 run.lock 的代码（M7 起）发起的运行才能被检测到。

## M10 — 筛选、信源主页与运维设置（2026-11-05，分支 `m10-filters-settings`）

**做了什么**（ADR 0017）
- 筛选：首页与 `/history` 有形式（`web/filters.item_form` 纯函数推导）和类型（L1 content_type）两排 chip，通过 `?form=&ctype=` 组合。只影响显示，lane 计数按筛选结果，被筛空的 lane 显示空状态。
- 信源主页：
  - 新增可选的 `homepage` 配置，没配时按类型推导；
  - RSS 的 channel `<link>` 在抓取时存入 `sources.homepage`（schema v8）；
  - 只输出 http(s) 链接，`rel="noopener noreferrer"`；
  - "进入前排"改为"进入推荐"，并加 tooltip。
- 配置：
  - `config/settings.yaml` 深合并覆盖在 example 之上；
  - 新增 `SB_CONFIG_DIR` / `SB_ENV_FILE` 覆盖路径；
  - CLAUDE.md 中的配置规则同步更新。
- `/ops` 设置：
  - API key 只显示状态与来源；写入或清除 `.env`：原子写、chmod 600、立即生效；环境变量优先时给出提示；
  - 测试连接（stage PING）；
  - cheap / strong / backend 只把与 example 不同的键写入 settings.yaml，可以恢复默认。
- CSRF / Host middleware：
  - Host 必须是本机地址或 `serve --host` 的值；
  - 修改类请求的 Origin / Referer 必须同源，两者都没有时放行。
- 测试：
  - 新增 `tests/test_m10.py`（27 个）；
  - conftest 新增两个 autouse fixture：一个把 config 目录与 .env 指向临时路径，一个把 TestClient 的 Host `testserver` 加入白名单（仅测试中）；
  - `test_manual_run.py::test_low_yield_sources` 因"进入前排"改名为"进入推荐"而更新了两处字符串。

**验收结果**
- `uv run pytest -q`：306 passed；`uv run ruff check .`、`uv run ruff format --check .`：通过。
- roadmap M10 的验收命令已在 `mktemp -d` 临时目录中全部实际运行（fake LLM，端口 8765；data、config、.env、LaunchAgents 都是临时路径）：
  - `.env` 权限为 600，页面不回显 key；
  - settings.yaml 只包含改动的那个键；
  - 跨源 POST 与恶意 Host 都返回 403，同源与无头请求返回 200。

**遗留问题**
- 非浏览器客户端（没有 Origin / Referer）不受 CSRF 校验，这是有意的取舍（ADR 0017）。
- 筛选状态下不显示概念复习。
- 通过环境变量设置的 key 无法在页面上修改。
