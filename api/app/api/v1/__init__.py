from fastapi import APIRouter

from app.api.v1 import admin, auth, health, library, notes, reports, research, tools, usage

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(health.router)
api_router.include_router(research.router)
api_router.include_router(notes.router)
api_router.include_router(reports.router)
api_router.include_router(tools.router)
api_router.include_router(usage.router)
api_router.include_router(library.router)
api_router.include_router(admin.router)

