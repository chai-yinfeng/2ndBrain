"""YouTube 信源、字幕抓取与格式化、视频相关的 L0 规则（全部离线）。"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import youtube_transcript_api as yta
from conftest import FIXTURES, NOW, fixture_bytes

from secondbrain import db
from secondbrain.extract import transcript as tr
from secondbrain.extract.transcript import Segment, Transcript, TranscriptUnavailable
from secondbrain.ingest import fetch_sources
from secondbrain.models import FetchSettings, L0Settings, SourceConfig
from secondbrain.pipeline.l0_rules import l0_reason
from secondbrain.sources.youtube import is_short, parse_channel_feed

FEED = "https://www.youtube.com/feeds/videos.xml"
FS = FetchSettings(transcript_interval_seconds=0, retry_backoff_seconds=0)


# ---------- RSS ----------


def test_parse_channel_feed():
    entries = parse_channel_feed(fixture_bytes("youtube_feed.xml"))
    assert [e.video_id for e in entries] == ["vid00000001", "short000001", "vid00000002"]
    first = entries[0]
    assert first.url == "https://www.youtube.com/watch?v=vid00000001"
    assert first.summary == "We build a paged KV cache step by step."
    assert first.published_at == datetime(2026, 11, 3, 10, tzinfo=UTC)
    assert is_short(entries[1].url) and not is_short(first.url)


# ---------- 字幕格式化 ----------


def test_format_transcript_merges_segments_with_timestamps():
    segs = [
        Segment(0, 3, "hello"),
        Segment(10, 3, "world\nagain"),
        Segment(31, 3, "next part"),
        Segment(3725, 5, "late"),
    ]
    text = tr.format_transcript(segs, paragraph_seconds=30)
    assert text.splitlines() == ["[00:00] hello world again", "[00:31] next part", "[1:02:05] late"]
    assert Transcript(segs, "transcript", "en", False).duration_seconds == 3730


def test_timestamp_helpers():
    assert tr.parse_timestamp("12:34") == 754
    assert tr.parse_timestamp("1:02:05") == 3725
    assert tr.leading_timestamp("[12:34] 要点") == (754, "要点")
    assert tr.leading_timestamp("- [00:05] x") == (5, "x")
    assert tr.leading_timestamp("无时间戳") == (None, "无时间戳")
    assert tr.video_id_from_url("https://www.youtube.com/watch?v=abc123&t=5") == "abc123"
    assert tr.video_id_from_url("https://youtu.be/abc123") == "abc123"
    assert tr.video_id_from_url("https://www.youtube.com/shorts/abc123") == "abc123"
    assert tr.youtube_link("abc123", 754) == "https://www.youtube.com/watch?v=abc123&t=754"
    url = "https://www.youtube.com/watch?v=abc123"
    assert tr.key_point_link("[12:34] 要点", url) == (
        "[12:34] 要点",
        tr.youtube_link("abc123", 754),
    )
    assert tr.key_point_link("[12:34] 要点", "https://blog.example.com/a") == ("[12:34] 要点", None)
    assert tr.key_point_link("没有时间戳", url) == ("没有时间戳", None)


def test_parse_json3():
    segs = tr.parse_json3((FIXTURES / "transcript_json3.json").read_bytes())
    assert [s.text for s in segs] == [
        "Welcome to the lecture.",
        "Today we cover paged attention.",
        "That's all, thanks.",
    ]
    assert segs[1].start == 12.0


def test_pick_ytdlp_track_prefers_manual_then_auto():
    info = {
        "subtitles": {"de": [{"ext": "json3", "url": "de"}]},
        "automatic_captions": {
            "en": [{"ext": "vtt", "url": "x"}, {"ext": "json3", "url": "auto-en"}]
        },
    }
    assert tr.pick_ytdlp_track(info) == ("auto-en", "en", True)
    info["subtitles"]["zh-Hans"] = [{"ext": "json3", "url": "manual-zh"}]
    assert tr.pick_ytdlp_track(info) == ("manual-zh", "zh-Hans", False)
    assert tr.pick_ytdlp_track({}) is None


class FakeList:
    def __init__(self, manual=None, generated=None):
        self.manual, self.generated = manual, generated

    def find_manually_created_transcript(self, langs):
        if self.manual is None:
            raise yta.NoTranscriptFound("v", langs, self)
        return self.manual

    def find_generated_transcript(self, langs):
        if self.generated is None:
            raise yta.NoTranscriptFound("v", langs, self)
        return self.generated


def test_pick_prefers_manual_transcripts():
    assert tr._pick(FakeList(manual="m", generated="g")) == "m"
    assert tr._pick(FakeList(generated="g")) == "g"
    with pytest.raises(TranscriptUnavailable, match="没有英文或中文字幕"):
        tr._pick(FakeList())


def _ok(method="transcript_ytdlp"):
    return Transcript([Segment(0, 2, "hi")], method, "en", True)


def test_blocked_api_falls_back_to_ytdlp(monkeypatch):
    def blocked(video_id, fs):
        raise yta.RequestBlocked(video_id)

    monkeypatch.setattr(tr, "_via_api", blocked)
    monkeypatch.setattr(tr, "_via_ytdlp", lambda video_id, fs: _ok())
    assert tr.fetch_transcript("v", FS).method == "transcript_ytdlp"


def test_disabled_transcripts_do_not_try_ytdlp(monkeypatch):
    def disabled(video_id, fs):
        raise TranscriptUnavailable("TranscriptsDisabled：字幕不可用")

    def forbidden(video_id, fs):
        raise AssertionError("字幕已关闭时不应再调用 yt-dlp")

    monkeypatch.setattr(tr, "_via_api", disabled)
    monkeypatch.setattr(tr, "_via_ytdlp", forbidden)
    with pytest.raises(TranscriptUnavailable, match="TranscriptsDisabled"):
        tr.fetch_transcript("v", FS)


def test_both_failures_are_reported(monkeypatch):
    def blocked(video_id, fs):
        raise yta.IpBlocked(video_id)

    def ytdlp_fails(video_id, fs):
        raise TranscriptUnavailable("yt-dlp：HTTP Error 429")

    monkeypatch.setattr(tr, "_via_api", blocked)
    monkeypatch.setattr(tr, "_via_ytdlp", ytdlp_fails)
    with pytest.raises(TranscriptUnavailable) as info:
        tr.fetch_transcript("v", FS)
    assert "IpBlocked" in str(info.value) and "429" in str(info.value)


def test_api_retries_request_failures_with_backoff(monkeypatch):
    calls = {"list": 0}

    class FlakyApi:
        def list(self, video_id):
            calls["list"] += 1
            if calls["list"] == 1:
                raise yta.YouTubeRequestFailed(video_id, Exception("429 Too Many Requests"))
            return FakeList(manual=FakeTranscript())

    class FakeTranscript:
        language_code, is_generated = "en", False

        def fetch(self):
            class Fetched:
                snippets = [type("S", (), {"start": 0.0, "duration": 1.0, "text": "hello"})()]

            return Fetched()

    monkeypatch.setattr(yta, "YouTubeTranscriptApi", FlakyApi)
    t = tr._via_api("v", FS)
    assert calls["list"] == 2
    assert (t.method, t.segments[0].text) == ("transcript", "hello")


# ---------- 入库 ----------


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "brain.db")
    yield c
    c.close()


def test_fetch_youtube_source(conn, make_client, monkeypatch):
    def fake_fetch(video_id, fs):
        if video_id == "vid00000001":
            segs = [Segment(i * 10, 10, f"part {i}") for i in range(90)]  # 15 分钟
            return Transcript(segs, "transcript", "en", False)
        raise TranscriptUnavailable("没有英文或中文字幕")

    monkeypatch.setattr(tr, "fetch_transcript", fake_fetch)
    client, _ = make_client({FEED: httpx.Response(200, content=fixture_bytes("youtube_feed.xml"))})
    src = SourceConfig(id="yt", type="youtube", url=FEED, max_age_days=30)
    (result,) = fetch_sources(conn, [src], client, client.settings, now=NOW)
    assert (result.status, result.added) == ("ok", 2)  # 旧视频超出时间窗
    assert result.extract_failed == 1
    assert "short000001 无字幕" in result.notes[0]
    rows = {
        r["url"]: r
        for r in conn.execute("SELECT url, content, extract_method, duration_seconds FROM items")
    }
    video = rows["https://www.youtube.com/watch?v=vid00000001"]
    assert video["extract_method"] == "transcript"
    assert video["duration_seconds"] == 900
    assert video["content"].startswith("[00:00] part 0 part 1 part 2")
    assert rows["https://www.youtube.com/shorts/short000001"]["extract_method"] == "no_transcript"


# ---------- L0 ----------


def _row(**kw):
    base = {
        "url": "https://www.youtube.com/watch?v=x",
        "title": "Lecture",
        "summary": "",
        "content_len": 0,
        "extract_method": "transcript",
        "duration_seconds": 1800,
        "published_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetched_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return base | kw


def test_l0_video_rules():
    s = L0Settings(min_video_minutes=5, min_content_chars=300)
    assert l0_reason(_row(url="https://www.youtube.com/shorts/x"), s, NOW) == "Shorts"
    assert l0_reason(_row(duration_seconds=120, content_len=5000), s, NOW) == "视频过短"
    assert l0_reason(_row(content_len=5000), s, NOW) is None
    # 没有字幕：不按正文长度排除（交给 L1 看标题与简介，但不会进 L2）
    assert l0_reason(_row(extract_method="no_transcript", duration_seconds=None), s, NOW) is None


def test_transcripts_use_their_own_length_cap(conn, make_client, monkeypatch):
    long_segs = [Segment(i, 1, "word " * 50) for i in range(3000)]  # 远超 content_max_chars
    monkeypatch.setattr(
        tr, "fetch_transcript", lambda vid, fs: Transcript(long_segs, "transcript", "en", False)
    )
    client, _ = make_client(
        {FEED: httpx.Response(200, content=fixture_bytes("youtube_feed.xml"))},
        content_max_chars=10_000,
        transcript_max_chars=50_000,
    )
    src = SourceConfig(id="yt", type="youtube", url=FEED, max_age_days=30)
    fetch_sources(conn, [src], client, client.settings, now=NOW)
    lengths = [r[0] for r in conn.execute("SELECT length(content) FROM items")]
    assert lengths and all(length == 50_000 for length in lengths)
