"""材料管理与手动同步的 Web 边界；只用隔离数据和 fake 模型。"""

import re
from contextlib import closing

from fastapi.testclient import TestClient

from secondbrain import config, db, ops
from secondbrain.web.app import create_app
from secondbrain.wiki.store import open_wiki


def reference(title, state="later"):
    result = ops.add_manual(
        [f"https://materials.example/{title}"], lambda _: None,
        reference_only=True, title=title, state=state,
    )
    return result["results"][0]["item_id"]


def test_material_batch_preview_and_restore_preserve_identity_and_queue(monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    read_id, later_id = reference("read"), reference("later")
    with closing(db.connect()) as conn:
        db.set_reading_state(conn, read_id, "read")
    with TestClient(create_app()) as client:
        before = client.post("/materials/hide-read")
        assert before.status_code == 200
        with closing(db.connect()) as conn:
            assert not db.lifecycle_states(conn)[read_id]["hidden"]
        client.post("/materials/hide-read", data={"confirm": "yes"})
        assert f'data-item-id="{read_id}"' not in client.get("/materials").text
        assert f'data-item-id="{read_id}"' in client.get("/materials?view=hidden").text
        request = {"action": "trash", "item_ids": str(later_id)}
        assert client.post("/materials/manage", data=request).status_code == 200
        with closing(db.connect()) as conn:
            assert not db.is_trashed(conn, later_id)
        applied = client.post(
            "/materials/manage", data={**request, "confirm": "yes"}, follow_redirects=False
        )
        assert applied.status_code == 303
        assert client.get(f"/item/{later_id}").status_code == 200
        assert client.post(f"/reading/{later_id}/add").status_code == 409
        with closing(db.connect()) as conn:
            assert later_id not in {r["id"] for r in db.reading_list_items(conn)}
        client.post("/materials/manage", data={"action": "restore", "item_ids": str(later_id)})
        with closing(db.connect()) as conn:
            assert later_id in {r["id"] for r in db.reading_list_items(conn)}
            assert db.item_detail(conn, read_id)["reading_state"] == "read"
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
        assert client.get("/materials?completeness=missing&view=all").status_code == 200
        assert client.get("/materials?view=invalid").status_code == 400


def test_web_sync_requires_preview_and_preserves_notes_on_trash_restore(monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    notes = "个人学习记录，不能随材料回收丢失。"
    result = ops.add_manual(
        [], lambda _: None, text="KV cache stores attention keys and values. " * 30,
        title="Lifecycle material", state="read", learning_notes=notes,
    )
    item_id = result["results"][0]["item_id"]
    wiki = open_wiki(config.wiki_dir(), config.raw_dir())
    material = wiki.find(f"material-{item_id}")
    body = material.body
    note_path = wiki.root / "notes" / f"material-{item_id}-notes.md"
    note_bytes = note_path.read_bytes()
    with closing(db.connect()) as conn:
        calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    app = create_app()
    with TestClient(app) as client:
        assert client.post("/wiki/sync").status_code == 422
        for action in ("trash", "restore"):
            client.post("/materials/manage", data={
                "action": action, "item_ids": str(item_id), "confirm": "yes",
            })
            preview = client.get("/wiki/sync")
            assert preview.status_code == 200
            token = re.search(r'name="token" value="([^"]+)"', preview.text).group(1)
            response = client.post("/wiki/sync", data={"token": token}, follow_redirects=False)
            assert response.status_code == 303
            task = app.state.tasks.get("wiki-sync")
            task.thread.join(20)
            assert task.status == "done", task.error
            assert task.result["failures"] == 0, task.result
            graph = client.get("/wiki/graph.json").json()
            assert (f"material-{item_id}" in {n["id"] for n in graph["nodes"]}) == (
                action == "restore"
            )
            wiki.reload_pages()
            assert wiki.find(f"material-{item_id}").body == body
            assert note_path.read_bytes() == note_bytes
        with closing(db.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == calls


def test_merged_material_redirects_old_links_and_rejects_stale_writes(monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    canonical, duplicate = reference("canonical"), reference("duplicate")
    with closing(db.connect()) as conn:
        conn.execute(
            "INSERT INTO material_aliases VALUES (?, ?, ?)",
            (duplicate, canonical, "2026-11-18T00:00:00+00:00"),
        )
        conn.commit()
        before = dict(db.item_detail(conn, canonical))
    with TestClient(create_app()) as client:
        response = client.get(f"/item/{duplicate}", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == f"/item/{canonical}"
        assert f'data-item-id="{duplicate}"' not in client.get("/materials").text
        assert client.post(f"/reading/{duplicate}/add").status_code == 409
        assert client.post(
            f"/item/{duplicate}/reading", data={"state": "reading"}
        ).status_code == 409
        assert client.post(
            f"/item/{duplicate}/reading", data={"state": "read"}
        ).status_code == 409
    with closing(db.connect()) as conn:
        assert dict(db.item_detail(conn, canonical)) == before
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
