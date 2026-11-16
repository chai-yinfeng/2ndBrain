"""M13：统一材料身份、原件、来源、阅读状态及旧库迁移；全部使用临时目录。"""

import json
import shutil
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from conftest import fast_settings, make_pdf

from secondbrain import config, db, ingest, ops
from secondbrain.models import RawEntry, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.urls import content_hash

NOW = datetime(2026, 11, 16, tzinfo=UTC)
URL = "https://example.org/paper"
DOI = "10.1234/material.1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(ops, "HttpClient", lambda *_args, **_kwargs: HttpClient(
        fast_settings(), transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ))
    return tmp_path


def material(url=URL, text="Original material body", **kwargs):
    title = kwargs.pop("title", "A material")
    return db.NewItem(
        source_id=None, url=url, url_canonical=url,
        content_hash=content_hash(title, text), title=title,
        author=None, summary=None, content=text, extract_method="manual_text",
        published_at=None, **kwargs,
    )


def add_result(*args, **kwargs):
    result = ops.add_manual(*args, echo=lambda _: None, **kwargs)["results"][0]
    assert result["status"] != "失败", result
    return result


def test_standalone_text_preserves_original_and_has_no_source(env):
    text = "  My own material\n\nThe entire original body.\n  "
    result = add_result([], text=text, title="Explicit title")
    conn = db.connect()
    try:
        row = db.item_detail(conn, result["item_id"])
        assert row["source_id"] is None and row["url"] == ""
        assert row["title"] == "Explicit title" and row["original_text"] == text
        assert row["reading_state"] == "unread" and row["read_at"] is None
        assert row["processing_status"] == "pending"
        assert row["content_completeness"] == "full" and not row["content_truncated"]
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM feedback").fetchone()[0] == 0
        origin, = db.material_origins(conn, row["id"])
        assert origin["kind"] == "text"
        assert (config.wiki_dir() / origin["text_path"]).read_text() == text
        assert "身份尚不确定" in result["message"]
    finally:
        conn.close()


def test_standalone_pdf_merges_with_doi_and_retains_both_originals(env):
    body = f"doi: {DOI}\nA complete local paper body."
    pdf = make_pdf([body])
    first = add_result([], pdf=pdf, title="Local filename is not identity")
    second = add_result([f"https://doi.org/{DOI}"], text=body, title="Other display title")
    assert second["item_id"] == first["item_id"]
    assert second["status"] == "已存在"
    conn = db.connect()
    try:
        row = db.item_detail(conn, first["item_id"])
        assert row["doi"] == DOI and row["reading_state"] == "unread"
        origins = db.material_origins(conn, row["id"])
        assert [r["kind"] for r in origins] == ["pdf", "text"]
        assert (config.wiki_dir() / origins[0]["original_path"]).read_bytes() == pdf
        assert body in (config.wiki_dir() / origins[0]["text_path"]).read_text()
        assert origins[1]["locator"] == f"https://doi.org/{DOI}"
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    finally:
        conn.close()


def test_default_import_preserves_reading_and_explicit_state_resets(env):
    first = add_result([], text="An independent reading state")
    iid = first["item_id"]
    conn = db.connect()
    db.set_reading_state(conn, iid, "reading")
    conn.close()
    add_result([], text="An independent reading state")
    conn = db.connect()
    assert db.item_detail(conn, iid)["reading_state"] == "reading"
    db.set_reading_state(conn, iid, "read")
    before = db.item_detail(conn, iid)["read_at"]
    conn.close()
    add_result([], text="An independent reading state", state="later")
    conn = db.connect()
    assert db.item_detail(conn, iid)["reading_state"] == "read"
    assert db.item_detail(conn, iid)["read_at"] == before
    assert db.feedback_state(conn, [iid])[iid] == "later"
    conn.close()
    add_result([], text="An independent reading state", state="unread")
    conn = db.connect()
    assert db.item_detail(conn, iid)["reading_state"] == "unread"
    assert db.item_detail(conn, iid)["read_at"] is None
    conn.close()


def test_ingest_is_independent_of_feedback_and_recommendations(env, monkeypatch):
    calls = []

    def ingest_items(ids, echo):
        calls.extend(ids)
        return ops.IngestSummary()

    monkeypatch.setattr(ops, "ingest_items", ingest_items)
    result = add_result([], text="Unread knowledge to organize", ingest=True)
    assert calls == [result["item_id"]]
    conn = db.connect()
    try:
        row = db.item_detail(conn, result["item_id"])
        assert row["reading_state"] == "unread" and row["status"] == "manual"
        for table in ("feedback", "assessments", "digests"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def test_content_window_and_pdf_page_limit_are_honest(env, monkeypatch):
    settings, filters, fetch = ops._settings()
    fetch = fetch.model_copy(update={"content_max_chars": 20, "pdf_max_pages": 1})
    monkeypatch.setattr(ops, "_settings", lambda: (settings, filters, fetch))
    text = "Beginning of original\n" + "Untruncated original tail. " * 10
    first = add_result([], text=text)
    pdf = make_pdf(["Page one text", "Page two text"])
    second = add_result([], pdf=pdf)
    conn = db.connect()
    try:
        row = db.item_detail(conn, first["item_id"])
        assert row["content"] == text[:20] and row["original_text"] == text
        assert row["content_truncated"] and row["content_completeness"] == "full"
        row = db.item_detail(conn, second["item_id"])
        origin, = db.material_origins(conn, row["id"])
        assert row["content_truncated"] and row["content_completeness"] == "partial"
        assert (origin["total_pages"], origin["extracted_pages"]) == (2, 1)
        assert (config.wiki_dir() / origin["original_path"]).read_bytes() == pdf
        assert "Page two" not in row["original_text"]
    finally:
        conn.close()


def test_page_limited_pdf_prefix_is_not_identity(env, monkeypatch):
    settings, filters, fetch = ops._settings()
    fetch = fetch.model_copy(update={"pdf_max_pages": 1})
    monkeypatch.setattr(ops, "_settings", lambda: (settings, filters, fetch))
    first = add_result([], pdf=make_pdf(["Common cover page", "Different work A"]))
    second = add_result([], pdf=make_pdf(["Common cover page", "Different work B"]))
    assert first["item_id"] != second["item_id"]
    duplicate = add_result([], pdf=make_pdf(["Common cover page", "Different work A"]))
    assert duplicate["item_id"] == first["item_id"]
    complete_short = add_result([], pdf=make_pdf(["Common cover page"]))
    assert complete_short["item_id"] not in (first["item_id"], second["item_id"])


def test_identity_conflict_and_canonical_alias_are_safe(env):
    conn = db.connect()
    try:
        first, _, _ = db.persist_material(conn, material(), NOW)
        second, _, _ = db.persist_material(
            conn, material("https://other.org/b", "Different work", doi=DOI), NOW
        )
        conn.commit()
        count = conn.execute("SELECT count(*) FROM item_sources").fetchone()[0]
        with pytest.raises(ValueError, match="身份冲突"):
            db.persist_material(conn, material(doi=DOI), NOW)
        assert conn.execute("SELECT count(*) FROM item_sources").fetchone()[0] == count
        assert first != second and db.item_detail(conn, first)["doi"] is None
        alias = "https://mirror.org/alias"
        merged, created, _ = db.persist_material(conn, material(alias), NOW)
        assert merged == first and not created
        assert db.item_by_canonical(conn, alias) == first
        merged, created, _ = db.persist_material(
            conn, material(alias, "Original material body with more details"), NOW
        )
        assert merged == first and not created
        assert len(db.material_origins(conn, first)) == 3
    finally:
        conn.close()


def test_more_complete_only_and_completion_preserves_read_state(env):
    conn = db.connect()
    item = material(text="A full original material", completeness="full")
    iid, _, _ = db.persist_material(conn, item, NOW)
    db.set_reading_state(conn, iid, "read")
    item = material(text="A much longer abstract. " * 100, completeness="abstract")
    item.extract_method = "abstract_only"
    _, created, improved = db.persist_material(conn, item, NOW)
    conn.commit()
    assert not created and not improved
    assert db.item_detail(conn, iid)["original_text"] == "A full original material"
    conn.close()
    method = ops.complete_item(iid, text="A full original material with appended details")
    assert method == "manual_text"
    conn = db.connect()
    try:
        assert db.item_detail(conn, iid)["original_text"].endswith("appended details")
        assert db.item_detail(conn, iid)["reading_state"] == "read"
        assert len(db.material_origins(conn, iid)) == 3
    finally:
        conn.close()


def test_subscription_duplicate_records_sources_and_upgrades_full_original(env, monkeypatch):
    body = "An actual complete subscribed article. " * 100
    html = f"<html><body><article><h1>Article</h1><p>{body}</p></article></body></html>"
    phase = [False]

    def fetcher(source, client, fs):
        return [RawEntry(
            url=URL, title="Subscribed article", published_at=NOW,
            feed_content=None if phase[0] else "<p>A feed excerpt</p>",
        )]

    monkeypatch.setitem(ingest.FETCHERS, "rss", fetcher)
    sources = [SourceConfig(id=s, type="rss", url=f"https://{s}.org/feed") for s in ("a", "b")]
    client = HttpClient(
        fast_settings(min_feed_content_chars=1, content_max_chars=100),
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text=html, headers={"content-type": "text/html"}
        )),
    )
    conn = db.connect()
    try:
        first = ingest.fetch_sources(conn, sources, client, client.settings, now=NOW)
        assert [(r.added, r.duplicates) for r in first] == [(1, 0), (0, 1)]
        phase[0] = True
        second = ingest.fetch_sources(conn, sources, client, client.settings, now=NOW)
        assert [(r.added, r.duplicates) for r in second] == [(0, 1), (0, 1)]
        row = db.item_detail(conn, 1)
        assert "complete subscribed article" in row["original_text"]
        assert len(row["original_text"]) > 100 and len(row["content"]) == 100
        assert row["reading_state"] == "unread" and row["status"] == "new"
        origins = db.material_origins(conn, 1)
        assert {o["source_id"] for o in origins} == {"a", "b"}
        assert len(origins) == 4
        assert (config.wiki_dir() / origins[-1]["raw_path"]).read_text() == html
        assert (config.wiki_dir() / origins[-1]["text_path"]).read_text() == row["original_text"]
    finally:
        conn.close()
        client.close()


def test_subscription_pdf_retains_downloaded_file(env, monkeypatch):
    pdf = make_pdf(["A full downloaded paper body. " * 20])
    monkeypatch.setitem(ingest.FETCHERS, "rss", lambda *args: [RawEntry(url=URL, title="PDF")])
    source = SourceConfig(id="pdf-feed", type="rss", url="https://example.org/feed")
    client = HttpClient(fast_settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=pdf, headers={"content-type": "application/pdf"}
        )
    ))
    conn = db.connect()
    try:
        result, = ingest.fetch_sources(conn, [source], client, client.settings, now=NOW)
        assert result.added == 1
        origin, = db.material_origins(conn, 1)
        assert (config.wiki_dir() / origin["original_path"]).read_bytes() == pdf
        assert origin["total_pages"] == origin["extracted_pages"] == 1
    finally:
        conn.close()
        client.close()


def test_identifier_discovery_does_not_use_references_or_titles():
    assert db.discover_ids(f"Own title\ndoi: {DOI}\nIntroduction\nBody") == (DOI, None)
    assert db.discover_ids(f"A discussion of https://doi.org/{DOI}") == (None, None)
    assert db.discover_ids(f"Own paper\nReferences\ndoi: {DOI}") == (None, None)
    assert db.discover_ids(f"doi: {DOI}\ndoi: 10.1234/other") == (None, None)
    assert db.discover_ids("arXiv: 2611.01234v2\nOwn title") == (None, "2611.01234")


def test_copied_v9_migration_preserves_identity_and_all_references(env):
    legacy = env / "legacy.db"
    old = sqlite3.connect(legacy)
    for script in db.MIGRATIONS[:9]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 9")
    old.execute(
        "INSERT INTO sources(id,type,trust,role,tags,max_age_days,enabled,backfill,"
        "needs_scraper,updated_at) VALUES('manual','manual',3,'core','[]',7,0,0,0,'x')"
    )
    old.execute(
        "INSERT INTO items(id,source_id,url,url_canonical,content_hash,title,content,"
        "extract_method,fetched_at,status,read_at,doi) "
        "VALUES(42,'manual',?,?,?,'Legacy paper','Legacy original text','manual_text',"
        "'2026-11-01T00:00:00Z','manual','2026-11-02T00:00:00Z',?)",
        (URL, URL, "legacy-hash", DOI),
    )
    old.execute("INSERT INTO feedback(item_id,kind,note,ts) VALUES(42,'gained','note','x')")
    old.execute(
        "INSERT INTO assessments(item_id,stage,model,data,score,created_at) "
        "VALUES(42,'L2','fake','{\"summary\":\"legacy\"}',8,'x')"
    )
    old.execute(
        "INSERT INTO digests(date,item_id,lane,rank,est_minutes,is_deep_read,created_at) "
        "VALUES('2026-11-02',42,'must_read',1,10,0,'x')"
    )
    old.execute(
        "INSERT INTO wiki_ingests(item_id,ts,pages) VALUES(42,'x',?)", (json.dumps(["old-paper"]),)
    )
    old.execute(
        "INSERT INTO item_ai(item_id,kind,data,model,created_at)"
        " VALUES(42,'summary','{}','fake','x')"
    )
    old.execute(
        "INSERT INTO digest_concepts(date,slug,title,est_minutes,related_item_id,created_at) "
        "VALUES('2026-11-02','concept','Concept',3,42,'x')"
    )
    old.commit()
    old.close()
    before = legacy.read_bytes()
    migrated = env / "copied.db"
    shutil.copyfile(legacy, migrated)
    conn = db.connect(migrated)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        row = db.item_detail(conn, 42)
        assert row["source_id"] is None and row["doi"] == DOI
        assert row["original_text"] == "Legacy original text"
        assert row["reading_state"] == "read" and row["processing_status"] == "processed"
        assert row["content_completeness"] == "partial"  # 旧库没有可靠的采集完整度证据
        assert db.feedback_for(conn, 42)[0]["note"] == "note"
        assert db.item_assessment(conn, 42, "L2")["summary"] == "legacy"
        assert conn.execute("SELECT item_id FROM digests").fetchone()[0] == 42
        assert db.ingested_ids(conn) == {42}
        pages = json.loads(conn.execute("SELECT pages FROM wiki_ingests").fetchone()[0])
        assert pages == ["old-paper"]
        assert conn.execute("SELECT related_item_id FROM digest_concepts").fetchone()[0] == 42
        assert db.get_ai(conn, 42, "summary")["_model"] == "fake"
        origin, = db.material_origins(conn, 42)
        assert origin["locator"] == URL and origin["source_id"] is None
        conn.execute("INSERT INTO feedback(item_id,kind,ts) VALUES(42,'clear','new')")
        conn.commit()
    finally:
        conn.close()
    assert legacy.read_bytes() == before  # 只迁移副本，不改原库


def test_url_pdf_keeps_exact_downloaded_bytes_and_text(env):
    pdf = make_pdf(["An exact original PDF from a URL. " * 20])
    client = HttpClient(fast_settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=pdf, headers={"content-type": "application/pdf"}
        )
    ))
    try:
        result = add_result([URL], client=client)
    finally:
        client.close()
    conn = db.connect()
    try:
        row = db.item_detail(conn, result["item_id"])
        origin, = db.material_origins(conn, row["id"])
        assert origin["kind"] == "url" and origin["locator"] == URL
        assert (config.wiki_dir() / origin["original_path"]).read_bytes() == pdf
        assert (config.wiki_dir() / origin["text_path"]).read_text() == row["original_text"]
    finally:
        conn.close()


def test_identity_does_not_follow_arxiv_text_in_an_unrelated_hostname():
    url = "https://notarxiv.org/abs/2611.01234"
    assert ops.manual_canonical(url) == url
    assert ops._url_ids(url) == (None, None)
    path = "https://example.org/arxiv.org/abs/2611.01234"
    assert ops.manual_canonical(path) == path
    assert ops._url_ids(path) == (None, None)


def test_content_upgrade_resets_processing_not_reading_or_history(env):
    conn = db.connect()
    try:
        iid, _, _ = db.persist_material(conn, material(text="Original body"), NOW)
        db.add_feedback(conn, iid, "gained", "Keep my feedback", NOW)
        db.record_ingest(conn, iid, ["material-1"], NOW)
        conn.execute("UPDATE items SET processing_status = 'processed' WHERE id = ?", (iid,))
        conn.commit()
    finally:
        conn.close()
    ops.complete_item(iid, text="Original body with extended details")
    conn = db.connect()
    try:
        row = db.item_detail(conn, iid)
        assert row["processing_status"] == "pending" and row["reading_state"] == "read"
        assert db.ingested_ids(conn) == {iid}
        assert db.feedback_for(conn, iid)[0]["note"] == "Keep my feedback"
    finally:
        conn.close()


def test_optional_pipeline_uses_feedback_from_sourceless_material(env):
    from dataclasses import replace

    from secondbrain.llm.fake import FakeBackend
    from secondbrain.models import FilterSettings
    from secondbrain.pipeline.run import run_pipeline
    from secondbrain.profile.loader import parse_profile

    liked = add_result([], text="A personally imported note", state="read", feedback="gained")
    conn = db.connect()
    try:
        db.add_feedback(conn, liked["item_id"], "gained", None, NOW)
        source = SourceConfig(id="optional", type="rss", url="https://example.org/rss")
        db.upsert_sources(conn, [source])
        candidate = replace(
            material(text="KV cache and inference serving blocks. " * 80),
            source_id=source.id,
        )
        candidate_id, _, _ = db.persist_material(conn, candidate, NOW)
        conn.commit()
        run_pipeline(
            conn, FilterSettings(), FakeBackend(), parse_profile(""),
            limit=None, now=NOW, today=NOW.date(), all_time=True,
        )
        assert db.item_assessment(conn, candidate_id, "L1") is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM digests WHERE item_id = ?", (candidate_id,)
        ).fetchone()[0] == 1
        assert db.item_detail(conn, liked["item_id"])["source_id"] is None
    finally:
        conn.close()
