from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import fixture_bytes

from secondbrain import config
from secondbrain.net import FetchError
from secondbrain.sources.arxiv import parse_api_response, parse_arxiv_id
from secondbrain.sources.hf_papers import parse_daily_papers
from secondbrain.sources.rss import parse_feed


def test_parse_rss():
    entries = parse_feed(fixture_bytes("rss.xml"))
    assert len(entries) == 3
    first = entries[0]
    assert first.title == "Paged KV cache explained"
    assert first.author == "Alice"
    assert first.published_at == datetime(2026, 11, 3, 8, tzinfo=UTC)
    assert "PagedAttention" in first.feed_content
    assert first.summary == "A short teaser about paged KV cache."


def test_parse_atom():
    entries = parse_feed(fixture_bytes("atom.xml"))
    assert [e.url for e in entries] == [
        "https://notes.example.org/raft-leases/",
        "https://notes.example.org/paxos/",
    ]
    assert entries[0].author == "Bob"
    assert "linearizable reads" in entries[0].feed_content
    # 有 published 时优先 published
    assert entries[1].published_at == datetime(2026, 11, 1, 9, tzinfo=UTC)


def test_parse_feed_rejects_non_feed():
    with pytest.raises(FetchError, match="不是有效的 RSS/Atom"):
        parse_feed(b"<html><body>not a feed")


def test_parse_arxiv():
    entries = parse_api_response(fixture_bytes("arxiv_api.xml"))
    assert [e.arxiv_id for e in entries] == ["2611.01234", "2611.05678"]
    assert entries[0].url == "https://arxiv.org/abs/2611.01234"
    assert entries[0].title == "Speculative Decoding at Scale"
    assert entries[0].author == "Carol, Dave"
    assert entries[0].summary == "We study speculative decoding for large-batch serving."
    assert entries[0].published_at == datetime(2026, 11, 2, 18, tzinfo=UTC)


def test_parse_arxiv_id_variants():
    assert parse_arxiv_id("http://arxiv.org/abs/2611.01234v12") == "2611.01234"
    assert parse_arxiv_id("http://arxiv.org/abs/cs/0101001v1") == "cs/0101001"
    assert parse_arxiv_id("https://example.com/x") is None


def test_parse_arxiv_error_entry():
    with pytest.raises(FetchError, match="incorrect id format"):
        parse_api_response(fixture_bytes("arxiv_api_error.xml"))


def test_parse_hf_daily_papers():
    entries = parse_daily_papers(fixture_bytes("hf_daily_papers.json"))
    assert [e.url for e in entries] == [
        "https://arxiv.org/abs/2611.01234",
        "https://arxiv.org/abs/2611.09999",
    ]
    assert entries[1].author == "Frank"
    # 使用进入 Daily Papers 的时间
    assert entries[0].published_at == datetime(2026, 11, 3, 21, tzinfo=UTC)


def test_parse_hf_rejects_bad_payload():
    with pytest.raises(FetchError):
        parse_daily_papers(b'{"error": "x"}')
    with pytest.raises(FetchError):
        parse_daily_papers(b"<html>")


def test_example_sources_are_valid():
    sources = config.load_sources(config.CONFIG_DIR / "sources.example.yaml")
    assert len(sources) > 10
    assert {s.type for s in sources} >= {"rss", "arxiv", "hf_papers"}
    assert any(s.role == "expansion" for s in sources)
    assert any(s.needs_scraper for s in sources)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "sources:\n  - {id: a, type: rss, url: 'https://a'}\n"
            "  - {id: a, type: rss, url: 'https://b'}\n",
            "重复",
        ),
        ("sources:\n  - {id: a, type: arxiv}\n", "query"),
        ("sources:\n  - {id: a, type: rss}\n", "url"),
        ("sources:\n  - {id: a, type: rss, url: 'https://a', trsut: 3}\n", "trsut"),
        ("sources:\n  - {id: a, type: rss, url: 'https://a', trust: 9}\n", "trust"),
        ("foo: bar\n", "sources"),
    ],
)
def test_invalid_sources(tmp_path: Path, text: str, message: str):
    with pytest.raises(config.ConfigError, match=message):
        config.load_sources(_write(tmp_path, text))


def test_source_defaults(tmp_path: Path):
    (source,) = config.load_sources(_write(tmp_path, "sources:\n  - {id: a, type: rss, url: u}\n"))
    assert source.role == "core"
    assert source.backfill is False
    assert source.needs_scraper is False
    assert source.enabled is True


def test_sources_path_falls_back_to_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    assert config.sources_path() == (tmp_path / "sources.example.yaml", True)
    (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    assert config.sources_path() == (tmp_path / "sources.yaml", False)
