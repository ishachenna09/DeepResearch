"""The wire contract between the API and its agents.

Why a `data` part instead of stuffing JSON into a text part?
A2A has a first-class `DataPart` (`{"data": {...}}`) precisely so that structured
payloads stay structured. Text parts are for prose the model reads *as prose*;
a registry of 30 sources is a document, not a sentence, and putting it in a text
part invites every client to reformat it.

Why pydantic models on both ends?
The sender builds a dict; the receiver validates it into a model. The agent then
fails the *task* with a readable message instead of raising inside the executor -
"Invalid input: registry.0.index: Input should be a valid integer" is a debuggable
error; a traceback in a log nobody reads is not.

Gotcha that cost real debugging time: protobuf `Struct` has no integer type, so
every number comes back as a float (`max_tokens: 2600.0`). The models below
declare `int` and pydantic coerces integral floats, which is why the contract is
validated rather than trusted.
"""
from __future__ import annotations

from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import BaseModel, Field

from a2a.types import Part


class AgentPayloadError(ValueError):
    """The message did not contain the payload the agent needs."""


class RegistryItem(BaseModel):
    """One numbered source, mirroring `writer.build_registry()` output."""

    index: int
    note_id: str = ""
    url: str = ""
    title: str | None = None
    claim: str = ""
    quote: str | None = None
    confidence: str = "medium"
    sub_question: str = ""


class WriterTask(BaseModel):
    """Input for the `write-cited-report` skill."""

    question: str = Field(min_length=3)
    plan: dict | None = None
    registry: list[RegistryItem] = Field(min_length=1)
    max_tokens: int | None = None
    temperature: float = 0.3


class CriticTask(BaseModel):
    """Input for the `critique-coverage` skill.

    The digest, not the notes: Phase 4 already decided that the critic judges a
    500-token summary rather than 60k tokens of corpus, and sending the corpus
    over the wire would undo that.
    """

    question: str = Field(min_length=3)
    sub_questions: list[str] = Field(default_factory=list)
    digest: str = Field(min_length=1)
    max_gaps: int = 3


class CriticVerdict(BaseModel):
    """Output of the `critique-coverage` skill (the orchestrator owns the brakes)."""

    sufficient: bool
    gaps: list[str] = Field(default_factory=list)
    reason: str


def _clean(value):
    """Drop `None` recursively: protobuf `Struct` has no null, so `ParseDict`
    rejects the payload otherwise (registry items legitimately have no title)."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def data_part(payload: dict) -> Part:
    """Wrap a dict as an A2A `DataPart`."""
    return ParseDict({"data": _clean(payload)}, Part())


def read_payload(parts, model: type[BaseModel]) -> BaseModel:
    """First data part -> validated model. Raises `AgentPayloadError`."""
    for part in parts or []:
        if part.WhichOneof("content") != "data":
            continue
        return model.model_validate(MessageToDict(part)["data"])
    raise AgentPayloadError(
        "expected one 'data' part carrying the task payload; got only "
        + (", ".join(p.WhichOneof("content") or "empty" for p in parts or []) or "no parts")
    )
