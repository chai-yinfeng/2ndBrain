"""PDF 文本提取（pypdf），只取前 max_pages 页。"""

from __future__ import annotations

import io
import logging

from pypdf import PdfReader

# pypdf 对不规范的 PDF 会打印大量告警（如 XObject 调用次数超限），不影响已提取的文本。
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf_text(data: bytes, max_pages: int) -> str:
    reader = PdfReader(io.BytesIO(data))
    count = min(len(reader.pages), max_pages)
    texts = [(reader.pages[i].extract_text() or "").strip() for i in range(count)]
    return "\n\n".join(t for t in texts if t).strip()
