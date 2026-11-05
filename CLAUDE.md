# CLAUDE.md（项目级）

## 项目简介

Second Brain：个人"知识过滤 + 分级推荐 + 知识 wiki"系统。信源以博客 / 论文为主（M5 加视频，X 暂不做），LLM 分层过滤（L0 规则 → L1 粗筛 → L2 精读）后分级推送到本地 Web UI，反馈驱动个性化，"学了"的内容编译进 Markdown wiki 并回馈过滤层。架构见 `docs/design.md`，计划见 `docs/roadmap.md`。

## 技术栈

- Python 3.12 + uv（src 布局，包名 `secondbrain`），CLI 用 Typer，入口命令 `sb`
- SQLite（`data/brain.db`）；wiki 为 Markdown（Obsidian 兼容）
- Web：FastAPI + Jinja2 + HTMX（M3 起）
- LLM：统一后端接口，默认 DeepSeek（OpenAI 兼容），`fake` 后端用于离线测试
- 质量：ruff（line-length 100）、pytest
- 输出语言：中文，术语和原文标题保留英文

## 常用命令

```bash
uv sync                  # 安装 / 同步依赖（含 dev 组）
uv run sb --help
uv run sb doctor         # 环境与配置自检
uv run pytest -q         # 默认跳过 live 测试
uv run pytest -q -m live # 只跑需要网络 / 真实 LLM 的测试
uv run ruff check .
uv run ruff format .
```

## 目录约定

```
config/              *.example.* 提交到仓库；同名去掉 .example 的正式文件（sources.yaml、settings.yaml）为个人配置，已在 .gitignore 中，不提交
docs/                design.md  roadmap.md  progress.md  decisions/NNNN-*.md（ADR）
src/secondbrain/     代码（按 milestone 逐步增加模块，不预建空模块）
tests/               test_*.py，fixtures/ 放离线数据
data/                运行时数据（gitignore），可用 SB_DATA_DIR 覆盖
.env                 密钥（gitignore）；模板见 .env.example
```

配置读取规则：`config/settings.yaml` 存在时深合并覆盖在 `config/settings.example.yaml` 之上（dict 递归，list 与标量直接替换），不存在时只用 example；环境变量 `SB_LLM_BACKEND` 覆盖 settings 中的 `llm.backend`；`.env` 中的值不覆盖已存在的环境变量。`SB_CONFIG_DIR` / `SB_ENV_FILE` 可覆盖配置目录与 .env 路径（验收与测试用）。

## 开发流程

- 每个 milestone 一个分支 `mN-<name>`（如 `m1-ingest`）→ 实现并跑通该 milestone 的验收命令 → 开 PR → 审阅验收通过后 merge 到 main。
- 每个 milestone 结束：更新 `docs/progress.md`（做了什么、验收结果、遗留问题），在 `docs/roadmap.md` 勾选完成项，必要时同步 `docs/design.md`。

## 规则

- 绝不提交或打印密钥；`sb doctor` 等只报告"已设置 / 未设置"。
- 测试默认离线（fake LLM + fixtures）；需要网络或真实 LLM 的测试标记 `@pytest.mark.live`，默认跳过。
- 任何偏离 `docs/roadmap.md` 计划的取舍，写一条 ADR 到 `docs/decisions/`。
- 新增依赖需说明理由（写在 PR 描述或 ADR 中）。
