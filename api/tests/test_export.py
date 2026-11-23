"""Exports are text transformations plus one PDF build; test both ends.

Why pypdf in the tests? "The response is a PDF" is a claim. Reading the bytes
back and finding the question, the audit line and the Sources header is
evidence - and `%PDF-` alone would pass for a blank page.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pypdf import PdfReader

from app.services.report_export import (
    ExportContext,
    build_context,
    export_filename,
    parse_blocks,
    render_markdown,
    render_pdf,
    slugify,
)

QUESTION = "How should a team choose between pgvector, Pinecone and FAISS?"
REPORT_ID = uuid.UUID("6f1c2d3e-4a5b-6c7d-8e9f-001122334455")

MARKDOWN = (
    "## Answer\n"
    "Pgvector is the default when Postgres is already there [1]. "
    "Pinecone removes the operations [2].\n\n"
    "## Comparison\n"
    "- **Cost** - pgvector is cheapest [1].\n"
    "- **Ops** - Pinecone is managed [2].\n\n"
    "| Option | Ops | Cost |\n"
    "|---|---|---|\n"
    "| pgvector | self | low [1] |\n"
    "| Pinecone | managed | higher [2] |\n"
)

CITATIONS = [
    {
        "index": 1,
        "note_id": "n1",
        "url": "https://alpha.example/pgvector",
        "title": "Pgvector benchmarks",
        "claim": "pgvector ran 220 QPS at 48 ms p95.",
        "quote": None,
        "confidence": "high",
        "sub_question": "Which is fastest?",
    },
    {
        "index": 2,
        "note_id": "n2",
        "url": "https://beta.example/pinecone",
        "title": None,
        "claim": "Pinecone is fully managed.",
        "quote": None,
        "confidence": "medium",
        "sub_question": "Which is managed?",
    },
]

VALIDATION = {
    "markers": 4,
    "valid_markers": 4,
    "unknown_markers": [],
    "precision": 1.0,
    "sentences": 5,
    "cited_sentences": 4,
    "coverage": 0.8,
    "unused_sources": 0,
    "words": 321,
    "repaired": False,
    "truncated": False,
    "uncited": False,
}


def context(**overrides) -> ExportContext:
    base = dict(
        report_id=REPORT_ID,
        question=QUESTION,
        markdown=MARKDOWN,
        citations=CITATIONS,
        validation=VALIDATION,
        model="support-chat-lite",
        created_at=datetime(2026, 11, 23, 8, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ExportContext(**base)


def test_parse_blocks_handles_headings_bullets_and_pipe_tables():
    blocks = parse_blocks(MARKDOWN)
    kinds = [block.kind for block in blocks]
    assert kinds == ["heading", "paragraph", "heading", "bullets", "table"]
    table = blocks[-1]
    assert len(table.rows) == 3                 # header + 2 rows, separator dropped
    assert table.rows[0] == ["Option", "Ops", "Cost"]
    assert table.rows[1][2] == "low [1]"


def test_markdown_export_carries_audit_sources_and_citations():
    text = render_markdown(context())
    assert text.startswith(f"# {QUESTION}")
    assert "321 words" in text and "precision 1.00" in text
    assert "coverage 0.80" in text
    assert "support-chat-lite" in text
    assert "## Sources" in text
    assert "1. **Pgvector benchmarks** — <https://alpha.example/pgvector>" in text
    assert "2. **beta.example** — <https://beta.example/pinecone>" in text
    assert "[1]" in text and "[2]" in text


def test_markdown_export_flags_a_truncated_report():
    text = render_markdown(context(validation={**VALIDATION, "truncated": True}))
    assert "truncated at the token cap" in text


def test_empty_citations_say_so_instead_of_an_empty_section():
    text = render_markdown(context(citations=[]))
    assert "No sources were recorded" in text


def test_filename_is_ascii_bounded_and_collision_resistant():
    ctx = context(question="Wie wählt man – pgvector vs. Pinecone? " + "x" * 100)
    name = export_filename(ctx, "pdf")
    assert name.startswith("deepresearch-wie-wahlt-man-pgvector-vs-pinecone")
    assert name.endswith(f"-{REPORT_ID.hex[:8]}.pdf")
    assert name.isascii()
    assert len(name) < 100


def test_slug_never_returns_an_empty_name():
    assert slugify("???") == "report"
    assert slugify("  --  ") == "report"


def test_pdf_is_a_real_document_with_the_prose_and_sources():
    data = render_pdf(context())
    assert data.startswith(b"%PDF-")
    assert len(data) > 5_000
    reader = PdfReader(__import__("io").BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    assert "pgvector" in text
    assert "Pgvector benchmarks" in text
    assert "Sources" in text
    assert "Page 1 of" in text


def test_pdf_survives_unicode_and_an_empty_report():
    data = render_pdf(
        context(
            question="Résumé – 1–5 × 3 — “quoted” ≥ 0.90",
            markdown="",
            citations=[],
            validation={},
        )
    )
    assert data.startswith(b"%PDF-")
    reader = PdfReader(__import__("io").BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    assert "quoted" in text
    assert "No sources were recorded" in text


def test_build_context_accepts_an_orm_shaped_object_or_a_fixture_dict():
    class Row:
        id = REPORT_ID
        markdown = MARKDOWN
        citations = CITATIONS
        validation = VALIDATION
        model = "support-chat"
        created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    from_row = build_context(Row(), QUESTION)
    from_dict = build_context(
        {
            "report_id": str(REPORT_ID),
            "markdown": MARKDOWN,
            "citations": CITATIONS,
            "validation": VALIDATION,
            "model": "support-chat",
            "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        },
        QUESTION,
    )
    assert from_row == from_dict
