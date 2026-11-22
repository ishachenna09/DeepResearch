"""Deterministic tests for the A2A layer (Phase 6).

What is worth testing here is *not* the SDK: it is our contract and our
decisions. So the tests cover:

- the wire payloads (a `None` title must not crash protobuf; ints come back as
  floats and are coerced),
- the accumulator that turns frames into a report (the only place where a
  duplicated or dropped chunk would corrupt the output),
- the executors' event order (Task first, then status, then chunks - the SDK
  rejects any other order),
- auth and discovery on a real ASGI app (card public, RPC protected),
- the Redis task store and the "remote critic failed -> critic_error" brake.

No provider call, no socket, no DB: these are the tests that can run at 3 a.m.

Run from api/:
    ..\\.venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from a2a.types import (
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict

from app.a2a import client as client_mod
from app.a2a.cards import critic_card, rpc_path, writer_card
from app.a2a.client import AgentError, _Accumulator, parse_sse
from app.a2a.critic_executor import CriticExecutor
from app.a2a.payloads import (
    AgentPayloadError,
    CriticTask,
    WriterTask,
    data_part,
    read_payload,
)
from app.a2a.task_store import RedisTaskStore
from app.a2a.writer_executor import WriterExecutor
from app.core.config import settings
from app.schemas.research import CoverageReview

TEST_TOKEN = "test-agent-token"


@pytest.fixture(autouse=True)
def fixed_token(monkeypatch):
    """Never depend on the developer's .env for an auth assertion."""
    monkeypatch.setattr(settings, "A2A_TOKEN", TEST_TOKEN)
    yield


# ── helpers ────────────────────────────────────────────────────────────────


def run(coro):
    """Run one coroutine without pytest-asyncio (not installed on purpose)."""
    return asyncio.run(coro)


class FakeQueue:
    """The only method `TaskUpdater` needs."""

    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class FakeRun:
    """Stand-in for `WriterRun`: deterministic text, no provider."""

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        type(self).last_kwargs = kwargs
        self.tokens = 42
        self.model = "fake-model"
        self.finish_reason = "stop"
        self.truncated = False

    async def astream(self):
        for chunk in ["Hello", " world"]:
            yield chunk


def writer_task(question: str = "What is HTTP/2?") -> WriterTask:
    return WriterTask(
        question=question,
        registry=[
            {
                "index": 1,
                "note_id": "n1",
                "url": "https://example.com/x",
                "title": None,  # must not reach protobuf as null
                "claim": "HTTP/2 multiplexes streams.",
                "quote": None,
                "confidence": "high",
                "sub_question": "How does it work?",
            }
        ],
        max_tokens=300,
    )


def context_with(payload: dict, task_id: str = "t-1", context_id: str = "c-1"):
    message = Message(
        message_id="m-1",
        role=Role.ROLE_USER,
        parts=[data_part(payload)],
    )
    return SimpleNamespace(task_id=task_id, context_id=context_id, message=message)


def artifact_text(event: TaskArtifactUpdateEvent) -> str:
    return "".join(p.text for p in event.artifact.parts if p.WhichOneof("content") == "text")


# ── payload contract ───────────────────────────────────────────────────────


def test_data_part_drops_none_and_roundtrips_numbers():
    part = data_part(
        {
            "question": "q",
            "max_tokens": 2600,
            "title": None,
            "nested": {"quote": None, "ok": True},
            "list": [{"title": None, "index": 1}],
        }
    )
    data = MessageToDict(part)["data"]
    # Struct has no null: a None would have raised inside ParseDict.
    assert "title" not in data
    assert "quote" not in data["nested"]
    assert data["list"][0]["index"] == 1.0  # numbers arrive as floats
    assert data["max_tokens"] == 2600.0


def test_writer_task_validates_float_integers_from_protobuf():
    part = data_part(writer_task().model_dump())
    parsed = read_payload([part], WriterTask)
    assert isinstance(parsed.registry[0].index, int)
    assert parsed.registry[0].index == 1
    assert parsed.max_tokens == 300


def test_read_payload_explains_a_missing_data_part():
    with pytest.raises(AgentPayloadError) as excinfo:
        read_payload([Part(text="not json")], WriterTask)
    assert "expected one 'data' part" in str(excinfo.value)


def test_critic_task_requires_a_digest():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CriticTask(question="q?", digest="")


# ── accumulator (the thing that can corrupt a report) ─────────────────────


def test_accumulator_appends_artifact_chunks():
    acc = _Accumulator()
    acc.feed({"task": {"id": "t", "status": {"state": "TASK_STATE_SUBMITTED"}}})
    acc.feed(
        {
            "artifactUpdate": {
                "taskId": "t",
                "artifact": {"artifactId": "report", "parts": [{"text": "Hello"}]},
            }
        }
    )
    acc.feed(
        {
            "artifactUpdate": {
                "taskId": "t",
                "artifact": {"artifactId": "report", "parts": [{"text": " world"}]},
                "append": True,
            }
        }
    )
    acc.feed(
        {
            "statusUpdate": {
                "taskId": "t",
                "status": {"state": "TASK_STATE_COMPLETED"},
            }
        }
    )
    reply = acc.reply()
    assert reply.text == "Hello world"
    assert reply.state == "TASK_STATE_COMPLETED"
    assert reply.events == 4


def test_final_metadata_chunk_does_not_wipe_the_text():
    acc = _Accumulator()
    acc.feed(
        {
            "artifactUpdate": {
                "taskId": "t",
                "artifact": {"artifactId": "report", "parts": [{"text": "body"}]},
            }
        }
    )
    # The real final chunk: parts=[], append=True, last_chunk, metadata only.
    acc.feed(
        {
            "artifactUpdate": {
                "taskId": "t",
                "artifact": {"artifactId": "report", "parts": [], "metadata": {"tokens": 12}},
                "append": True,
            }
        }
    )
    assert acc.reply().text == "body"
    assert acc.reply().tokens == 12


def test_task_snapshot_with_trailing_empty_artifact_keeps_text():
    # Non-streamed reply: the SDK may list a metadata-only artifact with the
    # same id after the merged one. `append=False` must not be read as "reset".
    acc = _Accumulator()
    acc.feed({"task": {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"}}})
    acc.feed(
        {
            "task": {
                "id": "t",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"artifactId": "report", "parts": [{"text": "body"}]},
                    {"artifactId": "report", "parts": [], "metadata": {"tokens": 9}},
                ],
            }
        }
    )
    reply = acc.reply()
    assert reply.text == "body"
    assert reply.tokens == 9


def test_accumulator_coerces_protobuf_floats_to_int():
    acc = _Accumulator()
    acc.feed(
        {
            "artifactUpdate": {
                "artifactId": "r",
                "artifact": {
                    "artifactId": "r",
                    "parts": [{"text": "x"}],
                    "metadata": {"tokens": 2600.5, "truncated": False, "model": "m"},
                },
            }
        }
    )
    assert acc.reply().tokens == 2600
    assert acc.reply().truncated is False


def test_failed_task_records_the_status_message():
    acc = _Accumulator()
    acc.feed(
        {
            "statusUpdate": {
                "taskId": "t",
                "status": {
                    "state": "TASK_STATE_FAILED",
                    "message": {"parts": [{"text": "no text"}]},
                },
            }
        }
    )
    assert acc.reply().message == "no text"


# ── SSE parser ─────────────────────────────────────────────────────────────


def test_sse_parser_skips_noise_and_malformed_frames():
    async def lines():
        for line in [
            "event: message",
            'data: {"result": {"task": {"id": "t"}}}',
            "",
            "data: {not json",
            'data: {"result": {"statusUpdate": {"taskId": "t"}}}',
        ]:
            yield line

    async def collect():
        return [frame async for frame in parse_sse(lines())]

    frames = run(collect())
    assert len(frames) == 2
    assert "task" in frames[0]["result"]
    assert "statusUpdate" in frames[1]["result"]


# ── executors: event order is the protocol ────────────────────────────────


def test_writer_executor_emits_task_then_chunks_then_metadata():
    queue = FakeQueue()
    ctx = context_with(writer_task().model_dump())
    run(WriterExecutor(run_factory=FakeRun).execute(ctx, queue))

    events = queue.events
    assert isinstance(events[0], Task), "the first event must define the task"
    assert events[0].status.state == TaskState.TASK_STATE_SUBMITTED
    assert events[0].history[0].message_id == "m-1"

    updates = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert [artifact_text(e) for e in updates[:2]] == ["Hello", " world"]
    assert updates[0].append is False, "the first chunk creates the artifact"
    assert updates[1].append is True

    final = updates[-1]
    assert final.last_chunk is True
    assert list(final.artifact.parts) == []
    meta = MessageToDict(final.artifact)["metadata"]
    assert meta["tokens"] == 42.0
    assert meta["model"] == "fake-model"

    statuses = [e for e in events if isinstance(e, TaskStatusUpdateEvent)]
    assert statuses[-1].status.state == TaskState.TASK_STATE_COMPLETED

    # The payload's max_tokens is honoured by the run, not silently dropped.
    assert FakeRun.last_kwargs["max_tokens"] == 300
    assert FakeRun.last_kwargs["temperature"] == 0.3


def test_writer_executor_fails_politely_on_bad_payload():
    queue = FakeQueue()
    message = Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hi")])
    ctx = SimpleNamespace(task_id="t", context_id="c", message=message)
    run(WriterExecutor(run_factory=FakeRun).execute(ctx, queue))

    statuses = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent)]
    assert statuses[-1].status.state == TaskState.TASK_STATE_FAILED
    assert "Invalid input" in statuses[-1].status.message.parts[0].text


def test_writer_executor_fails_when_the_model_says_nothing():
    class EmptyRun(FakeRun):
        async def astream(self):
            if False:
                yield ""

    queue = FakeQueue()
    ctx = context_with(writer_task().model_dump())
    run(WriterExecutor(run_factory=EmptyRun).execute(ctx, queue))
    statuses = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent)]
    assert statuses[-1].status.state == TaskState.TASK_STATE_FAILED
    assert "no text" in statuses[-1].status.message.parts[0].text


def test_critic_executor_caps_gaps_and_returns_json():
    review = CoverageReview(
        sufficient=False,
        gaps=["a", "b", "c"],
        reason="more evidence needed",
    )

    async def review_fn(question, sub_questions, digest):
        return review, 7, "critic-model", ""

    queue = FakeQueue()
    ctx = context_with(
        {
            "question": "what?",
            "sub_questions": ["s1"],
            "digest": "1 note about s1",
            "max_gaps": 2,
        }
    )
    run(CriticExecutor(review_fn=review_fn).execute(ctx, queue))

    updates = [e for e in queue.events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(updates) == 1
    verdict = json.loads(updates[0].artifact.parts[0].text)
    assert verdict["sufficient"] is False
    assert len(verdict["gaps"]) == 2, "max_gaps is enforced by the agent too"

    meta = MessageToDict(updates[0].artifact)["metadata"]
    assert meta["tokens"] == 7.0
    assert meta["model"] == "critic-model"


def test_critic_executor_fails_the_task_when_no_verdict():
    async def review_fn(question, sub_questions, digest):
        return None, 0, "", "provider exploded"

    queue = FakeQueue()
    ctx = context_with({"question": "what?", "digest": "d"})
    run(CriticExecutor(review_fn=review_fn).execute(ctx, queue))
    statuses = [e for e in queue.events if isinstance(e, TaskStatusUpdateEvent)]
    assert statuses[-1].status.state == TaskState.TASK_STATE_FAILED
    assert "provider exploded" in statuses[-1].status.message.parts[0].text


# ── remote critic failure keeps the existing brake ────────────────────────


def test_remote_review_turns_any_failure_into_an_unusable_verdict(monkeypatch):
    async def boom(**kwargs):
        raise AgentError("connection refused")

    monkeypatch.setattr(client_mod, "critique_remote", boom)
    from app.agents.nodes.critic import _remote_review

    review, tokens, model, error = run(_remote_review("q?", ["s1"], "digest"))
    assert review is None
    assert tokens == 0
    assert model == ""
    assert "AgentError" in error


def test_critic_node_records_critic_error_in_a2a_mode(monkeypatch):
    from app.agents.nodes import critic as critic_mod

    async def boom(question, sub_questions, digest):
        return None, 0, "", "AgentUnavailable: down"

    monkeypatch.setattr(settings, "AGENTS_MODE", "a2a")
    monkeypatch.setattr(critic_mod, "_remote_review", boom)

    state = {
        "question": "q?",
        "notes": [{"sub_question": "s1", "url": "https://x", "note": "claim"}],
        "plan": {"sub_questions": ["s1"], "budget_tokens": 100_000},
        "round": 1,
        "tokens_used": 100,
        "stats": {"notes_new": 5, "pages_read": 1},
    }
    result = run(critic_mod.critic_node(state))
    assert result["critique"]["stop_reason"] == "critic_error"
    assert result["gaps"] == []


# ── cards + ASGI surface ──────────────────────────────────────────────────


def test_cards_advertise_jsonrpc_streaming_and_a_skill():
    card = writer_card("http://127.0.0.1:8010")
    assert card.name == "deepresearch-writer"
    assert card.capabilities.streaming is True
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"
    assert card.supported_interfaces[0].protocol_version == "1.0"
    assert [s.id for s in card.skills] == ["write-cited-report"]
    assert "bearer" in card.security_schemes
    assert rpc_path(card) == "/"

    critic = critic_card("http://127.0.0.1:8011")
    assert [s.id for s in critic.skills] == ["critique-coverage"]


def agent_app(kind: str):
    """Build the real ASGI app without running uvicorn."""
    if kind == "writer":
        from app.a2a.writer_app import app

        return app
    from app.a2a.critic_app import app

    return app


async def asgi_request(app, method: str, path: str, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        return await client.request(method, path, **kwargs)


def test_card_is_public_and_healthz_is_public():
    app = agent_app("writer")
    card = run(asgi_request(app, "GET", "/.well-known/agent-card.json"))
    assert card.status_code == 200
    assert card.json()["name"] == "deepresearch-writer"

    health = run(asgi_request(app, "GET", "/healthz"))
    assert health.status_code == 200
    assert health.json()["agent"] == "deepresearch-writer"


def test_rpc_requires_the_shared_token():
    app = agent_app("writer")
    body = {"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "x"}}
    response = run(asgi_request(app, "POST", "/", json=body, headers={"A2A-Version": "1.0"}))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_agent_refuses_to_run_open_without_a_configured_token(monkeypatch):
    monkeypatch.setattr(settings, "A2A_TOKEN", "")
    app = agent_app("writer")
    body = {"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "x"}}
    response = run(
        asgi_request(
            app,
            "POST",
            "/",
            json=body,
            headers={"A2A-Version": "1.0", "Authorization": "Bearer anything"},
        )
    )
    assert response.status_code == 503


def test_unknown_method_is_minus_32601():
    app = agent_app("critic")
    response = run(
        asgi_request(
            app,
            "POST",
            "/",
            json={"jsonrpc": "2.0", "id": "1", "method": "Nope", "params": {}},
            headers={"A2A-Version": "1.0", "Authorization": f"Bearer {settings.A2A_TOKEN}"},
        )
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


def test_wrong_token_is_rejected_before_jsonrpc_parsing():
    app = agent_app("critic")
    response = run(
        asgi_request(
            app,
            "POST",
            "/",
            json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "x"}},
            headers={"A2A-Version": "1.0", "Authorization": "Bearer wrong"},
        )
    )
    assert response.status_code == 401


# ── Redis task store: survives a restart, tolerates a dead Redis ──────────


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.fail = fail

    async def setex(self, key, ttl, value):
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value

    async def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.data.pop(key, None)

    async def aclose(self):
        pass


# The SDK's in-memory store partitions tasks by owner, so `save`/`get` need a
# call context. Only `.user.user_name` is read.
CTX = SimpleNamespace(user=SimpleNamespace(user_name="test-user"))


def test_redis_task_store_survives_a_restart():
    fake = FakeRedis()
    first = RedisTaskStore("test", ttl=60)
    first._client = fake

    task = Task(id="t-9", context_id="c-9")
    run(first.save(task, CTX))

    # A brand-new process: empty in-memory store, same Redis.
    second = RedisTaskStore("test", ttl=60)
    second._client = fake
    loaded = run(second.get("t-9", CTX))
    assert loaded is not None
    assert loaded.id == "t-9"


def test_redis_task_store_soft_fails_when_redis_is_down():
    store = RedisTaskStore("test", ttl=60)
    store._client = FakeRedis(fail=True)
    task = Task(id="t-10", context_id="c-10")
    run(store.save(task, CTX))            # must not raise
    loaded = run(store.get("t-10", CTX))  # falls back to memory
    assert loaded is not None and loaded.id == "t-10"
