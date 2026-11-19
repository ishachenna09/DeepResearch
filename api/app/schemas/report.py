"""Report payloads: the cited answer, its sources, and the citation audit.

Why `validation` is part of the payload and not a log line?
"The report has citations" is a claim, not evidence. The UI renders precision,
coverage and the exact unknown markers, so a reader can see *which* sentence
cites nothing - and Phase 7's eval gate asserts on the same numbers.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ReportStatus = Literal["generating", "ready", "failed"]


class CitationOut(BaseModel):
    """One numbered source the writer was allowed to cite."""

    index: int                  # the [n] marker in the markdown
    note_id: uuid.UUID
    url: str
    title: str | None = None
    claim: str                  # the note text (what is being cited)
    quote: str | None = None    # verbatim support on the page
    confidence: str
    sub_question: str


class ReportValidationOut(BaseModel):
    markers: int                # citation markers found in the prose
    valid_markers: int          # ... that resolve to a real source
    unknown_markers: list[int]  # indices the model invented
    precision: float            # valid_markers / markers (0.0 when there are none)
    sentences: int              # claim-sized prose sentences (headings excluded)
    cited_sentences: int
    coverage: float             # cited_sentences / sentences
    unused_sources: int         # registry entries the model never cited
    words: int
    # True when a draft was replaced by the one second attempt (see
    # report_runner). The UI shows it as a badge: "repaired" is an honest
    # signal that the first generation drifted off the marker format.
    repaired: bool = False
    # True when the provider stopped at the token cap, i.e. the text ends
    # mid-sentence. The second attempt usually clears it; if it survives, the
    # reader is told rather than shown half a brief as if it were finished.
    truncated: bool = False
    # True when no marker in the shipped text resolves to a source. The prose is
    # still useful, but it is not traceable evidence, so the UI says so instead
    # of rendering it exactly like a cited brief.
    uncited: bool = False


class ReportOut(BaseModel):
    report_id: uuid.UUID
    job_id: uuid.UUID
    status: ReportStatus
    markdown: str
    citations: list[CitationOut]
    validation: ReportValidationOut | None = None
    tokens_used: int = 0
    model: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
