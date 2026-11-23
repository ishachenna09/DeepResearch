"""Langfuse client (Langfuse Cloud) — tracing wired fully in Phase 1.

Phase 7 adds *findability*: every call made for a report carries the job id as
the session and a `report` tag, so one trace can be pulled out of a busy project
without knowing an internal id first. Attributes are applied by
`report_trace_attributes()` below; when keys are absent it returns a no-op
context so `local` development never needs a Langfuse account.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext

from langfuse import Langfuse

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Langfuse | None = None


def get_langfuse() -> Langfuse | None:
    global _client
    if _client is None and settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY:
        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST or "https://cloud.langfuse.com",
        )
    return _client


class _SafeContext:
    """A context manager whose failures are logged and ignored.

    `propagate_attributes` talks to the tracing backend; a 401 or a network
    hiccup at __enter__ must not turn a report into a failure. Degrading to a
    no-op is the whole contract of this module.
    """

    def __init__(self, inner, label: str) -> None:
        self._inner = inner
        self._label = label
        self._entered = False

    def __enter__(self):
        try:
            self._inner.__enter__()
            self._entered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s disabled (%s: %s)", self._label, type(exc).__name__, exc)
        return None

    def __exit__(self, *exc_info):
        if not self._entered:
            return False
        try:
            self._inner.__exit__(*exc_info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s close failed (%s: %s)", self._label, type(exc).__name__, exc)
        return False


def report_trace_attributes(
    *,
    session_id: str,
    report_id: str,
    job_id: str,
    mode: str,
    event: str,
):
    """Attribute every span created inside this block to one report.

    Why `propagate_attributes` and not set fields after the fact? LangChain's
    callback creates the spans, not our code; the only handle is the ambient
    context it reads when a generation starts. Wrapping the generation is the
    supported seam.

    Tracing must never break a report: an auth error or a client bug degrades to
    a no-op context and a warning, not a failed generation.
    """
    client = get_langfuse()
    if client is None:
        return nullcontext()
    try:
        from langfuse import propagate_attributes

        inner = propagate_attributes(
            session_id=session_id,
            tags=["report", event, f"mode:{mode}"],
            metadata={"report_id": report_id, "job_id": job_id, "mode": mode},
            trace_name="report-generation",
        )
        return _SafeContext(inner, "langfuse attributes")
    except Exception as exc:  # noqa: BLE001 - observability is never critical path
        logger.warning("langfuse attributes disabled (%s: %s)", type(exc).__name__, exc)
        return nullcontext()


def shutdown_langfuse() -> None:
    if _client is not None:
        _client.flush()
