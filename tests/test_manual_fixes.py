"""M11 修复（ADR 0019）：DOI → Semantic Scholar、具体的失败原因、PDF 上传兜底、RSS 主页兜底。
全部离线（live 测试默认跳过）。"""

from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, fixture_bytes, make_pdf
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.net import HttpClient
from secondbrain.sources.semantic_scholar import doi_in_url
from secondbrain.web.app import create_app

runner = CliRunner()
S2 = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
ACM_ARXIV = "https://dl.acm.org/doi/10.1145/1111111.2222222"
OA = "https://doi.org/10.5555/oa.1"
BORG = "https://dl.acm.org/doi/10.1145/2741948.2741964"
LIMITED = "https://dl.acm.org/doi/10.1145/9999999.0000000"
CHALLENGE = "https://blocked.example.com/post"
EMPTY = "https://empty.example.com/"
ARXIV_HTML = (
    "<html><body><article>"
    + "".join(f"<p>Paragraph {n} about speculative decoding at scale.</p>" for n in range(60))
    + "</article></body></html>"
).encode()
PDF_TEXT = "Large-scale cluster management at Google with Borg full text"


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url.copy_with(query=None))
    routes = {
        S2 + "10.1145/1111111.2222222": httpx.Response(200, content=fixture_bytes("s2_arxiv.json")),
        S2 + "10.5555/oa.1": httpx.Response(200, content=fixture_bytes("s2_oapdf.json")),
        S2 + "10.1145/2741948.2741964": httpx.Response(
            200, content=fixture_bytes("s2_abstract.json")
        ),
        S2 + "10.1145/9999999.0000000": httpx.Response(429, text="rate limited"),
        "https://export.arxiv.org/api/query": httpx.Response(
            200, content=fixture_bytes("arxiv_api.xml")
        ),
        "https://arxiv.org/html/2611.01234": httpx.Response(200, content=ARXIV_HTML),
        "https://oa.example.org/paper.pdf": httpx.Response(
            200,
            content=make_pdf(["Open access full text page one"]),
            headers={"content-type": "application/pdf"},
        ),
        CHALLENGE: httpx.Response(
            200, text="<html><title>Just a moment...</title><div id='cf-chl'></div></html>"
        ),
        EMPTY: httpx.Response(200, text="<html><body><nav>menu</nav></body></html>"),
        BORG: httpx.Response(403, text="Forbidden"),
        LIMITED: httpx.Response(403, text="Forbidden"),
    }
    return routes.get(url, httpx.Response(404))


def mock_client(*_a, **_k) -> HttpClient:
    return HttpClient(fast_settings(), transport=httpx.MockTransport(handler))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(ops, "HttpClient", mock_client)
    monkeypatch.setattr("secondbrain.web.app._http_client", mock_client)
    return tmp_path


def items() -> list:
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("url", "doi"),
    [
        ("https://doi.org/10.1145/2741948.2741964", "10.1145/2741948.2741964"),
        ("https://dl.acm.org/doi/10.1145/2741948.2741964", "10.1145/2741948.2741964"),
        ("https://dl.acm.org/doi/pdf/10.1145/2741948.2741964", "10.1145/2741948.2741964"),
        ("https://dl.acm.org/doi/abs/10.1145/2741948.2741964?x=1", "10.1145/2741948.2741964"),
        ("https://www.tandfonline.com/doi/full/10.1080/ABC.2020.1/", "10.1080/abc.2020.1"),
        ("https://example.com/post", None),
    ],
)
def test_doi_in_url(url, doi):
    assert doi_in_url(url) == doi


def test_doi_canonical_dedupes_link_forms():
    a = ops.manual_canonical("https://dl.acm.org/doi/pdf/10.1145/2741948.2741964")
    assert a == ops.manual_canonical("https://doi.org/10.1145/2741948.2741964")
    assert a == "https://doi.org/10.1145/2741948.2741964"


def test_s2_arxiv_routes_to_arxiv(env):
    out = runner.invoke(cli_app, ["add", ACM_ARXIV])
    assert out.exit_code == 0 and "新增" in out.output, out.output
    (row,) = items()
    assert row["url_canonical"] == "https://arxiv.org/abs/2611.01234"
    assert row["extract_method"] == "arxiv_html" and row["title"] == "Speculative Decoding at Scale"
    again = runner.invoke(cli_app, ["add", "https://arxiv.org/abs/2611.01234v1"])
    assert "已存在" in again.output


def test_s2_open_access_pdf(env):
    out = runner.invoke(cli_app, ["add", OA])
    assert "新增 #1 Open Access Systems Paper" in out.output, out.output
    (row,) = items()
    assert row["extract_method"] == "pdf" and "Open access full text" in row["content"]
    assert row["url_canonical"] == "https://doi.org/10.5555/oa.1"
    assert row["author"] == "Dana, Eli" and row["published_at"].startswith("2024-01-01")


def test_s2_abstract_only_then_pdf_upload(env, tmp_path):
    out = runner.invoke(cli_app, ["add", BORG, "--read"])
    assert "新增（仅摘要） #1 Large-scale cluster management at Google with Borg" in out.output
    (row,) = items()
    assert row["extract_method"] == "abstract_only" and "Borg system" in row["content"]
    assert row["author"] == "Abhishek Verma, John Wilkes"
    assert "合计：新增 1，已存在 0，失败 0" in out.output
    # 同一篇论文换一种链接形式：按 DOI 去重
    assert (
        "已存在"
        in runner.invoke(cli_app, ["add", "https://doi.org/10.1145/2741948.2741964"]).output
    )
    # 之前只有摘要：再次添加时上传 PDF 补全正文
    pdf = tmp_path / "borg.pdf"
    pdf.write_bytes(make_pdf([PDF_TEXT]))
    again = runner.invoke(cli_app, ["add", BORG, "--pdf", str(pdf)])
    assert "合计：新增 0，已存在 1，失败 0" in again.output
    (row,) = items()
    assert row["extract_method"] == "manual_pdf" and PDF_TEXT in row["content"]


def test_pdf_upload_beats_abstract(env, tmp_path):
    pdf = tmp_path / "borg.pdf"
    pdf.write_bytes(make_pdf([PDF_TEXT]))
    out = runner.invoke(cli_app, ["add", BORG, "--pdf", str(pdf)])
    assert out.exit_code == 0 and "新增 #1" in out.output, out.output
    (row,) = items()
    assert row["extract_method"] == "manual_pdf" and PDF_TEXT in row["content"]
    assert row["title"] == "Large-scale cluster management at Google with Borg"  # S2 元数据
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"not a pdf")
    assert "不是 PDF" in runner.invoke(cli_app, ["add", OA, "--pdf", str(bad)]).output
    multi = runner.invoke(cli_app, ["add", OA, BORG, "--pdf", str(pdf)])
    assert multi.exit_code == 1 and "PDF 上传只能用于单个 URL" in multi.output


def test_specific_failure_reasons(env):
    out = runner.invoke(cli_app, ["add", LIMITED, CHALLENGE, EMPTY, "https://gone.example.com/x"])
    lines = {line.split("：", 1)[0]: line for line in out.output.splitlines() if "✗" in line}
    assert "Semantic Scholar 限流（HTTP 429）" in lines[f"✗ {LIMITED}"]
    assert "页面：HTTP 403：站点拒绝机器访问（被拦截）" in lines[f"✗ {LIMITED}"]
    assert "被拦截：站点返回了机器人验证页" in lines[f"✗ {CHALLENGE}"]
    assert "不是文章页" in lines[f"✗ {EMPTY}"]
    assert "HTTP 404" in lines["✗ https://gone.example.com/x"]
    assert all(line.endswith("可粘贴正文或上传 PDF") for line in lines.values())
    assert items() == []


def test_web_pdf_upload(env):
    client = TestClient(create_app())
    page = client.get("/add").text
    assert 'type="file" name="pdf"' in page and 'enctype="multipart/form-data"' in page
    assert 'rows="6"' in page and 'rows="8"' in page
    bad = client.post(
        "/add",
        data={"urls": BORG, "state": "later"},
        files={"pdf": ("x.pdf", b"hello", "application/pdf")},
    ).text
    assert "只接受 30MB 以内的 PDF" in bad
    client.post(
        "/add",
        data={"urls": BORG, "state": "read", "feedback": "gained"},
        files={"pdf": ("borg.pdf", make_pdf([PDF_TEXT]), "application/pdf")},
    )
    task = client.app.state.tasks.get("manual_add")
    task.thread.join(10)
    assert 'data-add-status="新增"' in client.get("/tasks/manual_add").text
    (row,) = items()
    assert row["extract_method"] == "manual_pdf"


# ---------- RSS 主页兜底 ----------


def test_peek_shows_and_records_channel_link(env, monkeypatch):
    def feed_client(*_a, **_k):
        def h(request):
            return httpx.Response(200, content=fixture_bytes("rss.xml"))

        return HttpClient(fast_settings(), transport=httpx.MockTransport(h))

    monkeypatch.setattr("secondbrain.web.app._http_client", feed_client)
    conn = db.connect()
    db.upsert_sources(conn, config.load_sources())
    conn.commit()
    conn.close()
    client = TestClient(create_app())
    sources = client.get("/sources").text
    row = sources.split('data-source="vllm-blog"', 1)[1].split("</tr>", 1)[0]
    assert 'href="https://vllm.ai/"' in row  # 没抓取过：兜底为站点根
    catalog_free = sources.split('data-source="hazy-research"', 1)[1].split("</tr>", 1)[0]
    assert 'href="https://hazyresearch.stanford.edu/"' in catalog_free
    peek = client.post("/sources/vllm-blog/peek").text
    assert "主页 <a href=" in peek
    conn = db.connect()
    recorded = db.source_overview(conn)["vllm-blog"]["homepage"]
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0  # 预览不写 items
    conn.close()
    assert recorded and recorded in peek


@pytest.mark.live
def test_live_borg_via_semantic_scholar(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path))
    out = ops.add_manual([BORG], lambda line: None, state="later")
    (result,) = out["results"]
    assert result["status"].startswith("新增") and "Borg" in result["title"]
