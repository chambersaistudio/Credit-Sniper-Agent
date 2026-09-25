"""
The operator control plane.

Production QA now runs over HTTPS — driven by an agent, read on a phone —
instead of from a shell on the deployed box. That trade is only acceptable if
the surface is genuinely narrow, so what is asserted here is mostly what the
control plane CANNOT do:

  * it cannot be reached without the credential, and the credential is not an
    ordinary user
  * it exposes a fixed list of named operations and no command, code, query,
    environment or filesystem path
  * one idempotency key buys one job, so a retried request cannot re-purchase
    provider work
  * a benchmark runs exactly one config, reaches exactly one model, banks
    nothing, and restores production defaults even when it fails
  * Sol costs an explicit acknowledgement
  * failures surface a class and a message we wrote, never a provider's
  * no consumer identity and no secret reaches a response

No network, no provider spend.
"""
import uuid

import httpx
import pytest

from app.services.ai import AIProviderError, AIResponseError, ProviderUsage, register_provider
from app.services.ai.providers import _instances
from app.services.operator.auth import reset_rate_limits
from app.services.operator.registry import BENCHMARK_CONFIGS, OPERATIONS
from tests.conftest import drain_operator_queue, requires_db
from tests.test_batch_extraction import BatchProvider, _pdf
from tests.test_operator_commands import production_index

pytestmark = requires_db

AGENT_TOKEN = "op_test_" + "z" * 40
AGENT_HEADERS = {"X-Operator-Token": AGENT_TOKEN}

# Ground truth for b0. Synthetic — the production truth holds the consumer's
# real accounts and is seeded through the API, never committed here.
B0_TRUTH = {"accounts": [
    {"creditor_name": "TRADELINE 1", "account_number": "1001", "balance": "$1,204"},
    {"creditor_name": "TRADELINE 2", "account_number": "1002", "balance": "$1,204"},
    {"creditor_name": "TRADELINE 3", "account_number": "1003", "balance": "$1,204"},
    {"creditor_name": "TRADELINE 4", "account_number": "1004", "balance": "$1,204"},
]}


@pytest.fixture
def operator_env(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "operator_agent_token", AGENT_TOKEN)
    monkeypatch.setattr(settings, "operator_agent_label", "codex")
    monkeypatch.setattr(settings, "auth_mode", "disabled")
    reset_rate_limits()
    yield settings
    reset_rate_limits()


@pytest.fixture
async def client(db_ready, operator_env):
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        yield c


@pytest.fixture
def batch_ai():
    saved = dict(_instances)
    holder = {}

    def install(**kwargs):
        holder["p"] = BatchProvider(**kwargs)
        register_provider("openai", holder["p"])
        return holder["p"]

    yield install
    _instances.clear()
    _instances.update(saved)


async def _seed_report(with_index=True):
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport
    from app.models.user import User
    from app.services.storage import get_storage, report_key

    async with async_session_maker() as db:
        # Unique per call: a test may seed more than one report.
        user = User(email=f"op-{uuid.uuid4().hex[:8]}@example.com", full_name="Consumer Name",
                    address="88 Birch Lane", ssn_last_four="4321")
        db.add(user)
        await db.flush()
        report = CreditReport(
            user_id=user.id, bureau="experian", source="manual_upload",
            raw_text="JANE Q CONSUMER 987-65-4321 88 Birch Lane",
            extraction_status="extraction_incomplete",
            parsed_data={"personal_info": {"name": "JANE Q CONSUMER",
                                           "ssn": "987-65-4321",
                                           "address": "88 Birch Lane"}},
            extraction_checkpoint=(
                {"index": production_index().model_dump(mode="json"),
                 "index_model": "gpt-5.6-luna"} if with_index else None
            ),
        )
        db.add(report)
        await db.flush()
        report.storage_key = report_key(user.id, report.id)
        await get_storage().put(report.storage_key, _pdf(28), "application/pdf")
        await db.commit()
        return str(report.id)


# ── Authentication ──────────────────────────────────────────────────────

OPERATOR_GETS = ["/api/operator/operations", "/api/operator/whoami",
                 "/api/operator/reports", "/api/operator/jobs"]


async def test_every_operator_route_rejects_an_unauthenticated_request(client, operator_env):
    """auth_mode is 'disabled' for this fixture — the mode that resolves
    consumer requests to a local user. The operator surface must still refuse
    a caller with no credential."""
    from app.config import settings

    settings.auth_mode = "jwt"
    try:
        for path in OPERATOR_GETS:
            assert (await client.get(path)).status_code == 401, path
        posted = await client.post("/api/operator/jobs/benchmark-batch", json={
            "report_id": str(uuid.uuid4()), "config": "A", "truth": B0_TRUTH,
        })
        assert posted.status_code == 401
    finally:
        settings.auth_mode = "disabled"


@pytest.mark.parametrize("headers", [
    {"X-Operator-Token": "wrong"},
    {"X-Operator-Token": ""},
    {"X-Operator-Token": AGENT_TOKEN[:-1]},          # near miss
    {"X-Operator-Token": AGENT_TOKEN + "x"},         # prefix of a valid token
    {"Authorization": "Bearer not-a-jwt"},
])
async def test_a_wrong_credential_is_refused(client, operator_env, headers):
    from app.config import settings

    settings.auth_mode = "jwt"
    try:
        response = await client.get("/api/operator/whoami", headers=headers)
        assert response.status_code == 401
        # The refusal says nothing about why, so it cannot be used to test
        # candidate secrets by their error message.
        assert response.json()["detail"] == "Not authenticated"
    finally:
        settings.auth_mode = "disabled"


async def test_an_unset_agent_token_cannot_be_matched_by_an_empty_one(client, operator_env):
    from app.config import settings

    settings.operator_agent_token = ""
    settings.auth_mode = "jwt"
    try:
        for headers in ({"X-Operator-Token": ""}, {}, {"X-Operator-Token": " "}):
            assert (await client.get("/api/operator/whoami", headers=headers)).status_code == 401
    finally:
        settings.auth_mode = "disabled"


async def test_the_agent_credential_is_never_echoed(client, operator_env):
    response = await client.get("/api/operator/whoami", headers=AGENT_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body == {"principal": "agent:codex", "kind": "agent"}
    assert AGENT_TOKEN not in response.text
    # Not even a fragment long enough to be useful.
    assert AGENT_TOKEN[:16] not in response.text


async def test_the_agent_credential_grants_no_consumer_privileges(client, operator_env):
    """It authenticates an operator, not a user. It must not open the
    consumer API, where it would be acting as somebody."""
    from app.config import settings

    settings.auth_mode = "jwt"
    try:
        for path in ("/api/reports/", "/api/accounts/", "/api/cases/", "/api/users/me",
                     "/api/dashboard"):
            response = await client.get(path, headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
            assert response.status_code == 401, f"{path} accepted the operator token"
    finally:
        settings.auth_mode = "disabled"


async def test_the_operator_surface_exposes_no_general_purpose_capability(client, operator_env):
    """The allowlist is the security boundary, so it is asserted rather than
    assumed: no shell, no eval, no SQL, no env, no file read."""
    from app.main import app

    paths = {r.path for r in app.routes if "/api/operator" in getattr(r, "path", "")}
    for forbidden in ("exec", "shell", "command", "run", "sql", "query", "eval",
                      "env", "config", "file", "download", "upload", "pdf"):
        assert not any(forbidden in p.lower() for p in paths), f"{forbidden} route exists"

    listed = {op["operation"] for op in (await client.get(
        "/api/operator/operations", headers=AGENT_HEADERS)).json()}
    assert listed == set(OPERATIONS)
    assert listed == {"benchmark_batch", "diagnose_report", "inspect_checkpoint",
                      "batch_plan", "extraction_status"}


async def test_rate_limiting_applies_per_principal(client, operator_env):
    operator_env.operator_rate_limit_per_minute = 5
    for _ in range(5):
        assert (await client.get("/api/operator/whoami", headers=AGENT_HEADERS)).status_code == 200
    assert (await client.get("/api/operator/whoami", headers=AGENT_HEADERS)).status_code == 429


# ── Free reads ──────────────────────────────────────────────────────────

async def test_the_batch_plan_is_free_and_read_only(client, operator_env):
    report_id = await _seed_report()
    response = await client.get(f"/api/operator/reports/{report_id}/batch-plan",
                                headers=AGENT_HEADERS)
    assert response.status_code == 200
    plan = response.json()
    assert [b["batch_id"] for b in plan["batches"]] == ["b0", "b1", "b2", "b3"]
    assert plan["pages_sent"] == 23
    assert plan["pages_if_whole_document"] == 112
    assert plan["reduction_pct"] == 79.5
    # No job was created: a free read answers directly.
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []


async def test_a_report_without_an_index_says_so_rather_than_failing_obscurely(client, operator_env):
    report_id = await _seed_report(with_index=False)
    response = await client.get(f"/api/operator/reports/{report_id}/batch-plan",
                                headers=AGENT_HEADERS)
    assert response.status_code == 409
    assert "index" in response.json()["detail"].lower()


async def test_checkpoint_and_diagnosis_report_shape_not_contents(client, operator_env):
    report_id = await _seed_report()
    checkpoint = (await client.get(f"/api/operator/reports/{report_id}/checkpoint",
                                   headers=AGENT_HEADERS)).json()
    assert checkpoint["index"]["tradelines"] == 15
    assert checkpoint["index"]["total_pages"] == 28
    # Shape, not the tradelines themselves.
    assert "tradelines" not in str(checkpoint["index"].get("quality", {}))

    diagnosis = (await client.get(f"/api/operator/reports/{report_id}/diagnosis",
                                  headers=AGENT_HEADERS)).json()
    assert diagnosis["extraction_status"] == "extraction_incomplete"
    assert diagnosis["ai_usage"] == []


async def test_no_consumer_identity_reaches_an_operator_response(client, operator_env):
    """The seeded report carries a name, an address and an SSN in exactly the
    places a careless serializer would pick them up."""
    report_id = await _seed_report()
    for path in (f"/api/operator/reports/{report_id}/checkpoint",
                 f"/api/operator/reports/{report_id}/diagnosis",
                 f"/api/operator/reports/{report_id}/batch-plan",
                 "/api/operator/reports"):
        body = (await client.get(path, headers=AGENT_HEADERS)).text
        for pii in ("JANE Q CONSUMER", "987-65-4321", "88 Birch Lane",
                    "Consumer Name", "4321", "%PDF"):
            assert pii not in body, f"{pii!r} leaked from {path}"
        assert "storage_key" not in body
        assert "raw_text" not in body


# ── Queueing a benchmark ────────────────────────────────────────────────

async def _queue(client, report_id, config="A", **overrides):
    body = {"report_id": report_id, "batch_id": "b0", "config": config, "truth": B0_TRUTH}
    body.update(overrides)
    return await client.post("/api/operator/jobs/benchmark-batch", json=body,
                             headers=AGENT_HEADERS)


async def test_queueing_returns_202_without_running_anything(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()
    response = await _queue(client, report_id)

    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert job["created"] is True
    assert job["model"] == "gpt-5.6-luna"
    assert job["max_model_calls"] == 1
    # Nothing expensive happened inside the request.
    assert provider.calls == []

    assert await drain_operator_queue() == ["succeeded"]
    finished = (await client.get(f"/api/operator/jobs/{job['job_id']}",
                                 headers=AGENT_HEADERS)).json()
    assert finished["status"] == "succeeded"
    assert finished["model_calls_made"] == 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize("config,model", sorted(
    (c, m) for c, (m, _) in BENCHMARK_CONFIGS.items()
))
async def test_each_config_reaches_exactly_one_model(client, operator_env, batch_ai,
                                                     config, model):
    provider = batch_ai()
    report_id = await _seed_report()
    response = await _queue(client, report_id, config=config, acknowledge_expensive=True)
    assert response.status_code == 202
    await drain_operator_queue()

    assert len(provider.calls) == 1, "one benchmark is one provider call"
    assert provider.calls[0]["model"] == model
    assert provider.calls[0]["detail"] == "high"

    job = (await client.get(f"/api/operator/jobs/{response.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["result"]["model"] == model
    assert job["result"]["config"] == config


async def test_sol_requires_an_explicit_acknowledgement(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()

    refused = await _queue(client, report_id, config="C")
    assert refused.status_code == 400
    assert "acknowledge_expensive" in refused.json()["detail"]
    assert provider.calls == [], "a refused request must not reach the provider"
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []

    allowed = await _queue(client, report_id, config="C", acknowledge_expensive=True)
    assert allowed.status_code == 202


async def test_luna_and_terra_need_no_acknowledgement(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    for config in ("A", "B"):
        assert (await _queue(client, report_id, config=config)).status_code == 202


async def test_there_is_no_way_to_ask_for_more_than_one_config(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    for attempt in ({"config": "all"}, {"config": ["A", "B"]}, {"config": "A,B"},
                    {"config": "*"}, {"config": "D"}):
        response = await _queue(client, report_id, **attempt)
        assert response.status_code == 422, f"{attempt} was accepted"


async def test_an_unknown_batch_is_refused_before_a_job_exists(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()
    response = await _queue(client, report_id, batch_id="b99")
    assert response.status_code == 404
    assert provider.calls == []
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []


async def test_truth_is_required(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    response = await client.post("/api/operator/jobs/benchmark-batch", headers=AGENT_HEADERS,
                                 json={"report_id": report_id, "config": "A",
                                       "truth": {"accounts": []}})
    assert response.status_code == 422


# ── Idempotency ─────────────────────────────────────────────────────────

async def test_a_repeated_idempotency_key_never_buys_the_work_twice(client, operator_env,
                                                                    batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()

    first = await _queue(client, report_id, idempotency_key="b0-luna-1")
    assert first.json()["created"] is True
    await drain_operator_queue()
    assert len(provider.calls) == 1

    again = await _queue(client, report_id, idempotency_key="b0-luna-1")
    assert again.status_code == 202
    assert again.json()["created"] is False
    assert again.json()["job_id"] == first.json()["job_id"]
    assert await drain_operator_queue() == []
    assert len(provider.calls) == 1, "the same key purchased the work twice"


async def test_a_reused_key_with_a_different_request_is_a_conflict(client, operator_env,
                                                                   batch_ai):
    """Silently returning the old job would answer a question nobody asked —
    corrected truth is a different question from the truth it was run with."""
    provider = batch_ai()
    report_id = await _seed_report()
    await _queue(client, report_id, idempotency_key="b0-luna")
    await drain_operator_queue()

    corrected = {"accounts": [{**B0_TRUTH["accounts"][0],
                               "status_raw": "Voluntarily surrendered. $7,684 past due as of Sep 2026."}]}
    response = await _queue(client, report_id, idempotency_key="b0-luna", truth=corrected)
    assert response.status_code == 409
    assert "different request" in response.json()["detail"]
    assert len(provider.calls) == 1


async def test_two_configs_under_one_key_is_a_conflict_not_a_silent_swap(client, operator_env,
                                                                         batch_ai):
    batch_ai()
    report_id = await _seed_report()
    await _queue(client, report_id, config="A", idempotency_key="b0")
    response = await _queue(client, report_id, config="B", idempotency_key="b0")
    assert response.status_code == 409


# ── What a benchmark must not do ────────────────────────────────────────

async def test_a_benchmark_banks_nothing(client, operator_env, batch_ai):
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    batch_ai()
    report_id = await _seed_report()
    await _queue(client, report_id)
    await drain_operator_queue()

    async with async_session_maker() as db:
        report = await db.get(CreditReport, uuid.UUID(report_id))
        assert (report.extraction_checkpoint or {}).get("batches") in (None, {})

    job = (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json()[0]
    assert job["result"]["banked_batches_unchanged"] is True
    assert job["result"]["banked_batches"] == []


async def test_production_defaults_are_restored_after_a_successful_benchmark(
    client, operator_env, batch_ai
):
    from app.config import settings

    batch_ai()
    before = (settings.ai_document_extraction_model, settings.document_extraction_detail)
    report_id = await _seed_report()
    await _queue(client, report_id, config="B")
    await drain_operator_queue()
    assert (settings.ai_document_extraction_model,
            settings.document_extraction_detail) == before


async def test_production_defaults_are_restored_after_a_failed_benchmark(
    client, operator_env, batch_ai
):
    """The case that matters: a failure must not leave production pointing at
    a benchmark's model."""
    from app.config import settings

    batch_ai(error=AIProviderError("OpenAI API error 429: insufficient_quota"))
    before = (settings.ai_document_extraction_model, settings.document_extraction_detail)
    report_id = await _seed_report()
    await _queue(client, report_id, config="C", acknowledge_expensive=True)
    assert await drain_operator_queue() == ["failed"]
    assert (settings.ai_document_extraction_model,
            settings.document_extraction_detail) == before


async def test_a_benchmark_cannot_exceed_its_model_call_budget(db_ready, operator_env, batch_ai):
    """The ceiling is metered, not trusted: a handler that called twice would
    fail the job rather than quietly spend twice."""
    from app.database import async_session_maker
    from app.services.operator import jobs as jobs_module
    from app.services.operator.jobs import enqueue, run_job, claim_next

    provider = batch_ai()
    report_id = await _seed_report()
    original = jobs_module._HANDLERS["benchmark_batch"]

    async def greedy(db, job, request):
        await original(db, job, request)
        return await original(db, job, request)   # a second paid call

    jobs_module._HANDLERS["benchmark_batch"] = greedy
    try:
        async with async_session_maker() as db:
            job, _ = await enqueue(db, operation="benchmark_batch",
                                   request={"report_id": report_id, "batch_id": "b0",
                                            "config": "A", "truth": B0_TRUTH},
                                   requested_by="agent:test", report_id=report_id)
            await db.commit()
            job_id = job.id
        await claim_next()
        assert await run_job(job_id) == "failed"
    finally:
        jobs_module._HANDLERS["benchmark_batch"] = original

    assert len(provider.calls) == 2  # it happened...
    async with async_session_maker() as db:
        from app.models.operator_job import OperatorJob
        stored = await db.get(OperatorJob, job_id)
        assert stored.model_calls_made == 2      # ...and was recorded...
        assert stored.status == "failed"         # ...and refused.


# ── Results ─────────────────────────────────────────────────────────────

async def test_the_result_carries_the_full_benchmark_measurement(client, operator_env,
                                                                 batch_ai):
    batch_ai()
    report_id = await _seed_report()
    response = await _queue(client, report_id)
    await drain_operator_queue()
    result = (await client.get(f"/api/operator/jobs/{response.json()['job_id']}",
                               headers=AGENT_HEADERS)).json()["result"]

    assert result["accounts"]["matched"] == result["accounts"]["asked"] == 4
    assert result["field_accuracy"]["accuracy"] is not None
    assert "payment_history" in result and "by_account" in result["payment_history"]
    assert result["provenance"]["accuracy"] is not None
    assert "gate" in result and "ok" in result["gate"]
    assert result["bundle"]["pages"] == [2, 3, 4, 5, 6, 7]
    assert result["remap"]["remapped"] > 0


async def test_payment_history_misses_reach_the_operator_result(client, operator_env, batch_ai):
    """The measurement the phone UI renders, and the one that distinguishes
    two models."""
    from app.services.document_extraction.schema import PaymentHistoryEntry

    provider = batch_ai()
    original = provider.generate_document

    async def with_history(config, **kwargs):
        result = await original(config, **kwargs)
        for account in result.output.accounts:
            account.payment_history = [
                PaymentHistoryEntry(year=2026, month=m, raw_status_code="30" if m == 4 else "OK",
                                    status_code=None, balance=None, past_due=None,
                                    amount_paid=None, amount_due=None, remarks=[], source_page=1)
                for m in (3, 4)
            ]
        return result

    provider.generate_document = with_history
    report_id = await _seed_report()
    truth = {"accounts": [{**a, "payment_history": {"2026-03": "OK", "2026-04": "OK",
                                                    "2026-05": "OK"}}
                          for a in B0_TRUTH["accounts"]]}
    response = await _queue(client, report_id, truth=truth)
    await drain_operator_queue()

    history = (await client.get(f"/api/operator/jobs/{response.json()['job_id']}",
                                headers=AGENT_HEADERS)).json()["result"]["payment_history"]
    assert history["correct"] == 4 and history["total"] == 12
    misses = {(m["account"], m["month"]): m for m in history["misses"]}
    assert misses[("TRADELINE 1", "2026-04")]["got"] == "30"
    assert misses[("TRADELINE 1", "2026-04")]["missing"] is False
    assert misses[("TRADELINE 1", "2026-05")]["missing"] is True
    assert history["by_account"]["TRADELINE 1"] == {"expected": 3, "extracted": 2, "correct": 1}


@pytest.mark.parametrize("error,expected_class,fragment", [
    (AIProviderError("OpenAI API error 429: insufficient_quota"),
     "AIProviderError", "nothing was billed"),
    (AIResponseError("Document response incomplete: max_output_tokens; spent 32000",
                     usage=ProviderUsage(input_tokens=9, output_tokens=32_000, max_tokens=32_000)),
     "AIResponseError", "output budget"),
])
async def test_failures_surface_a_safe_message_and_never_the_providers(
    client, operator_env, batch_ai, error, expected_class, fragment
):
    batch_ai(error=error)
    report_id = await _seed_report()
    response = await _queue(client, report_id)
    assert await drain_operator_queue() == ["failed"]

    job = (await client.get(f"/api/operator/jobs/{response.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["status"] == "failed"
    assert job["error"]["class"] == expected_class
    assert fragment in job["error"]["message"]
    assert job["result"] is None
    # The provider's own wording stays in the server log.
    for leak in ("openai", "429", "insufficient_quota", "max_output_tokens", "32000"):
        assert leak not in job["error"]["message"].lower()


async def test_jobs_can_be_listed_for_one_report(client, operator_env, batch_ai):
    batch_ai()
    first, second = await _seed_report(), await _seed_report()
    await _queue(client, first, config="A")
    await _queue(client, second, config="B")
    await drain_operator_queue()

    for_first = (await client.get(f"/api/operator/jobs?report_id={first}",
                                  headers=AGENT_HEADERS)).json()
    assert len(for_first) == 1
    assert for_first[0]["report_id"] == first
    assert len((await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json()) == 2


async def test_benchmark_truth_is_never_written_to_the_application_log(
    client, operator_env, batch_ai, caplog
):
    """Truth holds the consumer's real account values in production."""
    import logging

    batch_ai()
    report_id = await _seed_report()
    secret_value = "Voluntarily surrendered. $7,684 past due as of Sep 2026."
    truth = {"accounts": [{**B0_TRUTH["accounts"][0], "status_raw": secret_value}]}
    with caplog.at_level(logging.DEBUG):
        await _queue(client, report_id, truth=truth)
        await drain_operator_queue()
    assert secret_value not in caplog.text


# ── The consumer product is untouched ───────────────────────────────────

async def test_the_operator_surface_changes_nothing_about_consumer_upload(client, operator_env):
    """The control plane is additive. Upload still enqueues and returns 202,
    and nothing about it now requires or accepts an operator credential."""
    from tests.conftest import drain_extraction_queue
    from tests.test_api_flow import EQUIFAX
    from tests.test_api_flow import _pdf as text_pdf

    response = await client.post(
        "/api/reports/upload",
        files={"file": ("r.pdf", text_pdf(EQUIFAX), "application/pdf")},
        data={"bureau": "equifax"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["processing_stage"] == "queued"
    assert "job_id" not in body, "upload must not have become an operator job"

    await drain_extraction_queue()
    detail = (await client.get(f"/api/reports/{body['report_id']}")).json()
    assert detail["processing"] is False
    # And an operator job was not created as a side effect of a consumer action.
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []


async def test_a_consumer_endpoint_does_not_accept_the_operator_header(client, operator_env):
    """Presenting the machine credential to the consumer API must not
    authenticate anybody."""
    from app.config import settings

    settings.auth_mode = "jwt"
    try:
        response = await client.get("/api/reports/", headers=AGENT_HEADERS)
        assert response.status_code == 401
    finally:
        settings.auth_mode = "disabled"


async def test_the_operator_worker_is_off_unless_its_setting_is_on(operator_env):
    """A paid queue that drains itself because a module was imported would be
    the worst possible default."""
    from app.config import settings

    assert settings.operator_worker_enabled is False, "tests must not drain the paid queue"
