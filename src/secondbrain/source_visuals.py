"""显式按需下载来源封面；页面读取只查本地缓存，不请求网络。

公网与逐跳重定向检查、解压后响应/累计预算复用来源 Reader。
只保存已识别的有界栅格图片，永远不把上游图片 URL 交给浏览器。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from secondbrain.models import FetchSettings
from secondbrain.net import FetchError, HttpClient
from secondbrain.source_discovery import _public_url, _Reader

MAX_RESPONSE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 2_097_152
MAX_REQUESTS = 6
MAX_PIXELS = 16_777_216
MAX_CANDIDATES = 4
_CACHE_LOCK = threading.Lock()
_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon"}


@dataclass(frozen=True)
class CachedVisual:
    path: Path
    media_type: str
    version: str


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og: list[str] = []
        self.twitter: list[str] = []
        self.icons: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            target = self.og if key == "og:image" else self.twitter
            if key in {"og:image", "twitter:image", "twitter:image:src"}:
                value = values.get("content") or ""
                if value and len(value) <= 4096 and len(target) < MAX_CANDIDATES:
                    target.append(value)
        elif tag == "link":
            rel = (values.get("rel") or "").lower().split()
            value = values.get("href") or ""
            if "icon" in rel and value and len(value) <= 4096 and len(self.icons) < 2:
                self.icons.append(value)


def _dimensions(width: int, height: int) -> bool:
    return 0 < width <= 8192 and 0 < height <= 8192 and width * height <= MAX_PIXELS


def _raster_type(data: bytes) -> str | None:
    """识别实际栅格结构及尺寸；不信任扩展名/MIME，不解码远程图片。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset = 8
        first = True
        has_pixels = False
        while offset + 12 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], "big")
            end = offset + size + 12
            if end > len(data):
                return None
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:end - 4]
            if zlib.crc32(data[offset + 4:end - 4]) != int.from_bytes(data[end - 4:end], "big"):
                return None
            if first:
                if kind != b"IHDR" or size != 13:
                    return None
                if not _dimensions(*struct.unpack(">II", payload[:8])):
                    return None
                first = False
            has_pixels |= kind == b"IDAT" and bool(size)
            if kind == b"IEND":
                return "image/png" if size == 0 and end == len(data) and has_pixels else None
            offset = end
        return None
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 14 and data[-1:] == b";":
        return "image/gif" if _dimensions(*struct.unpack("<HH", data[6:10])) else None
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                return None
            while offset < len(data) and data[offset] == 255:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            size = int.from_bytes(data[offset:offset + 2], "big")
            if size < 2 or offset + size > len(data):
                return None
            if marker in {0xC0, 0xC1, 0xC2}:
                if size < 8:
                    return None
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return "image/jpeg" if _dimensions(width, height) else None
            offset += size
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            return None
        kind = data[12:16]
        size = int.from_bytes(data[16:20], "little")
        if size + 20 > len(data):
            return None
        if kind == b"VP8X" and size == 10:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif kind == b"VP8 " and size >= 10 and data[23:26] == b"\x9d\x01\x2a":
            width, height = (v & 0x3FFF for v in struct.unpack("<HH", data[26:30]))
        elif kind == b"VP8L" and size >= 5 and data[20] == 47:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        else:
            return None
        return "image/webp" if _dimensions(width, height) else None
    if data[:4] == b"\x00\x00\x01\x00" and len(data) >= 22:
        count = int.from_bytes(data[4:6], "little")
        if not 0 < count <= 32 or len(data) < 6 + count * 16:
            return None
        for index in range(count):
            entry = data[6 + index * 16:22 + index * 16]
            size, offset = struct.unpack("<II", entry[8:16])
            if offset < 6 + count * 16 or offset + size > len(data) or size < 40:
                return None
            bitmap = data[offset:offset + size]
            if bitmap.startswith(b"\x89PNG"):
                if _raster_type(bitmap) != "image/png":
                    return None
            elif bitmap[:4] == b"\x28\x00\x00\x00":
                width, height = struct.unpack("<ii", bitmap[4:12])
                if not _dimensions(width, abs(height) // 2):
                    return None
            else:
                return None
        return "image/x-icon"
    return None


def _key(homepage: str) -> str:
    return hashlib.sha256(_public_url(homepage).encode()).hexdigest()


def read_visual(homepage: str, cache_dir: Path) -> CachedVisual | None:
    """只读本地缓存。缓存绑定规范化主页，不与可编辑 source id 绑定。"""
    try:
        key = _key(homepage)
        metadata = cache_dir / f"{key}.json"
        if metadata.stat().st_size > 512:
            return None
        value = json.loads(metadata.read_text(encoding="utf-8"))
        version, media_type = value["version"], value["media_type"]
        if not re.fullmatch(r"[a-f0-9]{64}", version) or media_type not in _MIMES:
            return None
        path = cache_dir / f"{key}-{version}.image"
        if not 0 < path.stat().st_size <= MAX_RESPONSE_BYTES:
            return None
        return CachedVisual(path, media_type, version)
    except (FetchError, OSError, ValueError, KeyError, TypeError):
        return None


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_visual(
    homepage: str, client: HttpClient, settings: FetchSettings, cache_dir: Path,
) -> dict:
    """仅显式 POST 调用；失败保留已有封面，结果不影响采集配置/任务状态。"""
    reader = _Reader(client, settings)
    reader.remaining = MAX_REQUESTS
    reader.max_bytes = min(reader.max_bytes, MAX_RESPONSE_BYTES)
    reader.max_total_bytes = min(reader.max_total_bytes, MAX_TOTAL_BYTES)
    reader.phase = "来源封面"
    try:
        homepage = _public_url(homepage)
        candidates = [urljoin(homepage, "/favicon.ico")]
        try:
            response = reader.get(homepage)
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type in {"text/html", "application/xhtml+xml"}:
                images = _Images()
                images.feed(response.text)
                candidates = list(dict.fromkeys(
                    urljoin(str(response.url), value)
                    for value in images.og[:1] + images.twitter[:1] + images.icons[:1]
                    + [urljoin(str(response.url), "/favicon.ico")]
                ))[:MAX_CANDIDATES]
        except (FetchError, ValueError):
            # 主页失败仍可尝试站点图标，但复用已消耗的请求/字节预算。
            pass
        for candidate in candidates:
            try:
                image = reader.get(candidate)
                mime = image.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime == "image/vnd.microsoft.icon":
                    mime = "image/x-icon"
                actual = _raster_type(image.content)
                if actual is None or mime != actual:
                    continue
                key = _key(homepage)
                version = hashlib.sha256(image.content).hexdigest()
                cache_dir.mkdir(parents=True, exist_ok=True)
                with _CACHE_LOCK:
                    previous = read_visual(homepage, cache_dir)
                    path = cache_dir / f"{key}-{version}.image"
                    _atomic_write(path, image.content)
                    metadata = json.dumps({"version": version, "media_type": actual}).encode()
                    _atomic_write(cache_dir / f"{key}.json", metadata)
                    if previous and previous.path != path:
                        previous.path.unlink(missing_ok=True)
                return {"available": True, "version": version, "message": "本地封面已更新。"}
            except FetchError:
                continue
    except (FetchError, OSError, ValueError):
        pass
    cached = read_visual(homepage, cache_dir)
    return {
        "available": cached is not None,
        "version": cached.version if cached else "",
        "message": "未找到安全可用的图片；保留现有封面。" if cached else
                   "未找到安全可用的图片，使用名称首字母。",
    }
