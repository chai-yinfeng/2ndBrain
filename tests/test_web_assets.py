"""页面资源更新不能继续命中上一版 CSS；保留静态文件的条件请求。"""

import html
import os
import re

import pytest
from fastapi.testclient import TestClient

from secondbrain.web import app as web_app


@pytest.fixture
def assets(tmp_path, monkeypatch):
    directory = tmp_path / "static"
    directory.mkdir()
    for name, content in {
        "style.css": "body { color: red; }\n",
        "ui.js": "",
        "htmx.min.js": "",
    }.items():
        (directory / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(web_app, "STATIC_DIR", directory)
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    with TestClient(web_app.create_app()) as client:
        yield client, directory


def stylesheet_url(client):
    response = client.get("/")
    assert response.status_code == 200
    urls = re.findall(r'<link\b[^>]*href="([^"]+)"', response.text)
    return next(html.unescape(url) for url in urls if "/static/style.css" in url)


def test_css_update_bypasses_old_cache_without_server_restart(assets):
    client, directory = assets
    old_url = stylesheet_url(client)
    old_response = client.get(old_url)
    assert old_response.status_code == 200
    path = directory / "style.css"
    original = path.stat()
    replacement = "body { color: tan; }\n"
    path.write_text(replacement, encoding="utf-8")
    # 即使部署工具保留了 mtime 和大小，内容变化也必须更新浏览器资源地址。
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    new_url = stylesheet_url(client)
    assert new_url != old_url
    response = client.get(new_url)
    assert response.status_code == 200
    assert response.text == replacement and response.text != old_response.text


def test_assets_revalidate_and_keep_cache_policy_on_304(assets):
    client, _ = assets
    url = stylesheet_url(client)
    response = client.get(url)
    assert response.headers["cache-control"] == "no-cache"
    unchanged = client.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "no-cache"
    assert unchanged.content == b""


def test_html_and_task_status_are_not_stored(assets):
    client, _ = assets
    for path in ("/", "/tasks/manual_add"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
