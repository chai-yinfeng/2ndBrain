"""显式、限额的信源分类；只读内容样本，不导入材料、不修改人工标签。"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from secondbrain import config, db
from secondbrain.llm.base import LLMError, Usage, make_backend
from secondbrain.llm.schemas import SourceProfile
from secondbrain.llm.usage import full_price, record_call
from secondbrain.models import SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.source_discovery import preview_source, source_matches

MAX_SAMPLES = 10
MAX_INPUT_CHARS = 24_000
MAX_OUTPUT_TOKENS = 1024
PROMPT_PATH = Path(__file__).parent / "llm" / "prompts" / "source_profile.md"
_ACQUISITION = {
    "rss", "atom", "sitemap", "html_index", "arxiv", "api", "bookmark",
    "订阅", "网页抓取", "获取方式",
}


class SourceAnalysisError(LLMError):
    """分类被拒绝或失败；既有分析不被覆盖。"""


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value[: limit * 4])).split())[:limit]


def _labels(value: object, count: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    return [label for raw in value[:count] if (label := _text(raw, 64))]


def _sample(
    source: SourceConfig, url: str, title: str, summary: str | None,
    topics: list[str], origin: str,
) -> dict | None:
    if not isinstance(url, str) or len(url) > 1024:
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
        ):
            return None
    except ValueError:
        return None
    title, summary = _text(title, 240), _text(summary, 600)
    non_content = {
        source.id.casefold(), (source.name or "").casefold(),
        (urlsplit(source.homepage or source.url or "").hostname or "").casefold(),
    }
    if title.casefold() in non_content or title.startswith(("http://", "https://")):
        title = ""
    if not title and not summary:
        return None
    return {"url": url, "title": title, "summary": summary,
            "topics": _labels(topics), "origin": origin}


def _stored_samples(conn: sqlite3.Connection, source: SourceConfig) -> list[dict]:
    rows = conn.execute(
        """SELECT id, url, substr(title, 1, 960) AS title,
                  substr(summary, 1, 2400) AS summary
           FROM items WHERE source_id = ? OR id IN
               (SELECT item_id FROM item_sources WHERE source_id = ?)
           ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?""",
        (source.id, source.id, MAX_SAMPLES),
    ).fetchall()
    samples = []
    for row in rows:
        topics, summaries = [], []
        assessments = conn.execute(
            """SELECT stage, substr(data, 1, 20000) AS data FROM assessments
               WHERE item_id = ? AND stage IN ('L1', 'L2')
               ORDER BY id DESC LIMIT 4""", (row["id"],),
        ).fetchall()
        seen = set()
        for assessment in assessments:
            if assessment["stage"] in seen:
                continue
            seen.add(assessment["stage"])
            try:
                data = json.loads(assessment["data"])
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            topics.extend(_labels(data.get("topics")))
            for key in ("tldr", "one_line_reason"):
                if text := _text(data.get(key), 300):
                    summaries.append(text)
        summary = " ".join(filter(None, [row["summary"], *summaries]))
        sample = _sample(source, row["url"], row["title"], summary, topics, "stored")
        if sample is not None:
            samples.append(sample)
    return samples


def _payload(conn: sqlite3.Connection, source: SourceConfig, fetch_settings, echo) -> dict:
    stored = _stored_samples(conn, source)
    samples = stored[:5]
    seen = {sample["url"] for sample in samples}
    try:
        with HttpClient(fetch_settings) as client:
            preview = preview_source(source, client, fetch_settings)
        entries = preview["entries"]
        if not entries and preview.get("status") == "failed":
            echo("近期目录预览失败；仅使用已有内容样本。")
        for entry in entries[:MAX_SAMPLES]:
            if len(samples) >= MAX_SAMPLES:
                break
            sample = _sample(source, entry.url, entry.title, entry.summary, [], "preview")
            if sample is not None and sample["url"] not in seen:
                samples.append(sample)
                seen.add(sample["url"])
    except FetchError:
        echo("近期目录预览失败；仅使用已有内容样本。")
    for sample in stored[5:]:
        if len(samples) >= MAX_SAMPLES:
            break
        if sample["url"] not in seen:
            samples.append(sample)
            seen.add(sample["url"])
    catalog_tags = []
    catalog_path = config.config_dir() / "sources.example.yaml"
    if catalog_path.is_file():
        for candidate in config.load_sources(catalog_path):
            if source_matches(source, candidate):
                catalog_tags.extend(_labels(candidate.tags))
    payload = {
        "context": {"source_id": source.id[:128], "name": _text(source.name, 160),
                    "manual_tags": _labels(source.tags, 12),
                    "catalog_tags": _labels(catalog_tags, 12)},
        "samples": samples,
    }
    while samples and len(_json(payload)) > MAX_INPUT_CHARS:
        samples.pop()
    if not samples:
        raise SourceAnalysisError("没有可用的真实内容标题或摘要，拒绝推断分类；请先获取公开内容。")
    return payload


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(row: sqlite3.Row) -> dict:
    try:
        data = json.loads(row["data"])
        profile = data["profile"]
        validated = SourceProfile.model_validate(profile).model_dump()
        evidence = profile["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_SAMPLES:
            raise ValueError("缺少证据")
        if any(not isinstance(item, dict) or not isinstance(item.get("url"), str)
               or not isinstance(item.get("title"), str) for item in evidence):
            raise ValueError("证据格式错误")
        if not isinstance(profile.get("basis"), str) or not profile.get("analyzed_at"):
            raise ValueError("缺少分析依据或时间")
        profile = {**validated, "evidence": evidence, "basis": profile["basis"],
                   "analyzed_at": profile["analyzed_at"]}
        return {"source_id": row["source_id"], "profile": profile, "cached": True,
                "cost": 0.0, "analysis_cost": data.get("cost", 0.0),
                "input_hash": row["input_hash"], "model": row["model"],
                "analyzed_at": row["ts"]}
    except (ValueError, TypeError, KeyError, ValidationError) as exc:
        raise SourceAnalysisError("已有来源分析无法读取，已保留原记录，未调用模型或覆盖。") from exc


def get_source_analysis(source_id: str) -> dict | None:
    """只读有效缓存；损坏缓存不展示、不删除。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM source_analyses WHERE source_id = ?",
                           (source_id,)).fetchone()
        if row is None:
            return None
        try:
            return _decode(row)
        except SourceAnalysisError:
            return None
    finally:
        conn.close()


def source_analyses() -> dict[str, dict]:
    """UI 只读索引，不联网、不加载后端、不自动分析。"""
    conn = db.connect()
    try:
        result = {}
        for row in conn.execute("SELECT * FROM source_analyses ORDER BY source_id"):
            try:
                result[row["source_id"]] = _decode(row)
            except SourceAnalysisError:
                continue
        return result
    finally:
        conn.close()


def source_contexts() -> dict[str, dict]:
    """免费展示已有证据；不把目录标签或历史 topics 冒充新模型判断。"""
    sources = config.load_sources()
    catalog_path = config.config_dir() / "sources.example.yaml"
    catalog = config.load_sources(catalog_path) if catalog_path.is_file() else []
    conn = db.connect() if db.db_path().exists() else None
    try:
        result = {}
        for source in sources:
            tags = [tag for candidate in catalog if source_matches(source, candidate)
                    for tag in candidate.tags]
            samples = _stored_samples(conn, source) if conn is not None else []
            topics = [topic for sample in samples for topic in sample["topics"]]
            result[source.id] = {
                "catalog_tags": list(dict.fromkeys(_labels(tags, 12))),
                "existing_topics": list(dict.fromkeys(_labels(topics, 12))),
            }
        return result
    finally:
        if conn is not None:
            conn.close()


def analyze_source(source_id: str, echo: Callable[[str], None], *, force: bool = False) -> dict:
    """显式分析，最多十条标题 / 摘要；同输入命中先于环境密钥和后端初始化。"""
    settings = config.load_settings()
    fs = config.pipeline_settings(settings)
    source = next((s for s in config.load_sources() if s.id == source_id), None)
    if source is None:
        raise SourceAnalysisError("来源不存在或未关注。")
    conn = db.connect()
    try:
        old = conn.execute("SELECT * FROM source_analyses WHERE source_id = ?",
                           (source_id,)).fetchone()
        cached = _decode(old) if old is not None else None
        payload = _payload(conn, source, config.fetch_settings(settings), echo)
        system = PROMPT_PATH.read_text(encoding="utf-8")
        user = _json(payload)
        input_hash = hashlib.sha256((system + "\n" + user).encode()).hexdigest()
        if cached is not None and cached["input_hash"] == input_hash and not force:
            return cached
        name = config.llm_backend(settings)
        model = "fake-cheap" if name == "fake" else fs.llm.models["cheap"]
        if name != "fake" and model not in fs.pricing:
            raise SourceAnalysisError(f"模型 {model} 没有价格配置，无法执行费用保护，未调用。")
        # 保留既有后端的两次校验机会，但限制本操作输出；绝不提高配置上限。
        output_tokens = min(MAX_OUTPUT_TOKENS, fs.llm.max_tokens["cheap"], fs.llm.max_tokens_cap)
        llm_settings = fs.llm.model_copy(update={
            "max_tokens": {**fs.llm.max_tokens, "cheap": output_tokens},
            "max_tokens_cap": output_tokens,
        })
        # UTF-8 字节数作保守输入 token 上界；覆盖一次校验重试与短错误提示。
        input_tokens = len((system + user).encode()) + 1024
        estimate = 2 * full_price(
            Usage(model, input_tokens, 0, input_tokens, output_tokens), fs.pricing,
        )
        if not math.isfinite(estimate) or estimate < 0 or estimate > fs.run.max_cost_usd:
            raise SourceAnalysisError("预估费用超过 run.max_cost_usd 上限，未调用模型。")
        echo(f"来源分类：{len(payload['samples'])} 条样本，预估上限 ${estimate:.4f}")
        config.load_env()
        backend = make_backend(name, llm_settings, os.environ.get("DEEPSEEK_API_KEY"))
        db.upsert_sources(conn, [source])
        now = datetime.now(UTC)
        usage = None
        try:
            response, usage = backend.complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                SourceProfile, "cheap",
            )
            profile = SourceProfile.model_validate(response).model_dump()
            if any(label.strip().casefold() in _ACQUISITION
                   for label in profile["domains"] + profile["content_kinds"]):
                raise SourceAnalysisError("模型把获取方式作为分类，拒绝保存。", usage)
            if usage.model not in fs.pricing and name != "fake":
                raise SourceAnalysisError("模型返回未知价格，调用用量已记录，保留旧分类。", usage)
            actual = full_price(usage, fs.pricing)
            if actual > fs.run.max_cost_usd:
                raise SourceAnalysisError("实际费用超过 run.max_cost_usd 上限，保留旧分类。", usage)
        except (LLMError, ValidationError) as exc:
            failed_usage = exc.usage if isinstance(exc, LLMError) else usage
            record_call(conn, name, "SOURCE", failed_usage, now, fs.pricing,
                        fs.pricing_schedule, error=str(exc), model=model)
            conn.commit()
            raise SourceAnalysisError(str(exc), failed_usage) from exc
        cost = record_call(conn, name, "SOURCE", usage, now, fs.pricing, fs.pricing_schedule)
        ts = db.format_time(now)
        samples = payload["samples"]
        stored_count = sum(sample["origin"] == "stored" for sample in samples)
        profile.update({
            "evidence": [{"url": sample["url"], "title": sample["title"]} for sample in samples],
            "basis": f"基于 {stored_count} 条已存内容及其已有分析、"
                     f"{len(samples) - stored_count} 条近期目录标题/摘要；未读取全文；"
                     "人工及参考目录标签仅作上下文，不作内容证据。",
            "analyzed_at": ts,
        })
        conn.execute(
            """INSERT INTO source_analyses(source_id, input_hash, data, model, ts)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
               input_hash = excluded.input_hash, data = excluded.data,
               model = excluded.model, ts = excluded.ts""",
            (source_id, input_hash, _json({"profile": profile, "cost": cost}), usage.model, ts),
        )
        conn.commit()
        return {"source_id": source_id, "profile": profile, "cached": False,
                "cost": cost, "analysis_cost": cost, "input_hash": input_hash,
                "model": usage.model, "analyzed_at": ts}
    finally:
        conn.close()
