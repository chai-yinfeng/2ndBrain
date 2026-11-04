# 0006 — 过滤与分级流水线的实现细节（M2）

- 状态：已接受
- 日期：2026-11-04

## 背景

roadmap 的 M2 与 ADR 0003 / 0004 规定了 L1 / L2 的输出字段、lanes、时间预算、深读、积压，但没有写明几个衔接点：哪些条目能进前排 lane、意外 / 温故候选如何保证进入 L2、深读顺延如何存储、积压如何降级、`--limit` 选哪些条目、真实调用的费用如何兜底。

## 决策

- **只有经过 L2 的条目能进前排 lane**（must_read / worth_it / refresher / serendipity）。未进 L2 的只进 fyi / archived，因为前排需要 TL;DR、理由和 est_minutes。
- **L2 预留名额**：在 top_n 内先给意外栏、温故栏候选各预留 `lanes.serendipity`、`lanes.refresher` 条，否则低相关的意外候选在 L1 排序中几乎进不了 L2，意外栏会长期为空。
- **温故条目不进必读**：content_type 为 explainer / survey / tutorial、refresher_value ≥ 阈值且不低于 novelty 的条目只进温故栏（或值得读），不占必读名额。
- **lane 预算顺序**：must_read → refresher → serendipity → worth_it。温故与意外名额少，放在值得读之前，避免被大量值得读条目挤占预算。
- **顺延用 digests 中的 `deferred` 伪 lane 存储**，`items.status = deferred`；`deferred_from` 记录首次顺延日期。顺延超过 `backlog_max_days` 天的不再顺延，进 fyi，避免 est_minutes 超过整天预算的条目被无限顺延。
- **积压降级直接改写原 digest 行**：超期的 must_read 行的 lane 改为 fyi（"降级到 fyi"）。M2 没有反馈，所有必读都视为未读；M3 引入"已读"后排除已读条目。
- **`--limit` 的候选选择**：按信源轮询（trust 降序、信源内由新到旧），避免 arXiv 等高产信源占满名额。
- **assessments 以 JSON 存储各阶段输出**（`data` 列），外加 `score` 列，不为每个字段建列。schema 随 prompt 迭代时无需迁移。
- **费用兜底**：L2 前按字符数（每 3 字符 1 token，偏保守；每条输出按 1500 token）预估，若加上已花费超过 `run.max_cost_usd`（默认 $0.5）就停止，不调用 L2、不分级，条目保留 `l1` 状态，下次继续。
- **并发**：L1 批次与 L2 条目用线程池并发（`llm.concurrency`，默认 4）；只有主线程写库。
- **日期**：digest 按本地日期归档（用户感知的"今天"）；高峰计价按调用时刻的 UTC 判断。

## 后果

- 正面：意外与温故栏在真实数据上也能稳定产出；费用有硬上限；分级规则全部在纯函数里，可单测。
- 负面：前排 lane 的条数受 L2 top_n 限制（默认 15，略少于 lane 上限之和 16），通常是时间预算先用完，影响不大；`deferred` 是 ADR 0004 之外新增的 lane 取值，M3 的 UI 需要单独展示；改写原 digest 行会丢失"曾经是必读"的历史，如有需要 M3 可改为单独记录。
