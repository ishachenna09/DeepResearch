import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SourceNoteOut(BaseModel):
    id: uuid.UUID
    sub_question: str
    url: str
    title: str | None = None
    note: str
    quote: str | None = None
    confidence: str
    signals: list | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotesOut(BaseModel):
    job_id: uuid.UUID
    status: str
    count: int            # total for this job
    notes: list[SourceNoteOut]


class NoteSearchIn(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=8, ge=1, le=20)


class NoteHit(SourceNoteOut):
    score: float          # 1 - cosine distance, so 1.0 = identical direction


class NoteSearchOut(BaseModel):
    job_id: uuid.UUID
    query: str
    count: int
    results: list[NoteHit]
