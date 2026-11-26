from __future__ import annotations

from contextlib import closing

import httpx
import pytest

from secondbrain import capture, db, ops

URL = "https://example.org/research/article"


@pytest.mark.parametrize("shared", [URL, f"读完了这篇：{URL}。", f"[标题]({URL})", f"{URL}\n{URL}"])
def test_capture_extracts_one_explicit_url(shared):
    assert capture.parse_shared_url(shared) == URL


def test_capture_does_not_guess_identity_or_remove_balanced_delimiters():
    assert capture.parse_shared_url("See (https://example.org/wiki/Test_(paper)).") == (
        "https://example.org/wiki/Test_(paper)"
    )
    for value in (
        "看完了那篇新论文",
        f"{URL} https://example.org/another",
        "https://u:p@example.org/",
    ):
        with pytest.raises(ValueError):
            capture.parse_shared_url(value)


def test_fetch_failure_keeps_reference_and_later_user_text_upgrades_same_record(make_client):
    client, router = make_client({URL: httpx.Response(403, text="blocked")})
    normal = ops.add_manual([URL], lambda _: None, state="read", client=client)
    assert normal["results"][0]["item_id"] is None

    result = capture.import_url_or_content(f"读完：{URL}", lambda _: None, client=client)
    (row,) = result["results"]
    assert result["capture_fallback"] and result["ingest"] is None
    assert row["content_completeness"] == "missing"
    assert "403" in row["message"]
    assert row["acquisition"]["actual_url"] is None
    assert "403" in row["acquisition"]["fallback_reason"]
    item_id = row["item_id"]
    with closing(db.connect()) as conn:
        item = db.item_detail(conn, item_id)
        assert item["reading_state"] == "read" and item["read_at"]
        assert item["original_text"] == ""
        assert db.material_origins(conn, item_id)[0]["kind"] == "reference"

    before = len(router.requests)
    full = capture.import_url_or_content(
        URL,
        lambda _: None,
        text="这是用户提供的原文。 " * 25,
        client=client,
    )
    (updated,) = full["results"]
    assert updated["item_id"] == item_id
    assert not full["capture_fallback"] and updated["content_completeness"] == "full"
    assert len(router.requests) == before
    repeat = capture.import_url_or_content(URL, lambda _: None, client=client)
    repeated, = repeat["results"]
    assert repeated["item_id"] == item_id and repeated["acquisition"]["reused_existing"]
    assert len(router.requests) == before
    with closing(db.connect()) as conn:
        item = db.item_detail(conn, item_id)
        assert item["original_text"].startswith("这是用户提供的原文")
        assert len(db.material_origins(conn, item_id)) == 2
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_invalid_local_file_does_not_silently_save_a_link(make_client):
    client, router = make_client({})
    with pytest.raises(ops.OpsError, match="不是 PDF"):
        capture.import_url_or_content(URL, lambda _: None, pdf=b"not a pdf", client=client)
    assert not router.requests
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
