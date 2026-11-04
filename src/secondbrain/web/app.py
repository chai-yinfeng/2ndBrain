"""本地 Web UI：今日 digest、条目详情、历史、反馈（HTMX 局部刷新）。

只读写本地 SQLite（data/brain.db），默认由 `sb serve` 监听 127.0.0.1。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from secondbrain import __version__, config, db
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import record_call
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.feedback import recent_feedback
from secondbrain.profile.loader import Profile, load_profile, parse_profile, weak_prerequisites
from secondbrain.profile.rewrite import generate_update, save_profile, text_sha

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
    feedback_kinds=db.FEEDBACK_KINDS, lane_titles=LANE_TITLES, version=__version__
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


def _budget() -> int:
    try:
        return config.pipeline_settings(config.load_settings()).daily_budget_minutes
    except (config.ConfigError, OSError, ValueError):
        return 0


def _entry(row: sqlite3.Row, l1: dict, l2: dict | None, kinds: set[str], profile: Profile) -> dict:
    prereqs = (l2 or {}).get("prerequisites", [])
    weak = weak_prerequisites(profile, prereqs)
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
        "refresh_first": weak,
        "prerequisites": [p for p in prereqs if p not in weak],
        "kinds": kinds,
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
        with _conn() as conn:
            day = day or db.latest_digest_date(conn)
            rows = db.digest_rows(conn, day) if day else []
            l1 = db.latest_assessments(conn, "L1")
            l2 = db.latest_assessments(conn, "L2")
            kinds = db.feedback_kinds(conn, [r["item_id"] for r in rows])
            backlog = len(db.past_must_reads(conn, day)) if day else 0
        lanes: dict[str, list[dict]] = {}
        for row in rows:
            entry = _entry(
                row,
                l1.get(row["item_id"], {}),
                l2.get(row["item_id"]),
                kinds[row["item_id"]],
                profile,
            )
            lanes.setdefault(row["lane"], []).append(entry)
        used = sum(r["est_minutes"] for r in rows if r["lane"] in TOP_LANES)
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
                "archived": len(lanes.get("archived", [])),
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
            kinds = db.feedback_kinds(conn, [item_id])[item_id]
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
                "history": history,
                "kinds": kinds,
                "digests": lanes,
                "refresh_first": weak_prerequisites(profile, prereqs),
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request) -> HTMLResponse:
        with _conn() as conn:
            days = db.digest_dates(conn)
        return templates.TemplateResponse(request, "history.html", {"days": days})

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
            db.add_feedback(conn, item_key, kind, (note or "").strip() or None, datetime.now(UTC))
            kinds = db.feedback_kinds(conn, [item_key])[item_key]
        return templates.TemplateResponse(
            request,
            "_feedback.html",
            {"item_id": item_key, "kinds": kinds, "with_note": bool(with_note), "saved": kind},
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

    return app


def _history_dir() -> Path:
    return config.data_dir() / "profile_history"


def _normalize_newlines(text: str) -> str:
    """浏览器提交的 textarea 使用 CRLF。"""
    return text.replace("\r\n", "\n")
