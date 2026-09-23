import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import accounts, cases, dashboard, reports, users
from app.config import settings
from app.database import engine, run_migrations
from app.services.ai import (
    AIConfigurationError, AIError, AIRefusalError, AIResponseError, add_usage_listener,
)
from app.services.case_state_machine import InvalidTransition
from app.services.usage_sink import persist_usage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_VERSION = "1.5.0"

add_usage_listener(persist_usage)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.run_migrations_on_startup:
        await run_migrations()
    if not settings.auth_enabled:
        # Loud, because a hosted deployment in this mode serves every user's
        # data to anyone who can reach it. Fine for local/dev only.
        logger.warning(
            "AUTH IS DISABLED (AUTH_MODE=%s): all requests resolve to one local user. "
            "Do not expose this deployment to the internet with real data.",
            settings.auth_mode,
        )
    yield


app = FastAPI(title="Credit Sniper", version=APP_VERSION, lifespan=lifespan)

# Auth is a bearer token in the Authorization header (not a cookie), so
# cross-origin credentials stay off. Origins are an explicit allow-list plus
# an optional regex for Vercel preview deployments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.allowed_origin_regex or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Token"],
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
    """Liveness: the process is up. Touches nothing else."""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/health/ready")
async def ready():
    """Readiness: the API can reach Postgres and the schema is migrated.
    Use this as the platform health check so a broken database fails the deploy."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as e:
        logger.warning("Readiness check failed: %s", e)
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable or not migrated"})
    return {
        "status": "ok",
        "version": APP_VERSION,
        "database": "ok",
        "schema_revision": revision,
        "storage_backend": settings.storage_backend,
        "ai_configured": bool(settings.anthropic_api_key or settings.openai_api_key),
        # Surfaced so the deploy check can confirm auth is on before real data
        # is uploaded. "jwt" = enforced; "disabled" = single local user.
        "auth_mode": settings.auth_mode,
    }


@app.post("/api/migrate")
async def migrate(x_admin_token: str | None = Header(default=None)):
    """Manual migration trigger. Disabled unless ADMIN_TOKEN is configured, and requires it."""
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
