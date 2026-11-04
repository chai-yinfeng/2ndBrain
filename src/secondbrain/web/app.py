"""本地 Web UI：今日 digest、条目详情、历史、反馈（HTMX 局部刷新）。

只读写本地 SQLite（data/brain.db），默认由 `sb serve` 监听 127.0.0.1。
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from secondbrain import __version__, config, db, ops
from secondbrain.extract.transcript import key_point_link
from secondbrain.llm.base import LLMError, make_backend
from secondbrain.llm.usage import is_peak, record_call, summarize
from secondbrain.models import FetchSettings, FilterSettings, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.pipeline.run import deep_one
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.feedback import recent_feedback
from secondbrain.profile.loader import Profile, load_profile, parse_profile, weak_prerequisites
from secondbrain.profile.rewrite import generate_update, save_profile, text_sha
from secondbrain.profile.tune import source_stats
from secondbrain.sources import FETCHERS
from secondbrain.web.tasks import Task, TaskRegistry, TaskRunning
from secondbrain.wiki.graph import build_graph
from secondbrain.wiki.ingest import pending_items
from secondbrain.wiki.lint import lint, report_sections
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
        "budget_minutes": row["budget"],
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


def _run_task(task: Task) -> dict:
    report = ops.run_once(task.log)
    if report.aborted:
        raise ops.OpsError(report.aborted)
    return {"cost": report.cost_usd}


def _ingest_task(task: Task) -> dict:
    with _conn() as conn:
        ids = pending_items(conn)
    task.log(f"待编进 wiki：{len(ids)} 条")
    s = ops.ingest_items(ids, task.log)
    return {"created": s.created, "updated": s.updated, "cost": s.cost, "failures": s.failures}


PREVIEW_ENTRIES = 10
SOURCE_FIELDS = ("id", "type", "url", "query", "trust", "role", "tags", "max_age_days")


def _http_client(fs: FetchSettings) -> HttpClient:
    """试抓用的 HTTP 客户端（测试中替换为离线 transport）。"""
    return HttpClient(fs)


def _fetch_settings() -> FetchSettings:
    try:
        return config.fetch_settings(config.load_settings())
    except (config.ConfigError, OSError, ValueError):
        return FetchSettings()


def _source_from_form(form: dict[str, str]) -> SourceConfig:
    """表单 → SourceConfig（空字段不传，tags 用逗号分隔）；不合法时抛 ValueError。"""
    data: dict = {}
    for key in SOURCE_FIELDS:
        value = (form.get(key) or "").strip()
        if value:
            data[key] = value
    if "tags" in data:
        data["tags"] = [t.strip() for t in re.split(r"[,，]", data["tags"]) if t.strip()]
    if data.get("type") == "sitemap":
        if (form.get("url_pattern") or "").strip():
            data["url_pattern"] = form["url_pattern"].strip()
        data["use_lastmod"] = bool(form.get("use_lastmod"))
    try:
        return SourceConfig.model_validate(data)
    except ValidationError as exc:
        problems = "；".join(
            f"{'.'.join(str(p) for p in e['loc']) or '配置'}：{e['msg']}" for e in exc.errors()
        )
        raise ValueError(problems) from exc


def _editable_sources() -> tuple[Path, list[SourceConfig]]:
    """config/sources.yaml（不存在时先从 example 复制）及其中的信源。"""
    path, _ = ops.ensure_sources_file()
    return path, config.load_sources(path)


# 后台任务：kind → (标题, 函数)
TASKS = {
    "run": ("立即运行（与 sb run 相同）", _run_task),
    "wiki_ingest": ("编进 wiki", _ingest_task),
}


def _usage_rows(conn: sqlite3.Connection, since: datetime) -> tuple[list[sqlite3.Row], float]:
    rows = summarize(conn, since)
    return rows, sum(r["cost_usd"] for r in rows)


def create_app() -> FastAPI:
    app = FastAPI(title="Second Brain", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    tasks = TaskRegistry()
    app.state.tasks = tasks

    def task_fragment(request: Request, kind: str, message: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "_task.html", {"kind": kind, "task": tasks.get(kind), "message": message}
        )

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
            to_ingest = len(pending_items(conn))
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
        used = sum(r["budget"] for r in rows if r["lane"] in TOP_LANES)
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
                "to_ingest": to_ingest,
                "ingest_task": tasks.get("wiki_ingest"),
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

    # ---------- 后台任务 ----------

    @app.post("/tasks/{kind}", response_class=HTMLResponse)
    def task_start(request: Request, kind: str) -> HTMLResponse:
        if kind not in TASKS:
            raise HTTPException(404, "未知的任务")
        if kind == "run" and ops.is_running():
            return task_fragment(request, kind, "已有 sb run 在运行（CLI 或定时任务），请稍后再试")
        title, fn = TASKS[kind]
        try:
            tasks.start(kind, title, fn)
        except TaskRunning as exc:
            return task_fragment(request, kind, str(exc))
        return task_fragment(request, kind)

    @app.get("/tasks/{kind}", response_class=HTMLResponse)
    def task_status(request: Request, kind: str) -> HTMLResponse:
        if kind not in TASKS:
            raise HTTPException(404, "未知的任务")
        return task_fragment(request, kind)

    # ---------- 运维 ----------

    @app.get("/ops", response_class=HTMLResponse)
    def ops_page(request: Request) -> HTMLResponse:
        fs = _settings()
        now = datetime.now(UTC)
        midnight = datetime.combine(datetime.now().date(), datetime.min.time()).astimezone()
        try:
            sources, sources_error = config.load_sources(), None
        except config.ConfigError as exc:
            sources, sources_error = [], str(exc)
        with _conn() as conn:
            today_rows, today_cost = _usage_rows(conn, midnight)
            week_rows, week_cost = _usage_rows(conn, now - timedelta(days=7))
            stats = source_stats(conn, sources, now, fs.personalize)
        return templates.TemplateResponse(
            request,
            "ops.html",
            {
                "today_rows": today_rows,
                "today_cost": today_cost,
                "week_rows": week_rows,
                "week_cost": week_cost,
                "peak": is_peak(now, fs.pricing_schedule),
                "multiplier": fs.pricing_schedule.off_peak_multiplier,
                "max_cost": fs.run.max_cost_usd,
                "running": ops.is_running(),
                "run_task": tasks.get("run"),
                "stats": [st for st in stats if st.samples or st.suggested],
                "silent": sum(1 for st in stats if not (st.samples or st.suggested)),
                "suggestions": [st for st in stats if st.suggested],
                "pers": fs.personalize,
                "sources_error": sources_error,
            },
        )

    @app.post("/ops/tune", response_class=HTMLResponse)
    def ops_tune(request: Request) -> HTMLResponse:
        """应用信源 trust 建议（与 sb sources tune --apply 同一逻辑）。"""
        fs = _settings()
        lines: list[str] = []
        try:
            sources = config.load_sources()
        except config.ConfigError as exc:
            lines.append(f"✗ {exc}")
        else:
            with _conn() as conn:
                stats = source_stats(conn, sources, datetime.now(UTC), fs.personalize)
            changes = {st.source_id: st.suggested for st in stats if st.suggested}
            if changes:
                ops.apply_trust(changes, lines.append)
            else:
                lines.append("没有 trust 调整建议")
        return templates.TemplateResponse(request, "_lines.html", {"lines": lines})

    # ---------- 信源管理 ----------

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, saved: str | None = None) -> HTMLResponse:
        path, is_example = config.sources_path()
        try:
            sources, error = config.load_sources(path), None
        except config.ConfigError as exc:
            sources, error = [], str(exc)
        with _conn() as conn:
            overview = db.source_overview(conn)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": sources,
                "overview": overview,
                "path": path,
                "is_example": is_example,
                "error": error,
                "saved": saved,
                "types": SourceConfig.model_fields["type"].annotation.__args__,
            },
        )

    @app.post("/sources/preview", response_class=HTMLResponse)
    async def sources_preview(request: Request) -> HTMLResponse:
        """试抓：只拉取并解析信源（不抓正文、不写库），列出前几条标题与日期。"""
        form = {k: str(v) for k, v in (await request.form()).items()}
        context: dict = {"form": form, "error": None, "entries": [], "source": None}
        try:
            source = _source_from_form(form)
        except ValueError as exc:
            context["error"] = str(exc)
            return templates.TemplateResponse(request, "_source_preview.html", context)
        try:
            existing = {s.id for s in config.load_sources()}
        except config.ConfigError:
            existing = set()
        if source.id in existing:
            context["error"] = f"信源 id 已存在：{source.id}"
            return templates.TemplateResponse(request, "_source_preview.html", context)
        fs = _fetch_settings()
        try:
            with _http_client(fs) as client:
                entries = FETCHERS[source.type](source, client, fs)
        except FetchError as exc:
            context["error"] = f"试抓失败：{exc}"
            return templates.TemplateResponse(request, "_source_preview.html", context)
        entries.sort(key=lambda e: e.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        context.update(source=source, entries=entries[:PREVIEW_ENTRIES], total=len(entries))
        return templates.TemplateResponse(request, "_source_preview.html", context)

    @app.post("/sources/add")
    async def sources_add(request: Request) -> RedirectResponse:
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            source = _source_from_form(form)
            path, sources = _editable_sources()
            if any(s.id == source.id for s in sources):
                raise ValueError(f"信源 id 已存在：{source.id}")
            config.save_sources([*sources, source], path)
        except (ValueError, config.ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/sources?saved={source.id}", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    def sources_toggle(source_id: str) -> RedirectResponse:
        path, sources = _editable_sources()
        target = next((s for s in sources if s.id == source_id), None)
        if target is None:
            raise HTTPException(404, "信源不存在")
        target.enabled = not target.enabled
        config.save_sources(sources, path)
        return RedirectResponse(f"/sources?saved={source_id}", status_code=303)

    @app.post("/sources/{source_id}/delete", response_class=HTMLResponse)
    def sources_delete(
        request: Request, source_id: str, confirm: str | None = Form(None)
    ) -> HTMLResponse:
        """二次确认：没带 confirm=yes 时只显示确认页；已入库的条目保留。"""
        path, sources = _editable_sources()
        if not any(s.id == source_id for s in sources):
            raise HTTPException(404, "信源不存在")
        if confirm != "yes":
            return templates.TemplateResponse(
                request, "source_delete.html", {"source_id": source_id, "path": path}
            )
        config.save_sources([s for s in sources if s.id != source_id], path)
        return RedirectResponse("/sources?saved=deleted", status_code=303)

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

    @app.get("/wiki/graph.json")
    def wiki_graph_json() -> JSONResponse:
        return JSONResponse(build_graph(_wiki(), datetime.now().date()))

    @app.get("/wiki/graph", response_class=HTMLResponse)
    def wiki_graph(request: Request) -> HTMLResponse:
        wiki = _wiki()
        types = sorted({p.type for p in wiki.pages.values()})
        return templates.TemplateResponse(
            request, "wiki_graph.html", {"count": len(wiki.pages), "types": types}
        )

    @app.post("/wiki/lint", response_class=HTMLResponse)
    def wiki_lint(request: Request) -> HTMLResponse:
        """体检（不调用 LLM）：断链、缺失页面、孤立页、缺字段、index 不一致。"""
        sections = report_sections(lint(_wiki()))
        return templates.TemplateResponse(request, "_lint.html", {"sections": sections})

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
