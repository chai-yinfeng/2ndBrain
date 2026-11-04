import json
from datetime import UTC, datetime

import httpx
import pytest
import yaml

from secondbrain import config
from secondbrain.llm.base import LLMError
from secondbrain.llm.deepseek import DeepSeekBackend, parse_usage
from secondbrain.llm.schemas import L1Response, L2Response
from secondbrain.llm.usage import estimate_cost, is_peak
from secondbrain.models import LLMSettings, ModelPrice, PricingSchedule

SECRET = "sk-test-secret-value-1234567890"
L1_OK = {
    "items": [
        {
            "id": 7,
            "relevance": 8,
            "novelty": 6,
            "quality": 7,
            "content_type": "frontier",
            "refresher_value": 2,
            "topics": ["kv-cache"],
            "one_line_reason": "相关",
        }
    ]
}
USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "prompt_cache_hit_tokens": 60,
    "prompt_cache_miss_tokens": 40,
}


def completion(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage or USAGE,
        },
    )


def backend(responses: list, max_retries: int = 0) -> tuple[DeepSeekBackend, list]:
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return queue.pop(0)

    settings = LLMSettings(max_retries=max_retries)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeekBackend(settings, SECRET, http_client=client), requests


MESSAGES = [{"role": "system", "content": "输出 json"}, {"role": "user", "content": "{}"}]


def test_parses_json_and_usage():
    be, requests = backend([completion(json.dumps(L1_OK))])
    obj, usage = be.complete_json(MESSAGES, L1Response, "cheap")
    assert obj.items[0].id == 7
    assert (usage.model, usage.prompt_tokens, usage.completion_tokens) == (
        "deepseek-flash",
        100,
        20,
    )
    assert (usage.cache_hit_tokens, usage.cache_miss_tokens) == (60, 40)
    body = json.loads(requests[0].content)
    assert body["model"] == "deepseek-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"


def test_strong_tier_uses_strong_model():
    l2 = {
        "tldr": "t",
        "key_points": ["a"],
        "why_for_you": "w",
        "est_minutes": 20,
        "prerequisites": [],
        "scores": {"relevance": 8, "novelty": 7, "quality": 8, "refresher_value": 3},
    }
    be, requests = backend([completion(json.dumps(l2))])
    obj, usage = be.complete_json(MESSAGES, L2Response, "strong")
    assert obj.est_minutes == 20
    assert json.loads(requests[0].content)["model"] == "deepseek-v4-pro"


def test_invalid_output_is_retried_once():
    be, requests = backend([completion("not json"), completion(json.dumps(L1_OK))])
    obj, usage = be.complete_json(MESSAGES, L1Response, "cheap")
    assert len(requests) == 2
    assert usage.prompt_tokens == 200  # 两次调用的用量都计入


def test_gives_up_after_second_invalid_output():
    bad = json.dumps({"items": [{"id": 1}]})
    be, requests = backend([completion(bad), completion("")])
    with pytest.raises(LLMError, match="schema") as info:
        be.complete_json(MESSAGES, L1Response, "cheap")
    assert len(requests) == 2
    assert info.value.usage.prompt_tokens == 200


def test_error_messages_never_contain_key():
    body = {"error": {"message": f"Invalid key {SECRET}", "type": "auth"}}
    be, _ = backend([httpx.Response(401, json=body)])
    with pytest.raises(LLMError) as info:
        be.complete_json(MESSAGES, L1Response, "cheap")
    assert "401" in str(info.value)
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value.__cause__)


def test_rate_limit_is_retried():
    limited = httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": {}})
    be, requests = backend([limited, completion(json.dumps(L1_OK))], max_retries=2)
    obj, _ = be.complete_json(MESSAGES, L1Response, "cheap")
    assert obj.items[0].id == 7
    assert len(requests) == 2


def test_usage_fallback_to_cached_tokens():
    usage = {
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "total_tokens": 55,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    be, _ = backend([completion(json.dumps(L1_OK), usage)])
    _, u = be.complete_json(MESSAGES, L1Response, "cheap")
    assert (u.cache_hit_tokens, u.cache_miss_tokens) == (30, 20)
    assert parse_usage(None, "m").prompt_tokens == 0


def test_missing_key_is_rejected():
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        DeepSeekBackend(LLMSettings(), None)


def test_peak_detection_and_cost():
    schedule = PricingSchedule()
    wed_peak = datetime(2026, 11, 4, 7, 30, tzinfo=UTC)
    wed_off = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
    sat = datetime(2026, 11, 7, 7, 30, tzinfo=UTC)
    assert is_peak(wed_peak, schedule)
    assert not is_peak(wed_off, schedule)
    assert not is_peak(sat, schedule)
    assert not is_peak(datetime(2026, 11, 4, 10, 0, tzinfo=UTC), schedule)  # 右开

    from secondbrain.llm.base import Usage

    pricing = {"m": ModelPrice(input_cache_hit=0.1, input_cache_miss=1.0, output=2.0)}
    u = Usage("m", 2_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert estimate_cost(u, pricing, schedule, wed_peak) == (pytest.approx(3.1), True)
    assert estimate_cost(u, pricing, schedule, wed_off) == (pytest.approx(1.55), False)
    assert estimate_cost(Usage("unknown", 10, 0, 10, 10), pricing, schedule, wed_peak)[0] == 0


def test_example_settings_pricing_is_complete():
    raw = yaml.safe_load((config.CONFIG_DIR / "settings.example.yaml").read_text(encoding="utf-8"))
    fs = config.pipeline_settings(raw)
    for model in fs.llm.models.values():
        assert model in fs.pricing


def test_prompts_mention_json():
    from secondbrain.pipeline.run import PROMPTS_DIR

    for name in ("l1_system.md", "l2_system.md"):
        text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
        assert "json" in text
        assert "{profile}" in text and "{mastery}" in text
