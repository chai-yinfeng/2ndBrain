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

## M11 — 信源手动维护与手动添加文章（2026-11-06，分支 `m11-manual-sources`）

**做了什么**（ADR 0018）
- 反馈与 trust 解耦：
  - 删除 trust 建议与写入：`source_stats` / `_suggest` / `apply_trust_changes` / `ops.apply_trust`、`/ops` 的应用按钮与 `POST /ops/tune`、`sb sources tune --apply`、`personalize.tune_*`；
  - `sb sources tune` 改为只读统计；
  - `/sources` 显示各信源反馈分布，纯参考。
- `sources_file.py`：
  - `config/sources.yaml` 按条目文本块追加 / 删除 / 改 enabled，保留注释；写入前校验，原子写；
  - 支持 `sources: []`；首次编辑时复制 example 原文（含注释）。
- 信源目录：
  - 列出 example 中未添加的信源（主页、论文类标记），可预览（只解析、不写库）、原样追加；
  - 已配置的信源也可预览。
- 空信源：`sb fetch` / `sb run` / 首页给出提示，不调用 LLM。
- 手动添加文章：
  - 入口：`sb add`、`/add`（导航"添加"，稍后读页有链接）；
  - 条目归到虚拟信源 `manual`、状态 `manual`，不进 L1 / L2 / 分级 / 预算；
  - 准备读 = later，已读 = 读后反馈或"已读、无评价"（只设 read_at）；
  - 可选正文粘贴；"已读 + 有收获"可直接编入 wiki。
  - "待编进 wiki"在没有 digest 时也显示。
- 测试：
  - 新增 `test_sources_catalog.py`（9）、`test_manual_add.py`（7）；
  - 因语义变更修改的旧测试：
    - `test_profile.py`：3 个 trust 建议相关测试换成反馈分布与只读 CLI 测试；
    - `test_feedback.py`：tune 计数测试改为反馈分布；
    - `test_ops.py`：`/ops` 页测试去掉 trust 应用部分；空信源 `sb run` 的期望改为友好提示；
    - `test_sources_web.py`：写回保留注释；
    - `test_m10.py`：CSRF 的 Referer 用例改用仍存在的路由。

**验收结果**
- `uv run pytest -q`：320 passed；`uv run ruff check .`、`uv run ruff format --check .`：通过。
- roadmap M11 的验收命令已在 `mktemp -d` 临时目录中全部实际运行（fake LLM，端口 8765），包括：
  - 空信源提示；
  - 目录 46 条；预览 lmsys 解析到 138 条；
  - 添加后 sources.yaml 保留头部注释，条目原样追加；toggle 只改 enabled；
  - `sb add` 新增与已存在；`/add` 批量中一条新增、一条失败；
  - 待编入为 1。

**遗留问题**
- 用 flow 风格（`- {id: …}`）手写的条目无法按块定位。
- 旧 settings.yaml 中的 `tune_*` 键会被静默忽略。
- 手动条目不进每日推荐。

## M11 修复 — RSS 主页兜底、/add 布局、DOI / PDF 导入兜底（2026-11-06，分支 `m11-fixes`）

**做了什么**（ADR 0019）
- RSS 信源主页：优先级为 配置 > 已记录的 channel link > feed URL 的站点根。feeds.* 子域与 feedburner 等托管域名不给链接。"预览"时显示并记录 channel link，不写 items。
- `/add` 改为纵向表单：
  - 每个字段 label 在上、输入框整宽；URL 框 6 行，正文粘贴框 8 行；
  - 状态 / 已读时 / 兜底三组 fieldset，长说明放在下方小号灰字；
  - 选"准备读"时用纯 CSS（`:has`）把"已读时"一组调淡，无 JS 也可用。
  - 截图在 1280 与 375 宽下检查过。
- DOI：识别 URL 中的 DOI 并查 Semantic Scholar。
  - 有 arXiv → 走 arXiv 流程；有开放获取 PDF → 用 PDF；否则只导入标题 + 摘要（`abstract_only`，结果显示"新增（仅摘要）"）；
  - 规范 URL 为 `https://doi.org/<doi>`。
- 失败原因具体化（403 / 404 / 429 / 超时 / 机器人验证页 / 不是文章页），并提示"可粘贴正文或上传 PDF"。
- PDF 上传兜底：`/add` 的上传框与 `sb add --pdf`，≤ 30MB、只接受 PDF；`python-multipart` 已是现有依赖。已存在的"仅摘要"条目再次添加时，可以用上传的 PDF 或粘贴的正文补全正文。
- 测试：
  - 新增 `tests/test_manual_fixes.py`（14 个 + 1 个 live），fixture 为 `s2_arxiv.json`、`s2_oapdf.json`、`s2_abstract.json`；
  - 修改了 `test_m10.py` 中两处主页断言（rss 现在有兜底链接）和 `test_manual_add.py` 中一处失败原因（现在更具体）。

**验收结果**
- `uv run pytest -q`：334 passed；ruff check / format 均通过。
- 在临时目录用真实网络对 Borg 的 ACM 链接运行 `sb add --read`（fake 后端）：
  - 第一次尝试遇到 S2 限流（429）且页面返回 403，输出了两边合并的失败原因；
  - 再试一次成功："新增（仅标题，S2 没有摘要） #1 Large-scale cluster management at Google with Borg"，作者与年份来自 S2，规范 URL 为 `https://doi.org/10.1145/2741948.2741964`。

**遗留问题**
- 未带 key 的 S2 请求偶尔会被 429 限流，需要稍后重试。
- Borg 在 S2 中没有摘要，所以导入后正文只有标题，建议再次添加时上传 PDF（如 research.google 上的版本）来补全正文。

## M12 — html_index 信源与论文导入增强（2026-11-06，分支 `m12-index-papers`）

**做了什么**（ADR 0020）
- 信源目录：每个条目都写了 `homepage`，指向博客列表页或频道页，全部实测（核对表见下）。
- 新信源类型 `html_index`：
  - 解析列表页 `<a href>`，按 `url_pattern` 过滤、去重、保持顺序，可选 `max_links`；
  - 列表页结构化日期优先，其次正文页 meta；
  - 目录新增 `sglang-blog`。
- 手动导入：
  - `citation_pdf_url` 优先下载 PDF，citation_* 补元数据；
  - DOI 查 S2，Crossref 兜底；
  - Retry-After 有上限（10 秒）；可选 `S2_API_KEY`，`/ops` 可设置。
- 跨标识去重（schema v9：`items.doi` / `items.arxiv_id`）：按规范 URL → DOI → arXiv id 查重，更完整的正文会补全"已存在"条目；同名手动条目只提示。
- "正文不完整"：详情页 / 稍后读显示徽标，详情页可补全；新增 `sb items incomplete`。
- 已存在条目重新抓取：不完整的条目再次导入同一链接时自动重抓，仅在更完整时替换；manual 条目详情页"重新抓取"按钮；`sb add <url> --refetch`（审阅后补充，便于补全 M12 之前导入的条目）。
- 测试：
  - 新增 `test_html_index.py`（8 个 + 1 个 live）、`test_paper_import.py`（14 个）；
  - 修改的旧测试：
    - `test_m10.py` / `test_manual_fixes.py` 中检验主页推导的两个测试，改用去掉 homepage 的配置，因为 example 现在写了明确的 homepage；
    - `test_m10.py` 中 schema 版本断言改为 `len(MIGRATIONS)`。

**homepage 核对表**（2026-11-06 用浏览器 UA 实际请求）

| id | homepage | HTTP | 备注 / 页面标题 |
|---|---|---|---|
| vllm-blog | https://vllm.ai/blog | 200 | Blog / vLLM |
| lmsys-sglang-blog | https://lmsys.org/blog/ | 200 | 重定向到 www.lmsys.org/blog/ |
| sglang-blog | https://www.sglang.io/blog | 200 | SGLang Blog – Releases, Benchmarks &amp; Announcements |
| pytorch-blog | https://pytorch.org/blog/ | 200 | Blog - PyTorch |
| horace-he | https://www.thonking.ai/ | 200 | Thonk From First Principles / Horace He / Substack |
| tri-dao | https://tridao.me/blog/ | 200 | Blog / Tri Dao |
| hazy-research | https://hazyresearch.stanford.edu/blog | 200 | Hazy Research |
| tim-dettmers | https://timdettmers.com/ | 200 | Tim Dettmers &mdash; Making deep learning accessible. |
| chips-and-cheese | https://chipsandcheese.com/ | 200 | Chips and Cheese / Substack |
| marc-brooker | https://brooker.co.za/blog/ | 200 | Marc Brooker's Blog - Marc's Blog |
| murat-demirbas | https://muratbuffalo.blogspot.com/ | 200 | Metadata |
| aphyr-jepsen | https://aphyr.com/posts | 200 | Aphyr - Posts |
| martin-kleppmann | https://martin.kleppmann.com/archive.html | 200 | Martin Kleppmann&rsquo;s website |
| karpathy-github | https://karpathy.github.io/ | 200 | Andrej Karpathy blog |
| karpathy-bearblog | https://karpathy.bearblog.dev/blog/ | 200 | Blog – karpathy |
| lilian-weng | https://lilianweng.github.io/ | 200 | Lil&#39;Log |
| sebastian-raschka | https://magazine.sebastianraschka.com/archive | 200 | Archive - Ahead of AI |
| chip-huyen | https://huyenchip.com/blog/ | 200 | Blog |
| colah | https://colah.github.io/ | 200 | Home - colah's blog |
| interconnects | https://www.interconnects.ai/archive | 200 | Archive - Interconnects AI |
| hf-blog | https://huggingface.co/blog | 200 | Hugging Face – Blog |
| anthropic-research | https://www.anthropic.com/research | 200 | Research \ Anthropic |
| simon-willison | https://simonwillison.net/ | 200 | Simon Willison’s Weblog |
| hamel-husain | https://hamel.dev/ | 200 | Hamel Husain’s Blog – Hamel's Blog |
| eugene-yan | https://eugeneyan.com/writing/ | 200 | Writing • Eugene Yan |
| openai-news | https://openai.com/news/ | 200 | OpenAI News / OpenAI |
| anthropic-news | https://www.anthropic.com/news | 200 | Newsroom \ Anthropic |
| google-deepmind | https://deepmind.google/blog/ | 200 | /discover/blog/ 重定向，写最终地址 |
| meta-ai | https://ai.meta.com/blog/ | 400 | 机器请求 400（人工浏览可打开），保留 |
| qwen-blog | https://qwenlm.github.io/blog/ | 200 | Blog / Qwen |
| deepseek-news | https://api-docs.deepseek.com/news/ | 200 | 文档站标题通用，页面含 /news/ 条目 |
| hf-daily-papers | https://huggingface.co/papers | 200 | Daily Papers - Hugging Face |
| arxiv-cs-cl | https://arxiv.org/list/cs.CL/recent | 200 | Computation and Language |
| arxiv-cs-lg | https://arxiv.org/list/cs.LG/recent | 200 | Machine Learning |
| arxiv-cs-dc | https://arxiv.org/list/cs.DC/recent | 200 | Distributed, Parallel, and Cluster Computing |
| import-ai | https://importai.substack.com/archive | 200 | Archive - Import AI |
| latent-space | https://www.latent.space/archive | 200 | 重定向到 latent.space/archive/ |
| lesswrong-curated | https://www.lesswrong.com/recommendations | 200 | /curated 重定向，写最终地址 |
| alignment-forum | https://www.alignmentforum.org/ | 200 | AI Alignment Forum |
| gwern | https://gwern.net/ | 200 | Essays · Gwern.net |
| dan-luu | https://danluu.com/ | 200 |  |
| hillel-wayne | https://www.hillelwayne.com/post/ | 200 | Blog |
| karpathy-youtube | https://www.youtube.com/channel/UCXUPKJO5MZQN11PqgIvyuvQ | 200 | Andrej Karpathy - YouTube |
| stanford-online | https://www.youtube.com/channel/UCBa5G_ESCn8Yd4vw5U-gIcg | 200 | Stanford Online - YouTube |
| gpu-mode | https://www.youtube.com/channel/UCJgIbYl6C5no72a0NUAPcTA | 200 | GPU MODE - YouTube |
| dwarkesh-podcast | https://www.youtube.com/channel/UCXl4i9dYBrFOabk0xGmbkRA | 200 | Dwarkesh Patel - YouTube |
| adam-neely | https://www.youtube.com/channel/UCnkp4xDOwqqJD7sSM3xdUiQ | 200 | Adam Neely - YouTube |

**验收结果**
- `uv run pytest -q`：356 passed；ruff check / format 通过。
- roadmap M12 验收命令在临时目录实际运行（fake LLM，联网），结果：
  - sglang-blog 新增 3 条，日期分别为 2026-11-04 / 10-22 / 10-22；预览解析到 13 条；
  - USENIX Twine 通过 citation_pdf_url 拿到全文 PDF（84885 字符）；
  - 先导入 PagedAttention 的 arXiv 链接，再导入 ACM DOI 链接，显示"已存在"（arXiv API 带了 arxiv:doi）；
  - Dynamo（ACM DOI）只有摘要，被 `sb items incomplete` 列出；
  - schema 为 v9；S2_API_KEY 设置后不回显。

**遗留问题**
- 未带 key 时 S2 经常 429。此时如果先导入 DOI、再导入 arXiv（arXiv API 也没有 doi），无法按标识合并，只会提示"可能重复"。
- html_index 依赖列表页的静态 HTML。
- 一次全量测试中出现过 1 个未复现的失败（之后连续 16 次全绿），疑似与时间相关的测试偶发不稳定，没有定位到具体用例。

## 架构调整 — 材料与个人知识网络优先（2026-11-16，仅文档）

**已完成**
- ADR 0021 记录新方向：主动导入、阅读整理、Obsidian 兼容 wiki 与个人知识网络为主线；订阅和每日推荐保留为辅助能力。
- `docs/design.md` 区分目标架构与 M0–M12 现状，明确统一材料入口、阅读/处理/推荐状态分离、Markdown 事实来源及图谱浏览边界。
- `docs/roadmap.md` 增加未完成的 M13–M15、端到端验收场景、Goal 执行边界与可复用任务正文；README 同步实际 M12 进度及计划。

**范围与验证**
- 只修改文档，不修改应用代码、个人数据或 OMP 审批配置，不启动 Goal。
- 文档链接、milestone 状态与结构由本次静态检查验证；没有重跑应用测试或执行浏览器/Obsidian 验收。M13–M15 均未实施。

**待讨论**
- 图谱交互、材料页组织、关系语义的后续扩展，以及 Agent 检索、prompt 和思考强度等实现细节，未作为既定实现锁定。

## M13 — 统一材料与独立知识整理（2026-11-16）

**实现**
- 在独立 worktree `2ndbrain-material-network` / 分支 `m13-m15-material-network` 实施；验收根目录 `/tmp/secondbrain-m13-m15.mrpmhR`，data/config/.env/LaunchAgents 均隔离。未改真实数据、个人配置或 launchd，未推送或创建 PR。
- schema v10 保留材料 ID 与子表引用，移除强制 manual 信源，增加多来源、原件、完整性及独立阅读/处理状态。PDF、文本、URL、订阅、refetch/complete 共用材料持久化；身份冲突明确拒绝，不按标题强并。
- CLI/Web 可独立导入、阅读、记笔记、整理入 wiki；稳定材料页和共享概念不依赖 L2、digest 或 gained。新概念为陌生/未复习，重复处理不覆盖个人内容。长文逐段整理，成功和失败用量按阶段记账，保守预估受费用上限约束。

**实际验证**
- 相关 15 个测试文件首轮 `190 passed, 3 failed, 1 deselected`；失败为旧页面身份/反馈契约及 textarea 行数断言。更新真实契约、删除布局断言后，三项定向回归 `3 passed`。旧库实测额外发现无信源反馈进入个性化时的 None 拼接错误，修复并新增流水线回归；该回归与 `tests/test_profile.py` 合计 `22 passed`。受影响 Python 文件 `uv run ruff check ...` 通过。
- `m13_smoke.py` 在空信源/fake 环境用 CLI 独立导入本地 PDF、文本和真实博客 URL（Simon Willison 的 “Building Python tools with a one-shot prompt using uv run and Claude Projects”）：3 份材料、7 页 wiki；无 assessments/feedback/digest，仍全部未读，个人笔记经强制重整保留。
- `check_legacy.py` 在真实旧库只读副本上实际完成 v8→v10：3 材料、3 反馈、3 wiki_ingests、12 个 Markdown 文件保留，原字段/ID 和引用不变，外键检查通过；材料及 wiki/source 路由均可打开。迁移副本另用受控订阅入口新增 1 条并经 fake 生成 1 条推荐，修复后的来源缺省路径正常。
- WebKit 浏览器在 `127.0.0.1:8765` 实际导入文本、保存中文笔记、点击整理、进入材料知识页、打开来源/独立笔记和原 PDF；生成后仍为未读，刷新后处理状态为 processed。截图保存在本次运行的工具产物中。

**边界**
- 本阶段知识整理使用 fake，不声称真实模型质量通过；M13 要求的离线流程与旧库迁移已验证。跨端布局、公式和 Obsidian 验收属于 M14，尚未据此标记完成。两个验收脚本在临时目录运行，交付前清理。

## M14 — 知识网络与主入口（2026-11-16，实现与 Web 验收完成；跨端验收未完成）

**实现**
- 首页、主导航调整为材料/网络/导入；推荐移到 `/recommendations`，所有历史日期/筛选调用方同步迁移，无旧 root 推荐兼容壳。CLI 增加 `wiki reindex / show / graph --focus --depth`。
- 图谱、反向链接、lint、渲染共用中文、别名、相对路径和歧义解析；重复文件名使用 vault-relative `.md` ID，外部新增/删除/改链接可重建，代码中的 wikilink 不生成边。全局/局部图、逐步展开、名称定位、详情/来源/反链/返回图谱完整接通。
- 安全 Markdown 白名单渲染，危险 URL/事件属性/原始 HTML 不执行。KaTeX 0.16.22（MIT，来源及哈希见 README）离线渲染公式，`trust:false`；无新增 Python 依赖。
- Obsidian 实测发现独立笔记与材料页同名会错误跳转。改为 `notes/material-<id>-notes.md`，迁移旧文件并更新非代码引用；原字节和用户文字保留，Web/CLI 已验证迁移后笔记可读。

**实际验证**
- 首轮 `uv run pytest -q tests/test_network.py tests/test_graph.py tests/test_wiki_render.py tests/test_network_web.py tests/test_wiki.py tests/test_material_wiki.py tests/test_material_web.py tests/test_web.py tests/test_manual_run.py tests/test_summary.py tests/test_m10.py tests/test_budget.py tests/test_sources_catalog.py tests/test_feedback.py tests/test_ops.py tests/test_longform.py`：`192 passed, 4 failed`。修复旧 raw 链接语义映射及新/旧材料同标题时的旧 paper 关联；更新中文 ID/新首页契约。四个失败项与 renderer 定向复验 `37 passed`。笔记同名修复后的网络/Web/wiki 回归 `21 passed`；代码引用保护定向回归 `1 passed`，相关 `ruff check` 通过。
- 受影响 Python 文件 `ruff check`、两个新增 JavaScript 文件 `node --check` 通过；后续图布局改动由实际浏览器解析与渲染证明。
- 临时 vault 的 CLI `wiki reindex` 得到 14 页/12 条链接；`wiki graph --focus 跨端别名 --depth 1/2` 正确逐层包含节点，`wiki show 中文别名` 可读正文/出链/反链。
- WebKit 实际完成全局图→别名定位→局部 5 节点→从注意力机制展开到 6 节点→正文/来源/反链→回图谱；共享验收概念连接本地论文 PDF 与真实博客 URL。验证重复标题两候选选择、孤立页 1 节点/0 边、筛选空状态、空 vault 引导。1280 桌面与 390 窄屏实际截图覆盖节点/边/长标题、表格/代码/两类公式，窄屏无横向溢出；修复孤立页切换后过度缩放/标签重叠。
- 隔离 Obsidian 1.15.1 确实打开同一临时 vault，阅读模式截图显示中文/别名、MathJax 公式、表格与原样代码，发现了上述同名跳转缺陷。

**未完成条件（M14 仍不标整体完成）**
- 用户报告隔离 Obsidian 实例出现「找不到钥匙串」提示；立即停止实例，没有要求密码、创建/重置钥匙串或更改真实配置，不再使用临时 HOME 启动方式。
- 修复后的 Obsidian 材料跳转重验、原文件、局部/全局图、原生编辑链接→Web 重索引一致性尚未完成。Web/CLI 的 Markdown 外部编辑、反链与链接删除已由回归覆盖，不能代替这部分原生交互验收。按 Goal 约定继续完成可达的 M15，不隐瞒缺口。

## M15 — 论文名智能导入（2026-11-16）

**实现**
- 新增共享 `papers` 服务，CLI `sb paper add / choose` 与 Web `/add` 论文表单共用。完整标题或名称加作者/年份/会议可检索；明确候选自动导入，歧义只选择已保存候选。选择时不重跑查询、不信任客户端 URL；刷新可继续查看同一后台任务。
- 复用 Semantic Scholar/Crossref/arXiv 与现有正文提取，补充 OpenAlex 和 USENIX 官方检索；搜索不调用 LLM。标题不能证明材料身份；已共享公共标识时允许作者缩写/连字符差异，来源原记录仍保留。
- schema v11 保存不可变候选、各次导入结果、来源元数据及 LLM 错误/未知用量。所有正文仍走 M13 材料服务，摘要只作兜底，继续寻找其他已知全文地址；已知 arXiv 版本优先明确重放。CLI/材料详情页展示请求与实际版本和实际地址。

**离线与迁移验证**
- 首轮命令：`uv run pytest -q tests/test_paper_search.py tests/test_paper_surfaces.py tests/test_materials.py tests/test_material_wiki.py tests/test_material_web.py tests/test_paper_import.py tests/test_ops.py tests/test_web.py tests/test_summary.py tests/test_network_web.py tests/test_manual_add.py tests/test_manual_fixes.py`，结果 `151 passed, 8 failed, 1 deselected`。失败来自测试把客户端类型替换成函数、无效 HTML fixture 和摘要必须不含标题的实现断言；修复 fixture/契约后定向 `9 passed`。
- 真实多 provider 元数据的作者缩写导致原本明确的 DOI/arXiv 记录错误呈现歧义；修复仅在标识一致时的名字归一，并验证不同作者/年份/标识仍需选择，定向 `5 passed`。补充短名称+元数据、已选整理意图保留、明确版本优先与失败保留材料；相关定向 `3 passed`、`4 passed`，最终 `uv run pytest -q tests/test_paper_search.py` 得到 `40 passed`。
- `ruff check` 对 M15 受影响 Python 文件通过。覆盖准确匹配、同名歧义、无结果、只有摘要、跨版本/已有材料、候选篡改、身份冲突、安全 URL、实际 wiki 链接、费用拒绝和未知用量错误，不把摘要冒充全文。
- 最终代码在真实旧库的临时副本上完成 v10→v11：4 材料、3 反馈、2 assessments、1 digest、3 wiki_ingests 均保留，外键、7 个知识页及所有旧材料/页面路由和 Markdown 字节检查通过。原真实库仍只读使用，未执行重置。

**真实网络 / 模型 / Web 验收**
- `m15_live.py` 从完整论文名检索 Orca：USENIX 官方 `/conference/osdi22/presentation/yu` 找到 `https://www.usenix.org/system/files/osdi22-yu.pdf`，实际导入 19/19 页、90,213 字符及完整 PDF，材料 #1、未读、`full`。本次未能核实文件修订号，元数据版本为 published，实际修订号保留 unknown，不伪造。
- `uv run sb paper add 'Efficient Memory Management for Large Language Model Serving with PagedAttention' --ingest` 实际解析 DOI `10.1145/3600006.3613165` / arXiv `2309.06180`，自动导入材料 #2。明确版本重放取得 `https://arxiv.org/html/2309.06180v1`，保存请求/实际版本 v1、76,477 字符、`full`；原地址和文件/HTML、提取文本保留。
- `uv run sb paper add 'PagedAttention Kwon 2023'` 及已保存候选的 `sb paper choose` 实际命中同一材料。重复 CLI/Web 提交后仍只有 2 份材料、均未读，无 feedback/digest，外键完整。
- 已有凭据只读取不回显；一次真实 `deepseek-flash` 整理 PagedAttention：预估预留 `$0.359218`，现有上限 `$0.5`，7 次 WIKI_MAP + 1 次 WIKI 成功、0 错误，应用按用量估算 `$0.013557558`（不是账单声明）。未放宽费用上限；生成材料页、共享概念/实体及网络，mastery 不自动提升。
- WebKit 浏览器实际完成论文名提交→后台结果→重复材料 #1；又用带错误作者线索的查询得到候选，刷新后按真实 DOI/arXiv 选择正确候选→仍为材料 #2→未读/版本来源→真实机器整理正文→局部网络（6 节点/11 链接），完整页面与来源均可访问。全局网络 8 节点/13 链接，CLI 同样可查看来源和实际版本。
- Semantic Scholar 曾真实返回 429，已作为 provider 错误持久记录；Crossref/arXiv/USENIX 等可达路径继续完成验收，没有用 fake 候选掩盖网络失败。知识整理同时有离线 fake 与上述一次真实模型证据。

**剩余边界**
- M15 验收通过；M14 的 Obsidian 剩余项目仍未完成，未因 M15 成功而勾选。关于钥匙串，未执行创建、重置或解锁命令；[Electron 官方说明](https://www.electronjs.org/docs/latest/api/safe-storage) macOS safeStorage 依赖 Keychain，Linux 明文后端选项不能保证解决 macOS 问题，因此没有用未确认的参数再次触发用户提示。

## 阅读流程语义调整（2026-11-17，ADR 0022）

**已实现**

- 信源是长期关注的主页；先识别/预览，再确认关注，不自动把文章加入清单。参考目录折叠、可筛选/清除筛选；没有正式配置时默认零关注，首次保存不复制整份 example。既有正式文件按条目维护，保留其他内容和注释。
- 通用添加接受博客/网站 URL、仅保存引用、PDF、文本和论文名，默认进入用户维护的稍后清单。来源预览中主动选择的文章保留信源出处，但仍是手动材料，不进入推荐流水线。
- schema v12 拆出 `reading_list`；加入/移出不要求反馈。CLI `reading list/add/remove/read` 与 Web 共用登记服务；登记外部阅读后移出清单，保存学习笔记和讨论引用并整理知识。讨论不抓取；无正文时支持仅凭明确提交的学习笔记整理，完整性仍为 missing。
- 相同输入复用知识整理，不新增模型调用，也不要求 API key；有改动或显式 force 才重做。费用预估/记录、错误与失败重试保留，失败不删除已读记录、材料或笔记。
- 未读机器草稿与 recorded 正式知识分开，默认图/目录/复习/已知知识排除草稿；旧页面保留 legacy，不据此推断掌握度。页面与 CLI 支持 pin/block/reset 及原因，双向禁止约束在机器落盘和图/反链中执行，个人笔记保留。
- 主导航按信源、阅读列表、知识库、设置与管理组织；旧分析和推荐是辅助入口。本次没有引入 shadcn/ui、React 或整体视觉重设计。

**实际验证**

- 全程在独立 worktree `2ndbrain-material-network`、临时根目录 `/tmp/secondbrain-reading.y3Cpac`；data/config/.env/LaunchAgents 隔离，没有修改真实数据、个人配置或 launchd，没有启动 Obsidian。
- 离线分组覆盖新增 `test_reading_workflow.py`、`test_knowledge_governance.py`、`test_source_discovery.py`、`test_reading_semantics_web.py`，以及受影响的材料、配置、信源、CLI/Web 约定。分组运行中发现的旧约定用例已迁移，并单独通过；最后来源追溯端到端用例 + `tests/test_materials.py` 为 **17 passed**。相关 Python 文件的 Ruff 检查通过；本次未重跑全套测试，不把这些结果表述为全套通过。
- 真实 Web 操作：从 `https://simonwillison.net/` 识别 RSS、确认关注、预览并选择 `Release: llm 0.37.3`；确认队列、材料原文及信源出处；保存示例外部学习笔记/讨论引用并登记。重复登记显示复用/无模型调用；新增只保存引用的网站材料停留在稍后清单，没有请求该网站。
- 真实 DeepSeek：在该临时材料上 force 一次，保留原有 `$0.5` 应用限制，预估 `$0.0372`；一次 `deepseek-flash` WIKI 调用成功，按实际 usage 与配置价格估算 **$0.002735634**，错误为空、usage_known=1。新增 SQLite JSON functions 和依赖升级行为概念，更新材料页。仅是这份短材料的样本，不代表均价或服务商账单承诺。
- 人工约束：网页禁止 material-1 ↔ llm 后，fake 和真实模型重新整理均未恢复该边，个人笔记保持；另用标为“验收示例”的理由固定 llm ↔ sqlite-json-functions，网页图观察到 **4 节点/4 链接**。390px 窄屏阅读列表实际观察，无水平溢出。
- CLI 实跑主页识别预览，前后 `sources.yaml` 字节相同；`sources list` 只有明确关注的一项，`reading list` 仅剩未读引用。切到真实模型后端并移除该进程 API key，再执行 `reading read 1` 仍复用成功，数据库模型调用计数没有增加。
- 旧 v11 验收副本再复制到临时 migration-data，注入 later/clear 历史以检查最新意图后实际升到 v12：4 材料、6 反馈、2 分析、1 digest、3 wiki_ingests 保持；仅最新 later 的 #4 入队，原阅读状态保持。12 个 Markdown 文件哈希一致，4 个材料详情和 7 个知识页均能打开，foreign_key_check 无错误。

**可查看状态与限制**

- 预览服务 `sb-reading-final-preview`：`http://127.0.0.1:8765/`，使用上述临时数据，默认 fake 后端，继续体验不会调用付费模型；其中已有一次真实模型的整理结果。示例笔记/人工关联明确是验收内容，不代表用户已确认的理解。
- 真实网站主页发现实测 RSS；HTML 列表、sitemap、无订阅的手动主页、公网/重定向/体积边界由离线受控测试覆盖，不承诺任意动态或登录账号自动提取。私密 ChatGPT 对话未抓取。
- M14 原生 Obsidian 跳转、资源、图谱与原生编辑联动的剩余验收仍未完成，不改勾选；本次新增的草稿/禁止关联规则也不声称原生 Obsidian 会执行。用户先查看业务结果，再讨论整体 UI 风格。
- 本轮只本地提交，不 push、不创建 PR。

## Web 视觉改版与图谱工作区（2026-11-17）

**实现**

- 按用户确认的布局采用左侧导航、紧凑列表和 shadcn/ui 风格的中性色、细边框、圆角及主次操作；统一材料/信源/设置/画像/历史/知识页面。移动导航支持抽屉、背景遮罩、Escape、焦点回归和焦点约束。
- 亮色、暗色、跟随系统三态主题，默认跟随系统；浏览器本地持久化，跨页/刷新保持，并响应系统变化。图谱同步颜色，不修改个人 settings。
- 阅读列表本页标题/来源搜索，次级操作收进菜单；添加材料四类 tab 分别保留本页草稿，隐藏字段禁用，文件选择在切换后保留。信源参考目录保持临时展开、筛选、清除与关闭。
- 图谱使用画布＋节点 inspector，按类型着色、图例、标签省略/降噪、一跳高亮、别名搜索、来源跳转、全局/局部、逐步展开、缩放/适应/重置。验收发现力布局会把小图挤团，改为连接度对数分层；局部显式改变时排列当前子图，普通筛选/选择/主题不重排，返回全局恢复原位置。
- 参考官方 [Archify](https://github.com/tt-a1i/archify) 与 [Graphify](https://github.com/Graphify-Labs/graphify) 的聚焦、详情和可解释探索，不照搬其语义边，不引入 React、依赖、CDN、MCP 或第二份图数据。

**验证**

- 独立 worktree、既有临时目录 `/tmp/secondbrain-reading.y3Cpac`；没有改真实数据、个人配置或 launchd，没有启动 Obsidian。
- 相关 Web/材料/图谱/信源/论文/阅读语义分组：首轮 50 passed、2 个旧视觉断言失败；移除旧 ✗ 徽标及 table/tr 结构依赖，保留非法 id/HTTP 404 错误与数据库抓取状态验证；两例单独运行 **2 passed**。`ruff check tests/test_sources_web.py` 通过。未重跑全套。
- WebKit 实际验证亮暗与跟随系统、刷新持久化、系统变化和手动选择优先；阅读列表搜索/清空、信源参考目录展开/筛选/清除、四类输入草稿切换。修复后小图含标签包围盒没有交叠；图谱一跳定位与展开、筛选空态、别名定位和知识页跳转通过。
- 390px 窄屏实际遍历 add/sources/wiki/graph/item/materials/ops/profile/history/recommendations，页面宽度均为390、没有整页横向溢出；导航开关、inert 隔离、Escape 和焦点回归通过。
- 另建独立 Markdown 验收 vault：84 页面、160 链接。真实服务渲染全局图并进入 4 节点局部范围，长中文标题、节点类型、别名和 inspector 可操作。此服务验收后已停止，样例未混入主预览知识库。
- Chromium 验证文本材料和本地 PDF 实际新增、仅引用链接新增且不抓取正文，PDF 文件在类型切换后保留；已登记材料再次提交返回303、复用整理且个人笔记保持；drafts=1 在全局/重置后保持。实际浏览器没有应用脚本错误。
- 主要页面最终目视检查发现导航模板的 `path` 变量遮蔽了信源配置路径，已将导航变量限定在局部作用域。新增真实配置位置的回归用例 **1 passed**，浏览器确认显示临时 `config/sources.yaml`，不是页面路由 `/sources`。
- WebKit 截图在部分暗色切换/滚动时出现固定侧栏文字未重绘现象，已报告工具 QA；同一页面 Chromium 实际截图正常。不把截图工具现象当作原生 Obsidian 或全浏览器一致性验证。

**交付与边界**

- 预览继续使用 `http://127.0.0.1:8765/` 的隔离数据与 fake 后端；本轮新增的引用/文本/PDF 均为验收样例，既有真实模型知识页保留。密度验收服务8766已停止。
- 本轮没有更改 CLI/业务接口、数据 schema、AI 费用上限或人工关系语义；M14 原生 Obsidian 未完成验收仍保留。本地提交，不 push、不创建 PR。

## 修复 — 改版后的静态资源版本与缓存

**用户现象与证据**

- 用户在 `127.0.0.1:8765/ops#main-content` 截图中看到无固定侧栏、居中窄内容、旧深色背景及蓝色/胶囊按钮，与新版布局明显不同；这些特征与改版前 CSS 一致。直连同一端口的新浏览器能显示新版。
- 已确认发布缺口：模板仍使用不带版本的 `/static/style.css` 等资源 URL，服务未设置 Cache-Control。这允许浏览器/代理沿用旧资源。用户浏览器缓存未能直接读取，因此“该端具体命中了哪一层缓存”仍是推断，不声称已直接检查；没有证据据此判定 React/HTMX 不兼容或端口映射错误。

**修复**

- 全部模板 CSS/JS（包括图谱、KaTeX）统一以 `static_asset` 生成带文件内容摘要的 URL；缓存摘要以 mtime/ctime/大小失效，文件变化自动换地址，不要求每次改样式手动改应用版本。
- HTML 与任务片段 `no-store`，静态文件及304响应 `no-cache`，保留 ETag 条件请求。不清用户缓存、不改主题偏好、业务数据、个人配置或 launchd，不迁移 React、不增加依赖。
- 已将原隔离预览替换为 `sb-reading-cache-preview`，仍使用 `/tmp/secondbrain-reading.y3Cpac` 的 data/config/empty.env/LaunchAgents 和 fake 后端，端口仍为8765。

**验证与边界**

- 新增3个回归：已缓存 CSS 内容变更后无需重启即生成新地址（覆盖大小/mtime保持不变）；未变文件条件请求304仍带缓存策略；HTML与任务状态不存储。与相关 Web/网络测试分组首轮31 passed，仅一个旧硬编码 script URL 断言失败；删除该实现/文案断言，保留推荐预算和本地 vendored 文件行为验证，原用例单独通过。相关 Python Ruff 检查通过，未跑全套。
- 实际浏览器打开同一 `/ops`：样式 URL 为 `/static/style.css?v=11d0af502091a27d`，侧栏计算定位 fixed；知识页全部7项直接 CSS/JS 资源带版本并返回200/no-cache，图谱正常显示4节点/4链接，无应用脚本错误。
- 给用户的重新进入地址为 `http://127.0.0.1:8765/ops?ui=asset-version#main-content`，query 仅用于绕过旧页面入口缓存。用户自身浏览器恢复情况仍需其查看，不能用本端截图替代用户端验收。仅本地提交，不 push、不创建 PR。

## 开始个人试用前清理演示数据

- 用户授权移除开发填充内容。停服前只读确认：只有已知5份演示材料、4个知识页、4条稍后记录、1个演示信源；笔记与验收文本一致、画像与模板一致，没有用户新增材料或知识页，也没有运行中的预览任务。
- 停服后事务清空隔离库的演示记录及用量，移除演示 wiki/原件/笔记和当前根目录的密度、迁移验收副本；清空临时关注列表，保留默认画像、参考目录、配置模板及空 env。真实数据、个人正式配置、launchd 和源码测试 fixtures 未改。
- 用户明确选择真实 DeepSeek。服务现在为 `sb-personal-preview`，仍在8765；只读解析已有个人密钥并仅注入该预览进程，配置/写 key 仍指向临时 empty.env，不把真实密钥写入仓库、临时文件或日志。现有费用限制不变；本轮未调用模型。
- 浏览器实查：首页三项均为0，知识图谱空态、添加材料默认稍后、真实后端与 key 已设置状态正确，无脚本错误。未再导入任何验收样例；本轮没有代码变更，不重跑测试。用户材料的真实整理质量和本次模型连接留待其主动登记阅读检验。
- 试用入口：`http://127.0.0.1:8765/add?workspace=clean`。导入文件默认不调用模型；登记阅读会把提取正文/学习记录交给 DeepSeek，可能产生费用。
- **从此 `/tmp/secondbrain-reading.y3Cpac/data` 用于用户自己的试用内容，不能再当作纯开发样例清空。** 后续自动测试必须另建临时目录。该位置仍是临时试用存储，用户应保留重要原件；长期使用前需迁到持久化目录。

## 来源统一识别、原位维护与内容领域补齐（ADR 0023）

**完成内容**

- 主页、配置预览与 CLI/Web 共用 FETCHERS；复用已关注和完整参考适配，失败时尝试匹配的不同参考适配但保留原身份，全部失败不降级书签或写配置。网络失败、空结果、手动浏览和拒绝分开。
- 来源 name/ID 分离；按字段编辑、跨进程/线程锁、ID/实际外键/元数据原位迁移及文件/数据库失败补偿。修复不覆盖用户 tags/trust/role/启停/时间窗/主页和已有名称；检查期间采集配置被改则拒绝写旧结果。
- sitemap index 与实际采集共用同源有界展开，保留公网/重定向/体积限制。参考目录隐藏/清空/恢复只影响候选显示，不删关注或适配规则。
- 免费展示匹配目录标签与已有文章主题；显式 cheap 分类最多10条标题摘要，返回领域、内容性质、置信度和真实样本依据，独立缓存，不覆盖手动标签/材料/阅读状态。SOURCE 用量、错误、预算和未知价格保护；未在用户库自动调用分类模型。
- 关注行桌面约64–65px，设置和统计不再单独占一块；空候选不渲染大框。补 CLI repair/edit/analyze/catalog，以及 Web 对应维护、任务状态和证据展示。schema v13 仅追加两张元数据表。

**验证**

- 所有样例和模型测试使用 `/tmp/secondbrain-sources.K79JAB`、fake与离线 fixtures；用户试用库不用于测试。核心首轮145 passed，旧 sitemap 夹具4例因新增公网DNS检查失败，补受控DNS后进入整合。
- 整合分组188 passed、4例失败：一个fake样本缺少其已支持关键词，修正样例；旧目录文案/徽标断言删除；旧幂等用例保留新增0/重复3/记录不增长，删除已与 M13 材料补全路径不符的请求次数假设。本轮没有改 ingest.py。修正项连同新免费主题上下文及 CLI/Web 工作流共8 passed；相关 Python Ruff 通过。未重跑全套。
- 网络只读检查现有9来源全部 ready；HF Papers 主页走 hf_papers API、DeepMind 主页走 RSS，各有50条候选。没有为已知站点另写抓取器，也未重复失败主页来证明用户报告。
- 浏览器在独立副本验证显示名保存、显式fake领域分析及样本、目录清空/恢复仍保留9关注；390px设置展开无整页横向溢出。真实预览重启后只读核对9来源与65px紧凑行、参考标签、分类按钮，无脚本错误。实际模型分类质量尚未验证，不把fake结果当真实分类。

**用户关注保留与交付**

- 更新前确认用户任务未运行；当前仍为9关注、0材料。保存修复前配置副本至隔离验收目录后，仅逐项 apply 已验证修复。9项均 ready，本次实际只补缺失名称，没有重建来源或改采集规则。
- 严格核对ID及顺序、tags/trust/role/enabled/backfill/max_age_days/homepage和既有名称保持；材料/来源记录/阅读列表/反馈/知识整理/评估/笔记与文件哈希、LLM调用记录前后完全一致。没有清理用户来源、材料或个人内容，也没有批量分类费用。
- 当前服务 `sb-sources-personal-preview`：`http://127.0.0.1:8765/sources?update=unified`；仍为用户试用数据及真实DeepSeek进程环境，个人.env只读、临时配置隔离、上限不变。独立8767验收服务已停止。
- 不启动Obsidian、不改launchd、不push或PR；M14原生验收缺口不变。今后该试用目录继续视为用户内容，不可自动清空。

## 独立下载预算、栏目证据与 Qwen Research（ADR 0024）

**实现与取舍**

- 旧800000字节上限来自正文长度×4且最多1MB，已拆为 FetchSettings 独立单响应8MB/累计16MB（解压后），保留8请求/3跳/公网检查；正文窗口仍200000字符。诊断带阶段、去查询值的URL和预算；耗尽为 incomplete，不能当成已证实只能手动浏览。
- sitemap 的HTML响应先明确拒绝，不再按XML报行列号。主页/声明RSS/同源已知RSS提供有限证据，综合RSS和同名分类分别展示，用户确认key后服务端重新发现验证；不自动扩大/缩小栏目。rss_categories 后续真实采集也过滤，scope_note 保存当时依据，现有homepage/身份和人工设置保持。
- OpenAI Research 精选页包含 Product 等发布，而RSS也混合多个分类；同名Research过滤不等价。真实新流程2请求下载1,229,847字节，得到综合与Research两个候选：10个页面URL重合，其中分类Research命中2、另8个属于其他/无分类。页面候选链接还含导航，未核对数量不冒充可靠文章覆盖率。
- Qwen新增公开GET适配：qwen_ai/en-US retrieval与旧research.research-list/en配置；旧60项按官方0.0.91前端53项白名单过滤，再合并新41项，本次94条，排序后预览50。真实2请求4,919,053字节；不执行脚本，不以旧站RSS冒充新列表。真实HTML文章导入走接口，legacy摘要明确区分；未知文章不回退采集SPA壳。

**验证**

- 全部样例在 `/tmp/secondbrain-ingress.1R2RDJ`，fake模型/离线夹具。核心分组233 passed，仅旧“请求预算耗尽=failed”断言失败，更新为 incomplete 与明确request_limit；该项及预算/范围/材料整合等40 passed。相关Ruff通过；未跑全套。
- 浏览器在8768副本实际比较OpenAI宽/窄候选、确认Research过滤并核对保存字段；390px无整页横向溢出。Qwen从输入主页→确认关注→预览→首篇加入阅读列表成功，得到真实 `Qwen4-Max: Your New Teammate Has Arrived` 正文，不是导航页，无脚本错误、无模型调用。独立验收服务已停止。

**用户预览保护**

- 更新前确认无运行任务，用户现有13关注、0材料；没有自动关注OpenAI/Qwen，也没有替用户选择采集范围。
- 只同步两个示例规则/预算模板，正式sources/settings、env、画像保持；数据库所有表和wiki文件哈希基线前后相同。未清理任何用户关注或资料，未触发模型费用。
- 当前服务 `sb-scoped-personal-preview`，同一8765端口与用户试用目录，真实DeepSeek环境仍只在进程中读取密钥；页面只读核对13来源正常。M14原生验收缺口不变，无Obsidian/launchd/push/PR。

## 来源显示排序与独立设置/预览窗口

- 已关注默认最近导入（配置原顺序逆序），支持首字母排序；显示名称回退ID，使用浏览器中文collator，大小写/数字排序，本地保存选择。只改变视图，不写配置顺序或来源时间戳。
- 点击来源名称与右侧设置均打开独立原生dialog，主页跳转只留在右侧。主页识别、关注与参考目录选文预览也进入可关闭窗口，支持Escape/遮罩、焦点回归、预览→设置切换，主体独立滚动；不再撑开整行。删除旧data-disclosure脚本和行内展开样式。
- 预览导入反馈保留在窗口内，关闭不取消后台任务；迟到响应不重开弹窗。保持背景滚动位置，防止原生autofocus及导入状态回填造成列表位移。
- 回答用户Meta Blog提示问题：html_index已解析10条不是失败；范围推断/未抓正文的成功提示改成中性色，手动入口与真正失败分开；关注不等于自动推送。
- 隔离目录 `/tmp/secondbrain-dialogs.f1Ftli`：相关回归 **28 passed**、相关Ruff通过；未跑全套。浏览器实际验证排序发生改变、刷新/保存后的偏好保持、中文/数字名称、名称打开设置、保存、关闭/Escape/焦点回归、预览切设置、导入反馈和390px无横向溢出。受控延迟网络响应回归验证关闭后不重开；真实Meta列表10条成功提示为灰色，无应用脚本错误。
- 修正后实测弹窗前后背景scrollY、列表位置与高度相同，关闭预览后也不回填长结果挤压列表。样例和实际选文仅写隔离测试库；用户预览当前25来源，读取新页面前后正式sources与数据库各表哈希一致。本轮没有业务Python、schema、模型/费用或个人配置变更，没有业务模型调用。
- 原8765服务无需重启，版本化静态资源与HTML已更新。入口 `http://127.0.0.1:8765/sources?update=source-windows`；独立8769验收服务结束后停止。不push/PR、无launchd/Obsidian；M14原生缺口不变。
