# 0010 — 知识 wiki 与概念复习（M6）

- 状态：已接受
- 日期：2026-11-04

## 背景

roadmap 的 M6 要求把"学了"的条目编译进 LLM-Wiki 式知识库，并让 wiki 反过来影响过滤（ADR 0003：已知知识、到期复习、前置知识）。需要决定：文件布局、LLM 如何安全地修改文件、复习调度规则、概念如何进入 digest。

## 决策

### 布局（Obsidian 兼容）

- `data/wiki/index.md`：按 Concepts / Entities / Papers 列出 `- [[slug]] — 一行摘要`，**每次由代码根据页面 frontmatter 重建**（不让 LLM 直接改 index）。
- `data/wiki/log.md`：只追加，`## [YYYY-MM-DD] ingest | Title` 或 `review | Title`，下面列出本次改动。
- `data/wiki/pages/{concepts,entities,papers}/<slug>.md`：YAML frontmatter（`title, type, summary, aliases, sources, mastery, last_reviewed, interval_days, ease, created, updated`）+ Markdown 正文，`[[slug]]` 互链，"来源"小节链接到 `data/raw/<item_id>.md`（相对路径）与原文 URL。
- 首次使用任何 `sb wiki` 命令（或 `sb run` / Web）时自动初始化目录、空 index 与 log。

### ingest：LLM 只出操作，代码落盘

- 输入：条目的 L2 结果（TL;DR、要点、前置知识）+ 正文（短于 `wiki.ingest_max_chars` 才整篇给，长内容只给 L2 要点以控制成本）+ index.md（限长）+ 名字出现在条目中的现有页面（最多 `wiki.related_pages` 个，各自截断）。
- 输出 `WikiPlan`：`creates`（新页面）与 `updates`，后者只能是 `append_section` / `replace_section` / `add_links` / `add_aliases`。
- 代码校验：slug 统一规范化为 `[a-z0-9-]`（无法规范化的拒绝）；页面路径必须解析到 `data/wiki` 内；页面类型只能是 concept / entity / paper；create 已存在的页面时改为追加小节（避免重复页）；update 不存在的页面被拒绝；正文中的 `[[Title]]` 统一改写为 `[[slug]]`。
- 新建概念页的 mastery：概念名（标题 / slug / 别名）出现在 profile 某领域的名称或说明里时取该领域的掌握度，否则为"接触过"。
- `wiki_ingests` 表记录已编入的条目；`--pending` = 有"学了"反馈且未编入的条目，重复运行幂等；显式重编需 `--force`。
- 费用：按字符数预估（输出按 8000 token），超过 `run.max_cost_usd` 停止；用量记为 stage = WIKI。

### lint

只报告，不修改：断链 / 缺失页面、孤立页（没有其他页面链接）、frontmatter 缺字段、index 与页面不一致；`--llm` 时用 cheap 模型找可能矛盾的说法。

### 复习（SM-2 简化版）

- 新概念页：`interval_days = 1`、`ease = 2.5`、`last_reviewed = 创建日`，即第二天到期；到期日 = `last_reviewed + interval_days`。只有 concept 参与复习。
- 还记得：间隔 = max(round(间隔 × ease), 2)，ease + 0.1；陌生 → 接触过；间隔 ≥ 21 天时 想回炉 → 扎实。
- 模糊：间隔 = max(round(间隔 × 1.2), 1)，ease − 0.15；扎实 → 想回炉。
- 忘了：间隔 = 1，ease − 0.2；掌握度降一级（陌生不再降）。
- ease 限制在 [1.3, 3.0]。每次复习写回 frontmatter 并追加 log。

### 闭环

- **到期概念进温故栏**：每次 run 取最早到期的 `wiki.review_slots`（默认 1）个概念，作为占位候选（只进 refresher，est = `wiki.review_minutes`，默认 5 分钟，优先于普通温故条目）参与分级，仍受时间预算与 `lane_budget_reserve` 约束。放上的写入 `digest_concepts` 表（概念不是 items，不进 digests 表），并附上近 `wiki.recent_days` 天内讲到该概念的一条条目。
- **L1 "已知知识"**：每批候选在 user 消息开头附上名字出现在本批中的 wiki 页面及其掌握度（限 `wiki.known_max_chars`）。system 前缀（profile + 规则 + 反馈样例）保持不变，不破坏缓存；规则写在 L1 system prompt 中（扎实的入门内容降 novelty，不扎实的讲解提高 refresher_value，相关新进展加分）。
- **前置知识 ↔ wiki**：L2 的 prerequisites 若匹配到掌握度不是"扎实"的概念页，"建议先回炉"直接链接到该 wiki 页面。

### Web

`/wiki`（index + 到期列表 + 复习按钮）、`/wiki/page/<slug>`、`POST /wiki/review`。Markdown 用 `markdown` 库渲染（新增依赖：成熟、纯 Python，支持表格与代码块）；渲染前先把 `<` 转义，不渲染任何原始 HTML。

## 后果

- 正面：LLM 无法写出 wiki 目录以外的文件，也无法整页改写；index 永远与页面一致；wiki 是普通 Markdown，可直接用 Obsidian 打开与手改。
- 负面：受限操作的表达力有限（比如不能删除段落，需要手动编辑）；名字匹配是简单的子串匹配，可能漏配或误配；概念复习的到期规则较粗（以天为单位，无每日上限之外的平滑）。
