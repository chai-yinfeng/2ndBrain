# 0008 — 个性化：反馈 few-shot、profile 修改提案与 trust 调整的规则（M4）

- 状态：已接受
- 日期：2026-11-04

## 背景

roadmap 的 M4 要求根据反馈注入 few-shot、让 strong 模型提出 profile 修改、按 👍 / 👎 比例建议 trust 调整，但没有规定：LLM 的提案以什么形式落到文本上、掌握度调整允许多大幅度、trust 统计的口径，以及修改 YAML 时如何保留注释。

## 决策

- **few-shot 放在 L1 system prompt 的末尾**：profile + 规则在最前（同一次 run 内不变，利于 DeepSeek 前缀缓存），"最近反馈样例"段接在后面。正 / 负例各最多 `personalize.few_shot_per_polarity` 条。每个条目只取最新一次 👍 / 👎 / 学了；"需要回炉 / 已扎实"是掌握度信号，不作为样例。没有正负反馈时不出现该段。
- **提案是结构化 JSON，由代码生成文本**：
  - 掌握度表修改：`update` / `remove` 引用输入中的行号，`add` 给出领域名；
  - 其他段落修改只有两种：在某个 `##` 段落末尾 `append` 一行，或 `remove` 完全相同的一行。不允许自由改写整段，避免 LLM 擅改用户原文。
  - 代码重建表格时只重写被修改的行，未修改的行保留原文。
- **代码强制的规则**（违反的修改被拒绝并列出原因，其余照常应用）：
  - 每条修改必须带 evidence（反馈 id），且 id 必须来自本次送入的反馈；
  - 掌握度只能取 陌生 / 接触过 / 想回炉 / 扎实；
  - 升级需要"已扎实"或"学了"证据，每次只升一级；有"已扎实"证据时可以直接改为"扎实"；
  - 降级需要"需要回炉"证据，每次只降一级。
- **不足反馈时不调用 LLM**：近 `feedback_window_days` 天内的信号少于 `min_feedback_for_update`（默认 1）时直接提示"无足够反馈"。
- **写入前备份**：`--apply`、Web 的保存和应用都先把旧版本写到 `data/profile_history/profile-<本地时间>.md`。Web 应用提案时校验生成提案时的 profile sha256，期间被改过就返回 409。
- **trust 调整口径**：时间窗 `tune_window_days`（默认 90 天）内，每个条目只看最新一次 👍 / 👎 / 学了；有信号的条目数 < `tune_min_samples`（默认 5）不建议；正向比例 ≥ `tune_raise_ratio`（0.7）建议 +1，≤ `tune_lower_ratio`（0.3）建议 −1，限制在 1–5。
- **`sources tune --apply` 用行级替换**：只替换对应信源条目中的 `trust: N`，保留注释与格式，不引入 ruamel.yaml。`config/sources.yaml` 不存在时先从 example 复制。`config/sources.yaml` 与 `config/settings.yaml` 已加入 .gitignore。

## 后果

- 正面：LLM 无法越过证据规则修改掌握度；用户原文不会被整段改写；没有新依赖。
- 负面：段落修改的表达力有限（只能加 / 删整行），较大的改写仍需用户在 `/profile` 手动编辑；行级替换要求 `trust:` 写在信源条目内（example 满足），手写成内联格式（`{id: x, trust: 3}`）时不会被修改，命令会报"找不到 trust 行"。
