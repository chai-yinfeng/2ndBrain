"""有界、无需 LLM 的论文名检索；不可变候选 → 统一材料入口。

远端文本只作为数据解析。仅访问公开 HTTP(S)，不执行页面指令、不绕过访问限制。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import httpx

from secondbrain import config, db, ops
from secondbrain.ingest import ContentCapture
from secondbrain.models import RawEntry
from secondbrain.net import FetchError, HttpClient
from secondbrain.public_fetch import PublicClient, diagnostic_url
from secondbrain.public_fetch import public_url as _public_url
from secondbrain.sources import arxiv, crossref, semantic_scholar

LIMIT = 8
MAX_CANDIDATES = 24
SEARCH_BYTES = 2 * 1024 * 1024
S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_SEARCH = "https://api.crossref.org/works"
OPENALEX_SEARCH = "https://api.openalex.org/works"
_ARXIV = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")
_DOI = re.compile(r"^10\.\d{4,9}/[^\s<>]+$", re.I)


def _normal(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), re.UNICODE))


def _candidate(*, title, provider, authors=None, year=None, venue="", doi=None,
               arxiv_id=None, url=None, pdf_url=None, abstract="", version="", **extra):
    title = " ".join(str(title or "").split())[:1000]
    if not title:
        return None
    doi = str(doi or "").removeprefix("https://doi.org/").lower().strip()
    doi = doi if _DOI.fullmatch(doi) else None
    aid = str(arxiv_id or "").strip()
    aid = aid if _ARXIV.fullmatch(aid) else None
    if aid:
        match = re.search(r"v\d+$", aid)
        version = version or (match.group() if match else "")
        aid = re.sub(r"v\d+$", "", aid)
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 1000 <= year <= datetime.now(UTC).year + 1:
        year = None
    url = _public_url(url) or (
        f"https://doi.org/{quote(doi, safe='/')}" if doi else
        f"https://arxiv.org/abs/{aid}{version}" if aid else None
    )
    if not url:
        return None
    return {
        "id": uuid.uuid4().hex, "title": title,
        "authors": [str(a).strip() for a in authors or [] if a], "year": year,
        "venue": str(venue or ""), "doi": doi, "arxiv_id": aid, "url": url,
        "pdf_url": _public_url(pdf_url), "version": str(version or ""),
        "provider": provider, "abstract": str(abstract or ""), **extra,
    }


def _s2(query, client):
    key = os.environ.get("S2_API_KEY")
    response = client.get(S2_SEARCH, params={"query": query, "limit": LIMIT,
                         "fields": semantic_scholar.FIELDS + ",venue"},
                         headers={"x-api-key": key} if key else None, max_bytes=SEARCH_BYTES)
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Semantic Scholar 响应缺少 data")
    result = []
    for row in data["data"][:LIMIT]:
        ids = row.get("externalIds") or {}
        result.append(_candidate(
            title=row.get("title"), provider="semantic_scholar",
            authors=[a.get("name") for a in row.get("authors") or []],
            year=row.get("year"), venue=row.get("venue"), doi=ids.get("DOI"),
            arxiv_id=ids.get("ArXiv"), url=row.get("url"),
            pdf_url=(row.get("openAccessPdf") or {}).get("url"),
            abstract=row.get("abstract"), provider_id=row.get("paperId"),
        ))
    return result


def _crossref(query, client):
    data = client.get(CROSSREF_SEARCH, params={"query.bibliographic": query, "rows": LIMIT},
                      max_bytes=SEARCH_BYTES).json()
    rows = data["message"]["items"]
    if not isinstance(rows, list):
        raise ValueError("Crossref 响应缺少 items")
    result = []
    for row in rows[:LIMIT]:
        parts = ((row.get("issued") or {}).get("date-parts") or [[None]])[0]
        links = [link.get("URL") for link in row.get("link") or []
                 if "pdf" in (link.get("content-type") or "").lower()]
        result.append(_candidate(
            title=(row.get("title") or [""])[0], provider="crossref",
            authors=[" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name")
                     for a in row.get("author") or []],
            year=parts[0] if parts else None,
            venue=(row.get("container-title") or [""])[0], doi=row.get("DOI"),
            url=row.get("URL"), pdf_url=links[0] if links else None,
            abstract=crossref.jats_to_text(row.get("abstract")),
            version="published", alternate_pdf_urls=[u for u in links if _public_url(u)],
        ))
    return result


def _arxiv(query, client):
    # 不接受用户输入的 API 查询语法；作为普通词项进行标题/作者检索。
    words = re.findall(r"\w+", query, re.UNICODE)[:40]
    term = 'ti:"' + " ".join(words) + '"'
    response = client.get(arxiv.API_URL, params={"search_query": term, "max_results": LIMIT,
                                               "start": 0}, max_bytes=SEARCH_BYTES)
    entries = arxiv.parse_api_response(response.content)
    originals = feedparser.parse(response.content).entries
    versions = {arxiv.parse_arxiv_id(e.get("id", "")): re.search(r"v\d+$", e.get("id", ""))
                for e in originals}
    return [_candidate(
        title=e.title, provider="arxiv", authors=(e.author or "").split(", "),
        year=e.published_at.year if e.published_at else None, venue="arXiv", doi=e.doi,
        arxiv_id=e.arxiv_id, url=e.url, abstract=e.summary,
        version=versions[e.arxiv_id].group() if versions.get(e.arxiv_id) else "",
    ) for e in entries[:LIMIT]]


def _openalex(query, client):
    data = client.get(OPENALEX_SEARCH, params={"search": query, "per-page": LIMIT},
                      max_bytes=SEARCH_BYTES).json()
    rows = data["results"]
    if not isinstance(rows, list):
        raise ValueError("OpenAlex 响应缺少 results")
    result = []
    for row in rows[:LIMIT]:
        locations = row.get("locations") or []
        primary = row.get("primary_location") or {}
        best = row.get("best_oa_location") or {}
        locations = [best, primary, *locations]
        pdfs = [loc["pdf_url"] for loc in locations if _public_url(loc.get("pdf_url"))]
        urls = [loc["landing_page_url"] for loc in locations
                if _public_url(loc.get("landing_page_url"))]
        # 优先公开会议正文页，不猜测 USENIX slug 或硬编码题目。
        urls.sort(key=lambda u: "usenix.org" not in (urlsplit(u).hostname or ""))
        inverted = row.get("abstract_inverted_index") or {}
        positions = sorted((pos, word) for word, indexes in inverted.items() for pos in indexes)
        result.append(_candidate(
            title=row.get("display_name"), provider="openalex",
            authors=[(a.get("author") or {}).get("display_name")
                     for a in row.get("authorships") or []],
            year=row.get("publication_year"),
            venue=(primary.get("source") or {}).get("display_name"),
            doi=row.get("doi"), url=urls[0] if urls else row.get("doi"),
            pdf_url=pdfs[0] if pdfs else None, abstract=" ".join(w for _, w in positions),
            version=best.get("version") or primary.get("version") or "",
            provider_id=row.get("id"), alternate_urls=urls, alternate_pdf_urls=pdfs,
        ))
    return result


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.href = None
        self.text = []
        self.abstract = []
        self.abstract_depth = 0
        self.description = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("name") == "description"
                              or attributes.get("property") == "og:description"):
            self.description = attributes.get("content") or self.description
        if tag == "div":
            classes = attributes.get("class") or ""
            if self.abstract_depth or any(name in classes for name in (
                "field-name-field-paper-description", "field-name-field-paper-abstract",
            )):
                self.abstract_depth += 1
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.abstract_depth:
            self.abstract.append(data)
        if self.href:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "div" and self.abstract_depth:
            self.abstract_depth -= 1
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join("".join(self.text).split())))
            self.href = None


def _usenix(query, client):
    from secondbrain.extract.html import citation_meta

    response = client.get("https://www.usenix.org/search/site/" + quote(query, safe=""),
                          max_bytes=SEARCH_BYTES)
    parser = _Links()
    parser.feed(response.text)
    result, seen = [], set()
    for href, title in parser.links:
        url = urljoin(str(response.url), href)
        parts = urlsplit(url)
        if (parts.hostname not in ("www.usenix.org", "usenix.org")
                or not re.fullmatch(r"/conference/[^/]+/presentation/[^/]+/?", parts.path)
                or url in seen
                or not (_terms(query) & _terms(title) - _QUERY_WORDS)):
            continue
        seen.add(url)
        page = client.get(url, max_bytes=SEARCH_BYTES)
        meta = citation_meta(page.text)
        page_parser = _Links()
        page_parser.feed(page.text)
        abstract = " ".join(" ".join(page_parser.abstract).split()) or page_parser.description
        year = re.search(r"\b(?:19|20)\d{2}\b", meta.get("date", ""))
        candidate = _candidate(
            title=meta.get("title"), authors=meta.get("authors"), provider="usenix",
            year=year.group() if year else None,
            venue="USENIX " + parts.path.split("/")[2].upper(),
            doi=meta.get("doi"), url=url,
            pdf_url=urljoin(url, meta["pdf_url"]) if meta.get("pdf_url") else None,
            version="published", verified_official=True,
            abstract=re.sub(r"^Abstract\s*:?\s*", "", abstract, flags=re.I),
        )
        result.append(candidate)
        if len(seen) >= 3:
            break
    return result


_QUERY_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
    "the", "to", "with", "paper", "author", "year", "venue",
}


def _terms(value):
    return set(re.findall(r"[^\W\d_]+|\d+", value.casefold(), re.UNICODE))


def _metadata_terms(candidate):
    venue = candidate.get("venue") or ""
    known = _terms(" ".join(candidate.get("authors") or []) + " " + venue)
    if candidate.get("year"):
        known.add(str(candidate["year"]))
    initials = "".join(
        word[0] for word in _normal(venue).split()
        if word not in _QUERY_WORDS and not word.isdecimal()
    )
    if initials:
        known.add(initials)
    return known


def _description_match(query, candidate):
    if _normal(query) == _normal(candidate["title"]):
        return True
    words, title = _terms(query), _terms(candidate["title"])
    metadata = _metadata_terms(candidate)
    return (
        bool((words & title) - _QUERY_WORDS)
        and bool(words & (metadata - title - _QUERY_WORDS))
        and words <= title | metadata | _QUERY_WORDS
    )


def _relevant(query, candidate):
    title, words = _terms(candidate["title"]), _terms(query)
    if not title:
        return False
    if len(title & words) / len(title) >= 0.5:
        return True
    return (
        bool((title & words) - _QUERY_WORDS)
        and words <= title | _metadata_terms(candidate) | _QUERY_WORDS
    )


def _same_record(left, right):
    # 标题从不证明身份。共享明确标识才可合并 provider 记录；作者/年份差异留给用户。
    shared = any(left.get(key) and left.get(key) == right.get(key)
                 for key in ("doi", "arxiv_id"))
    conflict = any(left.get(key) and right.get(key) and left[key] != right[key]
                   for key in ("doi", "arxiv_id", "year"))
    # 已共享标识的记录允许名字缩写/连字符差异；不同作者仍保留为待选择候选。
    def author_key(name):
        parts = _normal(name.replace("-", "")).split()
        if len(parts) > 1:
            return parts[-1], parts[0][0]
        return (parts[0], "") if parts else ("", "")

    authors_left = {author_key(a) for a in left["authors"]}
    authors_right = {author_key(a) for a in right["authors"]}
    if authors_left and authors_right and authors_left != authors_right:
        conflict = True
    return shared and not conflict and _normal(left["title"]) == _normal(right["title"])


def _search(query, client):
    candidates, errors = [], []
    providers = [("Semantic Scholar", _s2), ("Crossref", _crossref), ("arXiv", _arxiv)]
    successes = 0
    # OpenAlex 补全无 DOI 的会议论文，也作为已有元数据失败时的公开兜底。
    for name, provider in [*providers, ("OpenAlex", _openalex), ("USENIX", _usenix)]:
        try:
            found = provider(query, client)
            successes += 1
        except (
            FetchError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError
        ) as exc:
            errors.append(f"{name} 检索失败：{exc}")
            continue
        for candidate in found:
            if candidate is None or not _relevant(query, candidate):
                continue
            candidate["records"] = [{k: v for k, v in candidate.items() if k != "id"}]
            old = next((c for c in candidates if _same_record(c, candidate)), None)
            if old:
                old["records"].extend(candidate["records"])
                for key in ("doi", "arxiv_id", "abstract", "pdf_url"):
                    old[key] = old[key] or candidate[key]
            elif len(candidates) < MAX_CANDIDATES:
                candidates.append(candidate)
    candidates.sort(key=lambda c: not _description_match(query, c))
    return candidates, errors, successes


def _result(session, status, message, *, selected=None, material_id=None, errors=None,
            ingest=None, acquisition=None):
    return {"status": status, "search_id": session["id"], "query": session["query"],
            "candidates": session["candidates"], "selected": selected, "material_id": material_id,
            "message": message, "errors": errors if errors is not None else session["errors"],
            "ingest": ingest, "acquisition": acquisition or {}}


def _save_result(conn, result):
    conn.execute(
        """INSERT INTO paper_import_attempts(search_id, candidate_id, result_json, created_at)
           VALUES (?, ?, ?, ?)""",
        (result["search_id"], (result["selected"] or {}).get("id"),
         json.dumps(result, ensure_ascii=False), db.format_time(datetime.now(UTC))),
    )
    conn.commit()
    return result


def _check_identity(candidate, fetched):
    entry = fetched.entry
    doi, aid = db.discover_ids(fetched.content)
    ops._identity(entry, fetched.content, RawEntry(
        url=candidate["url"], title=candidate["title"],
        doi=candidate["doi"], arxiv_id=candidate["arxiv_id"],
    ))
    if entry.title and _public_url(entry.title) is None:
        if _normal(entry.title) != _normal(candidate["title"]):
            raise ValueError("材料身份冲突：下载记录标题与所选候选不同")
    # 远端 PDF 常无元数据；仅在冲突检查后补上所选记录的标识。
    entry.doi = candidate["doi"] or entry.doi or doi
    entry.arxiv_id = candidate["arxiv_id"] or entry.arxiv_id or aid
    entry.title = candidate["title"]
    entry.author = entry.author or ", ".join(candidate["authors"]) or None
    entry.summary = max((entry.summary or "", candidate["abstract"]), key=len) or None
    if entry.published_at is None and candidate["year"]:
        entry.published_at = datetime(candidate["year"], 1, 1, tzinfo=UTC)


def _fetch_candidate(candidate, client, fs):
    records = sorted(
        candidate.get("records") or [candidate],
        key=lambda record: not (
            record.get("arxiv_id") and re.fullmatch(r"v\d+", record.get("version", ""))
        ),
    )
    targets = []
    for record in records:
        if record.get("arxiv_id"):
            aid = record["arxiv_id"] + (
                record.get("version", "")
                if re.fullmatch(r"v\d+", record.get("version", "")) else ""
            )
            targets.append((f"https://arxiv.org/abs/{aid}", record))
        for url in [record.get("pdf_url"), *(record.get("alternate_pdf_urls") or []),
                    record.get("url"), *(record.get("alternate_urls") or [])]:
            if url and _public_url(url):
                targets.append((url, record))
    seen, errors = set(), []
    attempt_start = len(client.attempts)
    response_start = len(client.responses)
    best = None

    def result(fetched, record):
        acquisition = fetched.acquisition
        acquisition["attempts"] = list(client.attempts[attempt_start:])
        provenance = {
            "requested_url": acquisition["requested_url"],
            "actual_url": acquisition["actual_url"],
            "requested_version": record.get("version", ""),
            "actual_version": (
                "metadata_only" if acquisition["status"] in ("abstract", "missing")
                else acquisition["version"]
            ),
            "provider_version": record.get("version", ""),
            "responses": list(client.responses[response_start:]),
        }
        if (acquisition["actual_url"] is None and candidate["abstract"]
                and fetched.entry.summary == candidate["abstract"]):
            provenance["abstract_source"] = "selected_candidate"
        return fetched, provenance, errors

    for url, record in targets:
        if url in seen:
            continue
        seen.add(url)
        capture = ContentCapture(client)
        try:
            fetched = ops._fetch_manual(url, capture, fs)
            _check_identity(candidate, fetched)
            if fetched.method == "html" and fetched.completeness == "full":
                # 普通页面没有声明论文身份时，也不能将出版社摘要当作全文。
                fetched.completeness = (
                    "abstract" if len(fetched.content) < max(2000, 2 * len(candidate["abstract"]))
                    else "partial"
                )
                fetched.acquisition["status"] = fetched.completeness
            if fetched.completeness in ("abstract", "missing"):
                summary = max((candidate["abstract"], fetched.entry.summary or ""), key=len)
                if (summary and summary not in fetched.content
                        and (fetched.method == "abstract_only"
                             or fetched.completeness == "missing"
                             or len(summary) > len(fetched.content))):
                    fetched.entry.summary = summary
                    fetched.content = f"{fetched.entry.title}\n\n{summary}"
                    fetched.note = "仅摘要"
                    fetched.method = "abstract_only"
                    fetched.completeness = "abstract"
                    fetched.acquisition.update(actual_url=None, status="abstract",
                                               method="abstract_only", version="unknown")
            if fetched.completeness == "missing":
                continue
            if fetched.completeness == "full":
                return result(fetched, record)
            score = (fetched.completeness == "partial", len(fetched.content))
            if best is None or score > best[0]:
                best = score, fetched, record
        except ValueError as exc:
            exc.acquisition = ops._acquisition(candidate["url"], capture)
            exc.acquisition["actual_url"] = None
            exc.acquisition["attempts"] = list(client.attempts[attempt_start:])
            raise
        except (ops.ManualFetchError, FetchError, httpx.HTTPError) as exc:
            errors.append(f"正文获取失败 {diagnostic_url(url)}：{exc}")
    if best is not None:
        return result(best[1], best[2])
    acquisition = {
        "requested_url": candidate["url"], "actual_url": None, "status": "missing",
        "method": "none", "version": "unknown",
        "attempts": list(client.attempts[attempt_start:]),
    }
    if not candidate["abstract"]:
        raise ops.ManualFetchError("未找到可访问正文或摘要。" + "；".join(errors), acquisition)
    entry = RawEntry(url=candidate["url"], title=candidate["title"],
                     doi=candidate["doi"], arxiv_id=candidate["arxiv_id"],
                     author=", ".join(candidate["authors"]) or None, summary=candidate["abstract"],
                     published_at=datetime(candidate["year"], 1, 1, tzinfo=UTC)
                     if candidate["year"] else None)
    acquisition.update(status="abstract", method="abstract_only")
    fetched = ops.ManualFetched(entry, candidate["abstract"], "abstract_only",
                               ops.manual_canonical(candidate["url"]), note="仅摘要",
                               completeness="abstract", acquisition=acquisition)
    return result(fetched, candidate)


def _import(session, candidate, conn, client, fs, echo, ingest):
    errors = list(session["errors"])
    try:
        fetched, provenance, fetch_errors = _fetch_candidate(candidate, client, fs)
        errors.extend(fetch_errors)
        item = ops._material(fetched, fs, kind="paper_search")
        item.provenance.update({"search_id": session["id"], "query": session["query"],
                                "candidate": candidate, **provenance})
        material_id, created, improved = db.persist_material(conn, item, datetime.now(UTC))
        db.add_to_reading_list(conn, material_id)
        conn.commit()
    except (ValueError, ops.ManualFetchError, FetchError) as exc:
        conn.rollback()
        errors.append(str(exc))
        result = _result(session, "failed", str(exc), selected=candidate, errors=errors,
                         acquisition=getattr(exc, "acquisition", {}))
        echo(result["message"])
        return _save_result(conn, result)
    abstract_only = fetched.completeness in ("abstract", "missing")
    message = f"{'已导入' if created else '已补全' if improved else '材料已存在'} #{material_id}"
    requested, actual = provenance["requested_version"], provenance["actual_version"]
    if requested and actual not in ("unknown", "metadata_only", requested):
        message += f"；请求版本 {requested}，实际取得 {actual}"
    if abstract_only:
        message += "；仅摘要，未获取到全文"
    elif item.completeness != "full":
        message += "；正文不完整，完整性与原件已保留"
    summary = None
    if ingest:
        try:
            summary = asdict(ops.ingest_items([material_id], echo))
            if summary["failures"]:
                errors.extend(summary["errors"] or ["知识整理失败"])
                message += "；知识整理失败（材料已保留）"
        except ops.OpsError as exc:
            errors.append(str(exc))
            message += f"；知识整理失败：{exc}（材料已保留）"
    result = _result(session, "abstract_only" if abstract_only else "imported", message,
                     selected=candidate, material_id=material_id, errors=errors, ingest=summary,
                     acquisition=fetched.acquisition)
    echo(message)
    return _save_result(conn, result)


def import_by_name(query: str, echo, *, ingest=False, client=None) -> dict:
    """完整标题或标题+作者/年份/会议。歧义时仅保存候选，不抓正文或调用 LLM。"""
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("请输入 1–1000 字符的论文名或名称 + 作者/年份/会议描述")
    if any(ord(c) < 32 for c in query):
        raise ValueError("论文名不能包含控制字符")
    query = " ".join(query.split())
    config.load_env()
    _, _, fs = ops._settings()
    own = client is None
    raw = client if client is not None else HttpClient(fs)
    public = PublicClient(raw)
    conn = db.connect()
    try:
        candidates, errors, successes = _search(query, public)
        session = {"id": uuid.uuid4().hex, "query": query, "candidates": candidates,
                   "errors": errors}
        conn.execute("INSERT INTO paper_searches VALUES (?, ?, ?, ?, ?)",
                     (session["id"], query, json.dumps(candidates, ensure_ascii=False),
                      json.dumps(errors, ensure_ascii=False), db.format_time(datetime.now(UTC))))
        conn.commit()
        if not candidates:
            message = "没有找到匹配论文" if successes else "学术检索服务不可用，未导入材料"
            if errors:
                message += "；部分检索失败，详见错误记录" if successes else "；请稍后重试"
            result = _result(session, "not_found" if successes else "failed", message)
        else:
            exact = [c for c in candidates if _description_match(query, c)]
            # 同名但不同作者/年份/标识的候选不因 provider 排序而被静默选中。
            same_title = [c for c in candidates if exact and
                          _normal(c["title"]) == _normal(exact[0]["title"])]
            if len(exact) == 1 and (
                len(same_title) == 1 or _normal(query) != _normal(exact[0]["title"])
            ):
                selected = exact[0]
                if selected["authors"] and (selected["doi"] or selected["arxiv_id"] or
                                             selected["provider"] in ("openalex", "usenix")):
                    return _import(session, selected, conn, public, fs, echo, ingest)
            result = _result(session, "ambiguous", "存在多个候选或身份尚不明确，请选择论文候选")
        echo(result["message"])
        return _save_result(conn, result)
    finally:
        conn.close()
        if own:
            raw.close()


def choose_candidate(search_id: str, candidate_id: str, echo, *, ingest=False, client=None) -> dict:
    """只接收已保存的 ID，不重跑检索，也不接受客户端提供的候选 URL。"""
    if not isinstance(search_id, str) or not isinstance(candidate_id, str):
        raise ValueError("检索与候选 ID 无效")
    conn = db.connect()
    public = None
    own = client is None
    raw = client
    try:
        row = conn.execute("SELECT * FROM paper_searches WHERE id = ?", (search_id,)).fetchone()
        if row is None:
            raise ValueError("检索记录不存在，请重新输入论文名")
        session = {"id": row["id"], "query": row["query"],
                   "candidates": json.loads(row["candidates_json"]),
                   "errors": json.loads(row["errors_json"])}
        candidate = next((c for c in session["candidates"] if c["id"] == candidate_id), None)
        if candidate is None:
            raise ValueError("候选不属于该检索记录")
        config.load_env()
        _, _, fs = ops._settings()
        raw = client if client is not None else HttpClient(fs)
        public = PublicClient(raw)
        return _import(session, candidate, conn, public, fs, echo, ingest)
    finally:
        conn.close()
        if own and raw is not None:
            raw.close()
