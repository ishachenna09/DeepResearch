"""Build a FastAPI app that speaks A2A, with auth in front of the RPC endpoint.

Why bearer auth at the middleware layer rather than inside the executor?
The executor only ever sees a *valid* A2A request. If auth lived there, an
unauthenticated caller would get a task that fails politely - and would still have
been parsed, validated and logged. Rejecting at the edge keeps "who may call"
and "what the agent does" separate, and the card can advertise one scheme.

Why is the card public?
Discovery has to work before authentication: a client that has never talked to us
cannot present a token it has not been told to use. The card contains no data -
only the promise of what the endpoint does.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.types import AgentCard

from app.a2a.cards import rpc_path
from app.a2a.task_store import RedisTaskStore
from app.core.config import settings
from app.core.log import configure_logging

logger = logging.getLogger(__name__)

PUBLIC_PATHS = ("/.well-known/", "/healthz")


def build_agent_app(*, card: AgentCard, executor: AgentExecutor, namespace: str) -> FastAPI:
    # Same rule as the API: without this every logger.info() (the audit trail
    # of a generation) is dropped, and a service that looks silent looks broken.
    configure_logging(settings.LOG_LEVEL)
    app = FastAPI(title=f"{card.name} (A2A)", version=card.version, docs_url=None, redoc_url=None)
    store = RedisTaskStore(namespace)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path.startswith(PUBLIC_PATHS):
            return await call_next(request)

        expected = settings.A2A_TOKEN
        if not expected:
            # Refuse to run open: an agent that writes reports is not a public
            # endpoint, and a silent "no token configured" would be worse than a
            # loud misconfiguration.
            logger.error("%s: A2A_TOKEN is not set - refusing RPC requests", card.name)
            return JSONResponse(
                {"error": "agent token not configured"}, status_code=503
            )

        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        # compare_digest, not ==: a length- or prefix-timing leak on a shared
        # secret is still a leak.
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=store,
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(
            handler,
            rpc_url=rpc_path(card),
            # v0.3 clients (the pre-1.0 method names) hit the same endpoint. It
            # costs nothing and means an older client is not a mystery 404.
            enable_v0_3_compat=True,
        ),
    )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "agent": card.name,
            "version": card.version,
            "skills": [s.id for s in card.skills],
            "modes": [i.protocol_binding for i in card.supported_interfaces],
        }

    return app
