"""wiki 的文件布局与页面读写。所有写操作都限制在 data/wiki 下（slug 规范化 + 路径校验）。

data/wiki/
  index.md                    按类别列出所有页面：- [[slug]] — 一行摘要（由代码根据页面重建）
  log.md                      只追加：## [YYYY-MM-DD] ingest | Title
  pages/{concepts,entities,papers,materials}/<slug>.md   YAML frontmatter + Markdown 正文
  raw/<item_id>.md            原文（标题、URL、正文）；不是 wiki 页面，lint / index / 图谱都忽略
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

PAGE_TYPES = {
    "concept": "concepts", "entity": "entities", "paper": "papers", "material": "materials",
}
TYPE_TITLES = {
    "concept": "概念",
    "entity": "实体",
    "paper": "论文",
    "article": "文章与网页",
    "video": "视频",
    "other": "其他资料",
}
REQUIRED_FIELDS = (
    "title",
    "type",
    "aliases",
    "sources",
    "created",
    "updated",
)
WIKILINK_RE = re.compile(r"\[\[([^\]\n|#]*)(?:#[^\]\n|]*)?(?:\|[^\]\n]*)?\]\]")
_SLUG_RE = re.compile(r"[\W_]+")
MAX_SLUG = 80


class WikiError(Exception):
    """非法的 wiki 操作（如 slug 无效、路径越界）。"""


def slugify(text: str) -> str:
    """保留 Unicode 字母和数字；原有 ASCII slug 不变，路径分隔符规范化为短横线。"""
    slug = _SLUG_RE.sub("-", unicodedata.normalize("NFC", text).lower())
    slug = slug.strip("-")[:MAX_SLUG].strip("-")
    if not slug:
        raise WikiError(f"无法生成有效的 slug：{text!r}")
    return slug


def markdown_without_code(text: str) -> str:
    """遮盖围栏和行内代码，保留偏移与换行，供 wikilink 消费者共用。"""
    lines = []
    fence: str | None = None
    fence_size = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence is not None:
            if marker and marker[1][0] == fence and len(marker[1]) >= fence_size:
                if not marker[2].strip():
                    fence = None
            lines.append(re.sub(r"[^\n]", " ", line))
        elif marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence, fence_size = marker[1][0], len(marker[1])
            lines.append(re.sub(r"[^\n]", " ", line))
        else:
            lines.append(line)
    text = "".join(lines)
    chars = list(text)
    position = 0
    while position < len(text):
        if text[position] != "`":
            position += 1
            continue
        end = position + 1
        while end < len(text) and text[end] == "`":
            end += 1
        marker = text[position:end]
        closing = re.search(rf"(?<!`){re.escape(marker)}(?!`)", text[end:])
        if closing is None:
            position = end
            continue
        stop = end + closing.end()
        for index in range(position, stop):
            if chars[index] != "\n":
                chars[index] = " "
        position = stop
    return "".join(chars)


def wikilink_targets(text: str) -> set[str]:
    """保留目标的中文、路径和大小写；别名/标题锚点不属于页面身份。"""
    return {match[1].strip() for match in WIKILINK_RE.finditer(markdown_without_code(text))}


def _name_key(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().casefold()


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
    def kind(self) -> str:
        """展示类别与稳定文件类型分离；人工分类优先。"""
        manual = self.meta.get("knowledge_kind")
        if manual in TYPE_TITLES:
            return manual
        if self.type == "material":
            kind = self.meta.get("material_kind")
            return kind if kind in {"paper", "article", "video", "other"} else "other"
        return self.type if self.type in TYPE_TITLES else "other"

    @property
    def is_trashed(self) -> bool:
        return self.meta.get("trashed") is True

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.slug

    @property
    def aliases(self) -> list[str]:
        aliases = self.meta.get("aliases") or []
        return [aliases] if isinstance(aliases, str) else list(aliases)

    @property
    def knowledge_status(self) -> str:
        """无标记历史页保持 legacy；不能由文件存在推断用户读过。"""
        status = self.meta.get("knowledge_status")
        return status if status in ("draft", "recorded", "legacy") else "legacy"

    @property
    def material_sync_status(self) -> str | None:
        marker = self.meta.get("material_sync")
        status = marker.get("status") if isinstance(marker, dict) else None
        return status if status in {"withdrawn", "unsupported", "review"} else None

    @property
    def is_visible(self) -> bool:
        """正式知识视图；人工正文始终留在文件中，来源失效只影响机器页可见性。"""
        return (
            not self.is_trashed
            and self.knowledge_status != "draft"
            and self.material_sync_status not in {"withdrawn", "unsupported"}
        )

    def names(self) -> list[str]:
        """用于名称查找；文件名与标题/别名均保留，不把中文转为空 slug。"""
        stem = self.path.stem if self.path else self.slug.rsplit("/", 1)[-1]
        names = [self.slug, stem, stem.replace("-", " "), self.title, *self.aliases]
        return [_name_key(n) for n in names if isinstance(n, str) and n]

    def links(self) -> set[str]:
        return wikilink_targets(self.body)


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

    def _is_page_path(self, path: Path) -> bool:
        path = path.resolve()
        root = self.root.resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".md":
            return False
        relative = path.relative_to(root)
        return (
            relative.as_posix() not in {"index.md", "log.md"}
            and not path.is_relative_to(self.raw_root.resolve())
            and not any(part.startswith(".") for part in relative.parts)
            and not any(
                part in {"raw", "resources", "attachments", "assets", "notes"}
                for part in relative.parts[:-1]
            )
        )

    def save_page(self, page: Page) -> Path:
        path = page.path.resolve() if page.path else self.page_path(page.slug, page.type)
        if not self._is_page_path(path):
            raise WikiError(f"页面路径越界：{path}")
        if page.path is None and path.exists():
            raise WikiError(f"页面已存在，请先解析目标再更新：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        front = yaml.safe_dump(page.meta, allow_unicode=True, sort_keys=False).strip()
        path.write_text(f"---\n{front}\n---\n\n{page.body.strip()}\n", encoding="utf-8")
        collision = any(
            existing.path is not None
            and existing.path.resolve() != path
            and _name_key(existing.path.stem) == _name_key(path.stem)
            for existing in self.pages.values()
        )
        page.path = path
        self.pages[page.slug] = page
        if collision:
            self.reload_pages()
        return path

    def save_metadata(self, page: Page, *, expected_hash: str | None = None) -> None:
        """只写 frontmatter，完整保留原文件正文；外部编辑后不覆盖旧预览。"""
        if page.path is None or not self._is_page_path(page.path):
            raise WikiError("元数据同步需要已存在的 wiki 页面")
        original = page.path.read_bytes().decode("utf-8")
        if expected_hash is not None:
            import hashlib

            if hashlib.sha256(original.encode("utf-8")).hexdigest() != expected_hash:
                raise WikiError("wiki 页面已变化，请重新预览")
        frontmatter = re.match(r"\A---\r?\n.*?\r?\n---(?=\r?\n|\Z)", original, re.S)
        suffix = original[frontmatter.end():] if frontmatter else "\n\n" + original
        front = yaml.safe_dump(page.meta, allow_unicode=True, sort_keys=False).strip()
        page.path.write_bytes(f"---\n{front}\n---{suffix}".encode())

    def write_raw(self, item_id: int, title: str, url: str, content: str) -> Path:
        path = self.raw_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {title}\n\nURL: {url}\n\n{content.strip()}\n", encoding="utf-8")
        return path

    def reload_pages(self) -> None:
        """以 Markdown 为事实来源，写入前重新读取外部编辑、删除与新增的页面。"""
        self.pages.clear()
        paths = sorted(path for path in self.root.rglob("*.md") if self._is_page_path(path))
        stems: dict[str, int] = {}
        for path in paths:
            key = _name_key(path.stem)
            stems[key] = stems.get(key, 0) + 1
        for path in paths:
            slug = path.stem
            if stems[_name_key(slug)] > 1:
                slug = path.relative_to(self.root).as_posix()
            self.pages[slug] = parse_page(path.read_text(encoding="utf-8"), slug, path)

    def reindex(self) -> None:
        """仅重读页面并重建派生目录；不重写页面或用户内容。"""
        self.reload_pages()
        self.rebuild_index()

    def raw_link(self, item_id: int, page_type: str, source: Page | None = None) -> str:
        """从 pages/<type>/x.md 指向原文的相对链接（Obsidian 可点开）；
        原文在 data/wiki/raw 时为 ../../raw/<id>.md。"""
        page_dir = self.root / "pages" / PAGE_TYPES[page_type]
        if source is not None and source.path is not None:
            page_dir = source.path.parent
        return Path(os.path.relpath(self.raw_path(item_id), page_dir)).as_posix()

    def append_log(self, day: date, action: str, title: str, lines: list[str]) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{day.isoformat()}] {action} | {title}\n\n")
            for line in lines:
                f.write(f"- {line}\n")

    def rebuild_index(self) -> None:
        lines = ["# Wiki Index", "", "由 `sb wiki` 根据页面自动生成，请勿手动编辑。"]
        for page_type, heading in TYPE_TITLES.items():
            entries = sorted(
                (p for p in self.pages.values()
                 if p.kind == page_type and p.is_visible),
                key=lambda p: p.slug,
            )
            if not entries:
                continue
            lines += ["", f"## {heading}", ""]
            for p in entries:
                summary = (p.meta.get("summary") or "").replace("\n", " ").strip()
                label = "〔历史页：阅读状态未知〕" if p.knowledge_status == "legacy" else ""
                if p.material_sync_status == "review":
                    label += "〔来源已变化：待复核〕"
                suffix = f" {label}" if label else ""
                lines.append(f"- [[{p.slug}]]{suffix}" + (f" — {summary}" if summary else ""))
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---------- 查询 ----------

    def resolve(self, name: str, source: Page | None = None) -> list[Page]:
        """精确 ID 优先；标题/别名歧义返回全部候选，不按遍历顺序选页面。"""
        target = name.strip()
        if target.startswith("[[") and target.endswith("]]"):
            target = target[2:-2]
        target = target.split("|", 1)[0].split("#", 1)[0].strip()
        if not target:
            return [source] if source is not None else []
        key = _name_key(target)
        exact = [page for slug, page in self.pages.items() if _name_key(slug) == key]
        if exact:
            return sorted(exact, key=lambda page: page.slug)
        explicit_path = "/" in target or target.lower().endswith(".md")
        if explicit_path:
            if not target.lower().endswith(".md"):
                target += ".md"
            roots = [self.root]
            if (
                source is not None and source.path is not None
                and not target.startswith(("pages/", "/"))
            ):
                roots.insert(0, source.path.parent)
            for root in roots:
                path = (root / target.lstrip("/")).resolve()
                matches = [
                    page for page in self.pages.values()
                    if page.path is not None and page.path.resolve() == path
                ]
                if matches:
                    return sorted(matches, key=lambda page: page.slug)
            # 无匹配路径时仍允许含斜线的真实标题/别名，但不退化为 basename 猜测。
            if "/" not in target:
                key = _name_key(target[:-3])
        matches = [page for page in self.pages.values() if key in page.names()]
        return sorted(matches, key=lambda page: page.slug)

    def find(self, name: str, source: Page | None = None) -> Page | None:
        """只返回唯一匹配；缺失与歧义均不猜测。"""
        matches = self.resolve(name, source)
        return matches[0] if len(matches) == 1 else None

    def material_page(self, item_id: int) -> Page | None:
        """按来源身份复用旧 paper 页，不按标题合并，不改变稳定路径。"""
        direct = self.pages.get(f"material-{item_id}")
        if direct is not None:
            return direct
        matches = [
            page for page in self.pages.values()
            if page.type in {"material", "paper"}
            and (page.meta.get("material_id") == item_id
                 or (page.type == "paper" and item_id in (page.meta.get("sources") or [])))
        ]
        if len(matches) > 1:
            raise WikiError("同一材料有多个知识页，请先处理身份冲突")
        return matches[0] if matches else None

    def is_resource_link(self, name: str, source: Page | None = None) -> bool:
        """原文与附件不是知识节点，也不是缺失知识页。"""
        target = name.split("|", 1)[0].split("#", 1)[0].strip()
        suffix = Path(target).suffix.lower()
        if suffix and suffix != ".md":
            return True
        root = source.path.parent if source is not None and source.path else self.root
        path = (root / target.lstrip("/")).resolve()
        if path.is_relative_to(self.raw_root.resolve()):
            return True
        parts = Path(target).parts
        return any(part in {"raw", "resources", "attachments", "assets", "notes"} for part in parts)

    def mentioned_in(self, text: str, limit: int) -> list[Page]:
        """名字出现在 text 中的页面（名字至少 3 个字符），按名字长度降序取前 limit 个。"""
        haystack = text.lower()
        hits = []
        for page in self.pages.values():
            if not page.is_visible:
                continue
            best = max((len(n) for n in page.names() if len(n) >= 3 and n in haystack), default=0)
            if best:
                hits.append((best, page))
        hits.sort(key=lambda h: (-h[0], h[1].slug))
        return [p for _, p in hits[:limit]]

    def concepts(self) -> list[Page]:
        return [
            p for p in self.pages.values()
            if p.type == "concept" and p.is_visible
        ]


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


def _migrate_material_notes(wiki: Wiki) -> None:
    """旧独立笔记与材料页重名会让 Obsidian 选错目标；保留字节并更新笔记路径引用。"""
    replacements = {}
    for old in sorted((wiki.root / "notes").glob("material-*.md")):
        if re.fullmatch(r"material-\d+\.md", old.name) is None:
            continue
        new = old.with_name(f"{old.stem}-notes.md")
        if new.exists():
            raise WikiError(f"笔记迁移目标已存在，未覆盖任何内容：{new}")
        old.rename(new)
        replacements[f"notes/{old.name}"] = f"notes/{new.name}"
    if not replacements:
        return
    for page in wiki.pages.values():
        if page.path is None:
            continue
        original = page.path.read_text(encoding="utf-8")
        edits = [
            (match.start(), match.end(), replacements[match.group()])
            for match in re.finditer(
                r"notes/material-\d+\.md", markdown_without_code(original)
            )
            if match.group() in replacements
        ]
        updated = original
        for start, end, replacement in reversed(edits):
            updated = updated[:start] + replacement + updated[end:]
        if updated != original:
            page.path.write_text(updated, encoding="utf-8")
    wiki.reload_pages()


def open_wiki(root: Path, raw_root: Path) -> Wiki:
    """打开 wiki（首次使用时初始化目录、index.md 与 log.md）。"""
    for folder in PAGE_TYPES.values():
        (root / "pages" / folder).mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    wiki = Wiki(root=root, raw_root=raw_root)
    wiki.reload_pages()
    _migrate_material_notes(wiki)
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
    sources: list[int] | None = None,
) -> dict:
    return {
        "title": title,
        "type": page_type,
        "summary": summary,
        "aliases": aliases or [],
        "sources": sources or [],
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
