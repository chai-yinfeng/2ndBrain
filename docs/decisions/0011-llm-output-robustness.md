# 0011 — LLM 结构化输出的健壮性：截断、错误反馈与宽容解析

- 状态：已接受
- 日期：2026-11-04
- 补充：ADR 0002（LLM 后端）、ADR 0010（wiki ingest）

## 背景

M6 合并后，在 main 上复跑真实的 wiki ingest 演示失败了：`模型输出 2 次未通过 schema 校验：1 validation error for WikiPlan`。直接调用 API 复现后，原因如下：

- `finish_reason = length`，`completion_tokens = 4096`，正好等于当时 strong 模型的 `max_tokens`。
- 其中 `reasoning_tokens = 3298`：deepseek-v4-pro 是推理模型，**推理 token 也计入 `max_tokens`**，留给正文的只有约 800 token，JSON 在第二个页面中途被截断（`Invalid JSON: EOF while parsing`）。
- 推理长度每次不同，所以同样的输入有时成功（上一次演示）、有时失败。原来的重试用同样的参数再跑一次，同样会被截断。
- 当时的错误信息只有 pydantic 报错的第一行，看不到字段路径。

## 决策

1. **提高默认上限**：`llm.max_tokens.strong` 默认改为 8192（cheap 仍为 4096）。只按实际生成的 token 计费，上限提高本身不增加成本。
2. **截断检测**：校验失败且 `finish_reason == "length"` 时，重试的 `max_tokens` 翻倍（不超过 `llm.max_tokens_cap`，默认 16384），并追加一条 user 消息，要求输出更精简、确保 JSON 闭合。
3. **把校验错误反馈给模型**：非截断的校验失败重试时，追加一条 user 消息，列出"字段路径: 原因"（最多 5 处，其余汇总为"另有 N 处"）。追加在原始消息之后，前缀不变，不影响缓存。
4. **错误信息带字段路径**：最终的 `LLMError` 同样列出字段路径与原因（密钥仍会被替换为 `***`）。
5. **WikiPlan 宽容解析**：
   - 可安全修正的偏差直接修正：`type` / `action` 的大小写与空白；`aliases` / `links` 写成字符串时转为列表；缺 `slug` 时用 `title`（之后照常规范化）；缺一行 `summary` 时留空。
   - 单个 create / update 操作仍不合法（如缺 `body`、类型不在白名单）时，只丢弃该操作，原因记入 `WikiPlan.dropped`，由 `apply_plan` 以"解析时丢弃 …"计入拒绝列表。这与 apply_plan 中已有的逐条拒绝机制一致。
   - JSON 本身不完整（截断）时不做修补，交给第 2 条重试，避免把半截计划落盘。

重试总次数不变：仍是首次加一次重试。

## 后果

- 正面：推理模型输出偏长时能自动恢复；失败时能看到是哪个字段出了什么问题；单个操作的小偏差不再让整次 ingest（以及已经花掉的费用）作废。
- 负面：截断后的重试会多生成一轮 token（最多到 16384）；宽容解析目前只用于 WikiPlan，L1 / L2 schema 仍严格校验（依靠第 2、3 条的重试）。
