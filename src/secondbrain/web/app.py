"""本地 Web UI：今日 digest、条目详情、历史、反馈（HTMX 局部刷新）。

只读写本地 SQLite（data/brain.db），默认由 `sb serve` 监听 127.0.0.1。
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from secondbrain import __version__, config, db
from secondbrain.extract.transcript import key_point_link
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import record_call
from secondbrain.models import FilterSettings
from secondbrain.pipeline.run import deep_one
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.feedback import recent_feedback
from secondbrain.profile.loader import Profile, load_profile, parse_profile, weak_prerequisites
from secondbrain.profile.rewrite import generate_update, save_profile, text_sha
from secondbrain.wiki.loop import wiki_refresh_link
from secondbrain.wiki.review import GRADE_HINTS, GRADES, due_concepts, due_date, review
from secondbrain.wiki.store import WIKILINK_RE, Wiki, open_wiki, slugify_safe

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# 页面上的 lane 顺序与标题
PAGE_LANES = (
    ("must_read", "必读"),
    ("refresher", "温故"),
    ("serendipity", "意外"),
    ("worth_it", "值得读"),
    ("deferred", "顺延的深读"),
)
LANE_TITLES = dict(PAGE_LANES) | {"fyi": "速览", "archived": "归档"}

templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(
    feedback_kinds=db.FEEDBACK_KINDS,
    feedback_hints=db.FEEDBACK_HINTS,
    feedback_groups=(("读之前", db.BEFORE_READ_KINDS), ("读完后", db.AFTER_READ_KINDS)),
    grade_hints=GRADE_HINTS,
    lane_titles=LANE_TITLES,
    version=__version__,
)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _profile() -> Profile:
    path = config.profile_path()
    return load_profile(path) if path.exists() else parse_profile("")


def _settings() -> FilterSettings:
    """settings 不可用时退回默认值（页面仍可浏览）。"""
    try:
        return config.pipeline_settings(config.load_settings())
    except (config.ConfigError, OSError, ValueError):
        return FilterSettings()


def _budget() -> int:
    try:
        return config.pipeline_settings(config.load_settings()).daily_budget_minutes
    except (config.ConfigError, OSError, ValueError):
        return 0


def _wiki() -> Wiki:
    return open_wiki(config.wiki_dir(), config.raw_dir())


def refresh_hints(
    prereqs: list[str], profile: Profile, wiki: Wiki | None
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """前置知识分成 (建议先回炉 [(概念, wiki 链接或 None)], 其余)。

    命中 wiki 中掌握度不是"扎实"的概念页 → 链接到该页；否则命中 profile 中不扎实的领域 → 无链接。"""
    weak_profile = set(weak_prerequisites(profile, prereqs))
    refresh, rest = [], []
    for prereq in prereqs:
        page, needs = wiki_refresh_link(wiki, prereq)
        if page is not None:
            if needs:
                refresh.append((prereq, f"/wiki/page/{page.slug}"))
            else:
                rest.append(prereq)
        elif prereq in weak_profile:
            refresh.append((prereq, None))
        else:
            rest.append(prereq)
    return refresh, rest


def _entry(
    row: sqlite3.Row,
    l1: dict,
    l2: dict | None,
    state: str | None,
    profile: Profile,
    wiki: Wiki | None = None,
) -> dict:
    prereqs = (l2 or {}).get("prerequisites", [])
    refresh, rest = refresh_hints(prereqs, profile, wiki)
    return {
        "item_id": row["item_id"],
        "title": row["title"],
        "url": row["url"],
        "source_id": row["source_id"],
        "lane": row["lane"],
        "est_minutes": row["est_minutes"],
        "is_deep_read": bool(row["is_deep_read"]),
        "deferred_from": row["deferred_from"],
        "tldr": (l2 or {}).get("tldr"),
        "reason": (l2 or {}).get("why_for_you") or l1.get("one_line_reason", ""),
        "content_type": l1.get("content_type"),
        "refresh_first": refresh,
        "prerequisites": rest,
        "state": state,
        "has_l2": l2 is not None,
        "no_transcript": row["extract_method"] == "no_transcript",
        "is_backfill": bool(row["is_backfill"]),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Second Brain", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        day = request.query_params.get("date")
        if day:
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise HTTPException(400, "date 格式应为 YYYY-MM-DD") from exc
        profile = _profile()
        wiki = _wiki()
        with _conn() as conn:
            day = day or db.latest_digest_date(conn)
            rows = db.digest_rows(conn, day) if day else []
            concept_rows = db.digest_concepts(conn, day) if day else []
            l1 = db.latest_assessments(conn, "L1")
            l2 = db.latest_assessments(conn, "L2")
            states = db.feedback_state(conn, [r["item_id"] for r in rows])
            backlog = len(db.past_must_reads(conn, day)) if day else 0
            later = len(db.items_in_state(conn, "later"))
        lanes: dict[str, list[dict]] = {}
        for row in rows:
            entry = _entry(
                row,
                l1.get(row["item_id"], {}),
                l2.get(row["item_id"]),
                states[row["item_id"]],
                profile,
                wiki,
            )
            lanes.setdefault(row["lane"], []).append(entry)
        used = sum(r["est_minutes"] for r in rows if r["lane"] in TOP_LANES)
        used += sum(c["est_minutes"] for c in concept_rows)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "day": day,
                "lanes": lanes,
                "page_lanes": PAGE_LANES,
                "used": used,
                "budget": _budget(),
                "backlog": backlog,
                "later": later,
                "archived": len(lanes.get("archived", [])),
                "concepts": concept_rows,
                "grades": GRADES,
            },
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item(request: Request, item_id: int) -> HTMLResponse:
        profile = _profile()
        with _conn() as conn:
            row = db.item_detail(conn, item_id)
            if row is None:
                raise HTTPException(404, "条目不存在")
            l1 = db.item_assessment(conn, item_id, "L1")
            l2 = db.item_assessment(conn, item_id, "L2")
            history = db.feedback_for(conn, item_id)
            state = db.feedback_state(conn, [item_id])[item_id]
            lanes = conn.execute(
                "SELECT date, lane, est_minutes, is_deep_read FROM digests"
                " WHERE item_id = ? ORDER BY date DESC",
                (item_id,),
            ).fetchall()
        prereqs = (l2 or {}).get("prerequisites", [])
        return templates.TemplateResponse(
            request,
            "item.html",
            {
                "item": row,
                "l1": l1,
                "l2": l2,
                "key_points": _key_points(l2, row["url"]),
                "history": history,
                "state": state,
                "digests": lanes,
                "refresh_first": refresh_hints(prereqs, profile, _wiki())[0],
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request) -> HTMLResponse:
        with _conn() as conn:
            days = db.digest_dates(conn)
        return templates.TemplateResponse(request, "history.html", {"days": days})

    @app.get("/later", response_class=HTMLResponse)
    def later(request: Request) -> HTMLResponse:
        """稍后再看：当前状态为"稍后读"的条目（不计入积压、没有期限）。"""
        with _conn() as conn:
            rows = db.items_in_state(conn, "later")
        return templates.TemplateResponse(request, "later.html", {"rows": rows})

    @app.post("/feedback", response_class=HTMLResponse)
    def feedback(
        request: Request,
        item_id: str = Form(...),
        kind: str = Form(...),
        note: str | None = Form(None),
        with_note: str | None = Form(None),
    ) -> HTMLResponse:
        if kind not in db.FEEDBACK_KINDS:
            raise HTTPException(400, f"未知的反馈类型：{kind}")
        try:
            item_key = int(item_id)
        except ValueError as exc:
            raise HTTPException(400, "item_id 必须是整数") from exc
        with _conn() as conn:
            if not db.item_exists(conn, item_key):
                raise HTTPException(404, "条目不存在")
            state = db.toggle_feedback(
                conn, item_key, kind, (note or "").strip() or None, datetime.now(UTC)
            )
        return templates.TemplateResponse(
            request,
            "_feedback.html",
            {
                "item_id": item_key,
                "state": state,
                "with_note": bool(with_note),
                "saved": kind,
                "undone": state is None,
            },
        )

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str | None = None) -> HTMLResponse:
        path, _ = config.ensure_profile()
        text = path.read_text(encoding="utf-8")
        return templates.TemplateResponse(
            request,
            "profile.html",
            {"profile": parse_profile(text), "text": text, "saved": saved, "path": path},
        )

    @app.post("/profile")
    def profile_save(text: str = Form(...)) -> RedirectResponse:
        path, _ = config.ensure_profile()
        backup = save_profile(path, _normalize_newlines(text), _history_dir(), datetime.now(UTC))
        return RedirectResponse(f"/profile?saved={backup.name}", status_code=303)

    @app.post("/profile/propose", response_class=HTMLResponse)
    def profile_propose(request: Request) -> HTMLResponse:
        path, _ = config.ensure_profile()
        context: dict = {"error": None, "result": None, "message": None}
        try:
            settings = config.load_settings()
            fs = config.pipeline_settings(settings)
        except (config.ConfigError, OSError, ValueError) as exc:
            context["error"] = str(exc)
            return templates.TemplateResponse(request, "_proposal.html", context)
        pers, now = fs.personalize, datetime.now(UTC)
        with _conn() as conn:
            feedback = recent_feedback(conn, now, pers.feedback_window_days, pers.summary_max_items)
            if len(feedback) < pers.min_feedback_for_update:
                days, count = pers.feedback_window_days, len(feedback)
                context["message"] = f"无足够反馈，不提议修改（近 {days} 天内 {count} 条）"
                return templates.TemplateResponse(request, "_proposal.html", context)
            config.load_env()
            backend_name = config.llm_backend(settings)
            try:
                backend = make_backend(backend_name, fs.llm, os.environ.get("DEEPSEEK_API_KEY"))
                text = path.read_text(encoding="utf-8")
                result = generate_update(backend, text, feedback)
            except LLMError as exc:
                if exc.usage:
                    record_call(
                        conn,
                        backend_name,
                        "PROFILE",
                        exc.usage,
                        now,
                        fs.pricing,
                        fs.pricing_schedule,
                    )
                    conn.commit()
                context["error"] = str(exc)
                return templates.TemplateResponse(request, "_proposal.html", context)
            cost = record_call(
                conn, backend.name, "PROFILE", result.usage, now, fs.pricing, fs.pricing_schedule
            )
            conn.commit()
        context.update(
            result=result, diff=result.diff(path.name), base_sha=text_sha(text), cost=cost
        )
        return templates.TemplateResponse(request, "_proposal.html", context)

    @app.post("/profile/apply", response_class=HTMLResponse)
    def profile_apply(
        request: Request, new_text: str = Form(...), base_sha: str = Form(...)
    ) -> HTMLResponse:
        path, _ = config.ensure_profile()
        if text_sha(path.read_text(encoding="utf-8")) != base_sha:
            return templates.TemplateResponse(
                request,
                "_proposal.html",
                {
                    "error": "profile 在生成提案之后被修改过，请重新生成提案",
                    "result": None,
                    "message": None,
                },
                status_code=409,
            )
        backup = save_profile(
            path, _normalize_newlines(new_text), _history_dir(), datetime.now(UTC)
        )
        return templates.TemplateResponse(
            request,
            "_proposal.html",
            {
                "result": None,
                "error": None,
                "message": f"已应用；旧版本备份为 {backup.name}，刷新页面查看",
            },
        )

    @app.post("/deep", response_class=HTMLResponse)
    def deep(request: Request, item_id: str = Form(...)) -> HTMLResponse:
        """按需精读单条（与 sb deep 同一逻辑），返回 TL;DR 片段。"""
        try:
            item_key = int(item_id)
        except ValueError as exc:
            raise HTTPException(400, "item_id 必须是整数") from exc
        context: dict = {"item_id": item_key, "l2": None, "error": None, "cost": 0.0}
        with _conn() as conn:
            row = db.item_detail(conn, item_key)
            if row is None:
                raise HTTPException(404, "条目不存在")
            try:
                settings = config.load_settings()
                fs = config.pipeline_settings(settings)
                config.load_env()
                backend = make_backend(
                    config.llm_backend(settings), fs.llm, os.environ.get("DEEPSEEK_API_KEY")
                )
            except (config.ConfigError, OSError, ValueError, LLMError) as exc:
                context["error"] = str(exc)
                return templates.TemplateResponse(request, "_deep.html", context)
            outcome = deep_one(conn, fs, backend, _profile(), row, datetime.now(UTC))
        context.update(cost=outcome.cost, error=outcome.error)
        if outcome.response is not None:
            l2 = outcome.response.model_dump()
            context.update(l2=l2, key_points=_key_points(l2, row["url"]))
        return templates.TemplateResponse(request, "_deep.html", context)

    # ---------- wiki ----------

    @app.get("/wiki", response_class=HTMLResponse)
    def wiki_index(request: Request) -> HTMLResponse:
        wiki = _wiki()
        today = datetime.now().date()
        due = [{"page": p, "due": due_date(p)} for p in due_concepts(wiki, today)]
        index_html = render_markdown(wiki.index_path.read_text(encoding="utf-8"), wiki)
        return templates.TemplateResponse(
            request,
            "wiki.html",
            {"index_html": index_html, "due": due, "grades": GRADES, "count": len(wiki.pages)},
        )

    @app.get("/wiki/page/{slug}", response_class=HTMLResponse)
    def wiki_page(request: Request, slug: str) -> HTMLResponse:
        wiki = _wiki()
        page = wiki.pages.get(slug) or wiki.find(slug)
        if page is None:
            raise HTTPException(404, "页面不存在")
        return templates.TemplateResponse(
            request,
            "wiki_page.html",
            {
                "page": page,
                "body_html": render_markdown(page.body, wiki),
                "due": due_date(page),
                "grades": GRADES,
            },
        )

    @app.post("/wiki/review", response_class=HTMLResponse)
    def wiki_review(
        request: Request, slug: str = Form(...), grade: str = Form(...)
    ) -> HTMLResponse:
        if grade not in GRADES:
            raise HTTPException(400, f"未知的复习结果：{grade}")
        wiki = _wiki()
        page = wiki.pages.get(slug)
        if page is None or page.type != "concept":
            raise HTTPException(404, "概念页不存在")
        outcome = review(
            wiki, page, grade, datetime.now().date(), _settings().wiki.mastered_interval_days
        )
        return templates.TemplateResponse(
            request, "_review.html", {"outcome": outcome, "grades": GRADES, "page": page}
        )

    return app


_RAW_LINK_RE = re.compile(r"\]\((?:\.\./)+raw/(\d+)\.md\)")


def render_markdown(text: str, wiki: Wiki) -> str:
    """wiki Markdown → HTML：先转义 "<"（不渲染任何原始 HTML），[[slug]] 变为站内链接，
    指向原文（raw/<id>.md）的链接变为条目详情页。"""

    def link(match: re.Match) -> str:
        target = slugify_safe(match.group(1))
        page = wiki.pages.get(target)
        if page is None:
            return f'<span class="missing">{match.group(1).replace("<", "&lt;")}</span>'
        return f"[{page.title}](/wiki/page/{page.slug})"

    safe = text.replace("<", "&lt;")
    safe = _RAW_LINK_RE.sub(r"](/item/\1)", safe)
    safe = WIKILINK_RE.sub(link, safe)
    return markdown.markdown(safe, extensions=["tables", "fenced_code", "sane_lists"])


def _key_points(l2: dict | None, url: str) -> list[tuple[str, str | None]]:
    """要点及其时间戳链接（仅视频）。"""
    return [key_point_link(p, url) for p in (l2 or {}).get("key_points", [])]


def _history_dir() -> Path:
    return config.data_dir() / "profile_history"


def _normalize_newlines(text: str) -> str:
    """浏览器提交的 textarea 使用 CRLF。"""
    return text.replace("\r\n", "\n")
