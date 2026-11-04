"""wiki 的文件布局与页面读写。所有写操作都限制在 data/wiki 下（slug 规范化 + 路径校验）。

data/wiki/
  index.md                    按类别列出所有页面：- [[slug]] — 一行摘要（由代码根据页面重建）
  log.md                      只追加：## [YYYY-MM-DD] ingest | Title
  pages/{concepts,entities,papers}/<slug>.md   YAML frontmatter + Markdown 正文
  raw/<item_id>.md            原文（标题、URL、正文）；不是 wiki 页面，lint / index / 图谱都忽略
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

PAGE_TYPES = {"concept": "concepts", "entity": "entities", "paper": "papers"}
TYPE_TITLES = {"concept": "Concepts", "entity": "Entities", "paper": "Papers"}
REQUIRED_FIELDS = (
    "title",
    "type",
    "aliases",
    "sources",
    "mastery",
    "last_reviewed",
    "interval_days",
    "ease",
    "created",
    "updated",
)
DEFAULT_EASE = 2.5
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
MAX_SLUG = 80


class WikiError(Exception):
    """非法的 wiki 操作（如 slug 无效、路径越界）。"""


def slugify(text: str) -> str:
    """小写，非 [a-z0-9] 的连续字符替换为短横线，去掉首尾短横线；结果为空时抛 WikiError。"""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:MAX_SLUG].strip("-")
    if not slug:
        raise WikiError(f"无法生成有效的 slug：{text!r}")
    return slug


@dataclass
class Page:
    slug: str
    meta: dict
    body: str
    path: Path | None = None

    @property
    def type(self) -> str:
        return self.meta.get("type", "concept")

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.slug

    @property
    def aliases(self) -> list[str]:
        return list(self.meta.get("aliases") or [])

    def names(self) -> list[str]:
        """用于匹配的名字：slug（短横线换空格）、标题、别名，小写。"""
        names = [self.slug.replace("-", " "), self.title, *self.aliases]
        return [n.lower() for n in names if n]

    def links(self) -> set[str]:
        return {slugify_safe(m) for m in WIKILINK_RE.findall(self.body)} - {""}


def slugify_safe(text: str) -> str:
    try:
        return slugify(text)
    except WikiError:
        return ""


@dataclass
class Wiki:
    root: Path
    raw_root: Path
    pages: dict[str, Page] = field(default_factory=dict)

    # ---------- 布局 ----------

    @property
    def index_path(self) -> Path:
        return self.root / "index.md"

    @property
    def log_path(self) -> Path:
        return self.root / "log.md"

    def page_path(self, slug: str, page_type: str) -> Path:
        if page_type not in PAGE_TYPES:
            raise WikiError(f"未知的页面类型：{page_type!r}")
        if not slug or slug != slugify_safe(slug):
            raise WikiError(f"slug 不合法：{slug!r}")
        path = (self.root / "pages" / PAGE_TYPES[page_type] / f"{slug}.md").resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise WikiError(f"路径越界：{path}")
        return path

    def raw_path(self, item_id: int) -> Path:
        return self.raw_root / f"{int(item_id)}.md"

    # ---------- 读写 ----------

    def save_page(self, page: Page) -> Path:
        path = self.page_path(page.slug, page.type)
        path.parent.mkdir(parents=True, exist_ok=True)
        front = yaml.safe_dump(page.meta, allow_unicode=True, sort_keys=False).strip()
        path.write_text(f"---\n{front}\n---\n\n{page.body.strip()}\n", encoding="utf-8")
        page.path = path
        self.pages[page.slug] = page
        return path

    def write_raw(self, item_id: int, title: str, url: str, content: str) -> Path:
        path = self.raw_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {title}\n\nURL: {url}\n\n{content.strip()}\n", encoding="utf-8")
        return path

    def raw_link(self, item_id: int, page_type: str) -> str:
        """从 pages/<type>/x.md 指向原文的相对链接（Obsidian 可点开）；
        原文在 data/wiki/raw 时为 ../../raw/<id>.md。"""
        page_dir = self.root / "pages" / PAGE_TYPES[page_type]
        return Path(os.path.relpath(self.raw_path(item_id), page_dir)).as_posix()

    def append_log(self, day: date, action: str, title: str, lines: list[str]) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{day.isoformat()}] {action} | {title}\n\n")
            for line in lines:
                f.write(f"- {line}\n")

    def rebuild_index(self) -> None:
        lines = ["# Wiki Index", "", "由 `sb wiki` 根据页面自动生成，请勿手动编辑。"]
        for page_type, heading in TYPE_TITLES.items():
            lines += ["", f"## {heading}", ""]
            entries = sorted(
                (p for p in self.pages.values() if p.type == page_type), key=lambda p: p.slug
            )
            for p in entries:
                summary = (p.meta.get("summary") or "").replace("\n", " ").strip()
                lines.append(f"- [[{p.slug}]] — {summary}" if summary else f"- [[{p.slug}]]")
            if not entries:
                lines.append("（暂无）")
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------- 查询 ----------

    def find(self, name: str) -> Page | None:
        """按 slug / 标题 / 别名查找页面（不区分大小写）。"""
        slug = slugify_safe(name)
        if slug in self.pages:
            return self.pages[slug]
        lowered = name.strip().lower()
        for page in self.pages.values():
            if lowered in page.names():
                return page
        return None

    def mentioned_in(self, text: str, limit: int) -> list[Page]:
        """名字出现在 text 中的页面（名字至少 3 个字符），按名字长度降序取前 limit 个。"""
        haystack = text.lower()
        hits = []
        for page in self.pages.values():
            best = max((len(n) for n in page.names() if len(n) >= 3 and n in haystack), default=0)
            if best:
                hits.append((best, page))
        hits.sort(key=lambda h: (-h[0], h[1].slug))
        return [p for _, p in hits[:limit]]

    def concepts(self) -> list[Page]:
        return [p for p in self.pages.values() if p.type == "concept"]


def parse_page(text: str, slug: str, path: Path | None = None) -> Page:
    meta: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            loaded = yaml.safe_load(text[4:end]) or {}
            meta = loaded if isinstance(loaded, dict) else {}
            body = text[end + 4 :].lstrip("\n")
    return Page(slug=slug, meta=meta, body=body, path=path)


def open_wiki(root: Path, raw_root: Path) -> Wiki:
    """打开 wiki（首次使用时初始化目录、index.md 与 log.md）。"""
    for folder in PAGE_TYPES.values():
        (root / "pages" / folder).mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    wiki = Wiki(root=root, raw_root=raw_root)
    for folder in PAGE_TYPES.values():
        for path in sorted((root / "pages" / folder).glob("*.md")):
            wiki.pages[path.stem] = parse_page(path.read_text(encoding="utf-8"), path.stem, path)
    if not wiki.log_path.exists():
        wiki.log_path.write_text("# Wiki Log\n\n只追加的操作记录。\n", encoding="utf-8")
    if not wiki.index_path.exists():
        wiki.rebuild_index()
    return wiki


def new_meta(
    title: str,
    page_type: str,
    today: date,
    *,
    summary: str = "",
    aliases: list[str] | None = None,
    mastery: str | None = None,
    sources: list[int] | None = None,
    interval_days: int = 1,
) -> dict:
    return {
        "title": title,
        "type": page_type,
        "summary": summary,
        "aliases": aliases or [],
        "sources": sources or [],
        "mastery": mastery,
        "last_reviewed": today.isoformat(),
        "interval_days": interval_days,
        "ease": DEFAULT_EASE,
        "created": today.isoformat(),
        "updated": today.isoformat(),
    }


# ---------- 正文的受限编辑 ----------


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    target = heading.strip().lower()
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip().lower() == target:
            end = next(
                (j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines)
            )
            return i, end
    return None


def append_section(body: str, heading: str, text: str) -> str:
    """在"来源"段之前追加一个新段落（若同名段已存在，则把内容追加到该段末尾）。"""
    lines = body.rstrip().splitlines()
    bounds = _section_bounds(lines, heading)
    block = text.strip().splitlines()
    if bounds:
        start, end = bounds
        return "\n".join(lines[:end] + ["", *block] + lines[end:]).strip() + "\n"
    sources = _section_bounds(lines, "来源")
    insert_at = sources[0] if sources else len(lines)
    new = ["", f"## {heading.strip()}", "", *block, ""]
    return "\n".join(lines[:insert_at] + new + lines[insert_at:]).strip() + "\n"


def replace_section(body: str, heading: str, text: str) -> str:
    lines = body.rstrip().splitlines()
    bounds = _section_bounds(lines, heading)
    if not bounds:
        return append_section(body, heading, text)
    start, end = bounds
    new = [lines[start], "", *text.strip().splitlines(), ""]
    return "\n".join(lines[:start] + new + lines[end:]).strip() + "\n"


def add_list_items(body: str, heading: str, items: list[str]) -> str:
    """向某段（默认不存在则新建）追加列表项，已存在的同一行不重复添加。"""
    lines = body.rstrip().splitlines()
    bounds = _section_bounds(lines, heading)
    existing = set(lines[bounds[0] : bounds[1]]) if bounds else set()
    new_items = [f"- {i}" for i in items if f"- {i}" not in existing]
    if not new_items:
        return body
    if bounds:
        start, end = bounds
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        return "\n".join(lines[:insert_at] + new_items + lines[insert_at:]).strip() + "\n"
    return "\n".join(lines + ["", f"## {heading}", "", *new_items]).strip() + "\n"
