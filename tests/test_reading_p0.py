import re
from datetime import date

from fastapi.testclient import TestClient
from test_reading_semantics_web import count_calls, settle
from test_reading_semantics_web import environment as environment  # noqa: F401
from test_reading_workflow import add_material
from test_reading_workflow import workflow as workflow  # noqa: F401

from secondbrain import config, db, ops
from secondbrain.web.app import create_app
from secondbrain.wiki.store import Page, new_meta, open_wiki


def _revision(body):
    return re.search(r'name="revision" value="([0-9a-f]+)"', body)[1]


def _token(body):
    return re.search(r'name="token" value="([0-9a-f]+)"', body)[1]


def test_mark_read_one_click_and_save_pair_with_stale_rejection(environment):
    item = ops.add_manual([], lambda _: None, state="later", text="Source facts")
    iid = item["results"][0]["item_id"]
    app = create_app()
    with TestClient(app) as client:
        assert "标为已读" in client.get("/later").text
        result = client.post(f"/item/{iid}/reading", data={"state": "read", "return_to": "later"})
        assert result.url.path == "/later" and f'data-item-id="{iid}"' not in result.text
        page = client.get(f"/item/{iid}").text
        assert '<details id="notes">' in page
        notes_url = f"/item/{iid}/notes"
        saved = client.post(notes_url, data={
            "revision": _revision(page), "notes": "current notes",
            "discussion_url": "https://chat.example/current",
        })
        assert '<details id="notes" open>' in saved.text and "已保存到本地" in saved.text
        stale = client.post(notes_url, data={
            "revision": _revision(page), "notes": "stale draft",
            "discussion_url": "https://chat.example/stale",
        })
        assert stale.status_code == 409 and "stale draft" in stale.text
        assert client.post(notes_url, data={"notes": "legacy blind attempt"}).status_code == 409
        record = ops.reading_record(iid)
        assert record.notes == "current notes" and record.discussion_url.endswith("/current")
        path = config.wiki_dir() / "notes" / f"material-{iid}-notes.md"
        path.write_text("edited from an external Markdown editor", encoding="utf-8")
        assert client.post(notes_url, data={
            "revision": record.revision, "notes": "would overwrite the external edit",
        }).status_code == 409
        assert ops.material_notes(iid) == "edited from an external Markdown editor"
        assert count_calls() == 0 and environment == []


def test_batch_confirmation_binds_selection_and_reports_each_item(environment):
    app = create_app()
    with TestClient(app) as client:
        ids = []
        for text in ["Source one facts", "Source two facts", "Source three facts"]:
            client.post("/add", data={"text": text, "state": "read", "ingest": "1"})
            ids.append(settle(app, "manual_add")["results"][0]["item_id"])
        assert count_calls() == 0
        first, second, unselected = ids
        assert 'name="item_ids"' in client.get("/wiki").text
        endpoint = "/tasks/wiki_ingest"
        unconfirmed = client.post(endpoint, data={"confirm": "yes", "item_ids": [first]})
        assert unconfirmed.status_code == 409
        preview = client.post(endpoint, data={"item_ids": [first, second]})
        assert "Source one facts" in preview.text and "Source three facts" not in preview.text
        assert "fake" in preview.text and "不提供独立个人笔记" in preview.text
        assert count_calls() == 0 and app.state.tasks.get("wiki_ingest") is None
        token = _token(preview.text)
        client.post(f"/item/{second}/reading", data={"state": "unread"})
        actual = client.post(endpoint, data={
            "confirm": "yes", "token": token, "item_ids": [unselected], "include_notes": "1",
        }, follow_redirects=False)
        assert actual.status_code == 303
        task = app.state.tasks.get("wiki_ingest")
        task.thread.join(20)
        assert task.status == "partial", task.error
        result = task.result
        assert [(item["id"], item["status"]) for item in result["items"]] == [
            (first, "processed"), (second, "failed"),
        ]
        assert count_calls() == 1
        results = client.get("/wiki").text
        assert "Source one facts" in results and "材料未读，不能整理" in results
        assert client.post(endpoint, data={"confirm": "yes", "token": token}).status_code == 409
        conn = db.connect()
        try:
            assert db.item_detail(conn, unselected)["processing_status"] == "pending"
        finally:
            conn.close()


def test_no_notes_filters_known_note_blocks_and_cache_is_mode_specific(workflow):
    _, backend, _ = workflow
    iid = add_material()
    ops.register_read(iid, notes="PRIVATE-FILE-768", discussion_url="https://chat.example/private")
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    wiki.save_page(Page("kv-cache", new_meta("KV cache", "concept", date.today()),
                        "# KV cache\n\n## 个人笔记\nPRIVATE-WIKI-349\n\n## 整理\npublic fact\n"))
    first = ops.ingest_items([iid], lambda _: None, include_notes=False)
    assert not first.failures and not first.cached
    payload = backend.calls[-1][0][-1]["content"]
    assert "PRIVATE-FILE" not in payload and "PRIVATE-WIKI" not in payload
    assert "public fact" in payload and "chat.example/private" not in payload
    ops.material_notes(iid, "PRIVATE-FILE-NEW")
    assert ops.ingest_items([iid], lambda _: None).cached == [iid]
    second = ops.ingest_items([iid], lambda _: None, include_notes=True)
    assert not second.failures and not second.cached
    payload = backend.calls[-1][0][-1]["content"]
    assert "PRIVATE-FILE-NEW" in payload and "PRIVATE-WIKI-349" in payload
    assert "chat.example/private" not in payload
    assert ops.material_notes(iid) == "PRIVATE-FILE-NEW"
