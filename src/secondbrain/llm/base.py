"""统一的 LLM 后端接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from secondbrain.models import LLMSettings

Tier = Literal["cheap", "strong"]
T = TypeVar("T", bound=BaseModel)


@dataclass
class Usage:
    model: str
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.cache_hit_tokens += other.cache_hit_tokens
        self.cache_miss_tokens += other.cache_miss_tokens
        self.completion_tokens += other.completion_tokens


class LLMError(Exception):
    """LLM 调用失败。消息中不含密钥；usage 为失败前已消耗的用量（可能为 None）。"""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class LLMBackend(Protocol):
    name: str

    def model_name(self, tier: Tier) -> str: ...

    def complete_json(
        self, messages: list[dict[str, str]], schema: type[T], tier: Tier
    ) -> tuple[T, Usage]: ...


def make_backend(name: str, settings: LLMSettings, api_key: str | None = None) -> LLMBackend:
    if name == "fake":
        from secondbrain.llm.fake import FakeBackend

        return FakeBackend()
    if name == "deepseek":
        from secondbrain.llm.deepseek import DeepSeekBackend

        return DeepSeekBackend(settings, api_key)
    raise LLMError(f"未知的 LLM 后端：{name}")
