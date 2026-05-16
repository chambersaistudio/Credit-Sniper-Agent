import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.config import settings
from app.database import create_tables
from app.api import reports, disputes, letters, users

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Credit Sniper Agent...")
    await create_tables()
    logger.info("Database tables ready.")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Credit Sniper Agent",
    description="Autonomous AI credit dispute agent — Phase 1 MVP",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reports.router)
app.include_router(disputes.router)
app.include_router(letters.router)
app.include_router(users.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "phase": "MVP — Phase 1"}


@app.get("/api/dashboard/stats")
async def dashboard_stats():
    """Quick stats for the dashboard — will be replaced with real DB queries."""
    return {
        "total_reports": 0,
        "active_disputes": 0,
        "resolved_disputes": 0,
        "pending_approvals": 0,
        "estimated_score_gain": 0,
    }


# Serve frontend static files
frontend_dir = os.path.join(os.path.dirname(__file__), "../../frontend/dist")
if os.path.exists(frontend_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index = os.path.join(frontend_dir, "index.html")
        return FileResponse(index)
