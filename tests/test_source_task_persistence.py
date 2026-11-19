"""富类型发现结果不能阻止信源检查完成记录持久化。"""

from fastapi.testclient import TestClient

from secondbrain import source_management, sources_file
from secondbrain.models import SourceConfig
from secondbrain.web.app import create_app


def test_source_check_stays_completed_after_restart_and_can_be_cleared(monkeypatch):
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    source = SourceConfig(id="mine", type="rss", url="https://example.org/feed")
    sources_file.write("sources:\n" + sources_file.dump_entry(source) + "\n")
    monkeypatch.setattr(source_management, "repair_source", lambda *args, **kwargs: {
        "source_id": source.id, "status": "ready", "applied": False,
        "changes": {"name": "Discovered name"}, "warnings": [],
        "discovery": {"source": source, "entries": []},
    })
    app = create_app()
    with TestClient(app) as client:
        response = client.post("/sources/mine/repair")
        assert response.status_code == 200
        task = app.state.tasks.get("source-repair-mine")
        task.thread.join(5)
        assert task.status == "done"
    restarted = create_app()
    restored = restarted.state.tasks.get(task.id)
    assert restored.status == "done"
    assert restored.result["results"][0]["changes"] == {"name": "Discovered name"}
    with TestClient(restarted) as client:
        response = client.post(
            "/task-history/delete", data={"task_ids": task.id, "confirm": "yes"}
        )
        assert response.status_code == 200
        assert restarted.state.tasks.get(task.id) is None
        assert f'data-task-record="{task.id}"' not in client.get("/task-history").text
