"""YouTube 字幕：youtube-transcript-api 优先，失败时退回 yt-dlp（只取字幕，不下载视频）。

字幕按约 N 秒合并为段落，每段以 `[mm:ss]`（超过 1 小时为 `[h:mm:ss]`）开头，存入 items.content
（ADR 0009）。遇到限流 / 封禁只做有限的退避重试并如实报错，不使用代理、不伪造 cookie。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from secondbrain.models import FetchSettings

# 语言优先级：英文、中文；先人工字幕，再自动字幕
LANGUAGES = ("en", "en-US", "en-GB", "zh-Hans", "zh-Hant", "zh", "zh-CN", "zh-TW")

TIMESTAMP_RE = re.compile(r"^\s*(?:[-*]\s*)?\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*")


class TranscriptUnavailable(Exception):
    """拿不到字幕；消息说明原因（无字幕 / 被限流 / 视频不可用 …）。"""


@dataclass(frozen=True)
class Segment:
    start: float
    duration: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    method: str  # transcript（youtube-transcript-api）| transcript_ytdlp
    language: str
    generated: bool

    @property
    def duration_seconds(self) -> int:
        if not self.segments:
            return 0
        last = self.segments[-1]
        return int(last.start + last.duration)


# ---------- 时间戳 ----------


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_timestamp(value: str) -> int:
    parts = [int(p) for p in value.split(":")]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def leading_timestamp(text: str) -> tuple[int | None, str]:
    """ "[12:34] 要点" → (754, "要点")；没有时间戳时返回 (None, 原文)。"""
    m = TIMESTAMP_RE.match(text)
    if not m:
        return None, text
    return parse_timestamp(m.group(1)), text[m.end() :]


def video_id_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.endswith("youtu.be"):
        return parts.path.strip("/") or None
    if parts.path.startswith("/shorts/"):
        return parts.path.split("/")[2] or None
    return (parse_qs(parts.query).get("v") or [None])[0]


def youtube_link(video_id: str, seconds: int) -> str:
    return "https://www.youtube.com/watch?" + urlencode({"v": video_id, "t": seconds})


def format_transcript(segments: list[Segment], paragraph_seconds: int) -> str:
    """把字幕片段合并为约 paragraph_seconds 秒一段，每段前加时间戳。"""
    lines: list[str] = []
    start: float | None = None
    buf: list[str] = []
    for seg in segments:
        text = " ".join(seg.text.split())
        if not text:
            continue
        if start is None:
            start = seg.start
        elif seg.start - start >= paragraph_seconds:
            lines.append(f"[{format_timestamp(start)}] {' '.join(buf)}")
            start, buf = seg.start, []
        buf.append(text)
    if buf and start is not None:
        lines.append(f"[{format_timestamp(start)}] {' '.join(buf)}")
    return "\n".join(lines)


# ---------- 抓取 ----------

_lock = threading.Lock()
_next_allowed = 0.0


def _throttle(interval: float) -> None:
    """全局限速：所有线程的字幕请求之间至少间隔 interval 秒。"""
    global _next_allowed
    with _lock:
        now = time.monotonic()
        start = max(now, _next_allowed)
        _next_allowed = start + interval
    if start > now:
        time.sleep(start - now)


def _pick(transcript_list):
    """人工字幕优先，其次自动字幕；只接受英文 / 中文。"""
    from youtube_transcript_api import NoTranscriptFound

    for finder in (
        transcript_list.find_manually_created_transcript,
        transcript_list.find_generated_transcript,
    ):
        try:
            return finder(LANGUAGES)
        except NoTranscriptFound:
            continue
    raise TranscriptUnavailable("没有英文或中文字幕")


def _via_api(video_id: str, fs: FetchSettings) -> Transcript:
    from youtube_transcript_api import (
        CouldNotRetrieveTranscript,
        IpBlocked,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        YouTubeRequestFailed,
        YouTubeTranscriptApi,
    )

    api = YouTubeTranscriptApi()
    for attempt in range(fs.transcript_retries + 1):
        _throttle(fs.transcript_interval_seconds)
        try:
            chosen = _pick(api.list(video_id))
            fetched = chosen.fetch()
            segments = [Segment(s.start, s.duration, s.text) for s in fetched.snippets]
            return Transcript(segments, "transcript", chosen.language_code, chosen.is_generated)
        except (TranscriptsDisabled, VideoUnavailable) as exc:
            raise TranscriptUnavailable(f"{type(exc).__name__}：字幕不可用") from None
        except (RequestBlocked, IpBlocked):
            raise  # 被 YouTube 限制：不重试，交给调用方决定是否退回 yt-dlp
        except YouTubeRequestFailed as exc:
            if attempt >= fs.transcript_retries:
                raise TranscriptUnavailable(
                    f"YouTube 请求失败（已重试）：{str(exc)[:160]}"
                ) from None
            time.sleep(fs.retry_backoff_seconds * 2**attempt)
        except CouldNotRetrieveTranscript as exc:  # PoTokenRequired、AgeRestricted 等
            raise TranscriptUnavailable(f"youtube-transcript-api：{type(exc).__name__}") from None
    raise TranscriptUnavailable("未知错误")


def parse_json3(data: bytes | str) -> list[Segment]:
    """yt-dlp 提供的 json3 字幕格式：events[].tStartMs / dDurationMs / segs[].utf8。"""
    payload = json.loads(data)
    segments = []
    for ev in payload.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in ev.get("segs") or [])
        if text.strip():
            segments.append(
                Segment(ev.get("tStartMs", 0) / 1000, ev.get("dDurationMs", 0) / 1000, text)
            )
    return segments


def pick_ytdlp_track(info: dict) -> tuple[str, str, bool] | None:
    """从 yt-dlp 的 info 中选字幕：返回 (json3 URL, 语言, 是否自动字幕)。"""
    for key, generated in (("subtitles", False), ("automatic_captions", True)):
        tracks = info.get(key) or {}
        for lang in LANGUAGES:
            for fmt in tracks.get(lang) or []:
                if fmt.get("ext") == "json3" and fmt.get("url"):
                    return fmt["url"], lang, generated
    return None


def _via_ytdlp(video_id: str, fs: FetchSettings) -> Transcript:
    import yt_dlp

    _throttle(fs.transcript_interval_seconds)
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noprogress": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise TranscriptUnavailable(f"yt-dlp：{str(exc)[:160]}") from None
    track = pick_ytdlp_track(info or {})
    if track is None:
        raise TranscriptUnavailable("yt-dlp：没有英文或中文字幕")
    url, lang, generated = track
    _throttle(fs.transcript_interval_seconds)
    try:
        resp = httpx.get(url, headers={"User-Agent": fs.user_agent}, timeout=fs.timeout_seconds)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise TranscriptUnavailable(f"yt-dlp 字幕下载失败：{type(exc).__name__}") from None
    return Transcript(parse_json3(resp.content), "transcript_ytdlp", lang, generated)


def fetch_transcript(video_id: str, fs: FetchSettings) -> Transcript:
    """先用 youtube-transcript-api；被限制或出错时退回 yt-dlp 一次。
    都失败时抛 TranscriptUnavailable（消息包含两次的原因）。"""
    from youtube_transcript_api import IpBlocked, RequestBlocked

    try:
        return _via_api(video_id, fs)
    except TranscriptUnavailable as exc:
        first = str(exc)
        if first.startswith(("TranscriptsDisabled", "VideoUnavailable", "没有英文或中文字幕")):
            raise
    except (RequestBlocked, IpBlocked) as exc:
        first = f"youtube-transcript-api 被 YouTube 限制（{type(exc).__name__}）"
    try:
        return _via_ytdlp(video_id, fs)
    except TranscriptUnavailable as exc:
        raise TranscriptUnavailable(f"{first}；{exc}") from None


def key_point_link(point: str, url: str) -> tuple[str, str | None]:
    """视频要点 "[12:34] …" → (原文, 跳到该时间点的链接)；不是视频或没有时间戳时链接为 None。"""
    video_id = video_id_from_url(url) if "youtu" in url else None
    seconds, _ = leading_timestamp(point)
    if video_id is None or seconds is None:
        return point, None
    return point, youtube_link(video_id, seconds)
