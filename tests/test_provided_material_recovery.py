"""显式提供正文/PDF后不重复请求受限站点，仍补全同一材料。"""

import json
from contextlib import closing

import httpx
import pytest
from conftest import fast_settings, make_pdf

from secondbrain import db, ops
from secondbrain.net import HttpClient


@pytest.mark.parametrize("format", ["text", "pdf"])
def test_provided_content_upgrades_reference_without_network_or_losing_notes(
    tmp_path, monkeypatch, format
):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    url = "https://zhuanlan.zhihu.com/p/public-article"
    body = "Supplied complete article body, obtained by the reader."

    def no_network(request):
        pytest.fail("Explicit reader-provided content must not request the reference URL")

    with HttpClient(fast_settings(), transport=httpx.MockTransport(no_network)) as client:
        first = ops.add_manual(
            [url], lambda _: None, reference_only=True, state="reading", client=client
        )["results"][0]
        item_id = first["item_id"]
        ops.material_notes(item_id, "Keep my reading notes.")
        payload = {"text": body} if format == "text" else {"pdf": make_pdf([body])}
        result = ops.add_manual([url], lambda _: None, client=client, **payload)["results"][0]
    assert result["item_id"] == item_id
    with closing(db.connect()) as conn:
        row = db.item_detail(conn, item_id)
        assert row["reading_state"] == "reading"
        assert row["content_completeness"] == "full"
        assert body in row["original_text"]
        assert row["url"] == url
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
        origin = db.material_origins(conn, item_id)[-1]
        acquisition = json.loads(origin["metadata_json"])["acquisition"]
        assert acquisition["actual_url"] is None
        assert acquisition["attempts"] == []
        assert origin["locator"] == url
    assert ops.material_notes(item_id) == "Keep my reading notes."
