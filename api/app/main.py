from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.checkpointer import close_checkpointer, init_checkpointer
from app.agents.graph import init_graph
from app.agents.mcp_client import close_toolbox, init_toolbox
from app.api.v1 import api_router
from app.core.cache import close_page_cache, init_page_cache
from app.core.config import settings
from app.core.embeddings import close_embeddings
from app.core.log import configure_logging
from app.core.observability import get_langfuse, shutdown_langfuse
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
    await init_report_stream()  # soft-fail: reports generate without live streaming
    await reconcile_stale_jobs()  # background tasks do not survive a restart
    await reconcile_stale_reports()
    yield
    await cancel_all()      # stop in-flight research before closing clients
    await cancel_all_reports()
    await close_toolbox()
    await close_page_cache()
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/")
async def root() -> dict:
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}
