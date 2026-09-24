"""
Authentication and per-user isolation, against a real Postgres
(TEST_DATABASE_URL) with the AI provider faked.

The backend verifies a provider-signed RS256 JWT against the provider's
public keys. These tests stand in for the provider: they generate an RSA
keypair, seed the JWKS cache with the public key (no network), and sign
tokens with the private key. They cover:
  * unauthenticated and invalid/expired tokens are rejected (401)
  * a user is provisioned on first sign-in; distinct subjects are distinct users
  * one user cannot read or write another user's reports, accounts, claims,
    cases, packages, or stored files — changing an id in the URL yields 404,
    never another user's data and never a hint that it exists
  * the full credit-analysis lifecycle works under authentication
"""
import time
from datetime import datetime, timezone
from io import BytesIO

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.conftest import mark_reports_verified, requires_db
from tests.test_api_flow import EQUIFAX, _responder

pytestmark = requires_db

KID = "test-key-1"
ISSUER = "https://test.clerk.accounts.dev"
_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pdf(text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    y = letter[1] - 50
    for line in text.split("\n"):
        c.drawString(50, y, line)
        y -= 14
    c.save()
    return buffer.getvalue()


def _token(sub: str, *, email: str | None = None, exp_offset: int = 3600, issuer: str = ISSUER) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iss": issuer, "iat": now, "exp": now + exp_offset}
    if email:
        claims["email"] = email
    return jwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": KID})


def _headers(sub: str, **kw) -> dict:
    return {"Authorization": f"Bearer {_token(sub, **kw)}"}


@pytest.fixture
def jwt_env(monkeypatch):
    """Turn on jwt auth and seed the provider's public key."""
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "auth_issuer", ISSUER)
    monkeypatch.setattr(settings, "auth_audience", "")
    auth.jwks_cache.seed(KID, _private_key.public_key())
    yield


@pytest.fixture
def ai(fake_ai):
    return fake_ai(_responder)


@pytest.fixture
async def client(db_ready, jwt_env, ai):
    from app.main import app
    from app.services.usage_sink import persist_usage
    from app.services.ai import add_usage_listener

    add_usage_listener(persist_usage)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


PROTECTED_GETS = ["/api/dashboard", "/api/activity", "/api/reports/", "/api/accounts/", "/api/cases/", "/api/users/me"]


async def test_unauthenticated_requests_are_rejected(client):
    for path in PROTECTED_GETS:
        res = await client.get(path)
        assert res.status_code == 401, f"{path} -> {res.status_code}"
    # A write endpoint too.
    upload = await client.post(
        "/api/reports/upload", files={"file": ("r.pdf", _pdf(EQUIFAX), "application/pdf")}
    )
    assert upload.status_code == 401


async def test_malformed_and_expired_tokens_are_rejected(client):
    assert (await client.get("/api/users/me", headers={"Authorization": "Bearer not-a-jwt"})).status_code == 401
    assert (await client.get("/api/users/me", headers={"Authorization": "Basic abc"})).status_code == 401
    expired = _headers("user_x", exp_offset=-10)
    assert (await client.get("/api/users/me", headers=expired)).status_code == 401
    wrong_issuer = _headers("user_x", issuer="https://evil.example.com")
    assert (await client.get("/api/users/me", headers=wrong_issuer)).status_code == 401


async def test_first_sign_in_provisions_distinct_users(client):
    a = (await client.get("/api/users/me", headers=_headers("clerk_user_a", email="a@example.com"))).json()
    b = (await client.get("/api/users/me", headers=_headers("clerk_user_b", email="b@example.com"))).json()
    assert a["id"] != b["id"]
    assert a["email"] == "a@example.com" and b["email"] == "b@example.com"
    # Same subject again → same row, not a second user.
    a_again = (await client.get("/api/users/me", headers=_headers("clerk_user_a"))).json()
    assert a_again["id"] == a["id"]


async def _upload(client, sub, text=EQUIFAX, bureau="auto_detect"):
    res = await client.post(
        "/api/reports/upload",
        files={"file": ("r.pdf", _pdf(text), "application/pdf")},
        data={"bureau": bureau},
        headers=_headers(sub),
    )
    assert res.status_code == 200, res.text
    return res.json()


async def test_users_see_only_their_own_data(client):
    a, b = "user_a", "user_b"
    await _upload(client, a)

    a_reports = (await client.get("/api/reports/", headers=_headers(a))).json()
    b_reports = (await client.get("/api/reports/", headers=_headers(b))).json()
    assert len(a_reports) == 1 and b_reports == []

    a_accounts = (await client.get("/api/accounts/", headers=_headers(a))).json()
    b_accounts = (await client.get("/api/accounts/", headers=_headers(b))).json()
    assert len(a_accounts) >= 1 and b_accounts == []

    a_dash = (await client.get("/api/dashboard", headers=_headers(a))).json()
    b_dash = (await client.get("/api/dashboard", headers=_headers(b))).json()
    assert a_dash["has_reports"] is True and b_dash["has_reports"] is False


async def test_cross_user_access_is_denied_without_leaking_existence(client):
    a, b = "owner", "intruder"
    report = await _upload(client, a)
    report_id = report["report_id"]
    account = (await client.get("/api/accounts/", headers=_headers(a))).json()[0]
    await mark_reports_verified()
    evaluation = (await client.post(f"/api/accounts/{account['id']}/evaluate", headers=_headers(a))).json()
    case = (await client.post(
        "/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "equifax"}, headers=_headers(a)
    )).json()
    case_id = case["id"]

    # Ensure the intruder exists as a user (first sign-in) so requests reach
    # the ownership checks rather than failing earlier.
    await client.get("/api/users/me", headers=_headers(b))

    intruder = _headers(b)
    # Reads: another user's records look exactly like nonexistent ones (404).
    assert (await client.get(f"/api/reports/{report_id}", headers=intruder)).status_code == 404
    assert (await client.get(f"/api/reports/{report_id}/file", headers=intruder)).status_code == 404
    assert (await client.get(f"/api/accounts/{account['id']}", headers=intruder)).status_code == 404
    assert (await client.get(f"/api/cases/{case_id}", headers=intruder)).status_code == 404

    # Writes / actions: same story — no mutation, no disclosure.
    assert (await client.post(f"/api/accounts/{account['id']}/evaluate", headers=intruder)).status_code == 404
    assert (await client.post(
        "/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "experian"}, headers=intruder
    )).status_code == 404
    assert (await client.post(f"/api/cases/{case_id}/package", headers=intruder)).status_code == 404
    assert (await client.post(f"/api/cases/{case_id}/approve", headers=intruder)).status_code == 404
    assert (await client.get(f"/api/cases/{case_id}/package.pdf", headers=intruder)).status_code == 404
    assert (await client.post(
        f"/api/cases/{case_id}/submitted", json={"channel": "certified_mail"}, headers=intruder
    )).status_code == 404
    assert (await client.post(
        f"/api/cases/{case_id}/transition", json={"target": "resolved"}, headers=intruder
    )).status_code == 404

    # The owner is untouched: the case is still there and still in draft.
    owner_case = (await client.get(f"/api/cases/{case_id}", headers=_headers(a))).json()
    assert owner_case["status"] == "draft"


async def test_lifecycle_works_under_authentication(client):
    a = "lifecycle_user"
    report = await _upload(client, a)
    account = (await client.get("/api/accounts/", headers=_headers(a))).json()[0]
    await mark_reports_verified()
    evaluation = (await client.post(f"/api/accounts/{account['id']}/evaluate", headers=_headers(a))).json()
    assert evaluation["has_dispute_ground"]
    case = (await client.post(
        "/api/cases/", json={"claim_ids": [evaluation["id"]], "recipient": "equifax"}, headers=_headers(a)
    )).json()
    assert case["status"] == "draft"

    await client.patch("/api/users/me", json={
        "email": "life@example.com", "full_name": "Life User", "address": "1 Main St",
        "city": "Anytown", "state": "ca", "zip_code": "90210", "ssn_last_four": "1234",
    }, headers=_headers(a))
    case = (await client.post(f"/api/cases/{case['id']}/package", headers=_headers(a))).json()
    assert case["package"]["ready"]
    case = (await client.post(f"/api/cases/{case['id']}/approve", headers=_headers(a))).json()
    assert case["status"] == "approved"
    # The owner can fetch their own report PDF (local storage streams it).
    pdf = await client.get(f"/api/reports/{report['report_id']}/file", headers=_headers(a))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
