"""Turn a stored report into a shareable Markdown or PDF file.

Why generate exports on demand instead of keeping a copy in S3/R2?
An export is a pure function of data already in Postgres: the markdown, the
citations, the validation audit and the question. A bucket would add
credentials, lifecycle rules, signed URLs and a second copy that can drift from
the row it came from - and the row is the source of truth. Object storage earns
its place when an artifact must be immutable (an emailed attachment, a legal
archive); a download link does not need that. `docs/TECH_DECISIONS.md` records
the R2 design we would use then (content-addressed keys, one row column).

Why ReportLab instead of WeasyPrint or a headless browser?
WeasyPrint needs Pango/GTK system libraries, which do not exist in this
Windows dev setup and are another thing to install in the container. A headless
Chrome is a ~300 MB process per export. ReportLab is pure Python, deterministic
and fast, and it ships Bitstream Vera so a report containing an en dash, a
multiplication sign or curly quotes is not rendered as boxes (the built-in
Helvetica is Latin-1 only).

Why a hand-written markdown subset instead of markdown-it-py?
`markdown-it` returns HTML, and ReportLab does not consume block HTML. Walking
its token stream into flowables is more code than parsing the small subset the
writer emits (headings, paragraphs, bullets, pipe tables, bold/italic/code).
The subset is pinned by the fixtures in `evals/fixtures/`, which include the
8-row comparison table the interview report produces.
"""
from __future__ import annotations

import html
import io
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# --------------------------------------------------------------------------- #
# Model -> text
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportContext:
    """The minimum a renderer needs; no ORM object leaks into the renderers."""

    report_id: uuid.UUID
    question: str
    markdown: str
    citations: list[dict]
    validation: dict
    model: str | None
    created_at: datetime


def build_context(report, question: str) -> ExportContext:
    """Adapt an ORM `Report` (or a dict-shaped fixture) to the renderers."""

    def field(name, default=None):
        if isinstance(report, dict):
            return report.get(name, default)
        return getattr(report, name, default)

    report_id = field("id") or field("report_id")
    return ExportContext(
        report_id=report_id if isinstance(report_id, uuid.UUID) else uuid.UUID(str(report_id)),
        question=question,
        markdown=field("markdown") or "",
        citations=list(field("citations") or []),
        validation=dict(field("validation") or {}),
        model=field("model"),
        created_at=field("created_at") or datetime.now(timezone.utc),
    )


def _when(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _audit_line(ctx: ExportContext) -> str:
    v = ctx.validation
    if not v:
        return "No citation audit recorded."
    line = (
        f"{v.get('words', 0)} words · {v.get('markers', 0)} citation markers · "
        f"precision {float(v.get('precision', 0)):.2f} · "
        f"coverage {float(v.get('coverage', 0)):.2f}"
    )
    flags: list[str] = []
    if v.get("truncated"):
        flags.append("truncated at the token cap")
    if v.get("uncited"):
        flags.append("no marker resolves to a source")
    if v.get("repaired"):
        flags.append("second attempt used")
    if flags:
        line += f" ({'; '.join(flags)})"
    return line


def slugify(question: str, limit: int = 60) -> str:
    """ASCII, filesystem-safe, bounded. Unicode is transliterated, not dropped."""
    ascii_text = (
        unicodedata.normalize("NFKD", question).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    slug = slug[:limit].rstrip("-")
    return slug or "report"


def export_filename(ctx: ExportContext, extension: Literal["md", "pdf"]) -> str:
    # The id suffix keeps two different answers to the same question apart, and
    # ASCII keeps the header safe (Starlette headers are latin-1).
    return f"deepresearch-{slugify(ctx.question)}-{ctx.report_id.hex[:8]}.{extension}"


def render_markdown(ctx: ExportContext) -> str:
    """A self-contained Markdown file: metadata, prose, then numbered sources."""
    sources: list[str] = []
    for citation in sorted(ctx.citations, key=lambda c: int(c.get("index", 0))):
        index = citation.get("index", "?")
        title = citation.get("title") or _host(citation.get("url", ""))
        url = citation.get("url", "")
        claim = (citation.get("claim") or "").strip()
        sources.append(f"{index}. **{title}** — <{url}>" + (f"\n   {claim}" if claim else ""))
    if not sources:
        sources.append("_No sources were recorded for this report._")

    body = ctx.markdown.strip()
    return "\n".join(
        [
            f"# {ctx.question.strip()}",
            "",
            f"> Generated {_when(ctx.created_at)} · model `{ctx.model or 'unknown'}` · "
            f"report `{ctx.report_id}`",
            f">",
            f"> {_audit_line(ctx)}",
            "",
            "---",
            "",
            body,
            "",
            "---",
            "",
            "## Sources",
            "",
            *sources,
            "",
            f"_Exported by DeepResearch. Every `[n]` above resolves to source n in this list._",
            "",
        ]
    )


def _host(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or url).lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "source"


# --------------------------------------------------------------------------- #
# Markdown subset -> blocks
# --------------------------------------------------------------------------- #

BlockKind = Literal["heading", "paragraph", "bullets", "numbers", "quote", "code", "rule", "table"]

_HEADING = re.compile(r"^(#{1,4})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^:?-{2,}:?$")
_FENCE = re.compile(r"^\s*```")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass
class Block:
    kind: BlockKind
    level: int = 0
    text: str = ""
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


def _flush(blocks: list[Block], current: Block | None) -> None:
    if current is not None:
        if current.kind in ("paragraph", "quote"):
            current.text = " ".join(current.text.split())
        blocks.append(current)


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_blocks(markdown: str) -> list[Block]:
    """Parse the subset the writer emits. Unknown syntax becomes a paragraph."""
    blocks: list[Block] = []
    current: Block | None = None
    in_code = False
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for raw in lines:
        line = _CONTROL.sub("", raw)

        if _FENCE.match(line):
            if in_code:
                _flush(blocks, current)
                current = None
                in_code = False
            else:
                _flush(blocks, current)
                current = Block("code")
                in_code = True
            continue
        if in_code:
            current.text += line + "\n"  # type: ignore[union-attr]
            continue

        if not line.strip():
            _flush(blocks, current)
            current = None
            continue

        heading = _HEADING.match(line)
        if heading:
            _flush(blocks, current)
            blocks.append(Block("heading", level=len(heading.group(1)), text=heading.group(2)))
            current = None
            continue

        if _RULE.match(line):
            _flush(blocks, current)
            blocks.append(Block("rule"))
            current = None
            continue

        if _TABLE_LINE.match(line):
            if current is None or current.kind != "table":
                _flush(blocks, current)
                current = Block("table")
            row = _split_row(line)
            if not (row and all(_TABLE_RULE.match(cell) for cell in row)):
                current.rows.append(row)
            continue

        bullet = _BULLET.match(line)
        if bullet:
            if current is None or current.kind != "bullets":
                _flush(blocks, current)
                current = Block("bullets")
            current.items.append(bullet.group(1).strip())
            continue

        number = _NUMBER.match(line)
        if number:
            if current is None or current.kind != "numbers":
                _flush(blocks, current)
                current = Block("numbers")
            current.items.append(number.group(1).strip())
            continue

        if line.lstrip().startswith(">"):
            if current is None or current.kind != "quote":
                _flush(blocks, current)
                current = Block("quote")
            current.text += " " + line.lstrip()[1:].strip()
            continue

        # An indented line under a list item is that item's continuation.
        if current is not None and current.kind in ("bullets", "numbers") and raw[:1].isspace():
            current.items[-1] += " " + line.strip()
            continue

        if current is None:
            current = Block("paragraph", text=line.strip())
        elif current.kind == "paragraph":
            current.text += " " + line.strip()
        else:
            _flush(blocks, current)
            current = Block("paragraph", text=line.strip())

    if in_code and current is not None:
        _flush(blocks, current)
    else:
        _flush(blocks, current)
    return blocks


# --------------------------------------------------------------------------- #
# Inline markdown -> ReportLab's mini-HTML
# --------------------------------------------------------------------------- #

_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _inline(text: str) -> str:
    """Escape first, then add the only tags ReportLab may see.

    Escaping first is what keeps a stray `<` in a scraped sentence from becoming
    an XML tag (and a 500). Code spans and links are removed to placeholders so
    bold/italic rules never edit a URL or an identifier.
    """
    text = _CONTROL.sub("", text)
    text = html.escape(text, quote=False)
    stash: list[str] = []

    def keep(value: str) -> str:
        stash.append(value)
        return f"\x01{len(stash) - 1}\x01"

    text = _CODE.sub(lambda m: keep(f'<font face="Courier">{m.group(1)}</font>'), text)
    text = _LINK.sub(
        lambda m: keep(f'<link href="{m.group(2)}">{m.group(1)}</link>'), text
    )
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    for index, value in enumerate(stash):
        text = text.replace(f"\x01{index}\x01", value)
    return text


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _fonts_and_styles():
    """Lazy: importing ReportLab costs more than every other export step."""
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_dir = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    registered = pdfmetrics.getRegisteredFontNames()
    if "Vera" not in registered:
        pdfmetrics.registerFont(TTFont("Vera", os.path.join(font_dir, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont("Vera-Bold", os.path.join(font_dir, "VeraBd.ttf")))
        pdfmetrics.registerFont(TTFont("Vera-Italic", os.path.join(font_dir, "VeraIt.ttf")))
        pdfmetrics.registerFont(
            TTFont("Vera-BoldItalic", os.path.join(font_dir, "VeraBI.ttf"))
        )
    grey = colors.HexColor("#555555")
    ink = colors.HexColor("#161616")
    rule = colors.HexColor("#dddddd")

    styles = {
        "title": ParagraphStyle(
            "Title", fontName="Vera-Bold", fontSize=19, leading=24, textColor=ink,
            spaceAfter=6,
        ),
        "meta": ParagraphStyle(
            "Meta", fontName="Vera", fontSize=8.3, leading=11.5, textColor=grey,
            spaceAfter=2,
        ),
        "h2": ParagraphStyle(
            "H2", fontName="Vera-Bold", fontSize=13, leading=17, textColor=ink,
            spaceBefore=14, spaceAfter=4, keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "H3", fontName="Vera-Bold", fontSize=11, leading=14.5, textColor=ink,
            spaceBefore=10, spaceAfter=3, keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "Body", fontName="Vera", fontSize=9.5, leading=14, textColor=ink,
            spaceAfter=7, alignment=TA_LEFT,
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName="Vera", fontSize=9.5, leading=13.5, textColor=ink,
            leftIndent=16, bulletIndent=4, spaceAfter=3,
        ),
        "quote": ParagraphStyle(
            "Quote", fontName="Vera-Italic", fontSize=9, leading=13, textColor=grey,
            leftIndent=12, spaceBefore=2, spaceAfter=7,
        ),
        "code": ParagraphStyle(
            "Code", fontName="Courier", fontSize=8, leading=10.5, textColor=ink,
            backColor=colors.HexColor("#f4f4f4"), borderPadding=5,
            leftIndent=4, spaceBefore=2, spaceAfter=8,
        ),
        "cell": ParagraphStyle(
            "Cell", fontName="Vera", fontSize=7.8, leading=10.2, textColor=ink,
        ),
        "cell_head": ParagraphStyle(
            "CellHead", fontName="Vera-Bold", fontSize=7.8, leading=10.2, textColor=ink,
        ),
        "source": ParagraphStyle(
            "Source", fontName="Vera", fontSize=8.4, leading=11.4, textColor=ink,
            spaceBefore=4,
        ),
        "claim": ParagraphStyle(
            "Claim", fontName="Vera", fontSize=8, leading=10.8, textColor=grey,
            leftIndent=10, spaceAfter=3,
        ),
        "footnote": ParagraphStyle(
            "Footnote", fontName="Vera-Italic", fontSize=7.8, leading=10.5, textColor=grey,
        ),
        "colors": {"grey": grey, "rule": rule, "ink": ink},
    }
    return styles


def _table(rows: list[list[str]], styles: dict, available: float):
    """A pipe table -> a real table (the writer emits an 8-row comparison)."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    weights = [
        max(1, max(len(row[column]) for row in rows)) for column in range(width)
    ]
    total = sum(weights)
    widths = [available * weight / total for weight in weights]
    # A 1-character header ("#") would otherwise get ~2 pt and ReportLab raises
    # on a negative cell width once padding is subtracted. Clamp, then rescale
    # to the exact frame width so wide tables do not overflow the margin.
    widths = [max(36.0, width) for width in widths]
    scale = available / sum(widths)
    widths = [width * scale for width in widths]
    data = [
        [
            Paragraph(_inline(cell), styles["cell_head"] if index == 0 else styles["cell"])
            for cell in row
        ]
        for index, row in enumerate(rows)
    ]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5d5d5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _flowables(markdown: str, styles: dict, available: float) -> list:
    from reportlab.platypus import HRFlowable, Paragraph, Preformatted, Spacer

    story: list = []
    for block in parse_blocks(markdown):
        if block.kind == "heading":
            style = styles["h2"] if block.level <= 2 else styles["h3"]
            story.append(Paragraph(_inline(block.text), style))
        elif block.kind == "paragraph":
            story.append(Paragraph(_inline(block.text), styles["body"]))
        elif block.kind in ("bullets", "numbers"):
            for offset, item in enumerate(block.items, 1):
                marker = "•" if block.kind == "bullets" else f"{offset}."
                story.append(
                    Paragraph(_inline(item), styles["bullet"], bulletText=marker)
                )
        elif block.kind == "quote":
            story.append(Paragraph(_inline(block.text.strip()), styles["quote"]))
        elif block.kind == "code":
            story.append(Preformatted(block.text.rstrip("\n"), styles["code"]))
        elif block.kind == "table" and block.rows:
            story.append(_table(block.rows, styles, available))
            story.append(Spacer(1, 6))
        elif block.kind == "rule":
            story.append(
                HRFlowable(
                    width="100%", thickness=0.6, color=styles["colors"]["rule"],
                    spaceBefore=4, spaceAfter=8,
                )
            )
    return story


class _NumberedCanvas:
    """A canvas factory that can print `Page x of y` (needs the page count)."""

    @staticmethod
    def make(page_footer: str):
        from reportlab.pdfgen import canvas as pdfcanvas

        class NumberedCanvas(pdfcanvas.Canvas):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._saved_page_states = []

            def showPage(self):
                self._saved_page_states.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total = len(self._saved_page_states)
                for state in self._saved_page_states:
                    self.__dict__.update(state)
                    self._draw_footer(total, page_footer)
                    super().showPage()
                super().save()

            def _draw_footer(self, total: int, footer: str):
                width, _ = self._pagesize
                self.setFont("Vera", 7.3)
                self.setFillGray(0.45)
                self.drawString(18 * 2.83465, 12 * 2.83465, footer)
                self.drawRightString(
                    width - 18 * 2.83465,
                    12 * 2.83465,
                    f"Page {self._pageNumber} of {total}",
                )

        return NumberedCanvas


def render_pdf(ctx: ExportContext) -> bytes:
    """A print-ready A4 report: metadata, prose/tables, then numbered sources."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    styles = _fonts_and_styles()
    buffer = io.BytesIO()
    margin = 18 * mm
    available = A4[0] - 2 * margin
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=20 * mm,
        title=ctx.question.strip()[:200],
        author="DeepResearch",
        subject="Cited research report",
        creator="DeepResearch export",
    )

    story: list = [
        Paragraph(_inline(ctx.question.strip()), styles["title"]),
        Paragraph(
            f"Generated {_when(ctx.created_at)} · model "
            f"{html.escape(ctx.model or 'unknown')} · report {ctx.report_id}",
            styles["meta"],
        ),
        Paragraph(html.escape(_audit_line(ctx)), styles["meta"]),
        Spacer(1, 8),
        HRFlowable(
            width="100%", thickness=0.6, color=styles["colors"]["rule"], spaceAfter=8
        ),
    ]
    story += _flowables(ctx.markdown, styles, available)

    if ctx.citations:
        story += [
            Spacer(1, 6),
            HRFlowable(
                width="100%", thickness=0.6, color=styles["colors"]["rule"],
                spaceBefore=4, spaceAfter=8,
            ),
            Paragraph("Sources", styles["h2"]),
        ]
        for citation in sorted(ctx.citations, key=lambda c: int(c.get("index", 0))):
            index = citation.get("index", "?")
            url = citation.get("url", "")
            title = citation.get("title") or _host(url)
            line = f'<b>[{index}]</b> {_inline(title)}'
            if url:
                line += f' — <link href="{html.escape(url, quote=True)}">{_inline(url)}</link>'
            story.append(Paragraph(line, styles["source"]))
            claim = (citation.get("claim") or "").strip()
            if claim:
                story.append(Paragraph(_inline(claim), styles["claim"]))
    else:
        story.append(Paragraph("No sources were recorded for this report.", styles["body"]))

    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "Every [n] in the prose resolves to source n in the list above. "
            "Generated by DeepResearch; the stored audit travels with the report.",
            styles["footnote"],
        )
    )

    doc.build(
        story,
        canvasmaker=_NumberedCanvas.make("DeepResearch · cited research report"),
    )
    return buffer.getvalue()
