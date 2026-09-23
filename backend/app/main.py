import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import accounts, cases, dashboard, reports, users
from app.config import settings
from app.database import run_migrations
from app.services.ai import (
    AIConfigurationError, AIError, AIRefusalError, AIResponseError, add_usage_listener,
)
from app.services.case_state_machine import InvalidTransition
from app.services.usage_sink import persist_usage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_VERSION = "1.5.0"

# Registered at import (not in lifespan) because serverless deployments
# don't run lifespan hooks.
add_usage_listener(persist_usage)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_migrations()
    yield


app = FastAPI(title="Credit Sniper", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (reports, accounts, cases, dashboard, users):
    app.include_router(module.router)


@app.exception_handler(InvalidTransition)
async def _invalid_transition(request: Request, exc: InvalidTransition):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(AIError)
async def _ai_error(request: Request, exc: AIError):
    logger.warning("AI error on %s: %s", request.url.path, exc)
    if isinstance(exc, AIConfigurationError):
        return JSONResponse(status_code=503, content={"detail": "AI analysis isn't configured on this server."})
    if isinstance(exc, AIRefusalError):
        return JSONResponse(status_code=422, content={"detail": "The AI model declined to evaluate this request."})
    if isinstance(exc, AIResponseError):
        return JSONResponse(status_code=502, content={"detail": "The AI model returned an unusable answer. Try again."})
    return JSONResponse(status_code=502, content={"detail": "The AI provider is unavailable. Try again shortly."})


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/api/migrate")
async def migrate(x_admin_token: str | None = Header(default=None)):
    """Serverless deployments don't run lifespan hooks: call once after each
    deploy. Disabled unless ADMIN_TOKEN is configured, and requires it."""
    if not settings.admin_token or not secrets.compare_digest(x_admin_token or "", settings.admin_token):
        raise HTTPException(status_code=403, detail="Forbidden")
    await run_migrations()
    return {"status": "ok"}


frontend_dir = os.path.join(os.path.dirname(__file__), "../../frontend/dist")
if os.path.exists(frontend_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(os.path.join(frontend_dir, "index.html"))
