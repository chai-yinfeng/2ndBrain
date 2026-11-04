"""本地 Web UI：今日 digest、条目详情、历史、反馈（HTMX 局部刷新）。

只读写本地 SQLite（data/brain.db），默认由 `sb serve` 监听 127.0.0.1。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from secondbrain import __version__, config, db
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.loader import Profile, load_profile, parse_profile, weak_prerequisites

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

    return app
