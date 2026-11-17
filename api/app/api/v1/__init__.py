from fastapi import APIRouter

from app.api.v1 import auth, health, notes, research, tools

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(health.router)
api_router.include_router(research.router)
api_router.include_router(notes.router)
api_router.include_router(tools.router)
