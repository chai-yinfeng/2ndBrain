# 0001 — 从头实现：Python 3.12 + SQLite + FastAPI/HTMX

- 状态：已接受
- 日期：2026-11-04

## 背景

需求是一个高度个人化的"知识过滤 + 分级推荐 + 知识 wiki"闭环系统。调研过的开源项目各只覆盖一部分：Horizon 类项目只有静态 profile 打分，LLM-Wiki 类项目只做笔记不做过滤，都没有"反馈驱动个性化 + wiki 回馈过滤"的闭环。个性化改动多，fork 后大部分代码都要改。

使用者只有一个人、本地运行，数据量小（每天数百条候选）。

## 决策

- 不 fork 现有项目，从头写；借鉴其原则（确定性预过滤、LLM 只做判断、推荐必带理由、允许空、意外名额）。
- 语言与工具：Python 3.12 + uv（src 布局），ruff，pytest；CLI 用 Typer（子命令多，省样板）。
- 存储：SQLite 单文件 `data/brain.db`；wiki 为 Markdown 文件（Obsidian 兼容）。
- Web：FastAPI + Jinja2 + HTMX（HTMX 固定版本 vendored 到 static），不引入前端构建链。

## 后果

- 正面：完全掌控数据模型与流水线；部署只需 `uv sync`；数据可直接用 sqlite3 / Obsidian 查看；无 Node 构建。
- 负面：采集、提取、去重等基础设施要自己写和维护；SQLite 不适合多人并发（本项目不需要）；HTMX 交互能力有限，复杂可视化（如 wiki 图谱）需要另外引入小型 JS 库。
