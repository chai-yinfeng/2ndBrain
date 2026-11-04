# Progress

每个 milestone 完成时追加一条：做了什么、验收结果、遗留问题。

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
