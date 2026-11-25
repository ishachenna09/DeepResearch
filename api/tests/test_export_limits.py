"""The PDF export CPU cap (Phase 9).

Why test this at all? An export is the one endpoint that spends real CPU per
request. Unbounded `to_thread` calls turn a burst into an unresponsive process;
the contract is "two at a time, then 503 with Retry-After", not "queue forever".

The fake renderer blocks a worker thread on an Event, so the test does not
depend on ReportLab being slow - it depends only on the semaphore.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone

import pytest

from app.services import report_export
from app.services.report_export import ExportContext


def run(coro):
    return asyncio.run(coro)


def _ctx(markdown: str = "## Finding\n\nA sentence with a citation [1].") -> ExportContext:
    return ExportContext(
        report_id=uuid.uuid4(),
        question="What is under test?",
        markdown=markdown,
        citations=[{"index": 1, "title": "Source", "url": "https://example.com"}],
        validation={"words": 8, "markers": 1, "precision": 1.0, "coverage": 1.0},
        model="test-model",
        created_at=datetime.now(timezone.utc),
    )


def test_third_concurrent_export_is_refused(monkeypatch):
    async def scenario():
        release = threading.Event()

        def fake_render(_ctx):
            release.wait(2)
            return b"%PDF-fake"

        monkeypatch.setattr(report_export, "render_pdf", fake_render)
        monkeypatch.setattr(report_export, "_PDF_SEMAPHORE", asyncio.Semaphore(1))
        ctx = _ctx()

        first = asyncio.create_task(report_export.render_pdf_limited(ctx, wait_seconds=5))
        await asyncio.sleep(0.05)  # let the first task acquire the only slot
        try:
            with pytest.raises(report_export.ExportBusy):
                await report_export.render_pdf_limited(ctx, wait_seconds=0.05)
        finally:
            release.set()
        return await first

    assert run(scenario()) == b"%PDF-fake"


def test_slot_is_released_after_a_render(monkeypatch):
    async def scenario():
        monkeypatch.setattr(report_export, "render_pdf", lambda _ctx: b"%PDF-ok")
        monkeypatch.setattr(report_export, "_PDF_SEMAPHORE", asyncio.Semaphore(1))
        return [
            await report_export.render_pdf_limited(_ctx(), wait_seconds=1)
            for _ in range(3)
        ]

    assert run(scenario()) == [b"%PDF-ok", b"%PDF-ok", b"%PDF-ok"]


def test_real_renderer_still_produces_a_pdf():
    data = run(report_export.render_pdf_limited(_ctx()))
    assert data.startswith(b"%PDF-")
    assert len(data) > 1_000
