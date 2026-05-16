import sys
import os

# Make backend importable from Vercel's serverless context
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from mangum import Mangum
from app.main import app

# Mangum wraps the ASGI app for Vercel's serverless runtime.
# lifespan="off" because Vercel functions don't support startup/shutdown events;
# table creation is handled via the /api/migrate endpoint or Alembic.
handler = Mangum(app, lifespan="off")
