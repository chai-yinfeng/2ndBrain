# Roadmap

每个 milestone：分支 `mN-<name>` → 实现 → 跑通验收命令 → PR → 审阅后 merge。完成后在此勾选并更新 `docs/progress.md`。偏离本计划的取舍写 ADR（`docs/decisions/`）。掌握度 / 温故栏 / 前置知识见 ADR 0003，每日时间预算见 ADR 0004。

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

- [x] `config/sources.yaml` schema：`id, type(rss|arxiv|hf_papers|youtube), url/query, trust(1–5), role(core|expansion), tags, max_age_days, enabled, backfill(默认 false), needs_scraper(默认 false)`
  - `role: expansion` 的信源只进入"意外"栏（serendipity）候选池
  - `backfill`：经典回补开关，M1 只解析字段，M5 实现（ADR 0004）
  - 无 RSS 的站点标注 `needs_scraper`，M1 记录为待支持，不阻塞
- [x] 种子信源清单（`config/sources.example.yaml`，URL 标 `# TODO verify` 的由 M1 实测确认）；trust 建议：个人一线博客与 infra 博客 5，实验室官方与论文 4，newsletter 3，expansion 3
  - core / 推理 infra：vLLM blog、LMSYS / SGLang blog、PyTorch blog、Horace He、Tri Dao、Hazy Research、Tim Dettmers、Chips and Cheese
  - core / 分布式系统：Marc Brooker、Murat Demirbas（metadata blog）、Aphyr / Jepsen、Martin Kleppmann
  - core / LLM 与研究：Karpathy（karpathy.github.io 与 karpathy.bearblog.dev）、Lilian Weng、Sebastian Raschka（Ahead of AI）、Chip Huyen、colah、Nathan Lambert（Interconnects）、HF Blog
  - core / harness：Anthropic Engineering blog、Simon Willison、Hamel Husain、Eugene Yan
  - core / 实验室官方：OpenAI、Anthropic、Google DeepMind、Meta AI、Qwen、DeepSeek（无 RSS 的标 `needs_scraper`）
  - core / 论文：HF Daily Papers、arXiv cs.CL / cs.LG / cs.DC（关键词：inference, serving, kv cache, speculative decoding, quantization, parallelism, attention, agent）
  - core / newsletter：Import AI、Latent Space
  - expansion / 工程哲学与理性思考：LessWrong（curated）、Alignment Forum、Gwern、Dan Luu、Hillel Wayne
  - 视频（M5 才启用，`enabled: false`）：Karpathy YouTube、Stanford CS25 / CS336 等课程、GPU MODE 讲座、Dwarkesh Podcast；expansion：音乐理论频道（如 Adam Neely）
- [x] `db.py`：建表 `sources, items`（`url_canonical` 唯一、`content_hash`、`published_at`、`fetched_at`、`title/author/summary/content`、`status`）；简单 schema 版本号迁移
- [x] `sources/*`：feedparser（新增依赖）解析 RSS/Atom；arXiv 官方 API（Atom）；HF Daily Papers 公开 API / 页面
- [x] `extract/html.py`：trafilatura（新增依赖）正文提取；`extract/pdf.py`：优先 arXiv HTML 版，退回 PDF 文本（pypdf）
- [x] 去重：URL 规范化 + content hash；httpx 超时、重试、礼貌 UA（通用客户端名，不含个人邮箱）
- [x] CLI：`sb sources list`、`sb fetch [--source ID] [--limit N]`、`sb items list [--since 2d] [--source ID]`

**模块**：`sources/`、`extract/`、`db.py`、`models.py`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_sources.py tests/test_extract.py tests/test_dedupe.py   # 基于 fixtures 离线
uv run sb sources list                          # 列出示例信源
uv run sb fetch --limit 20                      # 真实网络：新增 >0 条，逐源打印成功/失败
                                                # 每个启用信源 fetch 成功或给出明确失败原因
                                                # （无 RSS 的站点记录为待 M5/抓取支持，不阻塞）
uv run sb fetch --limit 20                      # 再跑一次：新增 0 条（去重生效）
uv run sb items list --since 7d | head
```

## M2 — LLM 后端 + 分层过滤流水线 + 分级

- [x] `llm/base.py`：`complete_json(messages, schema, tier="cheap"|"strong") -> (obj, usage)`
- [x] `llm/deepseek.py`：openai SDK、JSON 模式、重试、schema 校验失败重试一次
- [x] `llm/fake.py`：按 fixture / 规则返回确定性结果
- [x] `llm/usage.py`：`llm_calls` 表记录模型、tokens（含 cache hit）、按 config 价格估算 $
- [x] `profile.md`：首次运行从 `config/profile.example.md` 复制到 `data/profile.md`
- [x] L0 规则（零 token）：时间窗、include / exclude 关键词、最短长度、已处理跳过
- [x] 读取 profile 中的领域掌握度表（`陌生 / 接触过 / 想回炉 / 扎实`，ADR 0003）
- [x] L1 粗筛（cheap 模型，批量 ~10 条 / 调用，profile 放 system 前缀以吃缓存）：输出 `relevance, novelty, quality (0–10), content_type(frontier|explainer|survey|tutorial|opinion), refresher_value (0–10), topics[], one_line_reason`；"接触过 / 想回炉"领域的好 explainer / survey 加分，"扎实"领域只推 frontier；分数 × 信源 trust 取 top-N 进 L2
- [x] L2 精读（strong 模型）：全文截断（论文取 abstract + intro + conclusion，上限可配，默认 ~24k token），输出中文 `tldr, key_points[], why_for_you(两句), est_minutes, prerequisites[], scores`
- [x] `tiering.py`（纯代码）：以每日时间预算为主约束（`daily_budget_minutes`，默认 105，ADR 0004），条数配额为上限；lanes：`must_read / worth_it / refresher(温故) / serendipity / fyi / archived`；refresher 默认 1–2 条且不占 must_read 名额；serendipity 只从高质量低相关或 `role: expansion` 候选中选；允许为空
- [x] 深读标记：`est_minutes` > 预算 × `deep_read_ratio`（默认 0.6）的条目标"深读"，可顺延到预算有余的日子
- [x] 积压：未读 must_read 计入积压，超过 `backlog_max_days`（默认 3）天降级到 fyi
- [x] 表：`assessments`、`digests`
- [x] CLI：`sb run [--limit N] [--no-fetch]`、`sb digest show [--date]`、`sb usage [--since 7d]`
- [x] prompts 放 `llm/prompts/*.md`

**模块**：`llm/`、`pipeline/`、`db.py`、`cli.py`、`config/settings.example.yaml`

**验收**
```bash
uv run pytest -q tests/test_tiering.py tests/test_pipeline_fake.py tests/test_llm_deepseek_parse.py  # 离线
# test_tiering 至少覆盖：总 est_minutes ≤ 预算；想回炉领域的 explainer 进入 refresher 栏；
#                        超过预算 60% 的条目被标"深读"；积压超期降级到 fyi
SB_LLM_BACKEND=fake uv run sb run --limit 30 && uv run sb digest show           # 离线全链路
uv run sb run --limit 30          # 真实 DeepSeek（需 .env 中 DEEPSEEK_API_KEY）
uv run sb digest show             # 中文分级输出，每条带理由与 est_minutes；must_read ≤3；总耗时 ≤ 预算
uv run sb usage --since 1d        # 本次 token 与估算费用
```

## M3 — 本地 Web UI + 反馈采集 + 定时运行

- [x] `web/app.py`（FastAPI + Jinja2 + uvicorn，新增依赖；HTMX 固定版本 vendored 到 static）
- [x] `/` 今日 digest（按 lane 分组：TL;DR、理由、信源、原文链接、est_minutes、"深读"标记）
- [x] 页面显示今日预算进度条（已选条目总 est_minutes / `daily_budget_minutes`）与积压数量
- [x] 前置知识提示：L2 `prerequisites[]` 命中用户不扎实的领域 / 概念时显示"建议先回炉：[[概念]]"
- [x] `/item/{id}` 详情（key points、L1 / L2 评分、prerequisites、原文）
- [x] `/history` 按日期浏览
- [x] HTMX 反馈按钮：👍 / 👎 / 已读 / 稍后读 / 学了 / 需要回炉 / 已扎实 → `POST /feedback`，写 `feedback` 表（item_id, kind, ts, note）
- [x] `sb serve [--port 8000]`
- [x] `sb schedule install`（macOS launchd plist，每日跑 `sb run`，默认 DeepSeek 非高峰时段）/ `sb schedule uninstall`；文档说明 cron 替代方案

**模块**：`web/`、`db.py`（feedback 表）、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_web.py        # TestClient：首页 200、含条目；POST /feedback 写入成功
uv run sb serve --port 8000 &
curl -s localhost:8000/ | grep -c 'data-item-id'     # > 0
curl -s localhost:8000/ | grep -c 'data-budget'      # > 0（预算进度条存在）
curl -s -X POST localhost:8000/feedback -d 'item_id=1&kind=up' -o /dev/null -w '%{http_code}'   # 200
uv run sb schedule install --dry-run      # 打印将写入的 plist
```
（另用浏览器实际点按钮验证一遍。）

## M4 — 个性化与记忆

- [x] profile 结构化：Markdown 掌握度表格（领域 | 掌握度 | 优先级 | 说明）由代码解析，格式错误时给出明确提示
- [x] `profile/feedback.py`：汇总近期反馈（喜欢 / 不喜欢 / 学了 / 需要回炉 / 已扎实的标题、标签、信源）
- [x] L1 prompt 注入：profile.md + 最近 N 条正 / 负反馈样例（few-shot）
- [x] `profile/rewrite.py`：`sb profile update` 用 strong 模型提出 profile.md 修改（含依据"需要回炉 / 已扎实"等反馈调整掌握度的建议），输出 diff，`--apply` 才写入，旧版本存 `data/profile_history/`
- [x] 信源信任建议：按 👍 / 👎 比例建议 trust 调整（`sb sources tune --apply` 才改）
- [x] Web：`/profile` 查看 / 编辑 profile.md，查看并应用 LLM 修改提案

**模块**：`profile/`、`pipeline/l1_screen.py`、`web/`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_profile.py    # 掌握度表解析；fake 反馈 → 生成 diff（含掌握度调整）；L1 prompt 含反馈样例
uv run sb profile update --dry-run        # 打印 profile diff，不写文件
uv run sb profile update --apply && ls data/profile_history/
uv run sb sources tune                    # 打印信任度调整建议
```

## M5 — 视频讲座与长内容

- [x] 新信源类型 `youtube`（频道 RSS：`https://www.youtube.com/feeds/videos.xml?channel_id=...`）
- [x] `extract/transcript.py`：优先 youtube-transcript-api，退回 yt-dlp 字幕（新增依赖，说明理由）；保留时间戳
- [x] 长内容 L2 分块 map-reduce 总结（块大小可配），视频 key points 带时间戳链接
- [x] UI：未进入 L2 的条目提供"按需精读"按钮（触发单条 L2）
- [x] 经典回补：`backfill: true` 的信源抓取历史归档中的经典文章，进入"温故 / 补课"池，按每日限额慢速释放（ADR 0004）

**模块**：`sources/youtube.py`、`extract/transcript.py`、`pipeline/l2_deep.py`、`web/`

**验收**
```bash
uv run pytest -q tests/test_youtube.py tests/test_longform.py   # fixture 字幕 + fake LLM
uv run sb fetch --source <示例 youtube 源 id> --limit 2         # 真实拉取并有 transcript
uv run sb deep <item_id>                                        # 单条精读，输出带时间戳要点
uv run pytest -q tests/test_backfill.py                         # fixture 归档：回补条目按每日限额进入补课池
```

## M6 — 知识 Wiki（LLM-Wiki 闭环）

- [x] `data/wiki/`：`index.md`（目录 + 一行摘要）、`log.md`（只追加，`## [date] ingest | Title`）、`pages/{concepts,entities,papers}/*.md`，`[[wikilink]]`，YAML frontmatter；原文存 `data/raw/` 并链接回去
- [x] `wiki/ingest.py`：`sb wiki ingest <item_id>` / `--pending`；LLM 读条目 + index.md + 相关页面，输出结构化"页面新建 / 更新"操作，由代码落盘（LLM 不直接写文件）
- [x] `wiki/lint.py`：`sb wiki lint`：孤立页、断链、缺失概念页、可能矛盾（LLM 辅助）
- [x] 闭环：L1 prompt 注入 index.md 相关条目及其 mastery 作为"已知知识"→ 扎实的概念降低入门讲解的 novelty，不扎实的概念提高讲解类内容的 refresher_value，与已知概念相关的新进展加分（ADR 0003）
- [x] 复习调度：概念页 frontmatter 记录 `mastery`、`last_reviewed`、`interval`（SM-2 简化版）；`sb wiki due` 列出到期概念；到期概念进入温故栏，若近期采集到讲该概念的好文则一起推
- [x] 复习按钮："还记得 / 模糊 / 忘了"调整 interval 与 mastery
- [x] L2 `prerequisites[]` 与 wiki 概念页匹配，用于前置知识提示
- [x] Web：`/wiki` 浏览页面与 index（关系图未做，见 Backlog "wiki 图谱可视化增强"）

**模块**：`wiki/`、`pipeline/l1_screen.py`、`web/`、`cli.py`

**验收**
```bash
uv run pytest -q tests/test_wiki.py       # fake LLM：ingest 生成页面 + 更新 index/log；lint 能发现断链；复习按钮正确调整 interval
uv run sb wiki due                        # 列出到期复习的概念
uv run sb wiki ingest --pending           # 真实：把"学了"的条目编译进 wiki
cat data/wiki/index.md && tail -5 data/wiki/log.md
uv run sb wiki lint
```

## M7 — 反馈语义与 Web 运维（ADR 0013）

- [x] 文章反馈改为两组：读之前（稍后读 / 不感兴趣）、读完后（太浅 / 有收获 / 太难 / 没价值）；当前状态 = 最新一次点击，再点高亮按钮 = 撤销（记录 `clear`）；按钮高亮当前状态并有 title 说明
- [x] 稍后读进入 `/later`（首页显示"稍后再看：N 条"），不计入积压、不降级；不感兴趣移出积压；读完后四种设置 `read_at`
- [x] L1：正例有收获、反例不感兴趣 / 没价值、校准样例太浅 / 太难；近期"太难"条目的前置知识注入 L1 user 消息（`personalize.too_hard_prereq_max_chars`）
- [x] profile 证据规则：升级需要太浅；有收获只能用于 陌生 → 接触过；降级需要太难；每次最多一级
- [x] sources tune：有收获为正，不感兴趣 / 没价值为负
- [x] wiki：`--pending` = 当前状态为有收获；新概念页从"接触过"起步、首次复习间隔 `wiki.first_review_days`；复习新增"需要回炉 / 已扎实"（`wiki.mastered_interval_days`）
- [x] 后台任务（线程注册表 + HTMX 2 秒轮询）；`sb run` 与 Web"立即运行"共用 `data/run.lock`（fcntl.flock）
- [x] 首页"待编进 wiki：N 条" + 后台 ingest；`/wiki` 体检按钮、到期复习、图谱入口
- [x] `/ops`：今天 / 近 7 天用量，立即运行（高峰提示与费用倍率），信源 trust 建议与应用，画像入口
- [x] `/sources`：列表（最近抓取结果、入库数、正 / 负反馈数；schema v6），试抓预览后添加，启用 / 停用，删除二次确认；写入 `config/sources.yaml`（原子写，注释不保留）
- [x] `/wiki/graph` + `/wiki/graph.json`（Cytoscape.js 3.34.4 vendored）：颜色 = 掌握度，到期高亮，大小 ~ 度数，类型 / 掌握度筛选
- [x] 原文目录移到 `data/wiki/raw`，页面链接 `../../raw/<id>.md`；lint / index / 图谱忽略 raw/；README 增加 Obsidian 说明

**模块**：`db.py`、`profile/`、`wiki/`（`graph.py`）、`ops.py`、`web/`（`tasks.py`）、`cli.py`

**验收**（临时目录，fake LLM，端口 8765，不碰默认 data/）
```bash
uv run pytest -q tests/test_feedback.py tests/test_ops.py tests/test_sources_web.py tests/test_graph.py
export SB_DATA_DIR=$(mktemp -d) SB_LLM_BACKEND=fake
uv run sb fetch --source lmsys-sglang-blog --limit 5 && uv run sb run --no-fetch
uv run sb serve --port 8765 &
sleep 3
curl -s 127.0.0.1:8765/ | grep -c '读完后'                       # 首页每条都有两组反馈按钮
ITEM=$(sqlite3 "$SB_DATA_DIR/brain.db" "SELECT item_id FROM digests LIMIT 1")
curl -s -X POST 127.0.0.1:8765/feedback -d item_id=$ITEM -d kind=gained | grep -o '已记录：有收获'
sqlite3 "$SB_DATA_DIR/brain.db" "SELECT read_at FROM items WHERE id = $ITEM"   # 已设置
curl -s 127.0.0.1:8765/ | grep -o '稍后再看：[0-9]* 条'
curl -s 127.0.0.1:8765/later | grep -o '<h1>稍后再看'
curl -s 127.0.0.1:8765/ops | grep -o '立即运行' | head -1
curl -s 127.0.0.1:8765/sources | grep -o 'data-source="lmsys-sglang-blog"'
curl -s -X POST 127.0.0.1:8765/tasks/wiki_ingest > /dev/null; sleep 3
curl -s 127.0.0.1:8765/tasks/wiki_ingest | grep -o 'data-task-status="done"'
curl -s 127.0.0.1:8765/wiki/graph.json | python3 -c "import json, sys; g = json.load(sys.stdin); print(len(g['nodes']), 'nodes', len(g['edges']), 'edges')"
uv run python -c "import time; from secondbrain import ops; lock = ops.run_lock(); lock.__enter__(); time.sleep(15)" &
sleep 3; uv run sb run --no-fetch; echo "exit=$?"                # 锁冲突：被拒绝，exit=1
kill %1 %2 2> /dev/null                                           # 停掉 8765 的服务与持锁进程
```

## M8 — 深度速读与前置知识（ADR 0015）

- [x] 深读条目按速读时间计入预算：`deep_summary_minutes`（默认 15）；`digests.budget_minutes`（schema v7，旧行取 est_minutes）；"今日已排"、同日多次 run、`sb digest show` 都按占用预算算；卡片显示"速读约 15 分钟 · 全文约 90 分钟"；速读时间也放不下才顺延
- [x] 深度速读：读全文（上限 `summary.max_total_chars`），长内容 cheap 分块提要（SUMMARY_MAP）→ strong 汇总（SUMMARY）；输出问题与动机、核心方法、关键结果、局限、与已有知识的关系、值得精读的章节、前置知识；视频保留时间戳；结果缓存在 `item_ai`，重新生成需确认
- [x] 入口：`sb summary <id> [--force]`；item 详情页与深读卡片上的"深度速读"按钮（后台任务 + 轮询），按钮旁显示耗时与费用区间
- [x] 补前置：有速读时只用速读的前置知识，否则用 L2 的（名字轻量归一后去重）；wiki 已有 → 链接 + 掌握度；没有 → 一次 strong 调用批量生成简短讲解（PREREQ，缓存）；列出库中最多 3 条 explainer / survey / tutorial；"存为概念页"不调用 LLM（mastery 陌生，首次复习间隔 `wiki.first_review_days`）；`sb prereq <id> [--force]`
- [x] 按钮旁的费用提示改为按当前模型价格估算（非高峰 – 高峰区间）

**模块**：`pipeline/summary.py`、`pipeline/tiering.py`、`ops.py`、`db.py`、`web/`、`cli.py`

**验收**（临时目录，fake LLM，端口 8765，不碰默认 data/）
```bash
uv run pytest -q tests/test_summary.py tests/test_budget.py tests/test_tiering.py
export SB_DATA_DIR=$(mktemp -d) SB_LLM_BACKEND=fake
uv run sb fetch --source lmsys-sglang-blog --limit 5 && uv run sb run --no-fetch
# 预算：3 篇 90 分钟的深读都放进必读，各占 15 分钟
uv run python -c "from datetime import date; from secondbrain.models import FilterSettings; from secondbrain.pipeline.tiering import Candidate, tier; print([(p.lane, p.est_minutes, p.budget) for p in tier([Candidate(i, 9, 8, 8, 7, 2, 'frontier', 'core', 90, True) for i in (1, 2, 3)], FilterSettings(), date.today())])"
sqlite3 "$SB_DATA_DIR/brain.db" "SELECT SUM(budget_minutes) FROM digests WHERE lane IN ('must_read', 'worth_it', 'refresher', 'serendipity')"   # 与 sb run 输出的"今日已排"一致
ITEM=$(sqlite3 "$SB_DATA_DIR/brain.db" "SELECT item_id FROM digests WHERE lane = 'must_read' LIMIT 1")
uv run sb summary $ITEM | head -12
uv run sb summary $ITEM | tail -1                                  # 第二次读缓存
uv run sb prereq $ITEM
sqlite3 "$SB_DATA_DIR/brain.db" "SELECT stage, COUNT(*) FROM llm_calls WHERE stage IN ('SUMMARY', 'SUMMARY_MAP', 'PREREQ') GROUP BY stage"
uv run sb serve --port 8765 &
sleep 3
curl -s 127.0.0.1:8765/item/$ITEM | grep -o '<h2>深度速读</h2>\|<h2>补前置</h2>\|（非高峰）' | sort | uniq -c
curl -s -X POST 127.0.0.1:8765/summary/$ITEM > /dev/null; sleep 2
curl -s 127.0.0.1:8765/tasks/summary-$ITEM | grep -o 'data-task-status="done"'
curl -s -X POST 127.0.0.1:8765/prereq/$ITEM | grep -o 'AI 生成\|wiki 已有' | sort | uniq -c
NAME=$(sqlite3 "$SB_DATA_DIR/brain.db" "SELECT key FROM item_ai, json_each(item_ai.data, '$.explanations') WHERE item_id = $ITEM AND kind = 'prereq' LIMIT 1")
curl -s -X POST 127.0.0.1:8765/prereq/$ITEM/save --data-urlencode "name=$NAME" | grep -o '已存为概念页'
curl -s 127.0.0.1:8765/ops | grep -o 'SUMMARY' | head -1
kill %1
```

## M9 — 手动运行与信源产出率（ADR 0016）

- [x] 运行方式开关：定时任务装了 = 每天 HH:MM 自动，没装 = 手动；`/ops` 显示模式与 LaunchAgent 是否已加载，"改为手动 / 开启每日自动"按钮需确认并提示高峰；有运行在进行时拒绝改为手动；`SB_LAUNCH_AGENTS_DIR` 可覆盖目录（测试用临时目录）
- [x] 首页"开始今日过滤"：今天还没有 digest 时显示按钮与耗时 / 费用说明；后台运行并轮询，完成后整页刷新；锁被占用（含定时任务）时显示"正在运行"；今天已运行过时只留"再运行一次"入口
- [x] 不限时间窗：`sb run --all-time`、首页与 `/ops` 勾选项；collect 不按 max_age_days 截断（仍每源最新 fetch_limit 条再去重），L0 对新条目 ignore_age；预算、lane、`run.max_cost_usd` 不变
- [x] 速览与归档默认折叠为 `<details>`（"速览（N 条）"），折叠区域内反馈按钮可用
- [x] 信源产出率：`/sources` 显示近 30 天入库 / 精读 / 前排 / 正 / 负；入库 ≥ 5 但前排为 0（且历史覆盖整个窗口）标记"低产出，建议停用"并给停用按钮；`sb sources tune` 只读列出

**模块**：`ingest.py`、`pipeline/run.py`、`ops.py`、`schedule.py`、`profile/tune.py`、`db.py`、`web/`、`cli.py`

**验收**（临时目录，fake LLM，端口 8765，临时 LaunchAgents 目录，不碰默认 data/ 与真实 launchctl）
```bash
uv run pytest -q tests/test_manual_run.py
export SB_DATA_DIR=$(mktemp -d) SB_LLM_BACKEND=fake SB_LAUNCH_AGENTS_DIR=$(mktemp -d)
uv run sb fetch --source lmsys-sglang-blog --limit 5
uv run sb serve --port 8765 &
sleep 3
curl -s 127.0.0.1:8765/ | grep -o 'data-run-state="[a-z]*"\|开始今日过滤\|不限时间窗（只去重）' | sort | uniq -c
uv run python -c "import time; from secondbrain import ops; lock = ops.run_lock(); lock.__enter__(); time.sleep(12)" &
sleep 3
curl -s 127.0.0.1:8765/ | grep -o 'data-run-state="[a-z]*"'                 # running
curl -s -X POST 127.0.0.1:8765/tasks/run | grep -o '已有 sb run 在运行'
uv run sb run --no-fetch; echo "exit=$?"                                      # 锁冲突：exit=1
wait %2
# 不限时间窗：把 1 条改成 2024 年发布，先按时间窗跑，再重置为 new 用 --all-time 跑
sqlite3 "$SB_DATA_DIR/brain.db" "UPDATE items SET published_at = '2024-01-01T00:00:00Z' WHERE id = 1"
uv run sb run --no-fetch | grep 'L0：'
sqlite3 "$SB_DATA_DIR/brain.db" "UPDATE items SET status = 'new' WHERE id = 1"
uv run sb run --no-fetch --all-time | grep '不限时间窗\|L0：\|L1：'
curl -s 127.0.0.1:8765/ | grep -o 'data-run-state="[a-z]*"\|再运行一次\|<summary><h2>[^<]*</h2></summary>'
curl -s 127.0.0.1:8765/ops | grep -o 'data-mode="[a-z]*"\|当前：<b>[^<]*</b>\|name="all_time"'
curl -s 127.0.0.1:8765/sources | grep -o 'data-source="lmsys-sglang-blog"\|近 30 天产出\|data-yield="[0-9/]*"' | head -4
uv run sb sources tune | grep '低产出'
kill %1
```

## Backlog（不在本轮）

- [ ] X / Twitter 信源
- [ ] IM 推送（Telegram / 邮件）
- [ ] embedding 语义去重 + 同一事件多信源聚合（sqlite-vec）
- [ ] Anthropic / `claude -p` 后端
- [ ] wiki 图谱可视化增强（M7 已有基础图谱 `/wiki/graph`；更多布局、聚类等仍待）
- [ ] `needs_scraper` 信源的页面抓取支持（LMSYS、Hazy Research、Anthropic engineering / news、Meta AI、DeepSeek）
  - [x] sitemap 部分（`type: sitemap`，ADR 0012）：LMSYS、Anthropic news、Anthropic engineering（改为 `anthropic-research`，经 `/research/` 接入，混入研究文章由 L1 过滤）已接入
  - [ ] 其余仍待：Hazy Research、Meta AI（均无 sitemap）、DeepSeek
