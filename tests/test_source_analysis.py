"""显式来源分类的离线边界、费用保护和缓存回归。"""

import json

import pytest

from secondbrain import config, db
from secondbrain import source_analysis as analysis
from secondbrain.llm.base import LLMError, Usage
from secondbrain.llm.fake import FakeBackend
from secondbrain.llm.schemas import SourceProfile
from secondbrain.models import RawEntry
from secondbrain.net import FetchError


@pytest.fixture
def analysis_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("SB_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    sources = cfg / "sources.yaml"
    sources.write_bytes(
        b"# Preserve comments and label spelling\nsources:\n"
        b"  - id: research\n    name: Research Site\n    type: rss\n"
        b"    url: https://research.example/feed.xml\n"
        b"    homepage: https://research.example/\n"
        b"    trust: 5\n    tags: ['Manual-Tag', 'RSS'] # unchanged\n"
    )
    entries = [RawEntry(
        url="https://research.example/papers/one", title="LLM transformer paper",
        summary="Research on efficient artificial intelligence systems.",
        feed_content="FULLTEXT MUST NOT BE READ",
    )]
    calls = []

    def preview(source, client, settings):
        calls.append(source.id)
        return {"entries": list(entries), "status": "ready", "warnings": []}

    monkeypatch.setattr(analysis, "preview_source", preview)
    return {"config": cfg, "sources": sources, "entries": entries, "calls": calls}


def _forbidden(*args, **kwargs):
    raise AssertionError("model must not be initialized or called")


def _saved_row():
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM source_analyses WHERE source_id = 'research'").fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _calls():
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    finally:
        conn.close()


def _add_stored(source_id="research", title="Database engineering blog", summary="Systems work"):
    conn = db.connect()
    try:
        sources = config.load_sources()
        if source_id != "research":
            sources.append(sources[0].model_copy(update={"id": source_id}))
        db.upsert_sources(conn, sources)
        cursor = conn.execute(
            """INSERT INTO items(source_id, url, url_canonical, content_hash, title, summary,
                                 content, extract_method, fetched_at)
               VALUES (?, ?, ?, 'hash', ?, ?, 'SECRET FULL BODY', 'feed',
                       '2026-11-16T00:00:00Z')""",
            (source_id, f"https://research.example/{source_id}/stored",
             f"https://research.example/{source_id}/stored", title, summary),
        )
        conn.execute(
            """INSERT INTO assessments(item_id, stage, model, data, created_at)
               VALUES (?, 'L1', 'fake-cheap', ?, '2026-11-16T00:00:00Z')""",
            (cursor.lastrowid, json.dumps({"topics": ["distributed systems"],
                                         "one_line_reason": "Database systems explanation"})),
        )
        conn.execute(
            """INSERT INTO assessments(item_id, stage, model, data, created_at)
               VALUES (?, 'L2', 'fake-cheap', ?, '2026-11-16T00:00:00Z')""",
            (cursor.lastrowid, json.dumps({"tldr": "Distributed database design"})),
        )
        conn.commit()
    finally:
        conn.close()


def test_analysis_persists_profile_without_touching_materials_or_manual_tags(analysis_env):
    original = analysis_env["sources"].read_bytes()
    result = analysis.analyze_source("research", lambda _: None)
    assert result["cached"] is False
    assert result["cost"] == 0
    assert result["profile"]["domains"] == ["人工智能", "计算机系统"]
    assert "RSS" not in result["profile"]["domains"]
    assert result["profile"]["evidence"] == [{
        "url": "https://research.example/papers/one", "title": "LLM transformer paper",
    }]
    assert "未读取全文" in result["profile"]["basis"]
    assert analysis_env["sources"].read_bytes() == original
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    finally:
        conn.close()
    assert _calls()[0]["stage"] == "SOURCE"
    assert analysis.get_source_analysis("research")["cached"] is True
    assert analysis.source_analyses()["research"]["model"] == "fake-cheap"
    assert analysis.get_source_analysis("missing") is None


def test_same_input_cache_requires_no_key_or_backend(analysis_env, monkeypatch):
    first = analysis.analyze_source("research", lambda _: None)
    monkeypatch.setenv("SB_LLM_BACKEND", "deepseek")
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    monkeypatch.setattr(config, "load_env", _forbidden)
    cached = analysis.analyze_source("research", lambda _: None)
    assert cached["cached"] is True
    assert cached["input_hash"] == first["input_hash"]
    assert cached["cost"] == 0
    assert len(_calls()) == 1


def test_force_and_changed_input_reanalyze(analysis_env):
    first = analysis.analyze_source("research", lambda _: None)
    forced = analysis.analyze_source("research", lambda _: None, force=True)
    assert forced["cached"] is False
    assert forced["input_hash"] == first["input_hash"]
    analysis_env["entries"][0].summary = "A new mathematics theorem"
    changed = analysis.analyze_source("research", lambda _: None)
    assert changed["input_hash"] != first["input_hash"]
    assert len(_calls()) == 3


@pytest.mark.parametrize("failure", [False, True])
def test_no_samples_refuses_even_with_manual_tags(analysis_env, monkeypatch, failure):
    analysis_env["entries"].clear()
    if failure:
        def failed(*args):
            raise FetchError("network unavailable")
        monkeypatch.setattr(analysis, "preview_source", failed)
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    with pytest.raises(analysis.SourceAnalysisError, match="真实内容"):
        analysis.analyze_source("research", lambda _: None)
    assert _saved_row() is None
    assert _calls() == []


@pytest.mark.parametrize("title", ["https://research.example/about", "Research Site",
                                   "research.example", "research"])
def test_url_or_site_name_is_not_content_evidence(analysis_env, monkeypatch, title):
    analysis_env["entries"][:] = [RawEntry("https://research.example/about", title)]
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    with pytest.raises(analysis.SourceAnalysisError, match="真实内容"):
        analysis.analyze_source("research", lambda _: None)


def test_stored_content_and_assessments_support_network_failure(analysis_env, monkeypatch):
    _add_stored()

    def failed(*args):
        raise FetchError("unavailable")

    monkeypatch.setattr(analysis, "preview_source", failed)
    result = analysis.analyze_source("research", lambda _: None)
    assert result["profile"]["domains"] == ["计算机系统"]
    assert result["profile"]["evidence"][0]["title"] == "Database engineering blog"
    assert "1 条已存内容" in result["profile"]["basis"]


def test_evidence_and_input_are_bounded_and_source_scoped(analysis_env, monkeypatch):
    _add_stored()
    _add_stored("unrelated", "Unrelated medicine article", "genome")
    analysis_env["entries"][:] = [RawEntry(
        f"https://research.example/papers/{i}", "LLM paper " + "x" * 2000,
        summary="summary " * 10000, feed_content="NEVER SEND FULLTEXT",
    ) for i in range(30)]
    captured = []

    class CapturingFake(FakeBackend):
        def complete_json(self, messages, schema, tier):
            captured.append(messages)
            return super().complete_json(messages, schema, tier)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: CapturingFake())
    result = analysis.analyze_source("research", lambda _: None)
    user = captured[0][-1]["content"]
    payload = json.loads(user)
    assert len(payload["samples"]) <= 10
    assert len(user) <= analysis.MAX_INPUT_CHARS
    assert all(len(sample["title"]) <= 240 for sample in payload["samples"])
    assert all(len(sample["summary"]) <= 600 for sample in payload["samples"])
    assert "SECRET FULL BODY" not in user
    assert "NEVER SEND FULLTEXT" not in user
    assert "Unrelated medicine" not in user
    assert "distributed systems" in user
    assert "Distributed database design" in user
    assert payload["context"]["manual_tags"] == ["Manual-Tag", "RSS"]
    assert result["profile"]["evidence"] == [
        {"url": sample["url"], "title": sample["title"]} for sample in payload["samples"]
    ]


def test_reference_catalog_tags_are_context_not_evidence(analysis_env, monkeypatch):
    (analysis_env["config"] / "sources.example.yaml").write_text(
        "sources:\n  - id: catalog-different-id\n    type: rss\n"
        "    url: https://research.example/feed.xml\n    tags: [Catalog-Only]\n",
        encoding="utf-8",
    )
    captured = []

    class CapturingFake(FakeBackend):
        def complete_json(self, messages, schema, tier):
            captured.append(json.loads(messages[-1]["content"]))
            return super().complete_json(messages, schema, tier)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: CapturingFake())
    result = analysis.analyze_source("research", lambda _: None)
    assert captured[0]["context"]["catalog_tags"] == ["Catalog-Only"]
    assert "Catalog-Only" not in result["profile"]["domains"]


def _real_settings(analysis_env, *, limit=0.5, model="priced", priced=True):
    settings = {
        "llm": {"models": {"cheap": model, "strong": model}},
        "run": {"max_cost_usd": limit},
    }
    if priced:
        settings["pricing"] = {model: {"input_cache_hit": 1, "input_cache_miss": 2,
                                       "output": 3}}
    (analysis_env["config"] / "settings.yaml").write_text(json.dumps(settings), encoding="utf-8")


@pytest.mark.parametrize("priced", [True, False])
def test_budget_and_unknown_price_prevent_backend_call(analysis_env, monkeypatch, priced):
    monkeypatch.setenv("SB_LLM_BACKEND", "deepseek")
    _real_settings(analysis_env, limit=0 if priced else 0.5, priced=priced)
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    with pytest.raises(analysis.SourceAnalysisError, match="上限|价格"):
        analysis.analyze_source("research", lambda _: None)
    assert _calls() == []
    assert _saved_row() is None


@pytest.mark.parametrize("known_usage", [False, True])
def test_model_failure_persists_usage_and_preserves_old_analysis(
    analysis_env, monkeypatch, known_usage,
):
    analysis.analyze_source("research", lambda _: None)
    old = _saved_row()
    original = analysis_env["sources"].read_bytes()

    class FailingFake(FakeBackend):
        def complete_json(self, messages, schema, tier):
            usage = Usage("fake-cheap", 120, 0, 120, 20) if known_usage else None
            raise LLMError("provider failed", usage)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: FailingFake())
    with pytest.raises(analysis.SourceAnalysisError, match="provider failed"):
        analysis.analyze_source("research", lambda _: None, force=True)
    assert _saved_row() == old
    assert analysis_env["sources"].read_bytes() == original
    call = _calls()[-1]
    assert call["stage"] == "SOURCE"
    assert call["error"] == "provider failed"
    assert call["usage_known"] == int(known_usage)
    assert call["prompt_tokens"] == (120 if known_usage else 0)


def test_corrupt_old_analysis_is_not_overwritten_or_repaid(analysis_env, monkeypatch):
    analysis.analyze_source("research", lambda _: None)
    conn = db.connect()
    conn.execute("UPDATE source_analyses SET data = '{broken' WHERE source_id = 'research'")
    conn.commit()
    conn.close()
    old = _saved_row()
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    with pytest.raises(analysis.SourceAnalysisError, match="保留原记录"):
        analysis.analyze_source("research", lambda _: None, force=True)
    assert _saved_row() == old
    assert analysis.get_source_analysis("research") is None
    assert analysis.source_analyses() == {}
    assert len(_calls()) == 1


def test_acquisition_method_is_rejected_and_old_profile_retained(analysis_env, monkeypatch):
    analysis.analyze_source("research", lambda _: None)
    old = _saved_row()

    class BadFake(FakeBackend):
        def complete_json(self, messages, schema, tier):
            return SourceProfile(domains=["RSS"], content_kinds=[], summary="Invalid",
                                 confidence="low"), Usage("fake-cheap", 10, 0, 10, 10)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: BadFake())
    with pytest.raises(analysis.SourceAnalysisError, match="获取方式"):
        analysis.analyze_source("research", lambda _: None, force=True)
    assert _saved_row() == old
    assert "获取方式" in _calls()[-1]["error"]


def test_read_only_functions_never_call_preview_or_backend(analysis_env, monkeypatch):
    analysis.analyze_source("research", lambda _: None)
    monkeypatch.setattr(analysis, "preview_source", _forbidden)
    monkeypatch.setattr(analysis, "make_backend", _forbidden)
    assert analysis.get_source_analysis("research")["cached"] is True
    assert list(analysis.source_analyses()) == ["research"]


def test_real_usage_above_limit_keeps_old_profile_and_records_cost(analysis_env, monkeypatch):
    analysis.analyze_source("research", lambda _: None)
    old = _saved_row()
    monkeypatch.setenv("SB_LLM_BACKEND", "deepseek")
    _real_settings(analysis_env)

    class ExpensiveBackend(FakeBackend):
        def complete_json(self, messages, schema, tier):
            profile, _ = super().complete_json(messages, schema, tier)
            return profile, Usage("priced", 1_000_000, 0, 1_000_000, 0)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: ExpensiveBackend())
    with pytest.raises(analysis.SourceAnalysisError, match="实际费用"):
        analysis.analyze_source("research", lambda _: None, force=True)
    assert _saved_row() == old
    assert _calls()[-1]["cost_usd"] > 0
    assert "实际费用" in _calls()[-1]["error"]


def test_unexpected_returned_model_is_not_silently_free(analysis_env, monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "deepseek")
    _real_settings(analysis_env)

    class UnknownBackend(FakeBackend):
        def complete_json(self, messages, schema, tier):
            profile, _ = super().complete_json(messages, schema, tier)
            return profile, Usage("unexpected-model", 123, 0, 123, 10)

    monkeypatch.setattr(analysis, "make_backend", lambda *args: UnknownBackend())
    with pytest.raises(analysis.SourceAnalysisError, match="未知价格"):
        analysis.analyze_source("research", lambda _: None)
    assert _saved_row() is None
    assert _calls()[-1]["model"] == "unexpected-model"
    assert "未知价格" in _calls()[-1]["error"]


def test_source_profile_limits():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SourceProfile(domains=["x"] * 7, content_kinds=[], summary="test", confidence="low")
    with pytest.raises(ValidationError):
        SourceProfile(domains=[], content_kinds=["x"] * 5, summary="test", confidence="low")
