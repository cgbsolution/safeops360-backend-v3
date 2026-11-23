"""Knowledge bank: offline unit tests (no DB, no storage, no AI).

Covers text extraction from small generated PDF / DOCX / PPTX / XLSX / TXT
fixtures, chunking and ranking, lifecycle rules, the search SQL (compiled with
the postgresql dialect), and citation formatting against a mocked AI response.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.knowledge_bank import KbDocument
from app.services import knowledge_bank as svc

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)

HOT_WORK = (
    "Hot work permit is mandatory for welding, gas cutting and grinding within 11 m of combustible material. "
    "A fire watcher with a charged extinguisher shall remain at the location for 30 minutes after the job ends."
)
LADLE = "Ladle lining shall be inspected after every heat. A ladle with refractory wear beyond 50 mm is withdrawn."


# ── fixtures ─────────────────────────────────────────────────────────────────
def _pdf(pages: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for text in pages:
        pdf.add_page()
        pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())


def _docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, val in enumerate(row):
                t.cell(r, c).text = val
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _raw_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _pptx(slides: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, lines in enumerate(slides, start=1):
            paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
            z.writestr(f"ppt/slides/slide{i}.xml", f'<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="{_A}"><a:txBody>{paras}</a:txBody></p:sld>')
    return buf.getvalue()


def _xlsx(rows: list[list[str]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


PDF_MIME = "application/pdf"
DOCX_MIME = svc.EXT_MIME["docx"]


# ── extraction ───────────────────────────────────────────────────────────────
def test_pdf_extraction_keeps_pages_apart():
    text, status = svc.extract_text(_pdf(["Page one: " + HOT_WORK, "Page two: " + LADLE]), PDF_MIME)
    assert status == "OK"
    pages = text.split(svc.PAGE_BREAK)
    assert len(pages) == 2
    assert "fire watcher" in pages[0] and "refractory" in pages[1]


def test_docx_extraction_includes_tables():
    data = _docx(["Work at height SOP", "Full body harness above 1.8 m."], [["Item", "Check"], ["Lanyard", "No cuts"]])
    text, status = svc.extract_text(data, DOCX_MIME)
    assert status == "OK"
    assert "Full body harness above 1.8 m." in text
    assert "Lanyard | No cuts" in text


def test_docx_fallback_without_python_docx(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "docx":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    text, status = svc.extract_text(_raw_docx(["Confined space entry", "Gas test before entry"]), DOCX_MIME)
    assert status == "OK" and text == "Confined space entry\nGas test before entry"


def test_pptx_and_xlsx_and_text():
    text, status = svc.extract_text(_pptx([["Slide A", "Isolate energy"], ["Slide B", "Try-out"]]), svc.EXT_MIME["pptx"])
    assert status == "OK" and text.split(svc.PAGE_BREAK)[1] == "Slide B\nTry-out"
    text, status = svc.extract_text(_xlsx([["Check", "OK?"], ["Guard rails", "Yes"]]), svc.EXT_MIME["xlsx"])
    assert status == "OK" and "Guard rails | Yes" in text and text.startswith("Sheet: Checklist")
    text, status = svc.extract_text(b"Line one\x00\n\n\n\nLine  two", "text/plain")
    assert (text, status) == ("Line one\n\nLine two", "OK")


def test_images_skipped_and_garbage_fails_softly():
    assert svc.extract_text(b"\x89PNG\r\n\x1a\nxxxx", "image/png") == ("", "SKIPPED")
    assert svc.extract_text(b"%PDF-1.4 not really a pdf", PDF_MIME)[1] in ("FAILED", "EMPTY")
    assert svc.extract_text(b"PK\x03\x04broken", DOCX_MIME) == ("", "FAILED")


def test_xml_with_dtd_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(ValueError):
        svc._xml_text(bomb, "t", "p")


def test_mime_resolution_and_sniffing():
    assert svc.resolve_mime("policy.pdf", "") == PDF_MIME
    assert svc.resolve_mime("SOP.DOCX", "application/octet-stream") == DOCX_MIME
    assert svc.resolve_mime("tool.exe", "application/octet-stream") is None
    assert svc.resolve_mime("policy.pdf", "image/png") is None  # declared type contradicts the extension
    assert svc.resolve_mime("notes.txt", "text/plain; charset=utf-8") == "text/plain"
    assert svc.sniff_matches(_pdf(["x"]), PDF_MIME)
    assert not svc.sniff_matches(b"MZ\x90\x00", PDF_MIME)
    assert svc.sniff_matches(_docx(["x"]), DOCX_MIME)
    assert not svc.sniff_matches(b"a\x00b", "text/plain")


def test_doc_numbers_and_storage_path():
    assert svc.doc_no_stem("SOP") == "KB-SOP-"
    assert svc.doc_no_stem("STANDARD") == "KB-STD-"
    assert svc.doc_no_stem("NOPE") == "KB-DOC-"
    path = svc.build_kb_storage_path("doc1", "../../etc/passwd Hot Work.pdf")
    assert path.startswith("kb/doc1/") and "/.." not in path and " " not in path


def test_search_text_is_capped_and_nul_free():
    body = "x" * (svc.MAX_SEARCH_TEXT_CHARS + 50) + "\x00"
    s = svc.build_search_text(doc_no="KB-SOP-0001", title="Hot work", summary=None, tags=["welding"],
                              standard_ref="IS 3016", department=None, body=body)
    assert s.startswith("KB-SOP-0001 Hot work welding IS 3016")
    assert len(s) <= svc.MAX_SEARCH_TEXT_CHARS and "\x00" not in s


def test_tags_normalised():
    assert svc.normalize_tags([" Hot  work ", "hot work", "", "LOTO"]) == ["Hot work", "LOTO"]


# ── chunking + ranking ───────────────────────────────────────────────────────
def test_chunks_respect_pages_and_size():
    long_para = " ".join(f"Sentence {i} about isolation and lockout." for i in range(80))
    text = f"Intro line\n\n{long_para}{svc.PAGE_BREAK}Second page text about gas testing."
    chunks = svc.chunk_text(text, size=500, overlap=80)
    assert all(len(c.text) <= 500 + 80 + 50 for c in chunks)
    assert chunks[-1].page == 2 and "gas testing" in chunks[-1].text
    assert {c.page for c in chunks[:-1]} == {1}
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # overlap: the start of chunk 2 repeats the end of chunk 1
    assert chunks[1].text.split("\n")[0][-20:] in chunks[0].text


def test_unpaged_text_has_no_page_numbers():
    assert svc.chunk_text("just one block")[0].page is None


def _docs():
    return [
        {"documentId": "d1", "docNo": "KB-SOP-0001", "title": "Hot work permit", "versionId": "v1", "versionNo": 2,
         "text": f"Scope{svc.PAGE_BREAK}{HOT_WORK}"},
        {"documentId": "d2", "docNo": "KB-STD-0001", "title": "Ladle handling standard", "versionId": "v2", "versionNo": 1,
         "text": LADLE},
    ]


def test_ranking_picks_the_relevant_passage_with_page():
    ps = svc.rank_passages("How long must the fire watcher stay after hot work?", _docs())
    assert ps and ps[0].doc_no == "KB-SOP-0001" and ps[0].page == 2 and ps[0].ref == "P1"
    assert all(p.score > 0 for p in ps)
    assert "KB-STD-0001" not in {p.doc_no for p in ps}  # no shared term, no passage


def test_ranking_no_terms_no_passages():
    assert svc.rank_passages("what is the", _docs()) == []
    assert svc.rank_passages("quantum chromodynamics", _docs()) == []


def test_tokenizer_folds_plurals_and_drops_stopwords():
    assert svc.tokenize("The ladles and permits") == ["ladle", "permit"]
    assert svc.tokenize("Checklists for policies") == ["checklist", "policy"]


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_lifecycle_happy_path():
    assert svc.upload_blockers(doc_status="DRAFT", pending_status=None) == []
    assert svc.submit_blockers(doc_status="DRAFT", pending_status=None)  # nothing uploaded
    assert svc.submit_blockers(doc_status="DRAFT", pending_status="DRAFT") == []
    assert svc.doc_status_after_submit("DRAFT") == "UNDER_REVIEW"
    assert svc.publish_blockers(doc_status="UNDER_REVIEW", pending_status="DRAFT")  # must be submitted first
    assert svc.publish_blockers(doc_status="UNDER_REVIEW", pending_status="UNDER_REVIEW") == []
    assert svc.obsolete_blockers(doc_status="PUBLISHED") == []


def test_revision_of_published_doc_keeps_doc_published():
    assert svc.upload_blockers(doc_status="PUBLISHED", pending_status=None) == []
    assert svc.doc_status_after_submit("PUBLISHED") == "PUBLISHED"
    assert svc.doc_status_after_reject("PUBLISHED") == "PUBLISHED"
    assert svc.doc_status_after_reject("UNDER_REVIEW") == "DRAFT"


def test_lifecycle_refusals():
    assert svc.upload_blockers(doc_status="OBSOLETE", pending_status=None)
    assert svc.upload_blockers(doc_status="PUBLISHED", pending_status="UNDER_REVIEW")
    assert svc.submit_blockers(doc_status="DRAFT", pending_status="UNDER_REVIEW")
    assert svc.publish_blockers(doc_status="OBSOLETE", pending_status="UNDER_REVIEW")
    assert svc.reject_blockers(pending_status="DRAFT")
    assert svc.obsolete_blockers(doc_status="DRAFT")
    assert svc.delete_blockers(doc_status="DRAFT", current_version_id=None) == []
    assert svc.delete_blockers(doc_status="OBSOLETE", current_version_id="v1")


def test_ack_rules():
    ok = dict(doc_status="PUBLISHED", ack_required=True, current_version_id="v1", has_viewed=True)
    assert svc.ack_blockers(**ok) == []
    assert svc.ack_blockers(**{**ok, "has_viewed": False})
    assert svc.ack_blockers(**{**ok, "ack_required": False})
    assert svc.ack_blockers(**{**ok, "doc_status": "OBSOLETE"})


def test_audience():
    assert svc.applies_to_user(doc_plant_id=None, audience_roles=[], user_plant_ids={"p1"}, user_role_codes={"WORKER"})
    assert not svc.applies_to_user(doc_plant_id="p2", audience_roles=[], user_plant_ids={"p1"}, user_role_codes=set())
    assert svc.applies_to_user(doc_plant_id="p1", audience_roles=["SUPERVISOR"], user_plant_ids={"p1"}, user_role_codes={"SUPERVISOR"})
    assert not svc.applies_to_user(doc_plant_id=None, audience_roles=["SUPERVISOR"], user_plant_ids={"p1"}, user_role_codes={"WORKER"})


def test_review_dates():
    assert svc.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).day == 28
    assert svc.default_review_due(datetime(2026, 11, 23, tzinfo=timezone.utc), 24).year == 2028
    assert svc.review_state("PUBLISHED", NOW - timedelta(days=1), NOW) == "OVERDUE"
    assert svc.review_state("PUBLISHED", NOW + timedelta(days=10), NOW) == "DUE_SOON"
    assert svc.review_state("PUBLISHED", NOW + timedelta(days=90), NOW) is None
    assert svc.review_state("OBSOLETE", NOW - timedelta(days=1), NOW) is None
    assert svc.review_state("PUBLISHED", (NOW - timedelta(days=1)).replace(tzinfo=None), NOW) == "OVERDUE"


# ── search SQL (postgresql dialect) ──────────────────────────────────────────
def _pg(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_fulltext_sql_matches_the_gin_index_expression():
    tsq = svc.fts_plain_query("hot work")
    sql = _pg(select(KbDocument.id).where(svc.fts_match(tsq)).order_by(svc.fts_rank(tsq).desc()))
    assert "to_tsvector('simple'::regconfig, coalesce(\"KbDocument\".\"searchText\", ''))" in sql
    assert "@@ plainto_tsquery('simple'::regconfig, 'hot work')" in sql
    assert "ts_rank(" in sql


def test_or_tsquery_is_injection_safe():
    # operators, quotes and one-letter fragments never reach to_tsquery
    assert svc.or_tsquery_text("PPE for hot-work? ') | !xx & yy:*") == "ppe | hot | work | xx | yy"
    assert svc.or_tsquery_text("the of") is None
    sql = _pg(select(KbDocument.id).where(svc.fts_match(svc.fts_or_query("fire watcher"))))
    assert "to_tsquery('simple'::regconfig, 'fire | watcher')" in sql


def test_ilike_fallback_escapes_wildcards():
    sql = _pg(select(KbDocument.id).where(svc.ilike_terms("100% IS_3786")))
    # "_" is a LIKE wildcard: escaped, so IS_3786 does not also match "IS-3786"
    assert "\\_3786" in sql and "ESCAPE" in sql
    assert "'%%100%%'" in sql  # the user's "%" never becomes a wildcard
    assert svc.ilike_terms("a") is None


def test_snippet_centres_on_match():
    text = "A" * 500 + " fire watcher remains for 30 minutes " + "B" * 500
    snip = svc.make_snippet(text, "fire watcher", width=120)
    assert "fire watcher" in snip and snip.startswith("…") and snip.endswith("…")


# ── citations (mocked AI) ────────────────────────────────────────────────────
def _passages():
    return svc.rank_passages("fire watcher after hot work", _docs())


def test_prompt_labels_passages_with_doc_version_page():
    p = svc.build_ask_prompt("fire watcher?", _passages())
    assert '[P1] KB-SOP-0001 v2 "Hot work permit", page 2' in p
    assert "ONLY the passages" in svc.ASK_SYSTEM


def test_format_answer_verified_citation():
    ai = {"covered": True, "answer": "A fire watcher stays for 30 minutes after the job [P1].",
          "citations": [{"ref": "P1", "quote": "shall remain at the location for 30 minutes after the job ends"}]}
    out = svc.format_answer(ai, _passages())
    assert out["covered"] is True
    c = out["citations"][0]
    assert c["docNo"] == "KB-SOP-0001" and c["versionNo"] == 2 and c["page"] == 2 and c["quoteVerified"]
    assert c["label"] == "KB-SOP-0001 v2, p. 2" and c["href"] == "/knowledge-bank/d1"
    assert out["sources"] == [{"documentId": "d1", "docNo": "KB-SOP-0001", "title": "Hot work permit", "versionNo": 2,
                               "href": "/knowledge-bank/d1"}]


def test_format_answer_drops_invented_refs_and_flags_invented_quotes():
    ai = {"covered": True, "answer": "Stay 30 minutes [P1]. Also wear a respirator [P9].",
          "citations": [{"ref": "P1", "quote": "the watcher must stay for one hour"}, {"ref": "P9", "quote": "respirator"}]}
    out = svc.format_answer(ai, _passages())
    assert [c["ref"] for c in out["citations"]] == ["P1"]
    assert out["citations"][0]["quoteVerified"] is False
    assert "one hour" not in out["citations"][0]["quote"]
    assert "[P9]" not in out["answer"] and "[P1]" in out["answer"]


def test_uncited_coverage_is_downgraded():
    out = svc.format_answer({"covered": True, "answer": "Wear a respirator.", "citations": []}, _passages())
    assert out["covered"] is False and out["answer"] == svc.NOT_COVERED and out["citations"] == []


def test_marker_without_citation_gets_one():
    out = svc.format_answer({"covered": True, "answer": "Watcher stays 30 minutes [P1].", "citations": []}, _passages())
    assert out["covered"] is True and out["citations"][0]["ref"] == "P1" and out["citations"][0]["quoteVerified"] is False


def test_not_covered_and_garbage():
    out = svc.format_answer({"covered": False, "answer": "", "citations": []}, _passages())
    assert out == {"covered": False, "answer": svc.NOT_COVERED, "citations": [], "sources": []}
    assert svc.format_answer(None, _passages())["covered"] is False
    assert svc.format_answer({"covered": True, "answer": "x", "citations": ["junk", {"ref": None}]}, [])["covered"] is False


def test_quote_matching_tolerates_typography_only():
    assert svc.quote_in_passage("fire watcher with a charged extinguisher", HOT_WORK)
    assert svc.quote_in_passage("Fire  watcher, with a CHARGED extinguisher", HOT_WORK)
    assert not svc.quote_in_passage("fire watcher with two extinguishers", HOT_WORK)
    assert not svc.quote_in_passage("", HOT_WORK)


def test_vocabularies_have_no_duplicates():
    assert len(set(svc.DOC_TYPES.values())) == len(svc.DOC_TYPES)
    assert len({c[0] for c in svc.DEFAULT_CATEGORIES}) == len({c[1] for c in svc.DEFAULT_CATEGORIES}) == 10
