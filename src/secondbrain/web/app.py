"""本地 Web UI：今日 digest、条目详情、历史、反馈（HTMX 局部刷新）。

只读写本地 SQLite（data/brain.db），默认由 `sb serve` 监听 127.0.0.1。
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from secondbrain import __version__, config, db, ops, schedule, sources_file
from secondbrain.extract.transcript import key_point_link
from secondbrain.llm.base import LLMError, Usage, make_backend
from secondbrain.llm.usage import full_price, is_peak, record_call, summarize
from secondbrain.models import FetchSettings, FilterSettings, SourceConfig
from secondbrain.net import FetchError, HttpClient
from secondbrain.pipeline.l2_deep import EST_CHARS_PER_TOKEN, EST_L2_OUTPUT_TOKENS
from secondbrain.pipeline.run import deep_one
from secondbrain.pipeline.summary import (
    estimate_prereq_cost,
    estimate_summary_cost,
    summary_minutes,
)
from secondbrain.pipeline.tiering import TOP_LANES
from secondbrain.profile.feedback import recent_feedback
from secondbrain.profile.loader import Profile, load_profile, parse_profile, weak_prerequisites
from secondbrain.profile.rewrite import generate_update, save_profile, text_sha
from secondbrain.profile.tune import DISTRIBUTION_KINDS, feedback_distribution, source_yields
from secondbrain.sources import FETCHERS
from secondbrain.sources.homepage import safe_url, source_homepage
from secondbrain.web import filters
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
        "content_len": row["content_len"] or 0,
    }


def _run_task(task: Task, all_time: bool = False) -> dict:
    report = ops.run_once(task.log, all_time=all_time)
    if report.aborted:
        raise ops.OpsError(report.aborted)
    return {"cost": report.cost_usd}


def _schedule_checks(fs: FilterSettings, at: str) -> list[str]:
    """该本地时间换算成 UTC 后是否落在 DeepSeek 高峰（冬令时 / 夏令时各一行）。"""
    try:
        local = schedule.parse_time(at)
    except ValueError:
        return []
    return [
        f"{c.label}：本地 {local:%H:%M} = UTC {c.utc:%H:%M}，"
        + ("高峰时段（全价）" if c.peak else "非高峰（半价）")
        for c in schedule.utc_checks(local, fs.pricing_schedule)
    ]


def _filtered_days(conn: sqlite3.Connection, form: str | None, ctype: str | None) -> list[dict]:
    """历史页：按形式 / 类型筛选后各日期各 lane 的条数（与 db.digest_dates 同样的列）。"""
    l1 = db.latest_assessments(conn, "L1")
    days: dict[str, dict] = {}
    for r in db.digest_lane_rows(conn):
        item_form = filters.item_form(r["source_type"], r["url"], r["extract_method"])
        content_type = l1.get(r["item_id"], {}).get("content_type")
        if not filters.matches(item_form, content_type, form, ctype):
            continue
        day = days.setdefault(
            r["date"],
            {"date": r["date"], "total": 0}
            | dict.fromkeys(
                ("must_read", "worth_it", "refresher", "serendipity", "deferred", "fyi"), 0
            ),
        )
        day["total"] += 1
        if r["lane"] in day:
            day[r["lane"]] += 1
    return list(days.values())


def _filter_bar(
    path: str, keep: dict[str, str | None], form: str | None, ctype: str | None
) -> dict:
    """两排 chip（形式 / 类型），用普通链接组合筛选，不需要 JS。"""
    params = {**keep, "form": form, "ctype": ctype}
    return {
        "forms": filters.chips(path, params, "form", filters.FORMS),
        "ctypes": filters.chips(path, params, "ctype", filters.CTYPES),
        "active": bool(form or ctype),
    }


def _setting_context() -> dict:
    """运维页"设置"区：只给出 key 是否已设置与来源，绝不包含 key 的值。"""
    config.load_env()
    try:
        settings = config.load_settings()
        fs = config.pipeline_settings(settings)
    except (config.ConfigError, OSError, ValueError):
        settings, fs = {}, FilterSettings()
    backend = config.llm_backend(settings)
    key = ops.KEY_NAMES.get(backend, "DEEPSEEK_API_KEY")
    source = config.env_source(key)
    return {
        "key": key,
        "key_source": source,
        "key_in_dotenv": key in config.parse_env_file(config.env_file()),
        "shell_override": source == "环境变量",
        "backend": backend,
        "backend_setting": (settings.get("llm") or {}).get("backend") or config.DEFAULT_LLM_BACKEND,
        "env_backend": os.environ.get("SB_LLM_BACKEND"),
        "backends": config.KNOWN_LLM_BACKENDS,
        "models": fs.llm.models,
        "choices": sorted(fs.pricing),
        "env_file": config.env_file(),
    }


def _active_sources() -> int:
    try:
        return len(ops.active_sources(config.load_sources()))
    except config.ConfigError:
        return 0


def _ingest_task(task: Task) -> dict:
    with _conn() as conn:
        ids = pending_items(conn)
    task.log(f"待编进 wiki：{len(ids)} 条")
    s = ops.ingest_items(ids, task.log)
    return {"created": s.created, "updated": s.updated, "cost": s.cost, "failures": s.failures}


class CostHints:
    """按钮旁的费用提示：按当前配置的模型与价格表估算（高峰全价，非高峰 × off_peak_multiplier）。"""

    def __init__(self, fs: FilterSettings) -> None:
        self.fs = fs

    def _fmt(self, peak: float) -> str:
        if peak <= 0:
            return "费用无法估算（价格表中没有当前模型）"
        low = peak * self.fs.pricing_schedule.off_peak_multiplier

        def money(x: float) -> str:
            return f"${x:.3f}" if x >= 0.01 else f"${x:.4f}"

        return f"约 {money(low)}（非高峰）– {money(peak)}（高峰）"

    def _strong(self, input_chars: int, output_tokens: int) -> str:
        tokens = input_chars // EST_CHARS_PER_TOKEN
        usage = Usage(self.fs.llm.models["strong"], tokens, 0, tokens, output_tokens)
        return self._fmt(full_price(usage, self.fs.pricing))

    def l2(self, content_chars: int | None) -> str:
        chars = min(content_chars or 0, self.fs.l2.max_input_chars) + 3000
        return self._strong(chars, EST_L2_OUTPUT_TOKENS)

    def summary(self, content_chars: int | None) -> str:
        chars = content_chars or 0
        minutes = summary_minutes(chars, self.fs)
        return f"约 {minutes} 分钟，{self._fmt(estimate_summary_cost(chars, self.fs))}"

    def ingest(self) -> str:
        return self._strong(33_000, 8000) + " / 条"

    def profile(self) -> str:
        return self._strong(15_000, 3000)

    def prereq(self, count: int = 3) -> str:
        return self._fmt(estimate_prereq_cost(max(count, 1), self.fs))

    def run(self, sources: int) -> str:
        """一次完整运行的上限估计：每个信源最多 fetch_limit 条新条目进 L1（cheap），
        再选 l1.top_n 条做 L2（strong，按每条约 3 万字符计）。"""
        fs = self.fs
        items = sources * fs.run.fetch_limit
        l1_in = items * (fs.l1.item_chars + 600) + (items // fs.l1.batch_size + 1) * 3000
        l1_tokens = l1_in // EST_CHARS_PER_TOKEN
        l1 = Usage(fs.llm.models["cheap"], l1_tokens, 0, l1_tokens, items * 120)
        tokens = (30_000 + 3000) // EST_CHARS_PER_TOKEN
        l2 = Usage(fs.llm.models["strong"], tokens, 0, tokens, EST_L2_OUTPUT_TOKENS)
        peak = full_price(l1, fs.pricing) + fs.l1.top_n * full_price(l2, fs.pricing)
        return f"最多 {items} 条新条目时{self._fmt(peak)}"


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
    if data.get("type") in ("sitemap", "html_index"):
        if (form.get("url_pattern") or "").strip():
            data["url_pattern"] = form["url_pattern"].strip()
    if data.get("type") == "sitemap":
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


# Host 头白名单（防 DNS rebinding，ADR 0017）；`sb serve --host X` 时额外允许 X
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(netloc: str) -> str:
    """ "127.0.0.1:8765" → "127.0.0.1"；"[::1]:8765" → "::1"；小写。"""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1 : netloc.find("]")] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def request_problem(
    method: str, host: str | None, origin: str | None, referer: str | None, allowed: frozenset
) -> str | None:
    """返回拒绝原因；放行时返回 None。规则见 ADR 0017：
    - Host 必须是本机地址或显式 --host 的值；
    - 非 GET / HEAD / OPTIONS：有 Origin 时必须与 Host 同源；没有 Origin 时看 Referer；
      两者都没有时放行（非浏览器客户端，如 curl / 测试；CSRF 需要浏览器发起）。"""
    if not host or _hostname(host) not in allowed:
        return "Host 不被允许"
    if method.upper() in SAFE_METHODS:
        return None
    source = origin if origin is not None else referer
    if source is None:
        return None
    parsed = urlsplit(source)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.strip().lower():
        return "跨源请求被拒绝"
    return None


def create_app(extra_hosts: tuple[str, ...] = ()) -> FastAPI:
    app = FastAPI(title="Second Brain", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = LOCAL_HOSTS | {_hostname(h) for h in extra_hosts}

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        h = request.headers
        problem = request_problem(
            request.method, h.get("host"), h.get("origin"), h.get("referer"), allowed
        )
        if problem:
            return PlainTextResponse(f"403 Forbidden：{problem}", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    tasks = TaskRegistry()
    app.state.tasks = tasks

    def task_fragment(
        request: Request, kind: str, message: str | None = None, refresh: bool = False
    ) -> HTMLResponse:
        """refresh：首页的"开始今日过滤"轮询；任务完成时让 HTMX 刷新整页（HX-Refresh）。"""
        task = tasks.get(kind)
        response = templates.TemplateResponse(
            request,
            "_task.html",
            {"kind": kind, "task": task, "message": message, "refresh_on_done": refresh},
        )
        if refresh and task is not None and task.status == "done":
            response.headers["HX-Refresh"] = "true"
        return response

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
            latest = db.latest_digest_date(conn)
            day = day or latest
            rows = db.digest_rows(conn, day) if day else []
            concept_rows = db.digest_concepts(conn, day) if day else []
            l1 = db.latest_assessments(conn, "L1")
            l2 = db.latest_assessments(conn, "L2")
            states = db.feedback_state(conn, [r["item_id"] for r in rows])
            backlog = len(db.past_must_reads(conn, day)) if day else 0
            later = len(db.items_in_state(conn, "later"))
            summarized = db.ai_item_ids(conn, "summary")
            to_ingest = len(pending_items(conn))
        form, ctype = filters.clean_filters(
            request.query_params.get("form"), request.query_params.get("ctype")
        )
        lanes: dict[str, list[dict]] = {}
        for row in rows:
            content_type = l1.get(row["item_id"], {}).get("content_type")
            item_form = filters.item_form(row["source_type"], row["url"], row["extract_method"])
            if not filters.matches(item_form, content_type, form, ctype):
                continue  # 筛选只影响显示，不改 digest / 预算
            entry = _entry(
                row,
                l1.get(row["item_id"], {}),
                l2.get(row["item_id"]),
                states[row["item_id"]],
                profile,
                wiki,
            )
            entry["has_summary"] = row["item_id"] in summarized
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
                "ran_today": latest == datetime.now().date().isoformat(),
                "run_task": tasks.get("run"),
                "run_locked": ops.is_running(),
                "active_sources": _active_sources(),
                "hints": CostHints(_settings()),
                "tasks": tasks,
                "to_ingest": to_ingest,
                "ingest_task": tasks.get("wiki_ingest"),
                "archived": len(lanes.get("archived", [])),
                "concepts": [] if (form or ctype) else concept_rows,
                "grades": GRADES,
                "filter_bar": _filter_bar(
                    "/", {"date": request.query_params.get("date")}, form, ctype
                ),
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
            summary = db.get_ai(conn, item_id, "summary")
            prereq_cached = db.get_ai(conn, item_id, "prereq") is not None
            prereq_count = len(ops.prereq_names(conn, item_id))
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
                "summary": summary,
                "summary_points": _summary_points(summary, row["url"]),
                "summary_task": tasks.get(f"summary-{item_id}"),
                "prereq_cached": prereq_cached,
                "prereq_count": prereq_count,
                "hints": CostHints(_settings()),
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request) -> HTMLResponse:
        form, ctype = filters.clean_filters(
            request.query_params.get("form"), request.query_params.get("ctype")
        )
        with _conn() as conn:
            if form or ctype:
                days = _filtered_days(conn, form, ctype)
            else:
                days = db.digest_dates(conn)
        query = urlencode({k: v for k, v in (("form", form), ("ctype", ctype)) if v})
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "days": days,
                "filter_bar": _filter_bar("/history", {}, form, ctype),
                "filter_query": f"&{query}" if query else "",
            },
        )

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request) -> HTMLResponse:
        """手动添加文章（ADR 0018）：不进 L1 / L2 / 分级，只记录状态与反馈。"""
        return templates.TemplateResponse(
            request,
            "add.html",
            {
                "task": tasks.get("manual_add"),
                "read_kinds": {k: db.FEEDBACK_KINDS[k] for k in ops.READ_FEEDBACK},
            },
        )

    @app.post("/add", response_class=HTMLResponse)
    async def add_submit(request: Request) -> HTMLResponse:
        form = await request.form()
        urls = str(form.get("urls") or "").splitlines()
        state = str(form.get("state") or "later")
        feedback = str(form.get("feedback") or "") or None
        ingest = bool(form.get("ingest"))
        text = str(form.get("text") or "").strip() or None
        upload, pdf = form.get("pdf"), None
        if upload is not None and not isinstance(upload, str) and upload.filename:
            pdf = await upload.read(ops.MAX_PDF_UPLOAD + 1)
        try:
            urls = ops.validate_manual(urls, state, feedback, ingest, text, pdf)
            if pdf is not None and (len(pdf) > ops.MAX_PDF_UPLOAD or not pdf.startswith(b"%PDF")):
                raise ops.OpsError("只接受 30MB 以内的 PDF 文件")
        except ops.OpsError as exc:
            return task_fragment(request, "manual_add", f"✗ {exc}")

        def run(task: Task) -> dict:
            with _http_client(_fetch_settings()) as client:
                return ops.add_manual(
                    urls,
                    task.log,
                    state=state,
                    feedback=feedback,
                    ingest=ingest,
                    text=text,
                    pdf=pdf,
                    client=client,
                )

        try:
            tasks.start("manual_add", f"手动添加 {len(urls)} 篇", run)
        except TaskRunning as exc:
            return task_fragment(request, "manual_add", str(exc))
        return task_fragment(request, "manual_add")

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
            {
                "profile": parse_profile(text),
                "text": text,
                "saved": saved,
                "path": path,
                "hints": CostHints(_settings()),
            },
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
    async def task_start(request: Request, kind: str) -> HTMLResponse:
        if kind not in TASKS:
            raise HTTPException(404, "未知的任务")
        form = await request.form()
        refresh = bool(form.get("refresh"))
        if kind == "run" and ops.is_running():
            message = "已有 sb run 在运行（CLI 或定时任务），请稍后再试"
            return task_fragment(request, kind, message, refresh)
        title, fn = TASKS[kind]
        if kind == "run" and form.get("all_time"):
            title = "立即运行（不限时间窗，只去重）"

            def fn(task: Task) -> dict:
                return _run_task(task, all_time=True)

        try:
            tasks.start(kind, title, fn)
        except TaskRunning as exc:
            return task_fragment(request, kind, str(exc), refresh)
        return task_fragment(request, kind, refresh=refresh)

    @app.get("/tasks/{kind}", response_class=HTMLResponse)
    def task_status(request: Request, kind: str, refresh: str | None = None) -> HTMLResponse:
        known = kind in TASKS or kind == "manual_add" or re.fullmatch(r"summary-\d+", kind)
        if not known:
            raise HTTPException(404, "未知的任务")
        return task_fragment(request, kind, refresh=bool(refresh))

    # ---------- 深度速读与补前置（M8） ----------

    @app.post("/summary/{item_id}", response_class=HTMLResponse)
    def summary_start(
        request: Request, item_id: int, force: str | None = Form(None)
    ) -> HTMLResponse:
        """后台生成深度速读；已有结果时直接完成（force=1 时重新生成）。"""
        with _conn() as conn:
            if not db.item_exists(conn, item_id):
                raise HTTPException(404, "条目不存在")
        kind = f"summary-{item_id}"

        def run(task: Task) -> dict:
            out = ops.deep_summary(item_id, task.log, force=bool(force))
            return {"item_id": item_id, "cost": out.cost, "cached": out.cached}

        try:
            tasks.start(kind, f"深度速读 #{item_id}", run)
        except TaskRunning as exc:
            return task_fragment(request, kind, str(exc))
        return task_fragment(request, kind)

    @app.post("/prereq/{item_id}", response_class=HTMLResponse)
    def prereq(request: Request, item_id: int, force: str | None = Form(None)) -> HTMLResponse:
        """补前置（同步，一次 strong 调用；已缓存的讲解不再调用）。"""
        lines: list[str] = []
        context: dict = {"item_id": item_id, "report": None, "error": None, "lines": lines}
        try:
            context["report"] = ops.prerequisites(item_id, lines.append, force=bool(force))
        except ops.OpsError as exc:
            context["error"] = str(exc)
        return templates.TemplateResponse(request, "_prereq.html", context)

    @app.post("/prereq/{item_id}/save", response_class=HTMLResponse)
    def prereq_save(request: Request, item_id: int, name: str = Form(...)) -> HTMLResponse:
        """把 AI 讲解存为概念页（不调用 LLM）；已存在则返回链接。"""
        try:
            slug, created = ops.save_prereq_page(item_id, name)
        except ops.OpsError as exc:
            return HTMLResponse(f'<span class="refresh">✗ {html.escape(str(exc))}</span>')
        label = "已存为概念页" if created else "wiki 已有该页面"
        return HTMLResponse(
            f'<span class="saved">{label}：<a href="/wiki/page/{slug}">{slug}</a></span>'
        )

    # ---------- 运维 ----------

    @app.get("/ops", response_class=HTMLResponse)
    def ops_page(request: Request) -> HTMLResponse:
        fs = _settings()
        now = datetime.now(UTC)
        midnight = datetime.combine(datetime.now().date(), datetime.min.time()).astimezone()
        sources_error = None
        try:
            config.load_sources()
        except config.ConfigError as exc:
            sources_error = str(exc)
        with _conn() as conn:
            today_rows, today_cost = _usage_rows(conn, midnight)
            week_rows, week_cost = _usage_rows(conn, now - timedelta(days=7))
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
                "pers": fs.personalize,
                "sources_error": sources_error,
                "sched": schedule.status(),
                "setting": _setting_context(),
                "sched_checks": _schedule_checks(fs, schedule.DEFAULT_TIME),
                "default_time": schedule.DEFAULT_TIME,
            },
        )

    @app.post("/ops/schedule/install", response_class=HTMLResponse)
    def ops_schedule_install(request: Request, at: str = Form(...)) -> HTMLResponse:
        """开启每日自动（= sb schedule install）。按钮上已有确认。"""
        try:
            local = schedule.parse_time(at)
        except ValueError as exc:
            return templates.TemplateResponse(request, "_lines.html", {"lines": [f"✗ {exc}"]})
        target = schedule.install(local, config.PROJECT_ROOT, config.data_dir() / "logs")
        lines = [f"✓ 已开启每日自动：每天本地 {local:%H:%M} 运行 sb run（{target}）"]
        lines += _schedule_checks(_settings(), at)
        lines.append("刷新页面查看当前模式。")
        return templates.TemplateResponse(request, "_lines.html", {"lines": lines})

    @app.post("/ops/schedule/uninstall", response_class=HTMLResponse)
    def ops_schedule_uninstall(request: Request) -> HTMLResponse:
        """改为手动（= sb schedule uninstall）。有运行在进行时直接拒绝：unload 会终止它。"""
        if ops.is_running() or (tasks.get("run") and tasks.get("run").running):
            lines = ["✗ 当前有运行在进行，卸载会终止它；请等运行结束后再改为手动。"]
        elif schedule.uninstall():
            lines = ["✓ 已改为手动：每日自动运行已卸载。刷新页面查看当前模式。"]
        else:
            lines = ["当前已经是手动模式（没有安装定时任务）。"]
        return templates.TemplateResponse(request, "_lines.html", {"lines": lines})

    def lines(request: Request, items: list[str]) -> HTMLResponse:
        return templates.TemplateResponse(request, "_lines.html", {"lines": items})

    def busy_note() -> list[str]:
        if ops.is_running() or (tasks.get("run") and tasks.get("run").running):
            return ["当前有运行在进行：本次保存对下次运行生效。"]
        return []

    @app.post("/ops/settings/key", response_class=HTMLResponse)
    def settings_key(request: Request, value: str = Form("")) -> HTMLResponse:
        """写入 .env（保留其他行与注释，原子写入，chmod 600）。响应中绝不包含 key。"""
        key = _setting_context()["key"]
        value = value.strip()
        if not value:
            return lines(request, ["没有输入，未做修改。"])
        try:
            effective = config.set_env_var(key, value)
        except (config.ConfigError, OSError) as exc:
            reason = exc if isinstance(exc, config.ConfigError) else type(exc).__name__
            return lines(request, [f"✗ 写入 .env 失败：{reason}"])
        out = [f"✓ 已写入 .env：{key} 已设置"]
        if not effective:
            out.append(
                "! 该变量来自环境变量，环境变量优先；.env 的修改需要重启服务或 unset 后才生效。"
            )
        return lines(request, out + busy_note())

    @app.post("/ops/settings/key/clear", response_class=HTMLResponse)
    def settings_key_clear(request: Request) -> HTMLResponse:
        key = _setting_context()["key"]
        try:
            effective = config.set_env_var(key, None)
        except OSError as exc:
            return lines(request, [f"✗ 修改 .env 失败：{type(exc).__name__}"])
        out = [f"✓ 已从 .env 删除 {key}"]
        if not effective:
            out.append("! 该变量仍在环境变量中（环境变量优先），需要重启服务或 unset。")
        return lines(request, out)

    @app.post("/ops/settings/ping", response_class=HTMLResponse)
    def settings_ping(request: Request) -> HTMLResponse:
        r = ops.ping()
        if r["ok"]:
            text = f"✓ 连接正常：{r['backend']} / {r['model']}，耗时 {r['ms']} ms"
        else:
            text = f"✗ 连接失败：{r['backend']} / {r['model']}：{r['error']}"
        return lines(request, [text])

    @app.post("/ops/settings/models", response_class=HTMLResponse)
    def settings_models(
        request: Request, cheap: str = Form(...), strong: str = Form(...), backend: str = Form(...)
    ) -> HTMLResponse:
        try:
            changes = ops.save_llm_choice(cheap, strong, backend)
        except (ops.OpsError, config.ConfigError, OSError, ValueError) as exc:
            return lines(request, [f"✗ {exc}"])
        out = [
            "✓ 已保存（只把与 example 不同的键写入 config/settings.yaml）：" + "，".join(changes)
        ]
        if os.environ.get("SB_LLM_BACKEND"):
            out.append("! 环境变量 SB_LLM_BACKEND 已设置，会覆盖这里的 backend。")
        return lines(request, out + busy_note())

    @app.post("/ops/settings/models/reset", response_class=HTMLResponse)
    def settings_models_reset(request: Request) -> HTMLResponse:
        try:
            ops.reset_llm_choice()
        except (config.ConfigError, OSError, ValueError) as exc:
            return lines(request, [f"✗ {exc}"])
        return lines(
            request, ["✓ 已恢复默认：从 config/settings.yaml 删除了模型与后端设置。"] + busy_note()
        )

    # ---------- 信源管理 ----------

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, saved: str | None = None) -> HTMLResponse:
        path, is_example = config.sources_path()
        try:
            sources, error = config.load_sources(path), None
        except config.ConfigError as exc:
            sources, error = [], str(exc)
        pers = _settings().personalize
        catalog = ops.catalog_entries()
        with _conn() as conn:
            overview = db.source_overview(conn)
            yields = source_yields(conn, sources, datetime.now(UTC), pers)
            dist = feedback_distribution(conn)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": sources,
                "overview": overview,
                "yields": yields,
                "dist": dist,
                "dist_kinds": DISTRIBUTION_KINDS,
                "catalog": catalog,
                "catalog_homepages": {s.id: source_homepage(s) for s in catalog},
                "homepages": {
                    s.id: source_homepage(
                        s, overview[s.id]["homepage"] if s.id in overview else None
                    )
                    for s in sources
                },
                "pers": pers,
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
            _, sources = _editable_sources()
            if any(s.id == source.id for s in sources):
                raise ValueError(f"信源 id 已存在：{source.id}")
            block = sources_file.dump_entry(source)
            ops.edit_sources(lambda text: sources_file.append_entry(text, block))
        except (ValueError, config.ConfigError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/sources?saved={source.id}", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    def sources_toggle(source_id: str) -> RedirectResponse:
        _, sources = _editable_sources()
        target = next((s for s in sources if s.id == source_id), None)
        if target is None:
            raise HTTPException(404, "信源不存在")
        ops.edit_sources(lambda text: sources_file.set_enabled(text, source_id, not target.enabled))
        return RedirectResponse(f"/sources?saved={source_id}", status_code=303)

    @app.post("/sources/catalog/{source_id}/add")
    def sources_catalog_add(source_id: str) -> RedirectResponse:
        """把信源目录（sources.example.yaml）中的这一条原样追加到 config/sources.yaml。"""
        example = (config.config_dir() / "sources.example.yaml").read_text(encoding="utf-8")
        block = sources_file.entry_text(example, source_id)
        if block is None:
            raise HTTPException(404, "目录中没有该信源")
        _, sources = _editable_sources()
        if any(s.id == source_id for s in sources):
            raise HTTPException(400, f"信源 id 已存在：{source_id}")
        try:
            ops.edit_sources(lambda text: sources_file.append_entry(text, block))
        except config.ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/sources?saved={source_id}", status_code=303)

    @app.post("/sources/{source_id}/peek", response_class=HTMLResponse)
    def sources_peek(request: Request, source_id: str) -> HTMLResponse:
        """预览已配置或目录中的信源：只拉取并解析最新约 10 条，不写库、不调用 LLM。"""
        candidates = {s.id: s for s in ops.catalog_entries()}
        try:
            candidates |= {s.id: s for s in config.load_sources()}
        except config.ConfigError:
            pass
        source = candidates.get(source_id)
        if source is None:
            raise HTTPException(404, "信源不存在")
        context: dict = {"source": source, "entries": [], "error": None, "total": 0}
        if source.needs_scraper:
            context["error"] = "该信源标记为 needs_scraper：没有可用的 feed，无法预览"
        else:
            fs = _fetch_settings()
            try:
                with _http_client(fs) as client:
                    entries = FETCHERS[source.type](source, client, fs)
            except FetchError as exc:
                context["error"] = f"抓取失败：{exc}"
            else:
                entries.sort(
                    key=lambda e: e.published_at or datetime.min.replace(tzinfo=UTC), reverse=True
                )
                context.update(entries=entries[:PREVIEW_ENTRIES], total=len(entries))
                channel = safe_url(getattr(entries, "homepage", None))
                if channel:  # 顺手记录 channel <link>（只更新已有的 sources 行，不写 items）
                    context["channel"] = channel
                    with _conn() as conn:
                        db.record_homepage(conn, source.id, channel)
                        conn.commit()
        return templates.TemplateResponse(request, "_peek.html", context)

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
        ops.edit_sources(lambda text: sources_file.remove_entry(text, source_id))
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


def _summary_points(summary: dict | None, url: str) -> dict[str, list[tuple[str, str | None]]]:
    """深度速读中方法 / 结果 / 局限各条及其时间戳链接（仅视频）。"""
    if not summary:
        return {}
    keys = ("method", "results", "limitations")
    return {k: [key_point_link(p, url) for p in summary.get(k, [])] for k in keys}


def _key_points(l2: dict | None, url: str) -> list[tuple[str, str | None]]:
    """要点及其时间戳链接（仅视频）。"""
    return [key_point_link(p, url) for p in (l2 or {}).get("key_points", [])]


def _history_dir() -> Path:
    return config.data_dir() / "profile_history"


def _normalize_newlines(text: str) -> str:
    """浏览器提交的 textarea 使用 CRLF。"""
    return text.replace("\r\n", "\n")
