from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_vercel_requirements_match_backend():
    # Vercel installs api/requirements.txt; the backend is developed against
    # backend/requirements.txt. They drifted once (the deploy was pinned to a
    # stale SDK) — keep them identical.
    backend = (ROOT / "backend" / "requirements.txt").read_text().split()
    vercel = (ROOT / "api" / "requirements.txt").read_text().split()
    assert backend == vercel
