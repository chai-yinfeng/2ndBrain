"""知识页分类与可逆回收；只修改元数据，不移动或删除 Markdown 正文。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from secondbrain.wiki.store import Page, Wiki, WikiError, parse_page

KIND_TITLES = {
    "concept": "概念", "entity": "实体", "paper": "论文",
    "article": "文章与网页", "video": "视频", "other": "其他资料",
}


def selected_pages(wiki: Wiki, slugs: Iterable[str]) -> list[Page]:
    pages = []
    for slug in dict.fromkeys(slugs):
        page = wiki.pages.get(slug)
        if page is None:
            raise WikiError("知识页不存在，请刷新列表后重新选择")
        pages.append(page)
    if not pages:
        raise WikiError("请先选择知识页")
    return pages


def removal_impact(wiki: Wiki, slugs: Iterable[str]) -> list[dict]:
    """返回真实 Markdown 反链；不构造相似边，不删除关联页面。"""
    pages = selected_pages(wiki, slugs)
    result = []
    for page in pages:
        backlinks = [
            source for source in wiki.pages.values()
            if source.slug != page.slug and not source.meta.get("trashed")
            and any(page in wiki.resolve(target, source) for target in source.links())
        ]
        result.append({"page": page, "backlinks": backlinks})
    return result


def _metadata_snapshot(wiki: Wiki, page: Page) -> tuple[Page, str]:
    if page.path is None or not wiki._is_page_path(page.path):
        raise WikiError("知识页尚未保存或路径已变化")
    try:
        original = page.path.read_bytes()
    except OSError as exc:
        raise WikiError(f"无法读取知识页《{page.title}》，未改动该页：{exc}") from exc
    return (
        parse_page(original.decode("utf-8"), page.slug, page.path),
        hashlib.sha256(original).hexdigest(),
    )


def manage_pages(wiki: Wiki, slugs: Iterable[str], action: str) -> list[str]:
    if action not in {"trash", "restore"}:
        raise WikiError("未知知识页管理操作")
    wiki.reload_pages()
    # 在第一笔写入前验证所有选择，并以同一份字节生成元数据及并发校验值。
    snapshots = [_metadata_snapshot(wiki, page) for page in selected_pages(wiki, slugs)]
    pending = [
        (page, expected) for page, expected in snapshots
        if bool(page.meta.get("trashed")) != (action == "trash")
    ]
    changed = []
    try:
        for page, expected in pending:
            if action == "trash":
                page.meta["trashed"] = True
            else:
                page.meta.pop("trashed", None)
            wiki.save_metadata(page, expected_hash=expected)
            changed.append(page.slug)
        wiki.reload_pages()
        wiki.rebuild_index()
    except (WikiError, OSError) as exc:
        completed = [page.title for page, _ in pending if page.slug in changed]
        remaining = [page.title for page, _ in pending if page.slug not in changed]
        index_error = ""
        try:
            wiki.reload_pages()
            wiki.rebuild_index()
        except (WikiError, OSError) as index_exc:
            index_error = f"；派生目录未能重建：{index_exc}"
        # 不覆盖可能同时发生的外部编辑，也不把部分成功伪装成完整失败。
        detail = (
            "已处理：" + ("、".join(completed) or "无")
            + "；未处理：" + ("、".join(remaining) or "无")
            + f"。请刷新列表核对；错误：{exc}{index_error}"
        )
        raise WikiError(detail) from exc
    return changed


def set_kind(wiki: Wiki, slug: str, kind: str) -> None:
    if kind not in KIND_TITLES:
        raise WikiError("未知知识类别")
    wiki.reload_pages()
    page, expected = _metadata_snapshot(wiki, selected_pages(wiki, [slug])[0])
    page.meta["knowledge_kind"] = kind
    wiki.save_metadata(page, expected_hash=expected)
    wiki.reload_pages()
    wiki.rebuild_index()
