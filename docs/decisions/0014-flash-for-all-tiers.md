# 0014 — 所有 LLM 调用默认使用 deepseek-flash

- 状态：已接受
- 日期：2026-11-04
- 修订：ADR 0002（默认模型）

## 背景

ADR 0002 规定 cheap = `deepseek-flash`，strong = `deepseek-v4-pro`。strong 用于 L2 精读、按需精读（`sb deep`）、wiki ingest 和 profile 修改提案。

按价格表（高峰价，USD / 1M tokens），v4-pro 的输入约为 flash 的 4.4 倍，输出约为 3.3 倍。v4-pro 还是推理模型，reasoning token 也计入输出（ADR 0011），实际开销更高。用户使用一段时间后判断：v4-pro 并没有明显更聪明，性价比低。M8 计划的"深度速读"还会增加 strong 调用量。

## 决策

- `config/settings.example.yaml` 与 `LLMSettings` 的默认值都改为 cheap = strong = `deepseek-flash`。
- cheap / strong 两级机制保留，代码中按 tier 选模型的逻辑不变，只改默认值。
- 价格表保留 `deepseek-v4-pro` 条目。想切回时，在 `config/settings.yaml` 中把 `llm.models.strong` 改为 `deepseek-v4-pro` 即可，计价会自动正确。
- `max_tokens` 不改（cheap 4096，strong 8192，上限 16384）。只按实际生成的 token 计费，上限对 flash 无害；截断时的翻倍重试（ADR 0011）照常生效。

## 后果

- 正面：L2、wiki ingest、profile 提案的单次费用显著下降，为 M8 留出预算；换回 pro 只需改一行配置。
- 负面：strong 任务的输出质量可能略有下降。如果发现 L2 或 wiki ingest 的质量明显变差，可以按 tier 单独切回。
