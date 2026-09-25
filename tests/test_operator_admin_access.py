"""
Who counts as an operator when the credential is a signed-in session.

The operator surface is the one place in this codebase where being signed in
and being allowed are different questions. It spends money, and it reads the
telemetry of every report rather than only the caller's own — so if "signed in"
were sufficient, anyone who could create an account could also read another
consumer's creditor names and queue paid model calls against their report.

The rule asserted here is fail-closed: while auth is on, an empty allowlist
admits nobody. A deployment that forgot to configure its operators is reachable
only with the machine credential, which is the safe way to be wrong.

No network, no provider spend.
"""
import httpx
import pytest

from app.services.operator.auth import reset_rate_limits
from tests.conftest import drain_operator_queue, requires_db
from tests.test_auth import KID, _headers, _private_key  # noqa: F401
from tests.test_operator_api import (  # noqa: F401
    AGENT_HEADERS, AGENT_TOKEN, B0_TRUTH, _seed_report, batch_ai,
)

pytestmark = requires_db

OWNER = "owner@example.com"
STRANGER = "stranger@example.com"

# Free reads, so a refusal here costs nothing and proves nothing was spent.
OPERATOR_PATHS = ["/api/operator/whoami", "/api/operator/operations", "/api/operator/reports"]


@pytest.fixture
def jwt_operator_env(monkeypatch):
    from app import auth
    from app.config import settings

    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "auth_issuer", "https://test.clerk.accounts.dev")
    monkeypatch.setattr(settings, "auth_audience", "")
    monkeypatch.setattr(settings, "operator_agent_token", AGENT_TOKEN)
    monkeypatch.setattr(settings, "operator_agent_label", "codex")
    monkeypatch.setattr(settings, "operator_admin_emails", "")
    auth.jwks_cache.seed(KID, _private_key.public_key())
    reset_rate_limits()
    yield settings
    reset_rate_limits()


@pytest.fixture
async def client(db_ready, jwt_operator_env):
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


async def test_a_signed_in_user_is_not_an_operator_by_default(client):
    """The gap this closes: every account that can sign up could otherwise
    spend money here and read every report's telemetry."""
    headers = _headers("user_stranger", email=STRANGER)
    # The same token is a perfectly good consumer credential.
    assert (await client.get("/api/users/me", headers=headers)).status_code == 200
    for path in OPERATOR_PATHS:
        res = await client.get(path, headers=headers)
        assert res.status_code == 401, f"{path} -> {res.status_code}"


async def test_an_allowlisted_operator_gets_in(client, jwt_operator_env):
    jwt_operator_env.operator_admin_emails = OWNER
    headers = _headers("user_owner", email=OWNER)
    for path in OPERATOR_PATHS:
        assert (await client.get(path, headers=headers)).status_code == 200, path
    who = (await client.get("/api/operator/whoami", headers=headers)).json()
    assert who["kind"] == "admin"


async def test_the_allowlist_is_case_insensitive_and_tolerates_spacing(client, jwt_operator_env):
    """Email is case-insensitive, and a list typed into a settings field has
    spaces in it."""
    jwt_operator_env.operator_admin_emails = f" {OWNER.upper()} , someone-else@example.com "
    headers = _headers("user_owner", email=OWNER)
    assert (await client.get("/api/operator/whoami", headers=headers)).status_code == 200


async def test_a_non_listed_user_stays_out_when_others_are_listed(client, jwt_operator_env):
    jwt_operator_env.operator_admin_emails = OWNER
    headers = _headers("user_stranger", email=STRANGER)
    assert (await client.get("/api/operator/whoami", headers=headers)).status_code == 401


async def test_the_refusal_does_not_reveal_who_is_on_the_list(client, jwt_operator_env):
    """A distinct message would turn this endpoint into a way to enumerate the
    operators."""
    jwt_operator_env.operator_admin_emails = OWNER
    refused = await client.get("/api/operator/whoami",
                               headers=_headers("user_stranger", email=STRANGER))
    anonymous = await client.get("/api/operator/whoami")
    assert refused.status_code == anonymous.status_code == 401
    assert refused.json() == anonymous.json()
    for response in (refused, anonymous):
        assert OWNER not in response.text


async def test_the_machine_credential_is_unaffected_by_the_allowlist(client, jwt_operator_env):
    """The agent is not a user and is not on any user list."""
    jwt_operator_env.operator_admin_emails = ""
    who = (await client.get("/api/operator/whoami", headers=AGENT_HEADERS)).json()
    assert who["kind"] == "agent"


async def test_a_refused_operator_cannot_queue_a_paid_job(client, jwt_operator_env):
    """The point of the gate: money."""
    jwt_operator_env.operator_admin_emails = OWNER
    res = await client.post(
        "/api/operator/jobs/benchmark-batch",
        json={"report_id": "00000000-0000-0000-0000-000000000000", "batch_id": "b0",
              "config": "A"},
        headers=_headers("user_stranger", email=STRANGER),
    )
    assert res.status_code == 401


async def test_auth_disabled_still_works_for_local_development(db_ready, monkeypatch):
    """With auth off there is one fixed local user and nobody to keep out; that
    mode is already documented as dev/test only."""
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "auth_mode", "disabled")
    monkeypatch.setattr(settings, "operator_admin_emails", "")
    reset_rate_limits()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        assert (await c.get("/api/operator/whoami")).status_code == 200
    reset_rate_limits()


# ── The phone path, end to end ──────────────────────────────────────────

async def test_the_whole_phone_sequence_works_without_the_machine_credential(
    client, jwt_operator_env, batch_ai,  # noqa: F811
):
    """Exactly the calls the mobile Operator page makes, in order, as a
    signed-in operator holding no machine token.

    This is the sequence that has to work before anyone runs a paid benchmark
    from a phone, and the one thing it must refuse along the way is scoring a
    model against truth nobody has confirmed.
    """
    batch_ai()
    jwt_operator_env.operator_admin_emails = OWNER
    headers = _headers("user_owner", email=OWNER)
    report_id = await _seed_report()

    # The page's initial loads.
    assert (await client.get("/api/operator/reports", headers=headers)).status_code == 200
    plan = await client.get(f"/api/operator/reports/{report_id}/batch-plan", headers=headers)
    assert [b["batch_id"] for b in plan.json()["batches"]] == ["b0", "b1", "b2", "b3"]
    assert (await client.get(f"/api/operator/reports/{report_id}/checkpoint",
                             headers=headers)).status_code == 200
    # Nothing stored yet, and the listing says so without carrying any values.
    assert (await client.get(f"/api/operator/reports/{report_id}/truth",
                             headers=headers)).json() == []

    # Truth is entered once, through the API, and is not verified by saving it.
    saved = await client.put(f"/api/operator/reports/{report_id}/batches/b0/truth",
                             json=B0_TRUTH, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["verified"] is False

    # A paid run is refused while the truth is unconfirmed, before any job
    # exists and before any money moves.
    refused = await client.post("/api/operator/jobs/benchmark-batch",
                                json={"report_id": report_id, "batch_id": "b0", "config": "A"},
                                headers=headers)
    assert refused.status_code == 409
    assert (await client.get("/api/operator/jobs", headers=headers)).json() == []

    # Confirming is its own tap.
    verified = await client.post(
        f"/api/operator/reports/{report_id}/batches/b0/truth/verify", headers=headers)
    assert verified.json()["verified"] is True
    listing = (await client.get(f"/api/operator/reports/{report_id}/truth",
                                headers=headers)).json()
    assert [(t["batch_id"], t["verified"], t["account_count"]) for t in listing] == [("b0", True, 4)]
    # The listing is status, not data: no account value appears in it.
    assert "TRADELINE 1" not in str(listing)

    # Now the run goes, carrying a reference rather than the values.
    queued = await client.post("/api/operator/jobs/benchmark-batch",
                               json={"report_id": report_id, "batch_id": "b0", "config": "A"},
                               headers=headers)
    assert queued.status_code == 202
    assert queued.json()["requested_by"].startswith("user:")
    assert await drain_operator_queue() == ["succeeded"]

    job = (await client.get(f"/api/operator/jobs/{queued.json()['job_id']}",
                            headers=headers)).json()
    assert job["status"] == "succeeded"
    assert job["model_calls_made"] == 1
    # The job holds the reference and the fingerprint, never a second copy of
    # the account values.
    assert "truth" not in job["request"]
    assert job["request"]["truth_fingerprint"]
    assert job["result"]["accounts"]["asked"] == 4
