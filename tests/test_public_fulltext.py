"""公开全文获取：摘要不是终点，诊断不能冒充正文来源。全部离线。"""

import json

import httpx
import pytest
from conftest import fast_settings, make_pdf

from secondbrain import db, ingest, ops
from secondbrain.models import RawEntry
from secondbrain.net import FetchError, HttpClient
from secondbrain.public_fetch import MAX_REQUESTS, PublicClient

DOI = "10.1234/public.1"
URL = f"https://doi.org/{DOI}"
LANDING = "https://publisher.example.org/paper"
PDF = "https://publisher.example.org/paper.pdf?access_token=secret"
TITLE = "Public Material Acquisition"
ABSTRACT = "A verified metadata abstract about public material acquisition."
BODY = TITLE + "\n" + "Full paper methods and results about material acquisition. " * 70


def metadata(request):
    if request.url.host == "api.semanticscholar.org":
        return httpx.Response(200, json={
            "title": TITLE, "abstract": ABSTRACT,
            "externalIds": {"DOI": DOI}, "openAccessPdf": None,
        })
    if request.url.host == "api.crossref.org":
        return httpx.Response(200, json={"message": {"title": [TITLE], "DOI": DOI}})
    return None


def page(*, title=TITLE, doi=DOI, pdf=PDF, body=""):
    return ('<html><head><meta name="citation_title" content="' + title + '">'
            '<meta name="citation_doi" content="' + doi + '">'
            '<meta name="citation_pdf_url" content="' + pdf + '"></head>'
            '<body><article><p>' + body + '</p></article></body></html>')


def test_doi_summary_continues_to_redirected_landing_citation_pdf():
    seen = []
    raw_html = page(body=ABSTRACT)
    pdf_data = make_pdf([BODY])

    def handler(request):
        seen.append(str(request.url))
        result = metadata(request)
        if result is not None:
            return result
        if str(request.url) == URL:
            return httpx.Response(302, headers={"location": LANDING})
        if str(request.url) == LANDING:
            return httpx.Response(200, text=raw_html, headers={"content-type": "text/html"})
        if str(request.url) == PDF:
            return httpx.Response(200, content=pdf_data,
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        fetched = ops._fetch_manual(URL, client, fs)
    assert fetched.method == "pdf"
    assert fetched.entry.summary == ABSTRACT
    assert fetched.pdf_data == pdf_data
    assert fetched.raw_data == raw_html.encode()
    assert fetched.acquisition["status"] == "full"
    assert fetched.acquisition["actual_url"] == PDF
    assert fetched.acquisition["version"] == "unknown"
    assert any("api.crossref.org" in url for url in seen)
    assert "secret" not in json.dumps(fetched.acquisition["attempts"])
    assert ops._material(fetched, fs).provenance["acquisition"] == fetched.acquisition


def test_failed_pdf_keeps_metadata_abstract_actual_source_and_diagnostics():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        result = metadata(request)
        if result is not None:
            return result
        if str(request.url) == URL:
            return httpx.Response(200, text=page(body="Short landing abstract."))
        return httpx.Response(403)

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        fetched = ops._fetch_manual(URL, client, fs)
    assert fetched.method == "abstract_only"
    assert fetched.content == f"{TITLE}\n\n{ABSTRACT}"
    assert fetched.acquisition["status"] == "abstract"
    assert "api.semanticscholar.org" in fetched.acquisition["actual_url"]
    assert fetched.acquisition["actual_url"] != PDF
    assert fetched.pdf_data is None and fetched.raw_data is None
    assert seen.count(PDF) == 1
    assert any("403" in attempt["reason"] for attempt in fetched.acquisition["attempts"])


def test_page_abstract_preserves_original_html_not_failed_pdf_response():
    html = page(body=ABSTRACT * 6)

    def handler(request):
        if str(request.url) == LANDING:
            return httpx.Response(200, text=html)
        return httpx.Response(403)

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        fetched = ops._fetch_manual(LANDING, client, fs)
    assert fetched.acquisition["status"] == "abstract"
    assert fetched.acquisition["actual_url"] == LANDING
    assert fetched.completeness == "abstract"
    assert fetched.raw_data == html.encode()
    assert fetched.pdf_data is None


@pytest.mark.parametrize("conflict", ["landing_title", "landing_doi", "pdf_doi"])
def test_doi_conflicting_identity_is_not_abstract_fallback(conflict):
    def handler(request):
        result = metadata(request)
        if result is not None:
            return result
        if str(request.url) == URL:
            return httpx.Response(200, text=page(
                title="Different Work" if conflict == "landing_title" else TITLE,
                doi="10.1234/wrong" if conflict == "landing_doi" else DOI,
            ))
        if str(request.url) == PDF:
            return httpx.Response(200, content=make_pdf(["doi: 10.1234/wrong\n" + BODY]))
        return httpx.Response(404)

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="身份冲突") as error:
            ops._fetch_manual(URL, client, fs)
    assert error.value.acquisition["status"] == "missing"


@pytest.mark.parametrize("shell", [
    "<h1>Just a moment...</h1>",
    '<form><input type="password"></form>',
    "<h1>Sign in to access this paper</h1>",
])
def test_captcha_or_login_shell_never_becomes_fulltext(shell):
    html = shell + "<article><p>" + BODY + "</p></article>"
    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=html)
    )) as client:
        with pytest.raises(ops.ManualFetchError) as error:
            ops._fetch_manual(LANDING, client, fs)
    assert error.value.acquisition["actual_url"] is None
    assert error.value.acquisition["status"] == "missing"
    assert error.value.acquisition["attempts"]


def test_html_at_pdf_url_is_not_pdf_and_does_not_erase_abstract():
    def handler(request):
        result = metadata(request)
        if result is not None:
            if request.url.host == "api.semanticscholar.org":
                data = result.json()
                data["openAccessPdf"] = {"url": PDF}
                return httpx.Response(200, json=data)
            return result
        if str(request.url) == PDF:
            return httpx.Response(200, text="<article>" + BODY + "</article>",
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(403)

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        fetched = ops._fetch_manual(URL, client, fs)
    assert fetched.method == "abstract_only"
    assert fetched.pdf_data is None
    assert fetched.acquisition["status"] == "abstract"
    assert any("未返回 PDF" in a["reason"] for a in fetched.acquisition["attempts"])


def test_public_reader_rejects_private_redirect_and_dns(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        public = PublicClient(client)
        with pytest.raises(FetchError, match="不安全"):
            public.get(LANDING)
    assert seen == [LANDING]
    monkeypatch.setattr("secondbrain.public_fetch.socket.getaddrinfo", lambda *_: [
        (2, 1, 6, "", ("127.0.0.1", 443))
    ])
    with HttpClient(fs) as client:
        with pytest.raises(FetchError, match="非公开网络"):
            PublicClient(client).get(LANDING)


def test_public_reader_bounds_streamed_bytes_and_requests():
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * 80
            yield b"b" * 80

    fs = fast_settings(retries=0)
    with HttpClient(fs, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=Chunks())
    )) as client:
        with pytest.raises(FetchError, match="大小上限"):
            PublicClient(client).get(LANDING, max_bytes=100)
    seen = []

    def redirects(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/hop/{len(seen)}"})

    with HttpClient(fs, transport=httpx.MockTransport(redirects)) as client:
        public = PublicClient(client)
        with pytest.raises(FetchError):
            public.get(LANDING)
        with pytest.raises(FetchError, match="预算"):
            public.get("https://publisher.example.org/other")
    assert len(seen) == MAX_REQUESTS


def test_manual_failure_result_exposes_acquisition_without_importing():
    fs = fast_settings(retries=0)
    with HttpClient(fs, transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        result = ops.add_manual([LANDING], lambda _: None, client=client)["results"][0]
    assert result["status"] == "失败"
    assert result["acquisition"]["status"] == "missing"
    assert result["acquisition"]["attempts"]
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    conn.close()


def test_feed_article_uses_the_same_citation_pdf_path():
    fs = fast_settings()

    def handler(request):
        if str(request.url) == LANDING:
            return httpx.Response(200, text=page(body=ABSTRACT))
        return httpx.Response(200, content=make_pdf([BODY]))

    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        capture = ingest.ContentCapture(client)
        content, method = ingest.extract_content(
            RawEntry(url=LANDING, title=TITLE, doi=DOI), capture, fs
        )
    assert method == "pdf" and "Full paper methods" in content
    assert capture.actual_url == PDF
    assert capture.pdf_data


def test_diagnostic_reason_redacts_all_embedded_urls():
    public = PublicClient(None)
    public.note(
        PDF, "failed",
        "HTTP failure at https://user:password@example.org/paper?key=other-secret#part "
        f"while fetching {PDF}",
    )
    encoded = json.dumps(public.attempts)
    assert "secret" not in encoded
    assert "password" not in encoded
    assert "user:" not in encoded
    assert public.attempts[0]["url"].endswith("?…")


def test_public_redirect_does_not_forward_metadata_api_credentials():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": LANDING})
        return httpx.Response(200, text="public response")

    fs = fast_settings()
    with HttpClient(fs, transport=httpx.MockTransport(handler)) as client:
        PublicClient(client).get(
            "https://api.example.org/metadata", headers={"x-api-key": "test-api-secret"}
        )
    assert seen[0].headers["x-api-key"] == "test-api-secret"
    assert "x-api-key" not in seen[1].headers
