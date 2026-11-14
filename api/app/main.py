from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.checkpointer import close_checkpointer, init_checkpointer
from app.agents.graph import init_graph
from app.agents.mcp_client import close_toolbox, init_toolbox
from app.api.v1 import api_router
from app.core.config import settings
from app.core.observability import get_langfuse, shutdown_langfuse
from app.db import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_langfuse()          # instantiate tracing at boot -> bad keys fail loudly
    await init_checkpointer()
    init_graph()
    await init_toolbox()    # spawns the MCP server (soft-fail, see mcp_client)
    yield
    await close_toolbox()
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
