from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agents.checkpointer import close_checkpointer, init_checkpointer
from app.agents.graph import init_graph
from app.agents.mcp_client import close_toolbox, init_toolbox
from app.api.v1 import api_router
from app.core.cache import close_page_cache, init_page_cache
from app.core.config import settings
from app.core.embeddings import close_embeddings
from app.core.log import configure_logging
from app.core.middleware import GlobalRateLimitMiddleware, SecurityHeadersMiddleware
from app.core.observability import get_langfuse, shutdown_langfuse
from app.core.ratelimit import close_rate_limiter, init_rate_limiter
from app.core.streams import close_report_stream, init_report_stream
from app.db import engine
from app.services.report_runner import cancel_all_reports, reconcile_stale_reports
from app.services.research_runner import cancel_all, reconcile_stale_jobs

# Before anything else can log: without this, every logger.info() in the graph
# is dropped and only warnings survive (that is how a critic run looked silent).
configure_logging(settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_langfuse()          # instantiate tracing at boot -> bad keys fail loudly
    await init_checkpointer()
    init_graph()
    await init_toolbox()    # spawns the MCP server (soft-fail, see mcp_client)
    await init_page_cache()  # soft-fail: a dead cache means "fetch it again"
    await init_rate_limiter()  # soft-fail: falls back to per-process counters
    await init_report_stream()  # soft-fail: reports generate without live streaming
    await reconcile_stale_jobs()  # background tasks do not survive a restart
    await reconcile_stale_reports()
    yield
    await cancel_all()      # stop in-flight research before closing clients
    await cancel_all_reports()
    # Lazy: `local` mode never imports the A2A client (or its SDK) at all.
    from app.a2a.client import close_agent_clients

    await close_agent_clients()
    await close_toolbox()
    await close_page_cache()
    await close_rate_limiter()
    await close_report_stream()
    await close_embeddings()
    await close_checkpointer()
    shutdown_langfuse()
    await engine.dispose()
    


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
)

# Order matters: Starlette applies the last-added middleware outermost.
#   TrustedHost  -> reject a forged Host before any work
#   CORS         -> so even a 429 carries CORS headers the browser can read
#   SecurityHeaders -> wrap every response, including the limiter's own 429
#   GlobalRateLimit -> the per-IP backstop, innermost so the cheap middlewares
#                      above never do work for a flood
app.add_middleware(GlobalRateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The library downloads exports as blobs and reads the filename from this
    # header. Without expose_headers the browser hides it (CORS safelist) and
    # every download is called "report.pdf".
    expose_headers=["Content-Disposition"],
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

app.include_router(api_router)


@app.get("/")
async def root() -> dict:
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}
