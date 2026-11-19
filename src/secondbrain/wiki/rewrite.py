"""显式、可恢复的单页重整；读取会话绝不重发模型请求。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from secondbrain import config, db
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import record_call
from secondbrain.wiki.ingest import estimate_ingest_cost
from secondbrain.wiki.relations import filter_machine_links, relation_context
from secondbrain.wiki.store import Page, Wiki, WikiError, markdown_without_code, parse_page

Echo = Callable[[str], None]
_START = "<!-- wiki-rewrite:start -->"
_END = "<!-- wiki-rewrite:end -->"
_PROTECTED = {"个人笔记", "来源", "人工关系", "人工关联"}
_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROMPT = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "wiki_rewrite.md"


class RewriteError(WikiError):
    """可以直接向用户展示的会话或并发冲突。"""


class RewriteDraft(BaseModel):
    body: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=500)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _root() -> Path:
    return config.data_dir() / "wiki_rewrite"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identifier(value: str) -> str:
    if not _ID.fullmatch(value):
        raise RewriteError("会话或版本 ID 无效")
    return value


def _session_path(session_id: str) -> Path:
    return _root() / "sessions" / f"{_identifier(session_id)}.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RewriteError("会话或历史版本不存在") from exc


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_session(session: dict) -> None:
    session["updated_at"] = _now()
    _save(_session_path(session["id"]), session)


@contextmanager
def _lock() -> Iterator[None]:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "operation.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RewriteError("另一个重整操作正在进行，请稍后再试") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _busy() -> bool:
    try:
        with (_root() / "operation.lock").open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def _wiki() -> Wiki:
    # 不能用 open_wiki：只读恢复不应迁移、建目录或重建索引。
    wiki = Wiki(config.wiki_dir(), config.raw_dir())
    wiki.reload_pages()
    return wiki


def _page(wiki: Wiki, slug: str) -> Page:
    matches = wiki.resolve(slug)
    if len(matches) != 1:
        raise RewriteError("知识页不存在或名称有歧义，请使用页面路径")
    page = matches[0]
    if page.meta.get("trashed"):
        raise RewriteError("知识页已回收，请先恢复")
    return page


def _page_text(page: Page) -> str:
    if page.path is None:
        raise RewriteError("知识页尚未保存")
    return page.path.read_bytes().decode("utf-8")


def _sections(body: str) -> list[tuple[int, int, str]]:
    headings = list(re.finditer(r"(?m)^#{1,2}[ \t]+([^\n]+)", markdown_without_code(body)))
    result = []
    if not headings or headings[0].start():
        result.append((0, headings[0].start() if headings else len(body), ""))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        result.append((heading.start(), end, heading[1].strip()))
    return result


def _editable_spans(body: str) -> list[tuple[int, int]]:
    masked = markdown_without_code(body)
    marked = [(m.start(), m.end()) for m in re.finditer(
        re.escape(_START) + r".*?" + re.escape(_END) + r"(?:\r?\n)*", masked, re.S
    )]
    marked.extend((match.start(), match.end()) for match in re.finditer(
        r"<!-- wiki-ingest:start (material-\d+:[0-9a-f]+) -->.*?"
        r"<!-- wiki-ingest:end \1 -->(?:\r?\n)*", masked, re.S,
    ))
    for start, end, title in _sections(body):
        text = body[start:end]
        if (
            re.fullmatch(r"机器整理 · 材料 \d+", title)
            and re.search(r"<!-- material-\d+:[0-9a-f]{20} -->", text)
            and "> 机器整理，未经用户核对；" in text
        ):
            marked.append((start, end))
    # 无明确机器归属的旧页正文可能是人工文字，不能猜测归属并删除。
    merged: list[tuple[int, int]] = []
    for start, end in sorted(marked):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _editable(body: str) -> str:
    """提取版本中的整理正文，不把编辑边界或旧免责声明嵌套回新版本。"""
    parts = []
    for start, end in _editable_spans(body):
        text = body[start:end]
        text = re.sub(r"(?m)^## 机器整理 · 材料 \d+\s*\n", "", text)
        text = re.sub(r"(?m)^> 机器整理，未经用户核对；[^\n]*\n", "", text)
        text = re.sub(
            r"(?m)^<!-- (?:wiki-rewrite:(?:start|end)|"
            r"wiki-ingest:(?:start|end) material-\d+:[0-9a-f]+|"
            r"material-\d+:[0-9a-f]+) -->[ \t]*\r?\n?",
            "", text,
        )
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _replace_body(body: str, draft: str) -> str:
    block = f"{_START}\n\n{draft.strip()}\n\n{_END}\n\n" if draft.strip() else ""
    spans = _editable_spans(body)
    if spans:
        first = spans[0][0]
        for start, end in reversed(spans):
            body = body[:start] + body[end:]
        return body[:first] + block + body[first:]
    # 无机器正文时，放在个人笔记/来源之前，不覆盖这些段落。
    sections = _sections(body)
    position = next((start for start, _, title in sections if title in _PROTECTED), len(body))
    return body[:position].rstrip() + "\n\n" + block + body[position:]


def _safe_draft(wiki: Wiki, page: Page, text: str) -> str:
    if any(marker in text for marker in (
        _START, _END, "<!-- human-relation:", "<!-- wiki-ingest:", "<!-- wiki-ingest:start",
        "<!-- wiki-ingest:end", "<!-- material-",
    )):
        raise RewriteError("模型正文包含受保护的编辑标记")
    if any(title in _PROTECTED for _, _, title in _sections(text)):
        raise RewriteError("模型正文不能包含个人笔记、来源或人工关系段落")
    if not text.strip():
        raise RewriteError("模型返回了空正文")
    # 页面标题与受保护段保持原样；机器自己的子标题限定在三级及以下。
    text = re.sub(r"(?m)^#{1,2}[ \t]+", "### ", text.strip())
    return filter_machine_links(wiki, page, text)


def _context(wiki: Wiki, page: Page, fs, conn, guidance: str = "") -> dict:
    remaining = fs.wiki.ingest_max_chars
    sources = []
    source_ids = page.meta.get("sources") or []
    if not isinstance(source_ids, list):
        source_ids = [source_ids]
    for source_id in source_ids:
        if remaining <= 0:
            break
        try:
            source_id = int(source_id)
        except (ValueError, TypeError):
            continue
        row = db.item_detail(conn, source_id)
        if row is not None:
            text = row["original_text"] or row["content"] or row["summary"] or ""
            entry = {
                "title": row["title"], "url": row["url"], "content": text[:remaining],
                "completeness": row["content_completeness"],
            }
            entry["truncated"] = len(text) > remaining
            sources.append(entry)
            remaining -= len(entry["content"])
        else:
            raw = wiki.raw_path(source_id).resolve()
            if raw.is_relative_to(wiki.raw_root.resolve()) and raw.is_file():
                text = raw.read_text(encoding="utf-8")
                sources.append({"content": text[:remaining], "truncated": len(text) > remaining})
                remaining -= min(len(text), remaining)
    links = page.links()
    requested = wiki.mentioned_in(guidance, fs.wiki.related_pages)
    neighbors = [
        candidate for candidate in wiki.pages.values()
        if candidate.slug in links or page.slug in candidate.links()
    ]
    related = []
    seen = {page.slug}
    for candidate in [*requested, *sorted(neighbors, key=lambda candidate: candidate.slug)]:
        if candidate.slug not in seen and candidate.is_visible:
            related.append(candidate)
            seen.add(candidate.slug)
    related = related[:fs.wiki.related_pages]
    return {
        "page": {"slug": page.slug, "title": page.title, "body": page.body},
        "sources": sources,
        "source_note": "仅使用已保存来源；缺失或截断的原文不得假称已读。",
        "related_pages": [
            {"slug": candidate.slug, "title": candidate.title,
             "body": candidate.body[:fs.wiki.related_page_chars]}
            for candidate in related
        ],
        "human_relations": relation_context(wiki, [page, *related]),
    }


def _generate(session: dict, wiki: Wiki, page: Page, echo: Echo) -> dict:
    conn = None
    backend = None
    fs = None
    called = False
    accounted = False
    session["status"] = "generating"
    _save_session(session)
    try:
        config.load_env()
        settings = config.load_settings()
        fs = config.pipeline_settings(settings)
        backend = make_backend(
            config.llm_backend(settings), fs.llm, os.environ.get("DEEPSEEK_API_KEY")
        )
        conn = db.connect()
        guidance = "\n".join(
            item["content"] for item in session["messages"] if item["role"] == "user"
        )
        payload = _context(wiki, page, fs, conn, guidance)
        messages = [
            {"role": "system", "content": _PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            *[{"role": item["role"], "content": item["content"]} for item in session["messages"]],
        ]
        estimate = estimate_ingest_cost(messages, backend.model_name("strong"), fs)
        session["estimated_cost"] = estimate
        if not session.get("usage_known", True):
            raise RewriteError("之前调用的用量未知，不能确认剩余预算；请另建会话")
        if session["cost"] + estimate > fs.run.max_cost_usd:
            raise RewriteError(f"预估费用超过 run.max_cost_usd=${fs.run.max_cost_usd}")
        echo(f"重整预估 ${estimate:.4f}；只生成草稿，不改知识页")
        called = True
        response, usage = backend.complete_json(messages, RewriteDraft, "strong")
        session["cost"] += record_call(
            conn, backend.name, "WIKI_REWRITE", usage, datetime.now(UTC),
            fs.pricing, fs.pricing_schedule,
        )
        conn.commit()
        accounted = True
        if session["cost"] > fs.run.max_cost_usd:
            message = f"实际费用超过 run.max_cost_usd=${fs.run.max_cost_usd}，停止"
            conn.execute(
                "UPDATE llm_calls SET error = ? WHERE id = last_insert_rowid()", (message,)
            )
            conn.commit()
            raise RewriteError(message)
        draft = _safe_draft(wiki, page, response.body)
        session["draft"] = draft
        session["summary"] = response.summary
        session["messages"].append({
            "role": "assistant", "content": draft, "created_at": _now(),
        })
        session["status"] = "draft"
        echo(f"草稿已保存，累计费用 ${session['cost']:.4f}；需要显式采用")
    except (LLMError, WikiError, config.ConfigError, OSError, ValueError) as exc:
        if called and not accounted and conn is not None and backend is not None and fs is not None:
            usage = getattr(exc, "usage", None)
            session["cost"] += record_call(
                conn, backend.name, "WIKI_REWRITE", usage, datetime.now(UTC),
                fs.pricing, fs.pricing_schedule, error=str(exc), model=backend.model_name("strong"),
            )
            conn.commit()
            session["usage_known"] = usage is not None
        elif accounted and conn is not None:
            conn.execute(
                "UPDATE llm_calls SET error = ? WHERE id = last_insert_rowid()", (str(exc),)
            )
            conn.commit()
        session["status"] = "failed"
        session["errors"].append(str(exc))
        echo(f"重整失败：{exc}")
    finally:
        if conn is not None:
            conn.close()
        _save_session(session)
    return session


def start_session(slug: str, context: str, echo: Echo = lambda _: None) -> dict:
    if not context.strip():
        raise RewriteError("请填写本轮临时语境")
    with _lock():
        wiki = _wiki()
        page = _page(wiki, slug)
        now = _now()
        session = {
            "id": uuid4().hex, "slug": page.slug, "title": page.title,
            "path": page.path.resolve().relative_to(wiki.root.resolve()).as_posix(),
            "created_at": now, "updated_at": now, "base_hash": _hash(_page_text(page)),
            "messages": [{"role": "user", "content": context.strip(), "created_at": now}],
            "draft": "", "status": "generating", "cost": 0.0, "errors": [],
            "usage_known": True,
        }
        return _generate(session, wiki, page, echo)


def get_session(session_id: str) -> dict:
    session = _read(_session_path(session_id))
    if session["status"] == "generating" and not _busy():
        session["status"] = "interrupted"
        session["usage_known"] = False
        session["errors"] = [*session["errors"], "上次生成被中断，未自动重放；用量可能未知"]
    return session


def list_sessions(slug: str) -> list[dict]:
    wiki = _wiki()
    matches = wiki.resolve(slug)
    canonical = matches[0].slug if len(matches) == 1 else slug
    result = []
    for path in (_root() / "sessions").glob("*.json"):
        session = _read(path)
        if session["slug"] == canonical:
            result.append(get_session(session["id"]))
    return sorted(result, key=lambda session: session["created_at"], reverse=True)


def delete_session(session_id: str) -> dict:
    """显式删除会话；已采用正文、回退版本和真实用量记录均独立保留。"""
    with _lock():
        path = _session_path(session_id)
        session = _read(path)
        # 持有操作锁时不存在正在生成的请求；遗留 generating 是中断记录。
        path.unlink()
        return {"id": session["id"], "slug": session["slug"], "status": "deleted"}


def _session_page(wiki: Wiki, session: dict) -> Page:
    path = (wiki.root / session["path"]).resolve()
    if not wiki._is_page_path(path):
        raise RewriteError("知识页路径已失效")
    page = next((page for page in wiki.pages.values() if page.path.resolve() == path), None)
    if page is None or page.meta.get("trashed"):
        raise RewriteError("知识页不存在或已回收")
    if _hash(_page_text(page)) != session["base_hash"]:
        raise RewriteError("知识页已变化，请开始新会话；未覆盖外部编辑")
    return page


def continue_session(session_id: str, message: str, echo: Echo = lambda _: None) -> dict:
    if not message.strip():
        raise RewriteError("请输入继续重整的要求")
    with _lock():
        session = _read(_session_path(session_id))
        if session["status"] == "generating":
            raise RewriteError("上次生成被中断或仍在运行，不能重放付费请求；请另建会话")
        if session["status"] == "applied":
            # 已采用版本可以继续，但先确认用户/其他会话没有改动。
            session["draft"] = ""
        wiki = _wiki()
        page = _session_page(wiki, session)
        session["messages"].append({
            "role": "user", "content": message.strip(), "created_at": _now(),
        })
        return _generate(session, wiki, page, echo)


def _revision_dir(slug: str) -> Path:
    return _root() / "revisions" / _hash(slug)


def _save_revision(page: Page, text: str, summary: str) -> dict:
    revision = {
        "id": uuid4().hex, "slug": page.slug, "created_at": _now(), "summary": summary,
        "page_text": text, "editable": _editable(parse_page(text, page.slug).body),
    }
    _save(_revision_dir(page.slug) / f"{revision['id']}.json", revision)
    return revision


def _write_body(page: Page, original: str, body: str) -> str:
    # Frontmatter 包括未知人工字段按字节保留，不经 YAML 重排。
    front = re.match(r"\A---\r?\n.*?\r?\n---(?:\r?\n)*", original, re.S)
    updated = (front[0] if front else "") + body
    if _page_text(page) != original:
        raise RewriteError("知识页已变化，未覆盖外部编辑")
    temporary = page.path.with_name(f".{page.path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        if _page_text(page) != original:
            raise RewriteError("知识页已变化，未覆盖外部编辑")
        os.replace(temporary, page.path)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def apply_revision(session_id: str, echo: Echo = lambda _: None) -> dict:
    with _lock():
        session = _read(_session_path(session_id))
        if session["status"] == "applied":
            return session
        if session["status"] != "draft" or not session["draft"]:
            raise RewriteError("会话没有可采用的成功草稿")
        wiki = _wiki()
        page = _session_page(wiki, session)
        original = _page_text(page)
        draft = _safe_draft(wiki, page, session["draft"])
        _save_revision(page, original, "采用前版本")
        updated = _write_body(page, original, _replace_body(page.body, draft))
        revision = _save_revision(page, updated, session.get("summary") or "会话重整")
        session.update(status="applied", base_hash=_hash(updated), revision_id=revision["id"])
        _save_session(session)
        echo("已采用本页新正文；个人笔记、来源与人工关系保持不变")
        return session


def history(slug: str) -> list[dict]:
    matches = _wiki().resolve(slug)
    canonical = matches[0].slug if len(matches) == 1 else slug
    revisions = [_read(path) for path in _revision_dir(canonical).glob("*.json")]
    return sorted(
        [{key: revision[key] for key in ("id", "slug", "created_at", "summary")}
         for revision in revisions],
        key=lambda revision: revision["created_at"], reverse=True,
    )


def clear_history(slug: str) -> dict:
    """显式清除本页回退点；不写知识页，也不删除会话正文或用量账单。"""
    with _lock():
        matches = _wiki().resolve(slug)
        if len(matches) > 1:
            raise RewriteError("知识页名称有歧义，请使用页面路径")
        canonical = matches[0].slug if matches else slug
        paths = list(_revision_dir(canonical).glob("*.json"))
        removed_ids = {path.stem for path in paths}
        for session_path in (_root() / "sessions").glob("*.json"):
            session = _read(session_path)
            if session["slug"] == canonical and session.get("revision_id") in removed_ids:
                session.pop("revision_id")
                session["history_cleared"] = True
                _save_session(session)
        for path in paths:
            path.unlink()
        return {"slug": canonical, "status": "cleared", "deleted": len(paths)}


def restore_revision(slug: str, revision_id: str, echo: Echo = lambda _: None) -> dict:
    with _lock():
        wiki = _wiki()
        page = _page(wiki, slug)
        revision = _read(_revision_dir(page.slug) / f"{_identifier(revision_id)}.json")
        original = _page_text(page)
        _save_revision(page, original, "回退前版本")
        # 只回退 AI 正文；之后新增的个人笔记、来源及人工关系仍以当前页为准。
        draft = filter_machine_links(wiki, page, revision["editable"])
        updated = _write_body(page, original, _replace_body(page.body, draft))
        restored = _save_revision(page, updated, f"回退：{revision['summary']}")
        echo("已回退正文；当前个人笔记、来源与人工关系保持不变")
        return {**{key: restored[key] for key in ("id", "slug", "created_at", "summary")},
                "status": "restored", "revision_id": restored["id"], "base_hash": _hash(updated)}
