"""M12（ADR 0020）：论文导入增强——citation_pdf_url、Crossref 兜底、S2 限流与 key、
跨标识去重（DOI / arXiv id）、"正文不完整"标记。全部离线。"""

import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import fast_settings, fixture_bytes, make_pdf
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondbrain import config, db, ops
from secondbrain.cli import app as cli_app
from secondbrain.extract.html import citation_meta
from secondbrain.net import FetchError, HttpClient
from secondbrain.sources.arxiv import parse_api_response
from secondbrain.sources.crossref import jats_to_text
from secondbrain.web.app import create_app

runner = CliRunner()
S2 = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
CR = "https://api.crossref.org/works/"
USENIX = "https://www.usenix.org/conference/osdi20/presentation/tang"
USENIX_PDF = "https://www.usenix.org/system/files/osdi20-tang.pdf"
DOI = "10.1145/3132747.3132751"
ARXIV_ID = "2611.05678"
PAPER_PDF_TEXT = "Twine: a unified cluster management system. " * 80
ABSTRACT_PAGE = (
    "<html><head><title>Twine | USENIX</title>"
    '<meta name="citation_title" content="Twine: A Unified Cluster Management System">'
    '<meta name="citation_author" content="Chunqiang Tang">'
    '<meta name="citation_author" content="Kenny Yu">'
    '<meta name="citation_publication_date" content="2020/11/04">'
    '<meta name="citation_doi" content="10.5555/twine.1">'
    f'<meta name="citation_pdf_url" content="{USENIX_PDF}">'
    "</head><body><article>"
    + "".join(
        f"<p>Twine is Facebook's cluster management system, part {n} of the abstract page.</p>"
        for n in range(8)
    )
    + "</article></body></html>"
)
ARXIV_WITH_DOI = (
    fixture_bytes("arxiv_api.xml")
    .decode()
    .replace(
        "<summary>A simple scheme for expert parallel inference.</summary>",
        "<summary>A simple scheme for expert parallel inference.</summary>"
        f"<arxiv:doi>{DOI}</arxiv:doi>",
    )
    .replace(
        "<id>http://arxiv.org/abs/2611.01234v2</id>", "<id>http://arxiv.org/abs/2611.99999v1</id>"
    )
)
ARXIV_HTML = (
    "<html><body><article>"
    + "".join(f"<p>Section {n}: expert parallelism without tears in detail.</p>" for n in range(80))
    + "</article></body></html>"
)
CROSSREF = {
    "message": {
        "title": ["Expert Parallelism Without Tears"],
        "author": [{"given": "Erin", "family": "Example"}],
        "issued": {"date-parts": [[2017, 10, 28]]},
        "abstract": (
            "<jats:title>Abstract</jats:title><jats:p>We make "
            "<jats:italic>expert</jats:italic> parallelism simple.</jats:p>"
        ),
        "link": [{"URL": "https://publisher.example.org/x.pdf", "content-type": "application/pdf"}],
    }
}


def route_table(extra=None):
    routes = {
        USENIX: httpx.Response(200, text=ABSTRACT_PAGE),
        USENIX_PDF: httpx.Response(
            200,
            content=make_pdf([PAPER_PDF_TEXT[:400]] * 5),
            headers={"content-type": "application/pdf"},
        ),
        S2 + DOI: httpx.Response(
            200,
            json={
                "paperId": "p",
                "title": "Expert Parallelism Without Tears",
                "year": 2017,
                "authors": [{"name": "Erin"}],
                "abstract": None,
                "openAccessPdf": None,
                "externalIds": {"DOI": DOI},
            },
        ),
        CR + DOI: httpx.Response(200, json=CROSSREF),
        "https://publisher.example.org/x.pdf": httpx.Response(403, text="no"),
        "https://export.arxiv.org/api/query": httpx.Response(200, text=ARXIV_WITH_DOI),
        f"https://arxiv.org/html/{ARXIV_ID}": httpx.Response(200, text=ARXIV_HTML),
    }
    routes.update(extra or {})
    return routes


def make_client(routes, sleeps=None):
    seen = []

    def handler(request):
        seen.append(request)
        return routes.get(str(request.url.copy_with(query=None)), httpx.Response(404))

    client = HttpClient(
        fast_settings(),
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
    )
    client.seen = seen
    return client


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.delenv("S2_API_KEY", raising=False)
    routes = route_table()
    monkeypatch.setattr(ops, "HttpClient", lambda *_a, **_k: make_client(routes))
    monkeypatch.setattr("secondbrain.web.app._http_client", lambda fs: make_client(routes))
    return tmp_path


def rows():
    conn = db.connect()
    try:
        return conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    finally:
        conn.close()


# ---------- 解析 ----------


def test_citation_meta_and_jats():
    meta = citation_meta(ABSTRACT_PAGE)
    assert meta["pdf_url"] == USENIX_PDF and meta["doi"] == "10.5555/twine.1"
    assert meta["authors"] == ["Chunqiang Tang", "Kenny Yu"] and meta["date"] == "2020/11/04"
    assert jats_to_text(CROSSREF["message"]["abstract"]) == "We make expert parallelism simple."


def test_arxiv_api_doi_is_parsed():
    entries = parse_api_response(ARXIV_WITH_DOI.encode())
    assert {e.arxiv_id: e.doi for e in entries}[ARXIV_ID] == DOI


# ---------- citation_pdf_url ----------


def test_citation_pdf_preferred(env):
    out = runner.invoke(cli_app, ["add", USENIX])
    assert "新增 #1 Twine: A Unified Cluster Management System" in out.output, out.output
    (row,) = rows()
    assert row["extract_method"] == "pdf" and row["doi"] == "10.5555/twine.1"
    assert row["author"] == "Chunqiang Tang, Kenny Yu" and row["published_at"].startswith(
        "2020-11-04"
    )


def test_citation_pdf_blocked_falls_back_to_page(env, monkeypatch):
    routes = route_table({USENIX_PDF: httpx.Response(403, text="no")})
    monkeypatch.setattr(ops, "HttpClient", lambda *_a, **_k: make_client(routes))
    runner.invoke(cli_app, ["add", USENIX])
    (row,) = rows()
    assert row["extract_method"] == "html" and "Twine is Facebook" in row["content"]


# ---------- Crossref 与 S2 限流 ----------


def test_crossref_fills_abstract_when_s2_has_none(env):
    out = runner.invoke(cli_app, ["add", f"https://dl.acm.org/doi/{DOI}"])
    assert "新增（仅摘要）" in out.output, out.output
    (row,) = rows()
    assert row["extract_method"] == "abstract_only" and "parallelism simple" in row["content"]
    assert row["doi"] == DOI and row["published_at"].startswith("2017-10-28")


def test_crossref_when_s2_rate_limited(env, monkeypatch):
    routes = route_table(
        {S2 + DOI: httpx.Response(429, text="slow down", headers={"Retry-After": "3"})}
    )
    sleeps = []
    monkeypatch.setattr(ops, "HttpClient", lambda *_a, **_k: make_client(routes, sleeps))
    out = runner.invoke(cli_app, ["add", f"https://doi.org/{DOI}"])
    assert "新增（仅摘要） #1 Expert Parallelism Without Tears" in out.output, out.output
    assert 3.0 in sleeps  # 按 Retry-After 等待
    assert rows()[0]["author"] == "Erin Example"  # Crossref 的作者


def test_retry_after_is_capped_and_retries_bounded():
    sleeps = []
    client = make_client(
        {"https://x.example/": httpx.Response(429, headers={"Retry-After": "120"})}, sleeps
    )
    with pytest.raises(FetchError, match="HTTP 429"):
        client.get("https://x.example/")
    assert sleeps == [10.0, 10.0] and len(client.seen) == 3  # 上限 10 秒；重试 2 次


def test_s2_api_key_header(env, monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "s2-secret")
    routes = route_table()
    holder = {}

    def factory(*_a, **_k):
        holder["c"] = make_client(routes)
        return holder["c"]

    monkeypatch.setattr(ops, "HttpClient", factory)
    runner.invoke(cli_app, ["add", f"https://doi.org/{DOI}"])
    s2_requests = [r for r in holder["c"].seen if "semanticscholar" in str(r.url)]
    assert s2_requests and s2_requests[0].headers["x-api-key"] == "s2-secret"
    cr_requests = [r for r in holder["c"].seen if "crossref" in str(r.url)]
    assert cr_requests and "x-api-key" not in cr_requests[0].headers


# ---------- 跨标识去重 ----------


def test_doi_then_arxiv_merges_and_completes(env):
    first = runner.invoke(cli_app, ["add", f"https://doi.org/{DOI}"])
    assert "新增（仅摘要）" in first.output
    second = runner.invoke(cli_app, ["add", f"https://arxiv.org/abs/{ARXIV_ID}"])
    assert "已存在（补全正文） #1" in second.output, second.output
    (row,) = rows()
    assert row["extract_method"] == "arxiv_html" and row["arxiv_id"] == ARXIV_ID
    assert row["doi"] == DOI


def test_arxiv_then_doi_is_existing(env):
    runner.invoke(cli_app, ["add", f"https://arxiv.org/pdf/{ARXIV_ID}v1"])
    (row,) = rows()
    assert (row["arxiv_id"], row["doi"]) == (ARXIV_ID, DOI)  # arXiv API 带了 arxiv:doi
    again = runner.invoke(cli_app, ["add", f"https://dl.acm.org/doi/pdf/{DOI}"])
    assert "已存在 #1" in again.output and len(rows()) == 1


def test_same_title_hint_without_merge(env, monkeypatch):
    other = "https://blog.example.com/twine-copy"
    other_pdf = "https://blog.example.com/twine-copy.pdf"
    page = ABSTRACT_PAGE.replace("10.5555/twine.1", "10.5555/other").replace(USENIX_PDF, other_pdf)
    routes = route_table(
        {
            other: httpx.Response(200, text=page),
            other_pdf: httpx.Response(
                200,
                content=make_pdf(["A different copy of the Twine paper text. " * 10] * 5),
                headers={"content-type": "application/pdf"},
            ),
        }
    )
    monkeypatch.setattr(ops, "HttpClient", lambda *_a, **_k: make_client(routes))
    runner.invoke(cli_app, ["add", USENIX])
    out = runner.invoke(cli_app, ["add", other])
    assert "新增 #2" in out.output and "可能重复：#1" in out.output
    assert len(rows()) == 2


def test_migration_v9_backfills_ids(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    for script in db.MIGRATIONS[:8]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 8")
    old.execute(
        "INSERT INTO sources (id, type, trust, role, tags, max_age_days, enabled, backfill,"
        " needs_scraper, updated_at) VALUES ('s', 'arxiv', 3, 'core', '[]', 7, 1, 0, 0, 'x')"
    )
    for n, url in enumerate(["https://arxiv.org/abs/2611.00001", "https://doi.org/10.1/ABC"]):
        old.execute(
            "INSERT INTO items (source_id, url, url_canonical, content_hash, title,"
            " extract_method, fetched_at) VALUES ('s', ?, ?, ?, 't', 'feed', 'x')",
            (url, url, str(n)),
        )
    old.commit()
    old.close()
    conn = db.connect(path)
    got = conn.execute("SELECT arxiv_id, doi FROM items ORDER BY id").fetchall()
    conn.close()
    assert [tuple(r) for r in got] == [("2611.00001", None), (None, "10.1/abc")]


# ---------- 正文不完整 ----------


def test_incomplete_listing_badge_and_completion(env, tmp_path):
    runner.invoke(cli_app, ["add", f"https://doi.org/{DOI}"])
    out = runner.invoke(cli_app, ["items", "incomplete"])
    assert "#1  Expert Parallelism Without Tears  [abstract_only" in out.output
    assert f"doi:{DOI}" in out.output
    client = TestClient(create_app())
    page = client.get("/item/1").text
    assert "data-incomplete" in page and 'action="/item/1/complete"' in page
    assert "data-incomplete" in client.get("/later").text
    resp = client.post(
        "/item/1/complete",
        files={"pdf": ("p.pdf", make_pdf(["Full paper text"]), "application/pdf")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert rows()[0]["extract_method"] == "manual_pdf"
    assert client.post("/item/1/complete", data={}).status_code == 400
    # PDF 只有一行，仍短于 1500 字符：论文类手动条目继续标为不完整；粘贴长正文后不再标记
    client.post("/item/1/complete", data={"text": "Full text. " * 300})
    assert "data-incomplete" not in client.get("/item/1").text
    assert "没有正文不完整的条目" in runner.invoke(cli_app, ["items", "incomplete"]).output


def test_ops_s2_key_settings(env):
    client = TestClient(create_app())
    assert "S2_API_KEY：<b>未设置</b>" in client.get("/ops").text
    resp = client.post(
        "/ops/settings/key", data={"name": "S2_API_KEY", "value": "s2-secret-1"}
    ).text
    assert "S2_API_KEY 已设置" in resp and "s2-secret-1" not in resp
    page = client.get("/ops").text
    assert "S2_API_KEY：<b>已设置</b>（来源：.env）" in page and "s2-secret-1" not in page
    assert config.parse_env_file(config.env_file())["S2_API_KEY"] == "s2-secret-1"
    assert client.post("/ops/settings/key", data={"name": "PATH", "value": "x"}).status_code == 400
    client.post("/ops/settings/key/clear", data={"name": "S2_API_KEY"})
    assert "S2_API_KEY" not in config.parse_env_file(config.env_file())
