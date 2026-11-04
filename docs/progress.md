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
