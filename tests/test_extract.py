import httpx
import pytest
from conftest import fixture_bytes, make_pdf

from secondbrain.extract.html import extract_main_text, html_fragment_to_text
from secondbrain.extract.pdf import extract_pdf_text
from secondbrain.ingest import extract_content
from secondbrain.models import RawEntry

ARXIV_HTML = "https://arxiv.org/html/2611.01234"
ARXIV_PDF = "https://arxiv.org/pdf/2611.01234"
LONG_PAPER_HTML = (
    "<html><body><article>"
    + "".join(f"<p>Paragraph {i} about speculative decoding and drafts.</p>" for i in range(60))
    + "</article></body></html>"
)


def _paper() -> RawEntry:
    return RawEntry(
        url="https://arxiv.org/abs/2611.01234",
        title="Speculative Decoding at Scale",
        summary="abstract text",
        arxiv_id="2611.01234",
    )


def test_extract_main_text_drops_boilerplate():
    text = extract_main_text(fixture_bytes("article.html").decode())
    assert "Continuous batching schedules requests" in text
    assert "NAVIGATION-SHOULD-NOT-APPEAR" not in text
    assert "FOOTER-SHOULD-NOT-APPEAR" not in text


def test_html_fragment_to_text():
    assert html_fragment_to_text("<p>Hello <b>world</b></p>") == "Hello world"
    assert html_fragment_to_text("plain text ") == "plain text"


def test_extract_pdf_respects_max_pages():
    data = make_pdf(["First page text", "Second page text"])
    assert "Second page" in extract_pdf_text(data, max_pages=5)
    text = extract_pdf_text(data, max_pages=1)
    assert "First page text" in text
    assert "Second page" not in text


def test_arxiv_prefers_html(make_client):
    client, router = make_client({ARXIV_HTML: httpx.Response(200, text=LONG_PAPER_HTML)})
    text, method = extract_content(_paper(), client, client.settings)
    assert method == "arxiv_html"
    assert "drafts" in text
    assert ARXIV_PDF not in router.urls()


def test_arxiv_falls_back_to_pdf(make_client):
    pdf = make_pdf(["Speculative decoding full text"])
    client, _ = make_client({ARXIV_PDF: httpx.Response(200, content=pdf)})
    text, method = extract_content(_paper(), client, client.settings)
    assert method == "pdf"
    assert "Speculative decoding full text" in text


def test_arxiv_skips_oversized_pdf(make_client):
    pdf = make_pdf(["x"])
    client, _ = make_client({ARXIV_PDF: httpx.Response(200, content=pdf)}, pdf_max_bytes=10)
    text, method = extract_content(_paper(), client, client.settings)
    assert (text, method) == ("abstract text", "summary")


def test_long_feed_content_skips_page_fetch(make_client):
    client, router = make_client({}, min_feed_content_chars=20)
    entry = RawEntry(
        url="https://blog.example.com/a",
        title="A",
        feed_content="<p>This feed already carries the full article body.</p>",
    )
    text, method = extract_content(entry, client, client.settings)
    assert method == "feed"
    assert "full article body" in text
    assert router.requests == []


def test_short_feed_content_fetches_page(make_client):
    url = "https://blog.example.com/posts/continuous-batching"
    client, _ = make_client({url: httpx.Response(200, content=fixture_bytes("article.html"))})
    entry = RawEntry(url=url, title="CB", feed_content="Short summary only.")
    text, method = extract_content(entry, client, client.settings)
    assert method == "html"
    assert "tail latency drops" in text


@pytest.mark.parametrize(("status", "expected"), [(404, ("Short teaser", "feed"))])
def test_page_failure_falls_back_to_feed_text(make_client, status, expected):
    url = "https://blog.example.com/gone"
    client, _ = make_client({url: httpx.Response(status)})
    entry = RawEntry(url=url, title="Gone", feed_content="Short teaser", summary="Short teaser")
    assert extract_content(entry, client, client.settings) == expected
