"""The API's A2A client: discover a card, send a message, read the result.

Why hand-roll this client when the SDK has one?
The server uses `a2a-sdk` because conformance is the point there: any A2A client
should be able to call us, so the wire format must not be our invention. On this
side we are the *only* consumer, and the things that matter are precisely the
things a general client hides: an exact read timeout (a cold Gemini can take 127 s
to emit its first token), "do not retry a 401", and "never replay deltas you have
already delivered". That is ~100 lines, fully unit-testable as a pure parser, and
it is the same reasoning that kept `sse-starlette` out of Phase 5.

The protocol surface used here is small and explicit:
    GET  /.well-known/agent-card.json
    POST {interface url}  {"jsonrpc":"2.0","method":"SendMessage"...}
    POST {interface url}  {"jsonrpc":"2.0","method":"SendStreamingMessage"...}
    POST {interface url}  {"jsonrpc":"2.0","method":"GetTask","params":{"taskId":...}}

Failure model: every transport problem becomes `AgentUnavailable` (retryable
once when nothing has been delivered yet), every protocol/auth problem becomes
`AgentError` (not retryable). Callers decide what that means for a report or a
research round - this module never swallows the reason.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from google.protobuf.json_format import ParseDict

from a2a.types import AgentCard

from app.core.config import settings

logger = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"

# States in which the task can no longer change; a stream that ends in any
# other state was cut off mid-flight.
FINAL_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)


class AgentError(RuntimeError):
    """The agent answered, but not with something we can use."""


class AgentAuthError(AgentError):
    """401/403: a configuration problem, not a transient one."""


class AgentUnavailable(AgentError):
    """Transport, timeout or 5xx: worth one retry if nothing was delivered."""


@dataclass
class AgentReply:
    text: str = ""
    tokens: int = 0
    model: str = ""
    finish_reason: str | None = None
    truncated: bool = False
    task_id: str = ""
    state: str = ""
    message: str = ""
    events: int = 0


@dataclass
class _Accumulator:
    """Builds an AgentReply from stream frames (pure, testable)."""

    parts: list[str] = field(default_factory=list)
    artifact_id: str = ""
    meta: dict = field(default_factory=dict)
    task_id: str = ""
    state: str = ""
    message: str = ""
    events: int = 0

    def feed(self, result: dict) -> None:
        self.events += 1
        if "task" in result:
            task = result["task"]
            self.task_id = task.get("id") or self.task_id
            self._note_status(task.get("status") or {})
            # A non-streamed reply arrives as one already-merged Task. Merge
            # rather than replace: the SDK may append a trailing metadata-only
            # artifact with the same id, and that must not wipe the text.
            for artifact in task.get("artifacts") or []:
                self._merge(artifact)
            return
        if "statusUpdate" in result:
            update = result["statusUpdate"]
            self.task_id = update.get("taskId") or self.task_id
            self._note_status(update.get("status") or {})
            return
        if "artifactUpdate" in result:
            update = result["artifactUpdate"]
            self.task_id = update.get("taskId") or self.task_id
            self._take_artifact(update.get("artifact") or {}, bool(update.get("append")))
            return
        # `message` frames are narrative status, not report text: ignore them.

    def _note_status(self, status: dict) -> None:
        self.state = status.get("state") or self.state
        message = status.get("message") or {}
        text = "".join(
            part.get("text") or "" for part in message.get("parts") or []
        )
        if text:
            # Terminal failures carry their reason here, not in the history.
            self.message = text[:300]

    @staticmethod
    def _texts(artifact: dict) -> list[str]:
        return [
            part["text"]
            for part in artifact.get("parts") or []
            if isinstance(part.get("text"), str) and part["text"]
        ]

    def _merge(self, artifact: dict) -> None:
        """Accumulate an already-complete artifact (a `Task` snapshot)."""
        artifact_id = artifact.get("artifactId") or ""
        if artifact_id and artifact_id != self.artifact_id:
            self.parts, self.artifact_id = [], artifact_id
        self.parts.extend(self._texts(artifact))
        if artifact.get("metadata"):
            # Shallow: the last metadata wins. Only the final chunk carries any.
            self.meta.update(artifact["metadata"])

    def _take_artifact(self, artifact: dict, append: bool) -> None:
        artifact_id = artifact.get("artifactId") or ""
        texts = self._texts(artifact)
        if artifact_id and artifact_id != self.artifact_id:
            self.parts, self.artifact_id = [], artifact_id
        elif not append and texts:
            # `append=False` WITH text means "this chunk is the whole artifact".
            # An empty `append=False` chunk is metadata-only and must not reset.
            self.parts = []
        self.parts.extend(texts)
        if artifact.get("metadata"):
            self.meta.update(artifact["metadata"])

    def reply(self) -> AgentReply:
        return AgentReply(
            text="".join(self.parts),
            # Struct has no integers - 2962 arrives as 2962.0.
            tokens=int(self.meta.get("tokens") or 0),
            model=str(self.meta.get("model") or ""),
            finish_reason=(self.meta.get("finish_reason") or None),
            truncated=bool(self.meta.get("truncated", False)),
            task_id=self.task_id,
            state=self.state,
            message=self.message,
            events=self.events,
        )


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """Yield the JSON payload of each `data:` line in an SSE body.

    A2A streams one JSON-RPC response per frame, so unlike the report stream
    there is no multi-line payload to join; the parser stays this small on
    purpose and is unit-tested without a socket.
    """
    async for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("a2a client: dropping malformed SSE frame: %s", raw[:200])


class AgentClient:
    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = settings.A2A_TOKEN if token is None else token
        self._card: AgentCard | None = None
        self._card_at = 0.0
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ plumbing
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            headers = {"A2A-Version": "1.0"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            else:
                # Loud, early, and specific: an empty token is a deployment bug,
                # not something to discover as a 503 from the agent.
                raise AgentError(
                    "A2A_TOKEN is not set - set it in .env, the agent services check it too"
                )
            self._http = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=float(settings.A2A_READ_TIMEOUT),
                    write=30.0,
                    pool=5.0,
                ),
            )
        return self._http

    async def card(self, *, refresh: bool = False) -> AgentCard:
        """Discover the agent card, cached for A2A_CARD_TTL seconds.

        Why cache: every report would otherwise pay a round trip to be told the
        same five fields. Why not cache forever: an agent that moves or changes
        skills should be picked up without restarting the API.
        """
        fresh = self._card is not None and (time.monotonic() - self._card_at) < settings.A2A_CARD_TTL
        if fresh and not refresh:
            return self._card

        client = await self._client()
        try:
            response = await client.get(f"{self.base_url}{CARD_PATH}")
        except Exception as exc:  # noqa: BLE001
            raise AgentUnavailable(f"{self.base_url}: card unreachable ({exc})") from exc
        if response.status_code != 200:
            raise AgentUnavailable(f"{self.base_url}: card returned {response.status_code}")
        card = ParseDict(response.json(), AgentCard())
        self._card, self._card_at = card, time.monotonic()
        return card

    async def _rpc_url(self) -> str:
        card = await self.card()
        if not card.supported_interfaces:
            raise AgentError(f"{self.base_url}: card advertises no interface")
        interface = card.supported_interfaces[0]
        # Use the URL the agent published, not the one we guessed: that is what
        # discovery is for. Fall back to our base when the card is relative.
        url = interface.url or self.base_url
        if url.startswith("/"):
            url = self.base_url + url
        return url

    @staticmethod
    def _check(response: httpx.Response, url: str) -> None:
        if response.status_code in (401, 403):
            raise AgentAuthError(f"{url}: {response.status_code} - check A2A_TOKEN")
        if response.status_code >= 500:
            raise AgentUnavailable(f"{url}: {response.status_code}")

    async def _post(self, payload: dict, *, accept: str) -> httpx.Response:
        client = await self._client()
        url = await self._rpc_url()
        try:
            response = await client.post(url, json=payload, headers={"Accept": accept})
        except httpx.TimeoutException as exc:
            raise AgentUnavailable(f"{url}: timed out ({exc})") from exc
        except httpx.HTTPError as exc:
            raise AgentUnavailable(f"{url}: transport error ({exc})") from exc
        self._check(response, url)
        return response

    @asynccontextmanager
    async def _sse(self, payload: dict) -> AsyncIterator[httpx.Response]:
        """Open the stream without buffering the whole body.

        `client.post()` reads every frame into memory before `aiter_lines()`
        sees one - a minute of silence for the caller. `stream()` hands us each
        frame as it arrives, which also turns the read timeout into a
        per-frame timeout instead of a whole-response one.
        """
        client = await self._client()
        url = await self._rpc_url()
        try:
            async with client.stream(
                "POST", url, json=payload, headers={"Accept": "text/event-stream"}
            ) as response:
                self._check(response, url)
                yield response
        except httpx.TimeoutException as exc:
            raise AgentUnavailable(f"{url}: timed out ({exc})") from exc
        except httpx.HTTPError as exc:
            raise AgentUnavailable(f"{url}: transport error ({exc})") from exc

    @staticmethod
    def _envelope(method: str, payload: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "ROLE_USER",
                    "parts": [{"data": payload}],
                }
            },
        }

    # -------------------------------------------------------------------- calls
    async def call(self, payload: dict) -> AgentReply:
        """`SendMessage`: wait for the whole task. For short, non-streamed work."""
        body = self._envelope("SendMessage", payload)
        response = await self._post(body, accept="application/json")
        return self._read_response(response)

    async def stream(
        self,
        payload: dict,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentReply:
        """`SendStreamingMessage`: forward each text chunk, return the final reply.

        Retried once only when nothing has been delivered: replaying a stream
        that already reached the caller would duplicate text they have seen.
        """
        body = self._envelope("SendStreamingMessage", payload)
        delivered = False

        for attempt in (1, 2):
            acc = _Accumulator()
            try:
                async with self._sse(body) as response:
                    if response.status_code != 200:
                        raise AgentError(f"stream returned {response.status_code}")
                    async for frame in parse_sse(response.aiter_lines()):
                        if "error" in frame:
                            error = frame["error"] or {}
                            raise AgentError(
                                f"agent error {error.get('code')}: {error.get('message')}"
                            )
                        result = frame.get("result") or {}
                        before = len(acc.parts)
                        acc.feed(result)
                        if on_delta is not None and len(acc.parts) > before:
                            for delta in acc.parts[before:]:
                                await on_delta(delta)
                            delivered = True
                reply = acc.reply()
                if reply.state == "TASK_STATE_FAILED":
                    raise AgentError(
                        f"agent task failed: {reply.message}" if reply.message else "agent task failed"
                    )
                if reply.state not in FINAL_STATES:
                    raise AgentUnavailable(
                        f"stream ended while task was '{reply.state or 'unknown'}'"
                    )
                return reply
            except AgentError as exc:
                # Only an unavailable transport, before any text reached the
                # caller, is worth one more attempt. `AgentAuthError` is an
                # `AgentError` but not an `AgentUnavailable`, so a bad token
                # fails immediately.
                if isinstance(exc, AgentUnavailable) and attempt == 1 and not delivered:
                    logger.warning("a2a: retrying stream after %s", exc)
                    continue
                raise

        raise AgentUnavailable("stream failed after one retry")

    async def get_task(self, task_id: str) -> dict:
        """`GetTask`: the stored task, including the artifact, as JSON."""
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "GetTask",
            # The v1.0 proto field is `id`; the server rejects `taskId` with
            # "Validation failed: field 'id' is required" (measured).
            "params": {"id": task_id},
        }
        response = await self._post(body, accept="application/json")
        data = self._json(response)
        if "error" in data:
            raise AgentError(f"GetTask failed: {data['error']}")
        result = data.get("result") or {}
        # Measured: v1.0 returns the Task object directly as the result, not
        # `{"task": {...}}`. Accept both so a future envelope does not break us.
        return result.get("task") or result

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            return response.json()
        except ValueError as exc:
            raise AgentError(f"agent returned non-JSON: {response.text[:200]}") from exc

    def _read_response(self, response: httpx.Response) -> AgentReply:
        data = self._json(response)
        if "error" in data:
            error = data["error"] or {}
            raise AgentError(f"agent error {error.get('code')}: {error.get('message')}")
        result = data.get("result") or {}
        acc = _Accumulator()
        acc.feed(result)
        reply = acc.reply()
        if reply.state == "TASK_STATE_FAILED":
            detail = self._failure_text(result) or reply.message
            raise AgentError(
                f"agent task failed: {detail}" if detail else "agent task failed"
            )
        return reply

    @staticmethod
    def _failure_text(result: dict) -> str:
        task = result.get("task") or {}
        status_message = (task.get("status") or {}).get("message") or {}
        text = "".join(
            part.get("text") or "" for part in status_message.get("parts") or []
        )
        if text:
            return text[:300]
        for message in reversed(task.get("history") or []):
            for part in message.get("parts") or []:
                if part.get("text"):
                    return str(part["text"])[:300]
        return ""

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# --------------------------------------------------------------------- modules
_writer: AgentClient | None = None
_critic: AgentClient | None = None


def writer_client() -> AgentClient:
    global _writer
    if _writer is None:
        _writer = AgentClient(settings.A2A_WRITER_URL)
    return _writer


def critic_client() -> AgentClient:
    global _critic
    if _critic is None:
        _critic = AgentClient(settings.A2A_CRITIC_URL)
    return _critic


async def write_report_remote(
    *,
    question: str,
    plan: dict | None,
    registry: list[dict],
    max_tokens: int | None = None,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
) -> AgentReply:
    """Stream a report from the writer agent, forwarding each chunk."""
    return await writer_client().stream(
        {
            "question": question,
            "plan": plan or {},
            "registry": registry,
            "max_tokens": max_tokens,
        },
        on_delta=on_delta,
    )


async def critique_remote(
    *, question: str, sub_questions: list[str], digest: str, max_gaps: int = 3
) -> tuple[dict, int, str]:
    """Ask the critic agent for a verdict. Returns `(verdict, tokens, model)`."""
    reply = await critic_client().call(
        {
            "question": question,
            "sub_questions": sub_questions,
            "digest": digest,
            "max_gaps": max_gaps,
        }
    )
    try:
        verdict = json.loads(reply.text)
    except json.JSONDecodeError as exc:
        raise AgentError(f"critic returned non-JSON: {reply.text[:200]}") from exc
    if not isinstance(verdict, dict) or "sufficient" not in verdict:
        raise AgentError(f"critic verdict is missing 'sufficient': {verdict}")
    return verdict, reply.tokens, reply.model


async def agent_health() -> dict:
    """Best-effort card fetch for both agents, for `/healthz`.

    Concurrent: two agents that are both down should cost one connect timeout,
    not two, and `/healthz` is polled.
    """
    async def probe(name: str, factory) -> tuple[str, str]:
        try:
            card = await factory().card(refresh=True)
            return name, f"ok ({card.name})"
        except Exception as exc:  # noqa: BLE001
            return name, f"down ({type(exc).__name__}: {exc})"[:200]

    pairs = await asyncio.gather(
        probe("writer", writer_client), probe("critic", critic_client)
    )
    return dict(pairs)


async def close_agent_clients() -> None:
    for client in (_writer, _critic):
        if client is not None:
            await client.aclose()


def reset_agent_clients() -> None:
    """Tests only: forget cached clients and cards."""
    global _writer, _critic
    _writer = None
    _critic = None
