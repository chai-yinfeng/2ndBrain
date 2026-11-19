"""持久重整会话的消费者回归；所有模型回复通过真实 backend 接口注入。"""

import json
from datetime import UTC, datetime

import pytest

from secondbrain import config, db
from secondbrain.llm.base import LLMError, Usage
from secondbrain.models import FilterSettings, ModelPrice
from secondbrain.wiki import rewrite
from secondbrain.wiki.store import Page, open_wiki


class ReplyBackend:
    name = "fake"

    def __init__(self):
        self.calls = []
        self.replies = [
            {"body": "### 第一版\n\n按新语境解释缓存，不复制原机器段落。", "summary": "重组解释"},
            {"body": "### 第二版\n\n改用厨房类比解释缓存。", "summary": "采用厨房类比"},
        ]
        self.usage = Usage("rewrite-test", 20, 0, 20, 10)
        self.error = None

    def model_name(self, tier):
        return "rewrite-test"

    def complete_json(self, messages, schema, tier):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return schema.model_validate(self.replies.pop(0)), self.usage


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("SB_DATA_DIR", str(data))
    monkeypatch.setenv("SB_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    profile = data / "profile.md"
    profile.write_text("我的长期画像，不得写入临时语境。", encoding="utf-8")
    page = Page(
        "cache",
        {
            "type": "concept", "title": "缓存", "sources": [7], "custom": "人工字段",
            "human_relations": [{"target": "pages/concepts/blocked.md", "mode": "block"}],
        },
        "# 缓存\n\n手写的开场文字不能丢失。\n\n"
        "## 整理\n\n"
        "<!-- wiki-ingest:start material-7:0123456789abcdef0123 -->\n"
        "<!-- material-7:0123456789abcdef0123 -->\n\n"
        "原机器正文，需要被替换而非追加。\n\n"
        "<!-- wiki-ingest:end material-7:0123456789abcdef0123 -->\n\n"
        "## 个人笔记\n\n用户的理解和疑问。\n\n"
        "## 人工关系\n\n保持现有人工决定。\n\n"
        "## 来源\n\n[实际来源](../../raw/7.md)\n",
    )
    wiki.save_page(page)
    wiki.save_page(Page("blocked", {"type": "concept", "title": "禁止连接"}, "# 禁止连接\n"))
    wiki.save_page(Page("related", {"type": "concept", "title": "相关页"}, "# 相关页\n[[cache]]\n"))
    wiki.save_page(Page(
        "trashed", {"type": "concept", "title": "回收页", "trashed": True},
        "# 回收页\n[[cache]]\n不得发送模型\n",
    ))
    wiki.write_raw(7, "真实来源标题", "https://example.test/source", "真实来源的正文证据。")
    backend = ReplyBackend()
    monkeypatch.setattr(rewrite, "make_backend", lambda *args: backend)
    fs = FilterSettings()
    monkeypatch.setattr(config, "pipeline_settings", lambda settings: fs)
    return data, page.path, backend, fs


def _body(path):
    return path.read_text(encoding="utf-8")


def _calls():
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    finally:
        conn.close()


def test_start_and_read_only_resume_do_not_change_page_or_profile(workspace):
    data, path, backend, _ = workspace
    before = path.read_bytes()
    profile = (data / "profile.md").read_bytes()
    session = rewrite.start_session("cache", "临时语境：我在做厨房教学，不要作为长期兴趣。")
    assert session["status"] == "draft"
    assert "第一版" in session["draft"]
    assert path.read_bytes() == before
    assert (data / "profile.md").read_bytes() == profile
    saved = rewrite._session_path(session["id"])
    persisted = saved.read_bytes()
    assert rewrite.get_session(session["id"])["messages"] == session["messages"]
    assert rewrite.list_sessions("缓存")[0]["id"] == session["id"]
    assert saved.read_bytes() == persisted
    assert len(backend.calls) == 1
    payload = json.loads(backend.calls[0][1]["content"])
    assert "真实来源的正文证据" in payload["sources"][0]["content"]
    assert {entry["slug"] for entry in payload["related_pages"]} == {"related"}
    assert payload["human_relations"]
    assert _calls()[0]["stage"] == "WIKI_REWRITE"


def test_continue_apply_history_and_restore_preserve_human_content(workspace):
    data, path, backend, _ = workspace
    before = _body(path)
    session = rewrite.start_session("cache", "先简明解释")
    session = rewrite.continue_session(session["id"], "改用厨房类比")
    assert len(session["messages"]) == 4
    assert session["status"] == "draft"
    assert _body(path) == before
    result = rewrite.apply_revision(session["id"])
    current = _body(path)
    assert result["status"] == "applied"
    assert "厨房类比" in current
    assert "原机器正文，需要被替换" not in current
    assert "第一版" not in current
    assert current.count("<!-- wiki-rewrite:start -->") == 1
    for text in ("手写的开场文字", "用户的理解和疑问", "保持现有人工决定", "实际来源", "人工字段"):
        assert text in current
    assert current.split("---", 2)[1] == before.split("---", 2)[1]
    revisions = rewrite.history("cache")
    assert len(revisions) == 2
    assert all({"id", "created_at", "summary"} <= entry.keys() for entry in revisions)
    initial = next(entry for entry in revisions if entry["summary"] == "采用前版本")
    path.write_text(
        current.replace("用户的理解和疑问。", "用户后来新增的个人笔记。"), encoding="utf-8"
    )
    rewrite.restore_revision("cache", initial["id"])
    restored = _body(path)
    assert "原机器正文，需要被替换" in restored
    assert "厨房类比" not in restored
    assert "用户后来新增的个人笔记" in restored
    assert "用户的理解和疑问" not in restored
    assert len(backend.calls) == 2  # apply/history/restore 不发模型请求
    assert (data / "profile.md").read_text(encoding="utf-8") == "我的长期画像，不得写入临时语境。"


def test_continue_after_apply_replaces_previous_draft(workspace):
    _, path, backend, _ = workspace
    session = rewrite.start_session("cache", "首次要求")
    rewrite.apply_revision(session["id"])
    session = rewrite.continue_session(session["id"], "重新解释")
    assert "第一版" in _body(path)
    rewrite.apply_revision(session["id"])
    assert "第一版" not in _body(path)
    assert "第二版" in _body(path)
    assert len(backend.calls) == 2


def test_external_edit_rejects_apply_and_continue(workspace):
    _, path, backend, _ = workspace
    session = rewrite.start_session("cache", "本轮语境")
    path.write_text(_body(path) + "\n外部编辑器新写的内容。\n", encoding="utf-8")
    changed = path.read_bytes()
    with pytest.raises(rewrite.RewriteError, match="已变化"):
        rewrite.apply_revision(session["id"])
    with pytest.raises(rewrite.RewriteError, match="已变化"):
        rewrite.continue_session(session["id"], "继续")
    assert path.read_bytes() == changed
    assert len(backend.calls) == 1
    assert rewrite.history("cache") == []


def test_old_and_unmarked_human_pages_remain_safe(workspace):
    _, path, _, _ = workspace
    old = (
        "# 缓存\n\n## 机器整理 · 材料 7\n\n"
        "<!-- material-7:0123456789abcdef0123 -->\n\n"
        "> 机器整理，未经用户核对；材料陈述与机器推断分别标注。\n\n"
        "旧机器结论。\n\n## 个人笔记\n\n手写笔记。\n"
    )
    path.write_text(old, encoding="utf-8")
    session = rewrite.start_session("cache", "重整旧机器内容")
    rewrite.apply_revision(session["id"])
    assert "旧机器结论" not in _body(path)
    assert "手写笔记" in _body(path)
    unmarked = "# 缓存\n\n## 我的人工解释\n\n不能猜成机器正文。\n"
    path.write_text(unmarked, encoding="utf-8")
    session = rewrite.start_session("cache", "提供新AI解释")
    rewrite.apply_revision(session["id"])
    assert "不能猜成机器正文" in _body(path)
    assert "第二版" in _body(path)


def test_blocked_machine_link_and_metadata_survive(workspace):
    _, path, backend, _ = workspace
    backend.replies[0]["body"] = "新的解释：[[blocked]]，不得复活人工禁止边。"
    session = rewrite.start_session("cache", "保留人工关系")
    rewrite.apply_revision(session["id"])
    assert "[[blocked]]" not in _body(path)
    assert "human_relations:" in _body(path)
    assert "mode: block" in _body(path)


def test_interrupted_session_is_read_only_and_never_replayed(workspace):
    _, _, backend, _ = workspace
    session = rewrite.start_session("cache", "本轮语境")
    session["status"] = "generating"
    rewrite._save_session(session)
    saved = rewrite._session_path(session["id"])
    before = saved.read_bytes()
    assert rewrite.get_session(session["id"])["status"] == "interrupted"
    assert not rewrite.get_session(session["id"])["usage_known"]
    with pytest.raises(rewrite.RewriteError, match="不能重放"):
        rewrite.continue_session(session["id"], "继续")
    assert saved.read_bytes() == before
    assert len(backend.calls) == 1


def test_estimated_budget_refusal_persists_failed_session_without_call(workspace):
    _, path, backend, fs = workspace
    fs.pricing["rewrite-test"] = ModelPrice(input_cache_hit=1, input_cache_miss=1, output=1)
    fs.run.max_cost_usd = 0.000001
    before = path.read_bytes()
    session = rewrite.start_session("cache", "本轮语境")
    assert session["status"] == "failed"
    assert "run.max_cost_usd" in session["errors"][-1]
    assert rewrite.get_session(session["id"])["errors"] == session["errors"]
    assert backend.calls == []
    assert _calls() == []
    assert path.read_bytes() == before


def test_actual_over_budget_records_cost_and_stops_adoption(workspace):
    _, path, backend, fs = workspace
    fs.pricing["rewrite-test"] = ModelPrice(input_cache_hit=1, input_cache_miss=1, output=1)
    fs.pricing_schedule.off_peak_multiplier = 1
    fs.run.max_cost_usd = 0.5
    backend.usage = Usage("rewrite-test", 1_000_000, 0, 1_000_000, 10)
    before = path.read_bytes()
    session = rewrite.start_session("cache", "本轮语境")
    assert session["status"] == "failed"
    assert session["cost"] > 1
    assert _calls()[0]["error"]
    assert _calls()[0]["cost_usd"] == session["cost"]
    with pytest.raises(rewrite.RewriteError, match="没有可采用"):
        rewrite.apply_revision(session["id"])
    assert path.read_bytes() == before


def test_continuation_reserves_cumulative_session_budget(workspace, monkeypatch):
    _, _, backend, fs = workspace
    fs.pricing["rewrite-test"] = ModelPrice(input_cache_hit=1, input_cache_miss=1, output=1)
    fs.pricing_schedule.off_peak_multiplier = 1
    fs.run.max_cost_usd = 0.1
    backend.usage = Usage("rewrite-test", 50_000, 0, 50_000)
    monkeypatch.setattr(rewrite, "estimate_ingest_cost", lambda *args: 0.06)
    session = rewrite.start_session("cache", "第一轮")
    assert session["status"] == "draft"
    assert session["cost"] == pytest.approx(0.05)
    session = rewrite.continue_session(session["id"], "第二轮")
    assert session["status"] == "failed"
    assert len(backend.calls) == 1
    assert "run.max_cost_usd" in session["errors"][-1]


@pytest.mark.parametrize("known", [True, False])
def test_model_failure_records_actual_or_unknown_usage(workspace, known):
    _, path, backend, fs = workspace
    fs.pricing["rewrite-test"] = ModelPrice(input_cache_hit=1, input_cache_miss=1, output=1)
    usage = Usage("rewrite-test", 100, 0, 100, 10) if known else None
    backend.error = LLMError("上游 HTTP 403，禁止访问", usage)
    before = path.read_bytes()
    session = rewrite.start_session("cache", "本轮语境")
    assert session["status"] == "failed"
    assert "403" in session["errors"][-1]
    assert session["usage_known"] is known
    calls = _calls()
    assert len(calls) == 1
    assert calls[0]["error"] == "上游 HTTP 403，禁止访问"
    assert bool(calls[0]["usage_known"]) is known
    assert path.read_bytes() == before
    if not known:
        session = rewrite.continue_session(session["id"], "显式继续也不能绕过未知费用")
        assert session["status"] == "failed"
        assert len(backend.calls) == 1
        assert "用量未知" in session["errors"][-1]


def test_model_cannot_overwrite_personal_sections(workspace):
    _, path, backend, _ = workspace
    before = path.read_bytes()
    backend.replies[0]["body"] = "## 个人笔记\n替用户编造笔记。"
    session = rewrite.start_session("cache", "测试保护")
    assert session["status"] == "failed"
    assert "不能包含个人笔记" in session["errors"][-1]
    assert _calls()[0]["error"]
    assert path.read_bytes() == before


def test_session_identifier_and_trashed_page_are_rejected(workspace):
    with pytest.raises(rewrite.RewriteError, match="ID 无效"):
        rewrite.get_session("../../outside")
    with pytest.raises(rewrite.RewriteError, match="已回收"):
        rewrite.start_session("trashed", "不能自动恢复")


def test_delete_session_keeps_page_versions_and_usage(workspace):
    _, path, _, _ = workspace
    session = rewrite.start_session("cache", "临时上下文")
    rewrite.apply_revision(session["id"])
    page_before = path.read_bytes()
    versions_before = rewrite.history("cache")
    usage_before = _calls()
    result = rewrite.delete_session(session["id"])
    assert result == {"id": session["id"], "slug": "cache", "status": "deleted"}
    assert rewrite.list_sessions("cache") == []
    with pytest.raises(rewrite.RewriteError, match="不存在"):
        rewrite.get_session(session["id"])
    assert path.read_bytes() == page_before
    assert rewrite.history("cache") == versions_before
    assert _calls() == usage_before


def test_clear_history_keeps_page_session_draft_and_usage(workspace):
    _, path, _, _ = workspace
    session = rewrite.start_session("cache", "保留当前正文")
    applied = rewrite.apply_revision(session["id"])
    page_before = path.read_bytes()
    usage_before = _calls()
    result = rewrite.clear_history("缓存")
    assert result == {"slug": "cache", "status": "cleared", "deleted": 2}
    assert rewrite.history("cache") == []
    persisted = rewrite.get_session(session["id"])
    assert persisted["draft"] == applied["draft"]
    assert persisted["messages"] == applied["messages"]
    assert persisted["history_cleared"]
    assert "revision_id" not in persisted
    assert path.read_bytes() == page_before
    assert _calls() == usage_before
    assert rewrite.clear_history("cache")["deleted"] == 0
    with pytest.raises(rewrite.RewriteError, match="不存在"):
        rewrite.restore_revision("cache", applied["revision_id"])


def test_generation_lock_rejects_session_and_history_deletion(workspace):
    _, path, _, _ = workspace
    session = rewrite.start_session("cache", "预留记录")
    rewrite.apply_revision(session["id"])
    page_before = path.read_bytes()
    history_before = rewrite.history("cache")
    with rewrite._lock():
        with pytest.raises(rewrite.RewriteError, match="正在进行"):
            rewrite.delete_session(session["id"])
        with pytest.raises(rewrite.RewriteError, match="正在进行"):
            rewrite.clear_history("cache")
    assert rewrite.get_session(session["id"])["status"] == "applied"
    assert rewrite.history("cache") == history_before
    assert path.read_bytes() == page_before


def test_interrupted_session_can_be_explicitly_deleted(workspace):
    _, _, _, _ = workspace
    session = rewrite.start_session("cache", "已中断")
    session["status"] = "generating"
    rewrite._save_session(session)
    assert rewrite.get_session(session["id"])["status"] == "interrupted"
    rewrite.delete_session(session["id"])
    assert rewrite.list_sessions("cache") == []


def test_rewrite_uses_original_text_and_marks_its_real_completeness(workspace):
    _, _, backend, fs = workspace
    original = "完整正文。" * 100 + "末尾证据不能被处理窗口隐藏。"
    conn = db.connect()
    try:
        item_id, _, _ = db.persist_material(conn, db.NewItem(
            source_id=None, url="https://example.test/full", url_canonical="https://example.test/full",
            content_hash=db.text_fingerprint(original), title="完整来源", author=None, summary=None,
            content="只保留在处理窗口的开头", original_text=original, extract_method="manual_text",
            published_at=None, completeness="full",
        ), datetime.now(UTC))
        conn.commit()
    finally:
        conn.close()
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    page = wiki.pages["cache"]
    page.meta["sources"] = [item_id]
    wiki.save_metadata(page)
    fs.wiki.ingest_max_chars = len(original) + 100
    rewrite.start_session("cache", "核对末尾的证据")
    source = json.loads(backend.calls[0][1]["content"])["sources"][0]
    assert source["content"] == original
    assert source["completeness"] == "full" and not source["truncated"]


def test_temporary_context_can_retrieve_an_unlinked_existing_concept(workspace):
    _, _, backend, _ = workspace
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    wiki.save_page(Page(
        "redis", {"type": "concept", "title": "Redis", "sources": []},
        "# Redis\n\n一个尚未与缓存页连接的现有知识页。\n",
    ))
    rewrite.start_session("cache", "请结合 Redis 的语境重新解释")
    payload = json.loads(backend.calls[0][1]["content"])
    assert "redis" in {page["slug"] for page in payload["related_pages"]}
    assert "trashed" not in {page["slug"] for page in payload["related_pages"]}
