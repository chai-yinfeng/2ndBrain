# Roadmap

每个 milestone：分支 `mN-<name>` → 实现 → 跑通验收命令 → PR → 审阅后 merge。完成后在此勾选并更新 `docs/progress.md`。偏离本计划的取舍写 ADR（`docs/decisions/`）。

## M0 — 仓库与骨架

- [x] `git init`（main）、remote、`.gitignore`
- [x] `pyproject.toml`（uv，src 布局；typer、httpx、pyyaml、pydantic；dev：pytest、ruff）+ `uv.lock`
- [x] 包骨架 + `sb --help`、`sb doctor`（Python 版本、config 文件、data 目录可写、LLM key 只报已设置 / 未设置）
- [x] 文档：`CLAUDE.md`、`README.md`、`docs/design.md`、`docs/roadmap.md`、`docs/progress.md`、ADR 0001 / 0002
- [x] `.env.example`、`config/*.example.*`
- [x] 首个提交推送到 `origin/main`

**模块**：根目录配置、`src/secondbrain/cli.py`、`config.py`、`docs/`

**验收**
```bash
uv sync
uv run sb --help
uv run sb doctor
uv run pytest -q
uv run ruff check .
git log --oneline -1 && git remote -v && git status --short   # 干净、remote 正确
git ls-remote origin main                                       # 已推送
```

## M1 — 采集层（博客 RSS/Atom、arXiv、HF Daily Papers）

- [ ] `config/sources.yaml` schema：`id, type(rss|arxiv|hf_papers), url/query, trust(1–5), tags, max_age_days, enabled`；预置示例清单（Karpathy blog、Lilian Weng、Simon Willison、OpenAI / Anthropic / DeepMind / Meta AI 官方博客、HF Daily Papers、arXiv cs.CL / cs.LG + 关键词）
- [ ] `db.py`：建表 `sources, items`（`url_canonical` 唯一、`content_hash`、`published_at`、`fetched_at`、`title/author/summary/content`、`status`）；简单 schema 版本号迁移
- [ ] `sources/*`：feedparser（新增依赖）解析 RSS/Atom；arXiv 官方 API（Atom）；HF Daily Papers 公开 API / 页面
- [ ] `extract/html.py`：trafilatura（新增依赖）正文提取；`extract/pdf.py`：优先 arXiv HTML 版，退回 PDF 文本（pypdf）
- [ ] 去重：URL 规范化 + content hash；httpx 超时、重试、礼貌 UA（通用客户端名，不含个人邮箱）
- [ ] CLI：`sb sources list`、`sb fetch [--source ID] [--limit N]`、`sb items list [--since 2d] [--source ID]`

**模块**：`sources/`、`extract/`、`db.py`、`models.py`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_sources.py tests/test_extract.py tests/test_dedupe.py   # 基于 fixtures 离线
uv run sb sources list                          # 列出示例信源
uv run sb fetch --limit 20                      # 真实网络：新增 >0 条，逐源打印成功/失败
uv run sb fetch --limit 20                      # 再跑一次：新增 0 条（去重生效）
uv run sb items list --since 7d | head
```

## M2 — LLM 后端 + 分层过滤流水线 + 分级

- [ ] `llm/base.py`：`complete_json(messages, schema, tier="cheap"|"strong") -> (obj, usage)`
- [ ] `llm/deepseek.py`：openai SDK、JSON 模式、重试、schema 校验失败重试一次
- [ ] `llm/fake.py`：按 fixture / 规则返回确定性结果
- [ ] `llm/usage.py`：`llm_calls` 表记录模型、tokens（含 cache hit）、按 config 价格估算 $
- [ ] `profile.md`：首次运行从 `config/profile.example.md` 复制到 `data/profile.md`
- [ ] L0 规则（零 token）：时间窗、include / exclude 关键词、最短长度、已处理跳过
- [ ] L1 粗筛（cheap 模型，批量 ~10 条 / 调用，profile 放 system 前缀以吃缓存）：输出 `relevance, novelty, quality (0–10), one_line_reason`；分数 × 信源 trust 取 top-N 进 L2
- [ ] L2 精读（strong 模型）：全文截断（论文取 abstract + intro + conclusion，上限可配，默认 ~24k token），输出中文 `tldr, key_points[], why_for_you(两句), scores`
- [ ] `tiering.py`（纯代码）：`must_read ≤3 / worth_it ≤10 / fyi / archived`，配额可配，允许为空，保留 1 个"意外"名额
- [ ] 表：`assessments`、`digests`
- [ ] CLI：`sb run [--limit N] [--no-fetch]`、`sb digest show [--date]`、`sb usage [--since 7d]`
- [ ] prompts 放 `llm/prompts/*.md`

**模块**：`llm/`、`pipeline/`、`db.py`、`cli.py`、`config/settings.example.yaml`

**验收**
```bash
uv run pytest -q tests/test_tiering.py tests/test_pipeline_fake.py tests/test_llm_deepseek_parse.py  # 离线
SB_LLM_BACKEND=fake uv run sb run --limit 30 && uv run sb digest show           # 离线全链路
uv run sb run --limit 30          # 真实 DeepSeek（需 .env 中 DEEPSEEK_API_KEY）
uv run sb digest show             # 中文分级输出，每条带理由；must_read ≤3
uv run sb usage --since 1d        # 本次 token 与估算费用
```

## M3 — 本地 Web UI + 反馈采集 + 定时运行

- [ ] `web/app.py`（FastAPI + Jinja2 + uvicorn，新增依赖；HTMX 固定版本 vendored 到 static）
- [ ] `/` 今日 digest（按层级分组：TL;DR、理由、信源、原文链接）
- [ ] `/item/{id}` 详情（key points、L1 / L2 评分、原文）
- [ ] `/history` 按日期浏览
- [ ] HTMX 反馈按钮：👍 / 👎 / 已读 / 稍后读 / 学了 → `POST /feedback`，写 `feedback` 表（item_id, kind, ts, note）
- [ ] `sb serve [--port 8000]`
- [ ] `sb schedule install`（macOS launchd plist，每日跑 `sb run`，默认 DeepSeek 非高峰时段）/ `sb schedule uninstall`；文档说明 cron 替代方案

**模块**：`web/`、`db.py`（feedback 表）、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_web.py        # TestClient：首页 200、含条目；POST /feedback 写入成功
uv run sb serve --port 8000 &
curl -s localhost:8000/ | grep -c 'data-item-id'     # > 0
curl -s -X POST localhost:8000/feedback -d 'item_id=1&kind=up' -o /dev/null -w '%{http_code}'   # 200
uv run sb schedule install --dry-run      # 打印将写入的 plist
```
（另用浏览器实际点按钮验证一遍。）

## M4 — 个性化与记忆

- [ ] `profile/feedback.py`：汇总近期反馈（喜欢 / 不喜欢 / 学了的标题、标签、信源）
- [ ] L1 prompt 注入：profile.md + 最近 N 条正 / 负反馈样例（few-shot）
- [ ] `profile/rewrite.py`：`sb profile update` 用 strong 模型提出 profile.md 修改，输出 diff，`--apply` 才写入，旧版本存 `data/profile_history/`
- [ ] 信源信任建议：按 👍 / 👎 比例建议 trust 调整（`sb sources tune --apply` 才改）
- [ ] Web：`/profile` 查看 / 编辑 profile.md，查看并应用 LLM 修改提案

**模块**：`profile/`、`pipeline/l1_screen.py`、`web/`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_profile.py    # fake 反馈 → 生成 diff；L1 prompt 含反馈样例
uv run sb profile update --dry-run        # 打印 profile diff，不写文件
uv run sb profile update --apply && ls data/profile_history/
uv run sb sources tune                    # 打印信任度调整建议
```

## M5 — 视频讲座与长内容

- [ ] 新信源类型 `youtube`（频道 RSS：`https://www.youtube.com/feeds/videos.xml?channel_id=...`）
- [ ] `extract/transcript.py`：优先 youtube-transcript-api，退回 yt-dlp 字幕（新增依赖，说明理由）；保留时间戳
- [ ] 长内容 L2 分块 map-reduce 总结（块大小可配），视频 key points 带时间戳链接
- [ ] UI：未进入 L2 的条目提供"按需精读"按钮（触发单条 L2）

**模块**：`sources/youtube.py`、`extract/transcript.py`、`pipeline/l2_deep.py`、`web/`

**验收**
```bash
uv run pytest -q tests/test_youtube.py tests/test_longform.py   # fixture 字幕 + fake LLM
uv run sb fetch --source <示例 youtube 源 id> --limit 2         # 真实拉取并有 transcript
uv run sb deep <item_id>                                        # 单条精读，输出带时间戳要点
```

## M6 — 知识 Wiki（LLM-Wiki 闭环）

- [ ] `data/wiki/`：`index.md`（目录 + 一行摘要）、`log.md`（只追加，`## [date] ingest | Title`）、`pages/{concepts,entities,papers}/*.md`，`[[wikilink]]`，YAML frontmatter；原文存 `data/raw/` 并链接回去
- [ ] `wiki/ingest.py`：`sb wiki ingest <item_id>` / `--pending`；LLM 读条目 + index.md + 相关页面，输出结构化"页面新建 / 更新"操作，由代码落盘（LLM 不直接写文件）
- [ ] `wiki/lint.py`：`sb wiki lint`：孤立页、断链、缺失概念页、可能矛盾（LLM 辅助）
- [ ] 闭环：L1 prompt 注入 index.md 相关条目作为"已知知识"→ 已掌握的降低 novelty，与已知概念相关的新进展加分
- [ ] Web：`/wiki` 浏览页面与 index；（可选）简单关系图

**模块**：`wiki/`、`pipeline/l1_screen.py`、`web/`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_wiki.py       # fake LLM：ingest 生成页面 + 更新 index/log；lint 能发现断链
uv run sb wiki ingest --pending           # 真实：把"学了"的条目编译进 wiki
cat data/wiki/index.md && tail -5 data/wiki/log.md
uv run sb wiki lint
```

## Backlog（不在本轮）

- [ ] X / Twitter 信源
- [ ] IM 推送（Telegram / 邮件）
- [ ] embedding 语义去重 + 同一事件多信源聚合（sqlite-vec）
- [ ] Anthropic / `claude -p` 后端
- [ ] wiki 图谱可视化增强
