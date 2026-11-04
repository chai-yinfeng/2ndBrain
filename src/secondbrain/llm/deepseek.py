"""DeepSeek 后端（OpenAI 兼容 API，JSON 输出模式）。"""

from __future__ import annotations

import httpx
import openai
from pydantic import ValidationError

from secondbrain.llm.base import LLMError, T, Tier, Usage
from secondbrain.models import LLMSettings

# 模型输出未通过 schema 校验时的总尝试次数（首次 + 重试一次）。
VALIDATION_ATTEMPTS = 2


def parse_usage(raw, model: str) -> Usage:
    """解析 usage；DeepSeek 返回 prompt_cache_hit/miss_tokens，其他兼容实现可能用 cached_tokens。"""
    if raw is None:
        return Usage(model)
    prompt = raw.prompt_tokens or 0
    hit = getattr(raw, "prompt_cache_hit_tokens", None)
    miss = getattr(raw, "prompt_cache_miss_tokens", None)
    if hit is None:
        details = getattr(raw, "prompt_tokens_details", None)
        hit = (getattr(details, "cached_tokens", None) or 0) if details else 0
    if miss is None:
        miss = max(prompt - hit, 0)
    return Usage(
        model=model,
        prompt_tokens=prompt,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        completion_tokens=raw.completion_tokens or 0,
    )


class DeepSeekBackend:
    name = "deepseek"

    def __init__(
        self,
        settings: LLMSettings,
        api_key: str | None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise LLMError(
                "DEEPSEEK_API_KEY 未设置（在 .env 中填写；离线可用 SB_LLM_BACKEND=fake）"
            )
        self.settings = settings
        self._secret = api_key
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    def model_name(self, tier: Tier) -> str:
        return self.settings.models[tier]

    def _redact(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    def complete_json(
        self, messages: list[dict[str, str]], schema: type[T], tier: Tier
    ) -> tuple[T, Usage]:
        model = self.model_name(tier)
        total = Usage(model)
        last_error = ""
        for _ in range(VALIDATION_ATTEMPTS):
            try:
                resp = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.settings.max_tokens[tier],
                )
            except openai.APIStatusError as exc:
                detail = self._redact(str(exc))[:300]
                raise LLMError(f"DeepSeek HTTP {exc.status_code}：{detail}", total) from None
            except openai.APIError as exc:
                detail = self._redact(str(exc))[:300]
                raise LLMError(
                    f"DeepSeek 调用失败（{type(exc).__name__}）：{detail}", total
                ) from None
            total.add(parse_usage(resp.usage, model))
            content = resp.choices[0].message.content if resp.choices else None
            try:
                return schema.model_validate_json(content or ""), total
            except ValidationError as exc:
                last_error = self._redact(str(exc).splitlines()[0])
        raise LLMError(f"模型输出 {VALIDATION_ATTEMPTS} 次未通过 schema 校验：{last_error}", total)
