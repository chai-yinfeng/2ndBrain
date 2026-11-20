"""书目识别、跨入口身份与可恢复归并；只用临时数据及真实 PDF/HTTP 消费者。"""

import io
import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import fast_settings, make_pdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from secondbrain import config, db, ingest, material_identity, ops
from secondbrain import material_management as management
from secondbrain.bibliography import compare_bibliography
from secondbrain.extract.pdf import extract_pdf_text
from secondbrain.models import RawEntry, SourceConfig
from secondbrain.net import HttpClient
from secondbrain.urls import content_hash
from secondbrain.wiki.ingest import material_input_hash
from secondbrain.wiki.store import Page, new_meta, open_wiki

NOW = datetime(2026, 11, 18, tzinfo=UTC)
TITLE = "The Design and Implementation of Zap: A System for Migrating Computing Environments"
AUTHORS = ["Steven Osman", "Dinesh Subhraveti", "Gong Su", "Jason Nieh"]
DOI = "10.1145/1060289.1060323"
PDF_URL = "https://example.org/osdi2002.pdf"
BIB = {"title": TITLE, "authors": AUTHORS, "year": 2002, "kind": "paper",
       "evidence": ["html:citation-meta"], "verified": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SB_LLM_BACKEND", "fake")
    monkeypatch.setattr(ops, "HttpClient", lambda *_args, **_kwargs: HttpClient(
        fast_settings(), transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ))
    return tmp_path


def paper_pdf(*, cover=False, author_rows=None, publication=None):
    """物理页头在内容流尾部，模拟旧排版软件；不mock pypdf或提取器。"""
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)

    def page(lines):
        target = writer.add_blank_page(width=1000, height=800)
        target[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        stream = DecodedStreamObject()
        stream.set_data("\n".join(
            f"BT /F1 11 Tf 1 0 0 1 50 {y} Tm ({text}) Tj ET" for y, text in lines
        ).encode("ascii"))
        target[NameObject("/Contents")] = writer._add_object(stream)

    if cover:
        page([(730, "Publisher proceedings cover"), (700, "https://example.org/presentation")])
    page([
        (620, "Abstract"),
        (600, "We have created a system for migrating computing environments."),
        (570, "1 Introduction"),
        (550, "The complete paper body remains available to its real consumers."),
        (760, publication or
         "Appears in Proceedings of the 5th Symposium OSDI 2002, December 2002."),
        (710, "The Design and Implementation of Zap:"),
        (692, "A System for Migrating Computing Environments"),
        *(author_rows or [
            (660, "Steven Osman, Dinesh Subhraveti, Gong Su, and Jason Nieh"),
            (640, "Department of Computer Science, Columbia University"),
        ]),
    ])
    writer.add_metadata({"/Title": "ZAP-Paper-r30-UpdatedRef-MyScience-Numbers.mif",
                         "/Author": "Administrator", "/CreationDate": "D:20030101120000"})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def material(url, text, *, bibliography=None, title=None, **kwargs):
    title = title or url
    return db.NewItem(
        source_id=None, url=url, url_canonical=url, title=title,
        author=kwargs.pop("author", None), summary=None, content=text,
        extract_method=kwargs.pop("extract_method", "manual_text"),
        content_hash=content_hash(title, text),
        published_at=kwargs.pop("published_at", None),
        provenance={"bibliography": bibliography} if bibliography else {}, **kwargs,
    )


def added(*args, **kwargs):
    result = ops.add_manual(*args, echo=lambda _: None, **kwargs)["results"][0]
    assert result["status"] != "失败", result
    return result


def legacy_pair():
    """既有摘要与无书目旧 PDF，不能用新采集器预先去重来伪造归并回归。"""
    conn = db.connect()
    first, _, _ = db.persist_material(conn, material(
        "https://example.org/abstract", "A previously saved abstract.",
        bibliography={**BIB, "doi": DOI}, title=TITLE, author=", ".join(AUTHORS),
        doi=DOI, extract_method="abstract_only", published_at=datetime(2002, 1, 1, tzinfo=UTC),
    ), NOW)
    pdf = paper_pdf()
    text = extract_pdf_text(pdf, 10)
    second, created, _ = db.persist_material(conn, material(
        PDF_URL, text, pdf_data=pdf, extract_method="pdf",
    ), NOW)
    assert created and first != second
    conn.commit()
    return conn, first, second, pdf


def test_zap_physical_header_beats_stream_order_and_garbage_info(env):
    pdf = paper_pdf()
    assert extract_pdf_text(pdf, 10).index("Abstract") < extract_pdf_text(pdf, 10).index(
        "The Design and Implementation"
    )
    result = added([], pdf=pdf)
    assert result["title"] == TITLE
    assert result["bibliography"]["authors"] == AUTHORS
    assert result["bibliography"]["year"] == 2002
    conn = db.connect()
    try:
        row = db.item_detail(conn, result["item_id"])
        assert row["author"] == ", ".join(AUTHORS)
        assert row["published_at"].startswith("2002-")
        assert db.material_bibliography(conn, row["id"])["kind"] == "paper"
        source, = db.material_origins(conn, row["id"])
        assert (config.wiki_dir() / source["original_path"]).read_bytes() == pdf
        assert "Administrator" not in json.dumps(db.material_bibliography(conn, row["id"]))
        assert "2003" not in json.dumps(db.material_bibliography(conn, row["id"]))
    finally:
        conn.close()
    assert added([], pdf=pdf, title="My custom display title")["title"] == TITLE
    custom = added([], pdf=paper_pdf(cover=True), title="My custom display title")
    # The same paper is found by bibliography, not overwritten by a display label.
    assert custom["item_id"] == result["item_id"] and custom["title"] == TITLE


def test_cover_page_is_skipped_without_falsifying_extraction_page_counts(env):
    metadata = {}
    text = extract_pdf_text(paper_pdf(cover=True), 1, metadata=metadata)
    assert "Publisher proceedings cover" in text and "Abstract" not in text
    assert metadata["bibliography"]["title"] == TITLE
    assert metadata["bibliography"]["year"] == 2002
    assert (metadata["total_pages"], metadata["extracted_pages"]) == (2, 1)
    metadata = {}
    extract_pdf_text(make_pdf(["An ordinary personal document"]), 1, metadata=metadata)
    assert metadata["bibliography"] == {}


def test_explicit_display_title_survives_bibliographic_pdf_import(env):
    first = added([], pdf=paper_pdf(), title="My reading notebook")
    assert first["title"] == "My reading notebook"
    assert first["bibliography"]["title"] == TITLE
    conn = db.connect()
    try:
        assert db.material_bibliography(conn, first["item_id"])["title"] == TITLE
    finally:
        conn.close()


def test_html_subscription_and_local_pdf_share_unique_bibliography(env, make_client, monkeypatch):
    url = "https://example.org/publisher"
    tags = [f'<meta name="citation_title" content="{TITLE}">',
            '<meta name="citation_publication_date" content="2002/12/01">',
            '<meta name="citation_conference_title" content="OSDI">']
    tags += [f'<meta name="citation_author" content="{author}">' for author in AUTHORS]
    html = ("<html><head>" + "".join(tags) + "</head><body><article><h1>" + TITLE
            + "</h1><p>" + "A scholarly article with a substantial accessible body. " * 20
            + "</p></article></body></html>")
    client, _ = make_client({url: httpx.Response(200, text=html),
                             PDF_URL: httpx.Response(200, content=paper_pdf())})
    first = added([url], client=client)
    source = SourceConfig(id="course", type="rss", url="https://example.org/feed")
    monkeypatch.setitem(ingest.FETCHERS, "rss", lambda *_: [RawEntry(url=PDF_URL, title=PDF_URL)])
    incoming, _, notes, _ = ingest.collect(source, client, fast_settings(), None, NOW)
    assert not notes and incoming[0].title == TITLE
    assert incoming[0].provenance["bibliography"]["authors"] == AUTHORS
    conn = db.connect()
    try:
        db.upsert_sources(conn, [source])
        item_id, created, _ = db.persist_material(conn, incoming[0], NOW)
        conn.commit()
        assert item_id == first["item_id"] and not created
        assert len(db.material_origins(conn, item_id)) == 2
        assert db.material_bibliography(conn, item_id)["year"] == 2002
    finally:
        conn.close()
    assert added([], pdf=paper_pdf(), client=client)["item_id"] == first["item_id"]


def test_same_title_different_authors_remain_separate_and_initials_are_not_weak_identity(env):
    conn = db.connect()
    try:
        first, _, _ = db.persist_material(conn, material("https://example.org/a", "Work A",
                                                          bibliography=BIB), NOW)
        other = {**BIB, "authors": ["A Differentauthor"]}
        second, created, _ = db.persist_material(conn, material("https://example.org/b", "Work B",
                                                               bibliography=other), NOW)
        assert created and second != first
        assert material_identity.resolve_existing(conn, BIB) == first
        assert not compare_bibliography(BIB, other)["match"]
        initials = {**BIB, "authors": ["S. Osman"]}
        assert not compare_bibliography(initials, initials)["match"]
    finally:
        conn.close()


def test_bibliographic_identity_requires_unique_match_and_explicit_ids_cannot_be_masked(env):
    conn = db.connect()
    try:
        first, _, _ = db.persist_material(conn, material("https://example.org/a", "Old A"), NOW)
        second, _, _ = db.persist_material(conn, material("https://example.org/b", "Old B"), NOW)
        for item_id in (first, second):
            conn.execute("UPDATE item_sources SET metadata_json = ? WHERE item_id = ?",
                         (json.dumps({"bibliography": BIB}), item_id))
        with pytest.raises(ValueError, match="不唯一"):
            db.persist_material(
                conn, material("https://example.org/c", "New C", bibliography=BIB), NOW
            )
        assert conn.execute("SELECT count(*) FROM item_sources").fetchone()[0] == 2
        with pytest.raises(ValueError, match="身份冲突"):
            db.persist_material(conn, material("https://example.org/c", "New C", doi=DOI,
                                bibliography={**BIB, "doi": "10.1234/different"}), NOW)
    finally:
        conn.close()


def test_matching_public_identifier_with_author_conflict_rejects_import(env):
    conn = db.connect()
    try:
        db.persist_material(conn, material("https://example.org/a", "Original",
                            bibliography={**BIB, "doi": DOI}), NOW)
        with pytest.raises(ValueError, match="authors"):
            db.persist_material(conn, material(
                "https://example.org/b", "Different",
                bibliography={**BIB, "doi": DOI, "authors": ["Another Author"]},
            ), NOW)
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    finally:
        conn.close()


def test_completion_persists_pdf_bibliography_and_does_not_replace_custom_title(env):
    conn = db.connect()
    try:
        item_id, _, _ = db.persist_material(conn, material(
            "https://example.org/placeholder", "Brief abstract", title="My custom saved title",
            doi=DOI, extract_method="abstract_only",
        ), NOW)
        conn.commit()
        assert ops.complete_item(item_id, pdf=paper_pdf()) == "manual_pdf"
        assert db.material_bibliography(conn, item_id)["title"] == TITLE
        assert db.item_detail(conn, item_id)["title"] == "My custom saved title"
    finally:
        conn.close()


def test_merge_preview_rechecks_notes_and_database_changes(env):
    conn, primary, duplicate, _ = legacy_pair()
    try:
        preview = material_identity.preview_merge(primary, duplicate)
        ops.material_notes(primary, "New private note after preview")
        with pytest.raises(ValueError, match="过期"):
            material_identity.apply_merge(primary, duplicate, preview["token"])
        assert db.resolve_material_id(conn, duplicate) == duplicate
        ops.material_notes(duplicate, "A different private note")
        with pytest.raises(ValueError, match="非空个人笔记"):
            material_identity.preview_merge(primary, duplicate)
        ops.material_notes(duplicate, "")
        preview = material_identity.preview_merge(primary, duplicate)
        db.add_assessment(conn, duplicate, "L2", {"tldr": "New assessment"}, NOW)
        conn.commit()
        with pytest.raises(ValueError, match="过期"):
            material_identity.apply_merge(primary, duplicate, preview["token"])
        assert not (config.data_dir() / "material-merge-journal.json").exists()
    finally:
        conn.close()


def test_merge_keeps_originals_history_machine_bodies_and_rejects_alias_writes(env):
    conn, primary, duplicate, pdf = legacy_pair()
    try:
        db.add_feedback(conn, duplicate, "gained", "Useful paper", NOW)
        db.add_to_reading_list(conn, duplicate, NOW)
        db.add_assessment(conn, duplicate, "L2", {"tldr": "Old evaluation"}, NOW)
        db.set_ai(conn, duplicate, "summary", {"summary": "Old AI history"}, "fake", NOW)
        db.record_ingest(conn, duplicate, [f"material-{duplicate}", "migration"], NOW)
        conn.execute(
            "INSERT INTO digests (date, item_id, lane, rank, est_minutes, "
            "is_deep_read, created_at) "
            "VALUES ('2026-11-17', ?, 'must_read', 1, 5, 0, ?)", (duplicate, db.format_time(NOW)),
        )
        conn.execute("UPDATE items SET processing_status = 'processed' WHERE id = ?", (primary,))
        conn.commit()
        ops.material_notes(duplicate, "Preserved learning notes")
        wiki = open_wiki(config.wiki_dir(), config.raw_dir())
        body = "# Original machine body\n\n## Human addition\n\nDo not rewrite [[migration]].\n"
        meta = new_meta(TITLE, "material", NOW.date())
        meta.update(generated_by="wiki_ingest", material_id=duplicate, sources=[duplicate])
        old_page = wiki.save_page(Page(f"material-{duplicate}", meta, body))
        old_page.write_bytes(old_page.read_bytes().replace(b"\n", b"\r\n"))
        concept_meta = new_meta("Migration", "concept", NOW.date())
        concept_meta.update(generated_by="wiki_ingest", sources=[duplicate])
        concept = wiki.save_page(Page("migration", concept_meta, "Concept body stays unchanged.\n"))
        original_body = old_page.read_bytes().split(b"\r\n---", 1)[1]
        concept_body = concept.read_bytes().split(b"\n---", 1)[1]
        wiki.rebuild_index()
        assert f"[[material-{duplicate}]]" in wiki.index_path.read_text()
        original_paths = {
            origin["original_path"]: (wiki.root / origin["original_path"]).read_bytes()
            for iid in (primary, duplicate) for origin in db.material_origins(conn, iid)
        }
        history = {
            table: [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE item_id = ?", (duplicate,)
            )]
            for table in (
                "feedback", "assessments", "digests", "wiki_ingests", "item_ai", "reading_list",
            )
        }
        previous_hash = material_input_hash(db.item_detail(conn, primary), "")
        preview = material_identity.preview_merge(primary, duplicate)
        result = material_identity.apply_merge(primary, duplicate, preview["token"])
        assert result["improved"]
        assert result["index_updated"] and result["index_warning"] is None
        row = db.item_detail(conn, primary)
        assert row["content_completeness"] == "full" and row["reading_state"] == "read"
        assert row["processing_status"] == "pending"
        assert material_input_hash(row, ops.material_notes(primary)) != previous_hash
        assert db.lifecycle_states(conn)[primary] == {
            "hidden": False, "trashed": False, "revision": 1, "synced_revision": 0,
        }
        assert db.resolve_material_id(conn, duplicate) == primary
        assert db.item_exists(conn, duplicate)
        assert len(db.material_origins(conn, primary)) == 2
        preserved = "Preserved learning notes"
        assert ops.material_notes(primary) == ops.material_notes(duplicate) == preserved
        for relative, data in original_paths.items():
            assert (wiki.root / relative).read_bytes() == data
        assert pdf in original_paths.values()
        for table, expected in history.items():
            assert [tuple(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE item_id = ?", (duplicate,)
            )] == expected
        assert old_page.read_bytes().endswith(original_body)
        assert concept.read_bytes().endswith(concept_body)
        wiki.reload_pages()
        legacy = wiki.pages[f"material-{duplicate}"]
        assert legacy.meta["merged_into"] == primary
        assert type(legacy.meta["merged_into"]) is int
        assert legacy.meta["material_sync"]["status"] == "withdrawn"
        assert wiki.pages["migration"].meta["sources"] == [primary]
        assert f"[[material-{duplicate}]]" not in wiki.index_path.read_text()
        assert [row["id"] for row in db.list_items(conn)] == [primary]
        assert [row["id"] for row in management.list_materials(view="all")] == [primary]
        assert [row["id"] for row in db.reading_list_items(conn)] == [primary]
        assert db.digest_rows(conn, "2026-11-17") == []
        assert db.latest_assessments(conn, "L2") == {}
        assert db.gained_item_ids(conn) == [primary]
        assert db.feedback_for(conn, primary)[0]["item_id"] == duplicate
        assert added([PDF_URL], pdf=pdf)["item_id"] == primary
        mutations = [lambda: db.set_reading_state(conn, duplicate, "read"),
                     lambda: db.add_to_reading_list(conn, duplicate),
                     lambda: db.add_feedback(conn, duplicate, "gained", None, NOW),
                     lambda: db.set_status(conn, [duplicate], "new"),
                     lambda: db.add_assessment(conn, duplicate, "L2", {}, NOW),
                     lambda: db.record_ingest(conn, duplicate, [], NOW),
                     lambda: db.persist_material(conn, material(PDF_URL, "New"), NOW,
                                                 item_id=duplicate)]
        for mutate in mutations:
            with pytest.raises(db.MaterialMergedError) as error:
                mutate()
            assert error.value.canonical_id == primary
        with pytest.raises(ops.OpsError, match=f"主材料 #{primary}"):
            ops.material_notes(duplicate, "Must not replace old notes")
        assert not (config.data_dir() / "material-merge-journal.json").exists()
    finally:
        conn.close()


def test_pdf_authors_cross_affiliation_rows_and_strip_footnotes(env):
    pdf = paper_pdf(author_rows=[
        (680, "Steven Osman1,*  Dinesh Subhraveti2*"),
        (657, "1Example University  2Research Institute"),
        (635, "Gong Su*   Jason Nieh2, Example Inc."),
    ])
    result = added([], pdf=pdf)
    assert result["title"] == TITLE
    assert result["bibliography"]["authors"] == AUTHORS

    from secondbrain.bibliography import split_authors

    assert split_authors("Deepak Narayanan‡★, Amar Phanishayee∗") == [
        "Deepak Narayanan", "Amar Phanishayee"
    ]


def test_explicit_conference_short_year_is_not_a_pdf_creation_date(env):
    metadata = {}
    extract_pdf_text(paper_pdf(
        publication="Proceedings of the 10th Symposium NSDI '13"
    ), 10, metadata=metadata)
    assert metadata["bibliography"]["year"] == 2013


def test_arxiv_api_failure_uses_official_citation_metadata_before_pdf(env, make_client):
    url = "https://arxiv.org/abs/2211.17192v2"
    pdf_url = "https://arxiv.org/pdf/2211.17192v2"
    html = ('<html><head><meta name="citation_title" content="' + TITLE + '">'
            '<meta name="citation_date" content="2022/11/30">'
            '<meta name="citation_arxiv_id" content="2211.17192">'
            + "".join(f'<meta name="citation_author" content="{name.split()[-1]}, '
                      f'{" ".join(name.split()[:-1])}">' for name in AUTHORS)
            + '</head><body>Official record</body></html>')
    client, router = make_client({
        url: httpx.Response(200, text=html),
        pdf_url: httpx.Response(200, content=paper_pdf(
            publication="Proceedings of the 40th Conference on Machine Learning, 2023"
        )),
    })
    result = added([pdf_url], client=client)
    assert result["title"] == TITLE
    assert result["bibliography"]["authors"] == AUTHORS
    assert "html:citation-meta" in result["bibliography"]["evidence"]
    assert result["acquisition"]["actual_url"] == pdf_url
    assert result["acquisition"]["version"] == "v2"
    assert router.urls().index(url) < router.urls().index(pdf_url)
    assert result["bibliography"]["year"] == 2023
    assert result["bibliography"]["preprint_year"] == 2022
    conn = db.connect()
    try:
        assert db.item_detail(conn, result["item_id"])["published_at"].startswith("2023-")
        facts = db.material_bibliography(conn, result["item_id"])
        assert facts["year"] == 2023 and facts["preprint_year"] == 2022
    finally:
        conn.close()
    compared = compare_bibliography({**BIB, "year": 2022}, {**BIB, "year": 2023})
    assert compared["conflicts"] == ["year"]


def test_index_failure_reports_committed_merge_and_allows_explicit_reindex(env, monkeypatch):
    from secondbrain.wiki.store import Wiki

    reindex = Wiki.reindex

    conn, primary, duplicate, _ = legacy_pair()
    messages = []
    try:
        preview = material_identity.preview_merge(primary, duplicate)

        def fail_index(self):
            raise OSError("Index is temporarily unwritable")

        monkeypatch.setattr(Wiki, "reindex", fail_index)
        result = material_identity.apply_merge(
            primary, duplicate, preview["token"], messages.append
        )
        assert not result["index_updated"]
        warning = result["index_warning"]
        assert "归并已提交" in warning and "sb wiki reindex" in warning
        assert result["index_warning"] in messages
        assert db.resolve_material_id(conn, duplicate) == primary
        assert conn.execute("SELECT count(*) FROM material_merges").fetchone()[0] == 1
        monkeypatch.setattr(Wiki, "reindex", reindex)
        wiki = open_wiki(config.wiki_dir(), config.raw_dir())
        wiki.reindex()
        assert wiki.index_path.exists()
    finally:
        conn.close()


@pytest.mark.parametrize("head,heading", [
    ("Untitled Document", "<h1>THE TASK OF THE REFEREE</h1>"),
    ("Archive - Original research essay", "<h2>An Evaluation of Research Submissions</h2>"),
])
def test_generic_html_titles_use_visible_heading_without_guessing_h4_authors(
    env, make_client, head, heading
):
    url = "https://example.org/research-essay"
    html = (f"<html><head><title>{head}</title></head><body><article>{heading}"
            "<h4>An Author-Like Heading</h4><p>"
            + "This essay discusses scholarly writing and careful peer review. " * 20
            + "</p></article></body></html>")
    client, _ = make_client({url: httpx.Response(200, text=html)})
    result = added([url], client=client)
    assert result["title"] not in ("Untitled Document", "Archive", url)
    assert not result["bibliography"].get("authors")


@pytest.mark.parametrize("row,authors", [
    ("Christoffer Dall                    Jason          Nieh", ["Christoffer Dall", "Jason Nieh"]),
    ("Norman P. Jouppi, Pierre-luc Cantin, John C.S. Lui",
     ["Norman P. Jouppi", "Pierre-luc Cantin", "John C.S. Lui"]),
])
def test_pdf_initials_hyphens_and_wide_intra_name_spaces(env, row, authors):
    metadata = {}
    extract_pdf_text(paper_pdf(author_rows=[(660, row)]), 10, metadata=metadata)
    assert metadata["bibliography"]["authors"] == authors
