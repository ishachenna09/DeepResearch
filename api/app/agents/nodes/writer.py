"""The writer: turn stored notes into a cited markdown report (streamed).

Why a numbered registry instead of letting the model cite URLs?
A model that emits URLs invents them - and a URL that looks plausible is worse
than no citation, because it survives a skim. So the backend builds the source
list first, numbers it deterministically, and the prompt may only cite `[n]`
into *that* list. Citations are then constructed from the markers actually used,
which means every citation in the payload is traceable to a database row by
construction. The validator then only has to answer one question: did the model
invent a number?

Why not `with_structured_output` here?
The output is prose, not fields, and it has to stream token by token. Structured
output buffers to parse, which would make the UI show nothing for 30 s.

Why is the citation check deterministic (no LLM judge)?
Precision/coverage are arithmetic on the text: a marker either resolves to a
row or it does not. An LLM judge would add cost and variance to a check whose
ground truth is already exact - and Phase 7's eval gate needs a number that does
not move between runs.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable

from app.core.config import settings
from app.core.llm import get_llm, usage_tokens

WRITER_PROMPT = """You write the final research brief from a numbered source list.

Structure (markdown, no tables):
- Start with `## Answer`: 2-3 sentences that answer the user's question directly.
- Then 2-4 `## ` sections, each covering one part of the question. Short
  paragraphs; a few bullet lists are fine.
- End with `## What the evidence does not settle`: honest gaps, one or two
  sentences. If nothing is missing, say so.

Citation rules - these are audited automatically:
- Every factual claim ends with one or more markers like [3] or [1, 4] naming
  numbers from the source list you were given.
- Use ONLY numbers that appear in the list. Never invent a number, never cite a
  number that is not there.
- The ONLY accepted marker syntax is square brackets around numbers: [3], [1, 4].
  Do NOT use 【6†L1-L3】, (source 3), footnotes, superscripts or any other
  citation style - anything else is treated as uncited.
- If the sources disagree, say so and cite both.
- Do not write a "Sources" section - the app renders the cited list itself.
- Never state a fact that no provided source supports. Put it under
  "What the evidence does not settle" instead.
- The `## Answer` sentences are claims too: they need markers.
- Write only what the evidence covers. If the sources say more about one
  option than another, the brief must be lopsided in the same way - do NOT
  invent parallel bullets to make the sections look symmetric, and never write
  a bullet for a vendor the sources do not cover. Saying "no source covers X"
  is a correct sentence; it needs no marker.

Style: direct, no filler, no "in conclusion", at most 900 words.
"""
REPAIR_PROMPT = """You are fixing the citations of a research brief.

You receive the brief and a numbered source list. Rewrite the brief so that
EVERY factual sentence ends with at least one marker from the list.

Rules - the output is checked automatically:
- Marker syntax is exactly square brackets around numbers: [3] or [1, 4].
  Never use 【6†L1-L3】, (source 3), footnotes or superscripts.
- Only use numbers that exist in the list. If a sentence has no supporting
  source, move it to the final section or delete it.
- Do not change facts, numbers, structure or wording beyond what adding and
  correcting citations requires. Keep the same headings.
- Write the WHOLE brief and finish it. If the original is cut off mid-sentence,
  complete that thought. Never stop mid-sentence.
- Output ONLY the corrected markdown. No preamble, no commentary.
"""

_MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# Gemini's grounded-search citations look like 【6†L1-L3】. They index Gemini's
# OWN results, not our registry, so they must never be mapped to [n] - doing so
# would fabricate citations. We strip them and let the audit/repair pass handle
# the missing support honestly.
_GROUNDING_RE = re.compile(r"【[^】]*】")
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _host(url: str) -> str:
    """Bare hostname, used for site-level diversity in the registry."""
    return url.split("/")[2] if "://" in (url or "") else (url or "")


def build_registry(notes: Iterable, max_citations: int | None = None) -> list[dict]:
    """Deterministic, balanced, numbered source list for the prompt.

    Balance matters on two axes, and one pass over each:
    - across sub-questions (a plain "first N by confidence" could hand the
      writer 30 notes about one angle and nothing about the other five);
    - across *sites* within a sub-question (measured: 10 of 30 slots came from
      a single host, so one article's framing became the report's framing).

    Nested round-robin: outer loop = sub-questions, inner loop = hosts, both in
    best-confidence-first order. Every angle and every site gets a slot before
    any of them gets a second one.
    """
    limit = max_citations if max_citations is not None else settings.REPORT_MAX_CITATIONS
    ordered = sorted(
        notes,
        key=lambda n: (
            _CONFIDENCE_RANK.get(getattr(n, "confidence", "medium"), 1),
            getattr(n, "created_at", None) is None,
        ),
    )

    # sub_question -> notes, each list already confidence-ordered
    groups: dict[str, list] = {}
    for note in ordered:
        groups.setdefault(getattr(note, "sub_question", "") or "", []).append(note)

    def remaining() -> bool:
        return any(groups.values())

    # Each round takes exactly one note per sub-question (so angles stay level),
    # and inside a sub-question it takes the note from the *least-used host*, so
    # sites rotate. Taking every host of the first sub-question before moving on
    # (the obvious nested loop) starves the later angles - this does not.
    used_hosts: dict[str, int] = {}
    picked: list = []
    while len(picked) < limit and remaining():
        for name in list(groups):
            bucket = groups[name]
            if not bucket:
                continue
            best = min(
                range(len(bucket)),
                key=lambda i: (used_hosts.get(_host(getattr(bucket[i], "url", "")), 0), i),
            )
            note = bucket.pop(best)
            host = _host(getattr(note, "url", ""))
            used_hosts[host] = used_hosts.get(host, 0) + 1
            picked.append(note)
            if len(picked) >= limit:
                break

    registry: list[dict] = []
    for index, note in enumerate(picked, start=1):
        registry.append(
            {
                "index": index,
                "note_id": str(getattr(note, "id", "")),
                "url": getattr(note, "url", ""),
                "title": getattr(note, "title", None),
                "claim": (getattr(note, "note", "") or "")[:400],
                "quote": (getattr(note, "quote", None) or None),
                "confidence": getattr(note, "confidence", "medium"),
                "sub_question": getattr(note, "sub_question", "") or "",
            }
        )
    return registry


def render_sources(registry: list[dict]) -> str:
    lines: list[str] = []
    for item in registry:
        host = _host(item["url"])
        lines.append(
            f'[{item["index"]}] ({item["confidence"]}, {host}) {item["claim"]}'
        )
    return "\n".join(lines)


def build_messages(question: str, plan: dict | None, registry: list[dict]) -> list[dict]:
    plan = plan or {}
    sub_questions = [q for q in (plan.get("sub_questions") or []) if q and q.strip()]
    constraints = [c for c in (plan.get("constraints") or []) if c and c.strip()]
    parts = [f"User question:\n{question}"]
    if sub_questions:
        parts.append("Sub-questions the research covered:\n" + "\n".join(f"- {q}" for q in sub_questions))
    if constraints:
        parts.append("Constraints the user set:\n" + "\n".join(f"- {c}" for c in constraints))
    parts.append("Numbered sources (cite by number only):\n" + render_sources(registry))
    # Recency matters: the format rule is repeated at the END of the user turn
    # because that is the last thing the model reads before generating. Without
    # it, Gemini occasionally falls back to its native 【6†L1-L3】 grounding style.
    parts.append(
        "Reminder: cite with square brackets only, e.g. [3] or [1, 4]. "
        "Never use 【...】, parentheses-style sources, or footnotes."
    )
    return [
        {"role": "system", "content": WRITER_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def parse_markers(markdown: str) -> list[int]:
    """Unique citation numbers in first-appearance order (supports `[1, 4]`)."""
    seen: list[int] = []
    for match in _MARKER_RE.finditer(markdown or ""):
        for raw in match.group(1).split(","):
            value = int(raw.strip())
            if value not in seen:
                seen.append(value)
    return seen


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(markdown: str) -> list[str]:
    """Claim-sized units: prose sentences, excluding headings and shrapnel.

    Why sentences and not paragraphs? Models are unreliable about blank lines -
    a report with single newlines between a heading and its paragraph used to
    collapse into one block that started with "#", which silently scored
    coverage 0/0. A sentence is the unit a citation actually attaches to, and it
    survives any formatting choice the writer makes.
    """
    prose: list[str] = []
    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"^([-*+]|\d+\.)\s+", "", stripped)   # bullets
        prose.append(stripped)
    sentences: list[str] = []
    for sentence in _SENTENCE_RE.split(" ".join(prose)):
        if len(sentence.strip()) >= 30:      # a fragment is not a claim
            sentences.append(sentence.strip())
    return sentences


def validate(markdown: str, registry: list[dict]) -> dict:
    """Deterministic citation audit: precision (no invented sources) + coverage."""
    markers = parse_markers(markdown)
    valid = [m for m in markers if 1 <= m <= len(registry)]
    unknown = [m for m in markers if m not in valid]

    sentences = _sentences(markdown)
    cited = 0
    for sentence in sentences:
        if any(1 <= m <= len(registry) for m in parse_markers(sentence)):
            cited += 1

    return {
        "markers": len(markers),
        "valid_markers": len(valid),
        "unknown_markers": unknown,
        "precision": round(len(valid) / len(markers), 4) if markers else 0.0,
        "sentences": len(sentences),
        "cited_sentences": cited,
        "coverage": round(cited / len(sentences), 4) if sentences else 0.0,
        "unused_sources": max(0, len(registry) - len(set(valid))),
        "words": len((markdown or "").split()),
        # A brief with no resolvable marker is readable prose but not evidence.
        # Measured cause: the LiteLLM proxy failed over to a model that ignored
        # the marker contract, and the audit is the only thing that noticed.
        "uncited": len(valid) == 0,
    }


def citations_for(markdown: str, registry: list[dict]) -> list[dict]:
    """Citations in marker order, restricted to sources the text actually used."""
    used = [m for m in parse_markers(markdown) if 1 <= m <= len(registry)]
    return [registry[m - 1] for m in used]


def normalize(markdown: str) -> str:
    """Tidy the model's text for display without touching its meaning.

    Extracted notes carry typographic hyphens (U+2011) and non-breaking spaces;
    reflowed into prose they render as odd gaps. Normalising them here means the
    stored report and the citation audit see the same bytes.

    The 【...】 strip is not cosmetic: that is Gemini's own grounding citation
    format, indexing its search results rather than our registry. Leaving it in
    ships citations a reader cannot follow, and mapping it to [n] would invent
    support - so it is removed and the audit reports the truth.
    """
    return _GROUNDING_RE.sub(
        "",
        (markdown or "")
        .replace("\u2011", "-")     # non-breaking hyphen -> ASCII hyphen
        .replace("\u00a0", " "),    # non-breaking space -> space
    ).replace("\r\n", "\n").strip()


def needs_repair(validation: dict) -> bool:
    """One second attempt is worth it when the draft is structurally broken.

    Two independent triggers:
    - **uncited** - nothing resolves to a source, or more than one marker in ten
      is invented;
    - **truncated** - the provider stopped at the token cap (`finish_reason ==
      "length"`), which on a thinking model can cut the brief mid-sentence.

    A draft that cites well and finished is never touched - the retry costs
    ~2-4k tokens.
    """
    if validation.get("truncated"):
        return True
    if validation["markers"] == 0:
        return True
    return validation["precision"] < 0.9


async def repair_citations(
    question: str,
    plan: dict | None,
    registry: list[dict],
    draft: str,
    max_tokens: int | None = None,
) -> tuple[str, int]:
    """Ask once for the same brief with correct markers.

    Returns `(text, tokens, served_model)`. The model matters because a failover
    tier may be the one that ignored the marker format in the first place.
    """
    plan = plan or {}
    user_message = (
        f"User question:\n{question}\n\n"
        "Numbered sources:\n" + render_sources(registry) + "\n\n"
        f"Brief to fix:\n{draft}"
    )
    llm = get_llm(temperature=0.0, timeout=settings.REPORT_TIMEOUT).bind(
        max_tokens=max_tokens if max_tokens is not None else settings.REPORT_MAX_TOKENS
    )
    result = await llm.ainvoke(
        [
            {"role": "system", "content": REPAIR_PROMPT},
            {"role": "user", "content": user_message},
        ]
    )
    meta = getattr(result, "response_metadata", None) or {}
    served = meta.get("model_name") if isinstance(meta, dict) else None
    return (
        _chunk_text(result),
        usage_tokens(result),
        served if isinstance(served, str) and served else None,
    )


def _chunk_text(chunk) -> str:
    """Providers differ: content is a str, or a list of content parts."""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


class WriterRun:
    """One streaming generation.

    After iteration: `.tokens`, `.model` and `.finish_reason` are valid.
    `.model` is the model that ACTUALLY answered, which may not be the one we
    asked for: the LiteLLM proxy fails over Gemini -> Groq -> Gemini-Lite, and
    the response metadata reports the upstream that served the call. Recording
    the alias instead would hide the failover - measured: a report was written
    entirely by `openai/gpt-oss-120b` while its row claimed `support-chat`.
    `finish_reason == "length"` means the provider hit the token cap, which on a
    thinking model can happen mid-sentence - the caller retries rather than ship
    half a brief.
    """

    def __init__(self, question: str, plan: dict | None, registry: list[dict]) -> None:
        self.messages = build_messages(question, plan, registry)
        self.tokens = 0
        self.model = settings.LLM_PRIMARY_MODEL
        self.finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    async def astream(self) -> AsyncIterator[str]:
        llm = get_llm(temperature=0.3, timeout=settings.REPORT_TIMEOUT).bind(
            max_tokens=settings.REPORT_MAX_TOKENS
        )
        try:
            stream = llm.astream(self.messages, stream_usage=True)
        except TypeError:             # older signature: no usage on stream
            stream = llm.astream(self.messages)
        async for chunk in stream:
            text = _chunk_text(chunk)
            # Usage arrives once, on a late chunk, and is cumulative for the
            # call - assign, never add (adding would multiply the total).
            reported = usage_tokens(chunk)
            if reported:
                self.tokens = reported
            # finish_reason also arrives once, on the last chunk. Keep the last
            # non-null value: providers report None on every chunk but the end.
            meta = getattr(chunk, "response_metadata", None) or {}
            reason = meta.get("finish_reason") if isinstance(meta, dict) else None
            if reason:
                self.finish_reason = str(reason)
            # LiteLLM forwards the upstream model it actually called. Prefer it
            # over the proxy alias so a silent failover is visible in the row.
            served = meta.get("model_name") if isinstance(meta, dict) else None
            if isinstance(served, str) and served:
                self.model = served
            if text:
                yield text
