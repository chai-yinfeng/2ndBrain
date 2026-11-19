"""无需模型的资料分类；展示类别不改变 Markdown 身份与路径。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from secondbrain import db
from secondbrain.wiki.store import Wiki

MATERIAL_KINDS = frozenset({"paper", "article", "video", "other"})
_VIDEO_HOSTS = {"youtube.com", "youtu.be", "bilibili.com", "vimeo.com"}


def classify_material(row, bibliography=None) -> str:
    """只使用可信书目、资料字段与地址，不由标题猜论文。"""
    values = dict(row)
    facts = dict(bibliography or {})
    if (values.get("doi") or values.get("arxiv_id") or facts.get("doi")
            or facts.get("arxiv_id")
            or (facts.get("kind") == "paper" and facts.get("verified") is True)):
        return "paper"
    url = urlsplit(values.get("url") or "")
    host = (url.hostname or "").lower()
    if ((host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
         and url.path.startswith(("/abs/", "/pdf/")))
            or (host in {"doi.org", "dx.doi.org"} and url.path.startswith("/10."))):
        return "paper"
    source = str(values.get("source_type") or values.get("source_id") or "").lower()
    if (any(host == domain or host.endswith("." + domain) for domain in _VIDEO_HOSTS)
            or values.get("extract_method") in {"youtube_transcript", "bilibili_transcript",
                                                 "transcript", "no_transcript"}
            or source in {"youtube", "bilibili", "video"}
            or source.startswith(("youtube:", "bilibili:"))):
        return "video"
    if url.scheme in {"http", "https"} and not url.path.lower().endswith(".pdf"):
        return "article"
    return "other"


_LEGACY_BLOCK = re.compile(
    r"(?m)^## 机器整理 · 材料 (?P<id>\d+)\n\n"
    r"(?P<marker><!-- material-(?P=id):(?P<digest>[0-9a-f]{20}) -->)\n\n"
    r"> 机器整理，未经用户核对；(?P<basis>材料陈述与机器推断分别标注。|"
    r"仅依据用户学习记录；原文缺失，不代表原文结论。)\n\n"
    r"(?P<text>.*?)(?=^## |\Z)", re.S,
)


def migrate_machine_blocks(body: str, wiki: Wiki | None = None) -> str:
    """仅迁移完整匹配已发布自动模板的段落；不搜索替换人工措辞。"""
    def source_labels(text):
        if wiki is None:
            return text

        def label(match):
            page = wiki.find(match[1])
            return f"[[{match[1]}|{page.title}]]" if page is not None else match[0]

        return re.sub(r"\[\[(material-\d+)\]\]", label, text)

    def replace(match):
        key = f"material-{match['id']}:{match['digest']}"
        warning = (
            "> 仅依据用户学习记录；原文缺失，不代表原文结论。\n\n"
            if match["basis"].startswith("仅依据") else ""
        )
        return (
            f"## 整理\n\n<!-- wiki-ingest:start {key} -->\n{match['marker']}\n\n"
            f"{warning}{source_labels(match['text'].rstrip())}\n\n"
            f"<!-- wiki-ingest:end {key} -->\n\n"
        )
    return _LEGACY_BLOCK.sub(replace, body)


def sync_material_kinds(wiki: Wiki, conn) -> list[str]:
    """显式无模型迁移：只补源资料类别及确切自动块；保留路径与人工元数据。"""
    wiki.reload_pages()
    changed = []
    for page in wiki.pages.values():
        metadata_changed = False
        if page.type in {"material", "paper"} and not page.meta.get("material_kind"):
            if page.type == "paper":
                kind = "paper"
            elif page.meta.get("generated_by") == "wiki_ingest":
                item_id = page.meta.get("material_id")
                if type(item_id) is not int:
                    match = re.fullmatch(r"material-(\d+)", page.slug)
                    item_id = int(match[1]) if match else None
                row = db.item_detail(conn, item_id) if item_id is not None else None
                kind = (classify_material(row, db.material_bibliography(conn, item_id))
                        if row is not None else "other")
            else:
                continue
            page.meta["material_kind"] = kind
            metadata_changed = True
        original = page.path.read_bytes().decode("utf-8")
        migrated = migrate_machine_blocks(original, wiki)
        if migrated != original:
            page.path.write_bytes(migrated.encode("utf-8"))
        if metadata_changed:
            wiki.save_metadata(page)
        if metadata_changed or migrated != original:
            changed.append(page.slug)
    wiki.reload_pages()
    wiki.rebuild_index()
    return changed
