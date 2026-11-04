# 0002 — LLM 后端：统一接口，默认 DeepSeek

- 状态：已接受
- 日期：2026-11-04

## 背景

流水线需要两类调用：L1 粗筛（量大、要便宜）和 L2 精读 / wiki 编译（量小、要质量）。每天约 50 万输入 token 量级，成本敏感。测试需要完全离线、可重复。

候选后端：

- DeepSeek：OpenAI 兼容 API（`base_url=https://api.deepseek.com`），有 JSON 输出模式和上下文缓存计价，价格低（非高峰期半价）。
- Anthropic API：质量好，但单价更高。
- Claude Code headless（`claude -p`）：可以复用本机已登录的 Claude Code。但 Claude Agent SDK 文档写明，第三方产品不得向用户提供 claude.ai 登录或使用其订阅额度；本仓库是 public repo，默认路径必须是用户自备 API key。

## 决策

- 定义统一后端接口（M2 实现）：`complete_json(messages, schema, tier="cheap"|"strong") -> (obj, usage)`。
- 默认后端 DeepSeek，通过 `openai` SDK 调用（M2 新增依赖）：cheap = `deepseek-flash`（L1），strong = `deepseek-v4-pro`（L2）。模型名、`base_url`、价格表都放在 `config/settings.yaml`，不硬编码。
- `fake` 后端：确定性输出，供所有默认测试离线使用。
- 后端通过 `SB_LLM_BACKEND` 选择（覆盖 settings 中的 `llm.backend`）。
- 密钥只放 `.env`（`DEEPSEEK_API_KEY`），仓库只提交 `.env.example`；任何命令都只报告"已设置 / 未设置"。
- Anthropic API 与 `claude -p` 留作后续可插拔的可选后端（见 roadmap Backlog）；若加入 `claude -p`，只作为用户本机自行选择的选项，不作为默认，也不在项目中提供 claude.ai 登录。

## 后果

- 正面：成本低；换模型 / 换供应商只改配置或加一个后端实现；测试不依赖网络与密钥。
- 负面：依赖 DeepSeek 的 JSON 模式质量，需要 schema 校验 + 失败重试；价格与模型名会变，需要手动维护 config；多后端的输出质量差异需要在 prompt 层面兼顾。

后续：默认模型的调整见 ADR 0014。
