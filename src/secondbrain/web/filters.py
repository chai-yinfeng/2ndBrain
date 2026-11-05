"""首页 / 历史页按形式与类型筛选（只影响显示，ADR 0017）。纯函数，无 IO。"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

FORMS = {"paper": "论文", "blog": "博客", "video": "视频"}
CTYPES = {
    "frontier": "前沿",
    "explainer": "讲解",
    "survey": "综述",
    "tutorial": "教程",
    "opinion": "观点",
}
PAPER_SOURCES = {"arxiv", "hf_papers"}
PAPER_METHODS = {"arxiv_html", "pdf"}


def item_form(source_type: str | None, url: str | None, extract_method: str | None) -> str:
    """条目的形式：paper / video / blog。只看信源类型、URL 与正文提取方式，不调用 LLM。"""
    parts = urlsplit(url or "")
    host, path = parts.netloc.lower(), parts.path.lower()
    if (
        source_type in PAPER_SOURCES
        or host == "arxiv.org"
        or host.endswith(".arxiv.org")
        or path.endswith(".pdf")
        or extract_method in PAPER_METHODS
    ):
        return "paper"
    if source_type == "youtube" or host.endswith("youtube.com") or host == "youtu.be":
        return "video"
    return "blog"


def clean_filters(form: str | None, ctype: str | None) -> tuple[str | None, str | None]:
    """非法取值视为"全部"。"""
    return (form if form in FORMS else None, ctype if ctype in CTYPES else None)


def matches(form_value: str, ctype_value: str | None, form: str | None, ctype: str | None) -> bool:
    return (form is None or form_value == form) and (ctype is None or ctype_value == ctype)


def chips(
    path: str, params: dict[str, str | None], key: str, options: dict[str, str]
) -> list[tuple[str, str, bool]]:
    """一排 chip：[(文字, 链接, 是否选中)]。"全部"清除该维度，其余维度与参数保留。"""
    current = params.get(key)
    result = []
    for value, label in [(None, "全部"), *options.items()]:
        query = {k: v for k, v in params.items() if v and k != key}
        if value:
            query[key] = value
        href = f"{path}?{urlencode(query)}" if query else path
        result.append((label, href, current == value))
    return result
