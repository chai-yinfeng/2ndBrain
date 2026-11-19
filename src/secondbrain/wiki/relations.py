"""人工关系约束保存在页面 frontmatter；图谱与机器写入均由同一规则派生。"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from secondbrain.wiki.store import (
    WIKILINK_RE,
    Page,
    Wiki,
    WikiError,
    append_section,
    markdown_without_code,
)


def page_identity(wiki: Wiki, page: Page) -> str:
    """以 vault 相对路径记录身份，不随同名文件出现而改变。"""
    path = page.path or wiki.page_path(page.slug, page.type)
    return path.resolve().relative_to(wiki.root.resolve()).as_posix()


def human_relations(page: Page) -> list[dict]:
    entries = page.meta.get("human_relations") or []
    if not isinstance(entries, list):
        return []
    return [
        entry for entry in entries
        if isinstance(entry, dict)
        and entry.get("mode") in ("pin", "block")
        and isinstance(entry.get("target"), str)
    ]


def relation_controls(wiki: Wiki, page: Page) -> list[dict]:
    """列出本页与反向人工决定，供界面解释被隐藏的边；不改变文件。"""
    identity = page_identity(wiki, page)
    controls = []
    for owner in sorted(wiki.pages.values(), key=lambda p: p.slug):
        for entry in human_relations(owner):
            incoming = page_identity(wiki, owner) != identity
            if incoming and entry["target"] != identity:
                continue
            target = wiki.find(entry["target"])
            target_name = target.slug if target is not None else entry["target"]
            controls.append({
                **entry, "source": owner.slug, "target": target_name,
                "target_path": entry["target"], "incoming": incoming,
                "other": owner.slug if incoming else target_name,
            })
    return controls


def relation_blocked(wiki: Wiki, source: Page, target: Page) -> bool:
    """禁止关系无方向：反向机器链接也不能绕过用户决定。"""
    source_id, target_id = page_identity(wiki, source), page_identity(wiki, target)
    return any(
        entry["mode"] == "block" and entry["target"] == identity
        for page, identity in ((source, target_id), (target, source_id))
        for entry in human_relations(page)
    )


def filter_machine_links(wiki: Wiki, source: Page, text: str) -> str:
    """仅过滤本次机器文字；被禁止的链接退化为普通文字，不触碰个人正文。"""
    masked = markdown_without_code(text)
    edits = []
    for match in WIKILINK_RE.finditer(masked):
        targets = wiki.resolve(match[1], source=source)
        original = text[match.start():match.end()]
        if any(target.is_trashed or relation_blocked(wiki, source, target) for target in targets):
            label = original[2:-2].split("|", 1)[-1].split("#", 1)[0]
            if len(targets) == 1 and "|" not in original:
                label = targets[0].title
            if any(target.is_trashed for target in targets):
                label += "（已回收）"
            edits.append((match.start(), match.end(), label))
        elif (len(targets) == 1 and targets[0].type in {"material", "paper"}
              and "|" not in original):
            edits.append((match.start(), match.end(), f"[[{match[1]}|{targets[0].title}]]"))
    for start, end, label in reversed(edits):
        text = text[:start] + label + text[end:]
    return text


def _marker(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:20]


def _remove_entry_body(body: str, target: str) -> str:
    marker = _marker(target)
    return re.sub(
        rf"<!-- human-relation:{marker} -->\n.*?<!-- /human-relation:{marker} -->\n?",
        "", body, flags=re.DOTALL,
    )


def _remove_machine_links(wiki: Wiki, source: Page, target: Page) -> None:
    """释放约束时不复活旧机器边；只移除明确机器归属小节中的链接语法。"""
    masked = markdown_without_code(source.body)
    headings = list(re.finditer(r"(?m)^## (.+)$", masked))
    edits = []
    for i, heading in enumerate(headings):
        name = heading[1].strip()
        if name != "机器关联" and not name.startswith("机器整理 · 材料 "):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(masked)
        for match in WIKILINK_RE.finditer(masked, heading.end(), end):
            if target in wiki.resolve(match[1], source=source):
                original = source.body[match.start():match.end()]
                label = original[2:-2].split("|", 1)[-1].split("#", 1)[0]
                edits.append((match.start(), match.end(), label))
    for block in re.finditer(
        r"<!-- wiki-ingest:start (?P<key>material-\d+:[0-9a-f]{20}) -->"
        r".*?<!-- wiki-ingest:end (?P=key) -->", masked, re.S,
    ):
        for match in WIKILINK_RE.finditer(masked, block.start(), block.end()):
            if target in wiki.resolve(match[1], source=source):
                original = source.body[match.start():match.end()]
                label = original[2:-2].split("|", 1)[-1].split("#", 1)[0]
                edits.append((match.start(), match.end(), label))
    for block in re.finditer(
        r"<!-- wiki-rewrite:start -->.*?<!-- wiki-rewrite:end -->", masked, re.S,
    ):
        for match in WIKILINK_RE.finditer(masked, block.start(), block.end()):
            if target in wiki.resolve(match[1], source=source):
                original = source.body[match.start():match.end()]
                label = original[2:-2].split("|", 1)[-1].split("#", 1)[0]
                edits.append((match.start(), match.end(), label))
    for start, end, label in sorted(set(edits), reverse=True):
        source.body = source.body[:start] + label + source.body[end:]


def set_relation(
    wiki: Wiki,
    source_name: str,
    target_name: str,
    mode: str,
    reason: str = "",
    today: date | None = None,
) -> dict:
    """设置无方向人工 pin/block/reset；缺失、歧义或自连接拒绝且不落盘。

    返回用于界面展示的 source/target（当前页面 ID）、mode、reason。
    reset 仅解除约束/移除本 API 管理的关系条目，不恢复旧机器链接。
    """
    if mode not in {"pin", "block", "reset"}:
        raise WikiError("关系模式必须是 pin、block 或 reset")
    wiki.reload_pages()
    pages = []
    for name in (source_name, target_name):
        matches = wiki.resolve(name)
        if len(matches) != 1:
            detail = "页面不存在" if not matches else "名称有歧义，请使用页面 ID/路径"
            raise WikiError(f"{name!r}：{detail}")
        pages.append(matches[0])
    source, target = pages
    if source.slug == target.slug:
        raise WikiError("不能设置页面与自身的关系")
    before = {page.slug: (page.meta.copy(), page.body) for page in pages}
    day = today or date.today()
    for page, other in ((source, target), (target, source)):
        identity = page_identity(wiki, other)
        entries = [entry for entry in human_relations(page) if entry["target"] != identity]
        if entries:
            page.meta["human_relations"] = entries
        else:
            page.meta.pop("human_relations", None)
        page.body = _remove_entry_body(page.body, identity)
    if mode != "reset":
        identity = page_identity(wiki, target)
        source.meta.setdefault("human_relations", []).append({
            "target": identity, "mode": mode, "reason": reason,
            "owner": "human", "updated": day.isoformat(),
        })
        marker = _marker(identity)
        label = f"保留连接：[[{identity}]]" if mode == "pin" else f"禁止连接：`{identity}`"
        explanation = "\n".join(f"> {line}" for line in (reason or "未填写原因").splitlines())
        source.body = append_section(
            source.body, "人工关系",
            f"<!-- human-relation:{marker} -->\n- {label}\n{explanation}\n"
            f"<!-- /human-relation:{marker} -->",
        )
    if mode == "block":
        _remove_machine_links(wiki, source, target)
        _remove_machine_links(wiki, target, source)
    for page in (source, target):
        if (page.meta, page.body) != before[page.slug]:
            wiki.save_page(page)
    wiki.rebuild_index()
    wiki.append_log(day, "relation", source.title, [f"{mode} {target.slug}：{reason}"])
    return {"source": source.slug, "target": target.slug, "mode": mode, "reason": reason}


def relation_context(wiki: Wiki, relevant: list[Page], max_chars: int = 6000) -> list[dict]:
    """只提供相关人工决定的有界提示；真正的禁止校验不依赖提示或模型服从。"""
    identities = {page_identity(wiki, page) for page in relevant}
    context = []
    remaining = max_chars
    for page in sorted(wiki.pages.values(), key=lambda p: p.slug):
        for entry in human_relations(page):
            if page_identity(wiki, page) not in identities and entry["target"] not in identities:
                continue
            reason = str(entry.get("reason") or "")[:min(600, max(0, remaining))]
            size = len(page.slug) + len(entry["target"]) + len(reason) + 64
            if size > remaining:
                return context
            context.append({
                "source": page.slug, "target": entry["target"],
                "mode": entry["mode"], "reason": reason, "owner": "human",
            })
            remaining -= size
    return context
