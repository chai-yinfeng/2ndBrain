# 0015 — 深度速读、速读计入预算与补前置（M8）

- 状态：已接受
- 日期：2026-11-04
- 修订：ADR 0004（每日时间预算中深读条目的计算方式）

## 背景

2026-11-04 的默认数据中，7 条顺延的深读全是 arXiv / HF 论文，L2 估时 65–110 分钟。深读阈值为 `daily_budget_minutes` 105 × `deep_read_ratio` 0.6 = 63 分钟。

如果按全文精读计算预算，每天最多放下 1 篇论文，其余的不断顺延，3 天后降为 fyi。用户的实际做法是先看一份"相对深入的总结"，再决定是否精读全文。因此希望：

- 由 AI 按需生成这样的总结，手动触发、按需花钱；
- 分级时深读条目按"速读时间"占用预算。

## 决策

### 1. 深读条目按速读时间计入预算

- 新配置 `deep_summary_minutes`，默认 15。
- est_minutes > 深读阈值的条目，分级时按 `min(est_minutes, deep_summary_minutes)` 占用预算。全文时间仍然保留并显示，例如卡片上写"速读约 15 分钟 · 全文约 90 分钟"。
- `digests` 表新增 `budget_minutes`，记录占用的预算分钟数（schema v7；旧行取 est_minutes）。以下几处都按它计算：
  - 首页"今日已排 X / 105"；
  - `sb digest show` 的总耗时；
  - `RunReport.minutes_used`；
  - 同一天多次 run 共享的已用预算（`db.day_usage`）。
- 顺延逻辑不变：只有速读时间也放不进剩余预算时才标记 deferred。lane_budget_reserve 和各 lane 的条数上限不变。

### 2. 深度速读（按需）与 L2 的区别

| | L2 精读（自动 / `sb deep`） | 深度速读（按需 `sb summary` / Web） |
|---|---|---|
| 目的 | 分级：TL;DR、要点、估时、评分、前置知识 | 让用户约 15 分钟掌握全文 |
| 输入 | 论文只读 abstract + introduction + conclusion；其余截断到 `l2.max_input_chars` | **全文**（上限 `summary.max_total_chars`，默认 40 万字符） |
| 长内容 | 超过 `l2.mapreduce_threshold_chars` 时分块 | 超过 `summary.single_max_chars`（6 万）时分块：cheap 逐块提要（SUMMARY_MAP）→ strong 汇总（SUMMARY） |
| 输出 | `L2Response` | `DeepSummary`：问题与动机、核心方法（算法步骤 / 公式）、关键结果与数字、局限与争议、与已有知识的关系、值得精读的章节（名称 + 理由）、前置知识（名称 + 为什么需要） |
| 存储 | assessments（stage L2） | item_ai（kind = summary），按条目缓存 |
| 是否影响分级 | 是 | 否 |

- "与已有知识的关系"的输入：system prompt 中的 profile，加上 user 消息开头的"已知知识"。后者与 L1 的取法相同（`wiki.loop.known_knowledge`）。
- 视频保留时间戳：逐段提要带时间戳，方法 / 结果的每条以时间戳开头，页面上生成跳转链接。
- 已有结果时直接展示。重新生成需要确认：Web 用 hx-confirm，CLI 用 `--force`。
- 预估按高峰全价计算，当前若不是高峰时段再乘以倍率，然后与 `run.max_cost_usd` 比较，超过就拒绝。
- Web 中以后台任务运行：同一条目同时只跑一个，HTMX 轮询状态。

### 3. 补前置的数据流

1. 前置知识的来源：已有深度速读时只用速读的 `prerequisites`（更具体、带"为什么需要"）；没有速读时用 L2 的 `prerequisites`。
   - 起初的做法是把两者合并。真实 flash 运行中，合并后 10 条里约一半是近重复（如 induction heads / induction head），多花了 token，所以改为只取一边。
   - 名字去重时做轻量归一：小写、去掉首尾空白、去掉末尾复数 s。
2. 每个知识点用 `wiki.find` 按 slug / 标题 / 别名查找：
   - **找到**：给出链接和掌握度；
   - **找不到**：放入待生成列表。
3. 对待生成列表做**一次** strong 调用（stage PREREQ），批量生成简短讲解：每个约 300–500 字，讲到"读懂这篇需要的程度"，包括直觉和关键公式，页面上标注"AI 生成"。结果缓存在 item_ai（kind = prereq）：
   - 已缓存的知识点不再调用；
   - `--force` 时重新生成全部。
4. 同时在已入库的条目中查找讲这个知识点的文章：L1 的 content_type 属于 explainer / survey / tutorial，且标题包含该名字或 L1 topics 匹配；最多列 `summary.explainers_per_prereq`（3）条。
5. Web 中补前置是同步请求（只有一次调用）。

### 4. "存为概念页"的规则

- 不调用 LLM，直接用缓存的讲解创建 concept 页：
  - frontmatter 字段齐全：mastery = 陌生，sources = [该条目]，interval_days = `wiki.first_review_days`；
  - 正文开头注明"AI 生成、未经人工核对"，"来源"段链接到原文。
- 创建后重建 index，并在 log 中追加 `prereq` 记录。
- 已有同名页面（按 slug / 标题 / 别名匹配）时不新建，改为返回链接。
- 名字无法转成 slug 时（如中文）用 `concept-<sha1 前 8 位>`，标题保留原名，之后 `wiki.find` 仍能按标题找到。

### 5. 费用提示

按钮旁的费用由当前配置的模型与价格表估算，显示为"非高峰 – 高峰"区间，替换了原先按 v4-pro 写的"约几美分"。涉及的按钮：精读、深度速读、补前置、编进 wiki、profile 提案。

## 后果

- 正面：
  - 长论文不再因为全文时间放不进预算而反复顺延（模拟结果见 progress.md）；
  - 深度速读与补前置按需付费、结果缓存；
  - AI 讲解可以一键沉淀为概念页，进入复习。
- 负面：
  - 今天的 digest 可能放进更多深读条目，用户需要自觉"先速读、再决定是否精读"；
  - 旧 digest 行的 budget_minutes 取 est_minutes，所以升级当天先前 run 已占用的预算按全文计（第二天起生效）；
  - AI 讲解未经核对，可能有误；
  - 深度速读是全文输入，费用高于 L2，但都按需触发。
