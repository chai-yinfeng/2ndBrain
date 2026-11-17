"""来源身份迁移、文本编辑与目录隐藏的隔离回归，不访问网络或个人配置。"""

from __future__ import annotations

import json
import multiprocessing
import threading

import pytest

from secondbrain import config, db, sources_file
from secondbrain import source_management as management
from secondbrain.models import SourceConfig

PERSONAL = """# 我的来源，不能被整份重排
sources:
  # 自选栏目
  - id: personal # 身份注释
    name: 我的名称
    type: rss
    url: https://example.org/old.xml # 采集注释
    homepage: https://example.org/blog/
    tags:
      # 人工标签说明
      - 人工
    trust: 5 # 人工信任
    role: expansion
    enabled: false

  # 第二个来源必须保持原样
  - id: second
    type: rss
    url: https://example.net/feed.xml
"""
CATALOG = """# 完整参考目录
sources:
  - id: catalog
    type: rss
    url: https://example.org/new.xml
    homepage: https://example.org/blog/
  - id: second
    type: rss
    url: https://example.net/feed.xml
"""


@pytest.fixture
def environment(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv("SB_CONFIG_DIR", str(directory))
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    (directory / "sources.yaml").write_text(PERSONAL, encoding="utf-8")
    (directory / "sources.example.yaml").write_text(CATALOG, encoding="utf-8")
    conn = db.connect()
    db.upsert_sources(conn, config.load_sources())
    conn.execute(
        "UPDATE sources SET last_fetch_status='failed', last_fetch_message='历史错误', "
        "homepage='https://example.org/inferred/' WHERE id='personal'"
    )
    conn.execute(
        "INSERT INTO items(id,source_id,url,url_canonical,content_hash,title,extract_method,"
        "fetched_at,reading_state,read_at,original_text) "
        "VALUES(41,'personal','https://example.org/post','https://example.org/post',"
        "'hash','已导入材料','feed','2026-11-01','read','2026-11-02','完整正文')"
    )
    conn.execute(
        "INSERT INTO item_sources(id,item_id,source_id,kind,locator,completeness,method,"
        "imported_at,metadata_json) VALUES(51,41,'personal','subscription',"
        "'https://example.org/post','full','feed','2026-11-01',?)",
        (json.dumps({"nested": {"source_id": "personal"}, "title": "personal"}),),
    )
    conn.execute(
        "INSERT INTO feedback(id,item_id,kind,note,ts) "
        "VALUES(61,41,'gained','我的笔记','2026-11-02')"
    )
    conn.execute("INSERT INTO wiki_ingests(item_id,ts,pages) VALUES(41,'2026-11-02','[\"概念\"]')")
    conn.execute("INSERT INTO reading_list(item_id,added_at) VALUES(41,'2026-11-02')")
    conn.execute(
        "INSERT INTO source_analyses(source_id,input_hash,data,model,ts) "
        "VALUES('personal','input','{\"topics\":[\"AI\"]}','fake','2026-11-02')"
    )
    conn.commit()
    conn.close()
    note = tmp_path / "data" / "wiki" / "notes" / "41.md"
    note.parent.mkdir(parents=True)
    note.write_text("个人手写笔记", encoding="utf-8")
    return directory, note


def test_update_entry_keeps_other_sources_order_and_comments():
    original_second = sources_file.entry_text(PERSONAL, "second")
    result = sources_file.update_entry(
        PERSONAL, "personal", {"url": "https://example.org/new.xml", "name": "名称 # 一\n二"}
    )
    assert sources_file.entry_text(result, "second") == original_second
    assert [source.id for source in config.parse_sources_text(result)] == ["personal", "second"]
    assert "# 身份注释" in result and "# 采集注释" in result
    assert "# 人工标签说明" in result and "trust: 5 # 人工信任" in result
    first = config.parse_sources_text(result)[0]
    assert first.name == "名称 # 一\n二"
    assert first.url == "https://example.org/new.xml" and first.tags == ["人工"]


def test_update_entry_handles_multiline_and_null_values():
    text = PERSONAL.replace("    name: 我的名称\n", "    name: >\n      我的\n      名称\n")
    text = text.replace("    homepage: https://example.org/blog/", "    homepage: # 主页说明")
    result = sources_file.update_entry(
        text, "personal", {"name": "新名称", "tags": ["新标签"], "homepage": "https://example.org/"}
    )
    assert "# 人工标签说明" in result and "# 主页说明" in result
    first = config.parse_sources_text(result)[0]
    assert first.name == "新名称" and first.tags == ["新标签"]
    assert first.homepage == "https://example.org/"
    assert first.trust == 5 and len(config.parse_sources_text(result)) == 2


def test_update_entry_adds_fields_without_consuming_next_entry():
    text = (
        "sources:\n  - id: first\n    type: rss\n    url: https://example.org/feed\n"
        "    tags:\n      - old\n  # 下一项\n  - id: next\n    type: rss\n"
        "    url: https://example.net/feed\n"
    )
    result = sources_file.update_entry(text, "first", {"name": "显示名", "tags": ["new"]})
    parsed = config.parse_sources_text(result)
    assert [source.id for source in parsed] == ["first", "next"]
    assert parsed[0].name == "显示名" and parsed[0].tags == ["new"]
    assert "# 下一项" in result


@pytest.mark.parametrize(
    "changes", [{"id": "second"}, {"trust": 9}, {"type": "arxiv"}, {"oops": 1}]
)
def test_update_entry_rejects_invalid_or_duplicate_fields(changes):
    with pytest.raises(config.ConfigError):
        sources_file.update_entry(PERSONAL, "personal", changes)
    with pytest.raises(config.ConfigError):
        sources_file.update_entry(PERSONAL, "missing", changes)


def test_rename_preserves_material_history_and_personal_records(environment):
    directory, note = environment
    result = management.update_source("personal", {"id": "renamed", "name": "新名称"})
    assert result.id == "renamed" and result.name == "新名称"
    assert [source.id for source in config.load_sources()] == ["renamed", "second"]
    conn = db.connect()
    try:
        assert conn.execute("SELECT id FROM sources ORDER BY id").fetchall()[0][0] == "renamed"
        assert conn.execute("SELECT 1 FROM sources WHERE id='personal'").fetchone() is None
        source = conn.execute("SELECT * FROM sources WHERE id='renamed'").fetchone()
        assert source["last_fetch_message"] == "历史错误" and source["trust"] == 5
        assert source["homepage"] == "https://example.org/inferred/"
        item = conn.execute("SELECT * FROM items WHERE id=41").fetchone()
        assert item["source_id"] == "renamed" and item["original_text"] == "完整正文"
        assert item["read_at"] == "2026-11-02" and item["reading_state"] == "read"
        origin = conn.execute("SELECT * FROM item_sources WHERE id=51").fetchone()
        assert origin["source_id"] == "renamed"
        assert json.loads(origin["metadata_json"]) == {
            "nested": {"source_id": "renamed"}, "title": "personal"
        }
        assert conn.execute("SELECT note FROM feedback WHERE id=61").fetchone()[0] == "我的笔记"
        assert conn.execute("SELECT item_id FROM wiki_ingests").fetchone()[0] == 41
        assert conn.execute("SELECT item_id FROM reading_list").fetchone()[0] == 41
        assert conn.execute("SELECT source_id FROM source_analyses").fetchone()[0] == "renamed"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert note.read_text(encoding="utf-8") == "个人手写笔记"
    assert "# 人工标签说明" in (directory / "sources.yaml").read_text(encoding="utf-8")


def test_edit_only_passed_fields_and_can_edit_unfetched_source(environment):
    management.update_source("personal", {"homepage": "https://example.org/new/", "enabled": True})
    first = config.load_sources()[0]
    assert first.enabled and first.homepage == "https://example.org/new/"
    assert first.name == "我的名称" and first.trust == 5 and first.tags == ["人工"]
    updated = management.update_source("second", {"name": "尚未抓取来源", "trust": 2})
    assert updated.name == "尚未抓取来源" and updated.trust == 2


@pytest.mark.parametrize("changes", [{"id": "second"}, {"id": "INVALID"}, {"trust": 0}])
def test_invalid_update_leaves_both_stores_unchanged(environment, changes):
    directory, _ = environment
    with pytest.raises(config.ConfigError):
        management.update_source("personal", changes)
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL
    conn = db.connect()
    try:
        assert conn.execute("SELECT source_id FROM items WHERE id=41").fetchone()[0] == "personal"
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    finally:
        conn.close()


def test_rename_rejects_historical_database_identity(environment):
    directory, _ = environment
    conn = db.connect()
    db.upsert_sources(conn, [SourceConfig(id="historical", type="rss", url="https://example.org/old")])
    conn.close()
    with pytest.raises(config.ConfigError, match="历史"):
        management.update_source("personal", {"id": "historical"})
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL


@pytest.mark.parametrize("after_replace", [False, True])
def test_yaml_failure_rolls_back_database_and_restores_original(
    environment, monkeypatch, after_replace
):
    directory, _ = environment
    real_write = sources_file.write

    def fail_write(text):
        if after_replace:
            real_write(text)
        raise OSError("模拟 YAML 写入失败")

    monkeypatch.setattr(sources_file, "write", fail_write)
    with pytest.raises(OSError, match="YAML"):
        management.update_source("personal", {"id": "renamed", "trust": 1})
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL
    conn = db.connect()
    try:
        assert conn.execute("SELECT source_id FROM items WHERE id=41").fetchone()[0] == "personal"
        assert conn.execute("SELECT trust FROM sources WHERE id='personal'").fetchone()[0] == 5
        assert conn.execute("SELECT source_id FROM source_analyses").fetchone()[0] == "personal"
    finally:
        conn.close()


def test_commit_failure_compensates_yaml_and_identity(environment, monkeypatch):
    directory, _ = environment
    real_connect = db.connect

    class FailedCommit:
        def __init__(self):
            self.connection = real_connect()

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise OSError("模拟提交失败")

    monkeypatch.setattr(db, "connect", FailedCommit)
    with pytest.raises(OSError, match="提交"):
        management.update_source("personal", {"id": "renamed"})
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL
    conn = real_connect()
    try:
        row = conn.execute("SELECT source_id FROM item_sources WHERE id=51").fetchone()
        assert row[0] == "personal"
        assert conn.execute("SELECT source_id FROM source_analyses").fetchone()[0] == "personal"
        assert conn.execute("SELECT 1 FROM sources WHERE id='renamed'").fetchone() is None
    finally:
        conn.close()


def test_database_failure_never_replaces_yaml(environment):
    directory, _ = environment
    conn = db.connect()
    conn.execute(
        "CREATE TRIGGER refuse_edit BEFORE UPDATE ON sources "
        "BEGIN SELECT RAISE(ABORT, '模拟数据库失败'); END"
    )
    conn.commit()
    conn.close()
    with pytest.raises(Exception, match="数据库"):
        management.update_source("personal", {"name": "不会保存"})
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL


def test_repair_preview_and_apply_preserve_manual_identity(environment, monkeypatch):
    directory, _ = environment
    monkeypatch.setattr(config, "load_settings", lambda: {})
    candidate = SourceConfig(
        id="catalog", name="目录名称", type="hf_papers", url="https://example.org/api/papers",
        homepage="https://example.org/blog/", trust=1, tags=["机器标签"], role="core",
    )
    discovered = []

    def discover(location, client, settings):
        discovered.append(location)
        return {
            "homepage": location, "title": "目录名称", "method": "hf_papers", "source": candidate,
            "entries": [], "warnings": [], "status": "empty", "matched_source_id": "catalog",
            "matched_from": "catalog",
        }

    monkeypatch.setattr(management, "discover_source", discover)
    messages = []
    preview = management.repair_source("personal", messages.append)
    assert not preview["applied"] and preview["changes"]["type"] == "hf_papers"
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL
    result = management.repair_source("personal", messages.append, apply=True)
    assert result["applied"] and result["status"] == "empty"
    first = config.load_sources()[0]
    assert first.id == "personal" and first.type == "hf_papers"
    assert first.name == "我的名称" and first.tags == ["人工"]
    assert first.trust == 5 and first.role == "expansion" and not first.enabled
    assert [source.id for source in config.load_sources()] == ["personal", "second"]
    assert discovered == ["https://example.org/blog/"] * 2


@pytest.mark.parametrize("status", ["failed", "manual", "rejected"])
def test_failed_repair_never_writes_even_when_candidate_exists(environment, monkeypatch, status):
    directory, _ = environment
    monkeypatch.setattr(config, "load_settings", lambda: {})
    candidate = SourceConfig(id="catalog", type="rss", url="https://example.org/new.xml")
    monkeypatch.setattr(management, "discover_source", lambda *args: {
        "homepage": "https://example.org/blog/", "title": "失败", "method": "rss",
        "source": candidate, "entries": [], "warnings": ["适配器失败"], "status": status,
        "matched_source_id": "catalog", "matched_from": "catalog",
    })
    result = management.repair_source("personal", lambda message: None, apply=True)
    assert not result["applied"] and result["warnings"] == ["适配器失败"]
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL


def test_catalog_hiding_does_not_delete_followed_sources_or_materials(environment):
    directory, note = environment
    management.hide_catalog("second")  # 已关注来源也只隐藏目录显示。
    assert management.catalog_hidden_ids() == {"second"}
    management.hide_catalog("second")
    with pytest.raises(config.ConfigError):
        management.hide_catalog("not-in-example")
    management.clear_catalog()
    assert management.catalog_hidden_ids() == {"catalog", "second"}
    assert (directory / "sources.yaml").read_text(encoding="utf-8") == PERSONAL
    assert (directory / "sources.example.yaml").read_text(encoding="utf-8") == CATALOG
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
        assert conn.execute("SELECT source_id FROM items WHERE id=41").fetchone()[0] == "personal"
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    finally:
        conn.close()
    management.restore_catalog()
    assert management.catalog_hidden_ids() == set()
    assert note.read_text(encoding="utf-8") == "个人手写笔记"


def _hold_source_lock(directory, entered, release):
    import os

    os.environ["SB_CONFIG_DIR"] = directory
    with management.source_lock():
        entered.set()
        release.wait(10)


def test_source_lock_serializes_threads_and_is_reentrant(environment):
    started = threading.Event()
    acquired = threading.Event()

    def contender():
        started.set()
        with management.source_lock():
            acquired.set()

    with management.source_lock(), management.source_lock():
        thread = threading.Thread(target=contender)
        thread.start()
        assert started.wait(2)
        assert not acquired.wait(0.05)
    thread.join(2)
    assert not thread.is_alive() and acquired.is_set()


def test_source_lock_serializes_processes(environment):
    directory, _ = environment
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_source_lock, args=(str(directory), entered, release))
    process.start()
    try:
        assert entered.wait(10)
        import fcntl

        with (directory / "sources.lock").open("a+") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_changing_block_tags_preserves_comments_but_not_hashes_inside_strings():
    text = PERSONAL.replace(
        "      - 人工\n", "      - 人工 # 逐项说明\n      - 'tag # literal'\n"
    )
    result = sources_file.update_entry(text, "personal", {"tags": ["新标签"]})
    assert "# 人工标签说明" in result and "# 逐项说明" in result
    assert "literal" not in result
    assert config.parse_sources_text(result)[0].tags == ["新标签"]


def test_update_entry_supports_flow_mappings_without_rewriting_other_entries():
    text = (
        'sources: [{id: one, type: rss, url: "https://example.org/feed"}, '
        '{id: two, type: rss, url: "https://example.net/feed"}] # 完整注释\n'
    )
    result = sources_file.update_entry(text, "one", {"name": "名称", "trust": 4})
    assert '{id: two, type: rss, url: "https://example.net/feed"}' in result
    assert "# 完整注释" in result
    first, second = config.parse_sources_text(result)
    assert first.name == "名称" and first.trust == 4 and second.id == "two"


def test_repair_can_fill_only_a_missing_name_from_discovered_title(environment, monkeypatch):
    directory, _ = environment
    (directory / "sources.yaml").write_text(
        PERSONAL.replace("    name: 我的名称\n", ""), encoding="utf-8"
    )
    monkeypatch.setattr(config, "load_settings", lambda: {})
    current = config.load_sources()[0]
    monkeypatch.setattr(management, "discover_source", lambda *args: {
        "homepage": current.homepage, "title": "有据标题", "method": "rss",
        "source": current, "entries": [], "warnings": [], "status": "empty",
        "matched_source_id": current.id, "matched_from": "followed",
    })
    result = management.repair_source("personal", lambda message: None, apply=True)
    assert result["changes"] == {"name": "有据标题"}
    updated = config.load_sources()[0]
    assert updated.name == "有据标题" and updated.id == "personal" and updated.tags == ["人工"]
