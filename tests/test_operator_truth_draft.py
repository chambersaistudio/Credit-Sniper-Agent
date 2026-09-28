"""
Drafting benchmark truth when there is nothing banked to draft from.

The first phone benchmark stalled here. The Experian report had a banked
Stage-1 index and no banked Stage-2 batch — the earlier b0 read was a
benchmark, and benchmarks bank nothing by design — so the free "draft from
banked" had nothing to read, the benchmark refused because no verified truth
existed, and the only way forward was typing four accounts of payment grid into
raw JSON on a phone.

`draft_truth_batch` closes that gap with one paid read of one batch, stored as
an UNVERIFIED draft. What is asserted here is mostly what it must not do:

* bank anything, or change the report's extraction
* count as truth before a human has verified it
* overwrite truth someone already stored — refused before any money moves,
  and again after the read if a correction landed in the meantime
* leave a draft behind under a job that failed
* hide that a model is being scored against truth it drafted itself

No network, no provider spend.
"""
import dataclasses
import json
import uuid


from app.services.operator.registry import OPERATIONS
from tests.conftest import drain_operator_queue, requires_db
from tests.test_batch_extraction import BatchProvider
from tests.test_operator_api import (  # noqa: F401
    AGENT_HEADERS, _seed_report, batch_ai, client, operator_env,
)
from tests.test_operator_truth import CREDIT_ACCEPTANCE, _put, _queue

pytestmark = requires_db


async def _draft(client, report_id, **overrides):
    body = {"report_id": report_id, "batch_id": "b0", "config": "A"}
    body.update(overrides)
    return await client.post("/api/operator/jobs/draft-truth", json=body, headers=AGENT_HEADERS)


async def _truth(client, report_id, batch_id="b0"):
    return await client.get(f"/api/operator/reports/{report_id}/batches/{batch_id}/truth",
                            headers=AGENT_HEADERS)


async def _job(client, job_id):
    return (await client.get(f"/api/operator/jobs/{job_id}", headers=AGENT_HEADERS)).json()


async def _checkpoint_batches(report_id):
    from app.database import async_session_maker
    from app.models.credit_report import CreditReport

    async with async_session_maker() as db:
        report = await db.get(CreditReport, uuid.UUID(report_id))
        return (report.extraction_checkpoint or {}).get("batches") or {}


# ── The path the phone needed ────────────────────────────────────────────

async def test_a_batch_with_nothing_banked_can_be_drafted_by_one_paid_read(
    client, operator_env, batch_ai,
):
    provider = batch_ai()
    report_id = await _seed_report()
    assert await _checkpoint_batches(report_id) == {}

    queued = await _draft(client, report_id)
    assert queued.status_code == 202
    assert queued.json()["model"] == "gpt-5.6-luna"
    assert await drain_operator_queue() == ["succeeded"]

    assert len(provider.calls) == 1, "one draft is one model call"
    job = await _job(client, queued.json()["job_id"])
    assert job["model_calls_made"] == 1
    assert job["result"]["accounts_drafted"] == 4
    assert job["result"]["months_drafted"] > 0, "the payment grid is drafted too"
    assert job["result"]["verified"] is False

    stored = (await _truth(client, report_id)).json()
    assert stored["verified"] is False
    assert stored["source"] == "drafted_from_model"
    assert stored["drafted_by_model"] == "gpt-5.6-luna"
    assert stored["drafted_by_config"] == "A"
    assert len(stored["accounts"]) == 4
    assert all(row.get("payment_history") for row in stored["accounts"])


async def test_drafting_banks_nothing(client, operator_env, batch_ai):
    """A draft is a read, not the report's extraction. It must not become
    Stage-2 progress by the back door."""
    batch_ai()
    report_id = await _seed_report()
    queued = await _draft(client, report_id)
    await drain_operator_queue()

    assert await _checkpoint_batches(report_id) == {}
    assert (await _job(client, queued.json()["job_id"]))["result"]["banked_batches_unchanged"]


async def test_the_job_result_carries_counts_not_account_values(client, operator_env, batch_ai):
    """The values live in the truth store and are fetched only to be corrected.
    The job listing, polled every few seconds on a phone, holds none of them."""
    batch_ai()
    report_id = await _seed_report()
    queued = await _draft(client, report_id)
    await drain_operator_queue()

    job = await _job(client, queued.json()["job_id"])
    text = json.dumps(job["result"])
    for value in ("TRADELINE 1", "1001", "$1,204"):
        assert value not in text


# ── A draft is not truth ─────────────────────────────────────────────────

async def test_a_drafted_batch_still_cannot_be_benchmarked_until_verified(
    client, operator_env, batch_ai,
):
    provider = batch_ai()
    report_id = await _seed_report()
    await _draft(client, report_id)
    await drain_operator_queue()
    calls_after_draft = len(provider.calls)

    refused = await _queue(client, report_id)
    assert refused.status_code == 409
    assert len(provider.calls) == calls_after_draft

    await client.post(f"/api/operator/reports/{report_id}/batches/b0/truth/verify",
                      headers=AGENT_HEADERS)
    assert (await _queue(client, report_id)).status_code == 202


# ── Never over someone's work ────────────────────────────────────────────

async def test_a_draft_is_refused_before_spending_when_truth_already_exists(
    client, operator_env, batch_ai,
):
    provider = batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id)

    refused = await _draft(client, report_id)
    assert refused.status_code == 409
    assert "Correct it instead" in refused.json()["detail"]
    assert provider.calls == []
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []


async def test_truth_saved_after_queueing_wins_and_the_draft_spends_nothing(
    client, operator_env, batch_ai,
):
    """Checked again at run time, BEFORE the model call."""
    provider = batch_ai()
    report_id = await _seed_report()
    queued = await _draft(client, report_id)
    assert queued.status_code == 202
    await _put(client, report_id)  # saved by hand while the job waited

    assert await drain_operator_queue() == ["failed"]
    job = await _job(client, queued.json()["job_id"])
    assert job["error"]["class"] == "TruthExists"
    assert job["model_calls_made"] == 0
    assert provider.calls == []
    assert (await _truth(client, report_id)).json()["source"] == "operator"


class _SavesTruthMidRead(BatchProvider):
    """A provider that, while 'reading', lets someone save truth by hand."""

    def __init__(self, on_read, **kw):
        super().__init__(**kw)
        self.on_read = on_read

    async def generate_document(self, *a, **k):
        result = await super().generate_document(*a, **k)
        await self.on_read()
        return result


async def test_a_correction_saved_while_the_model_reads_is_kept(client, operator_env):
    """The read is paid for by then, but a human's corrections are worth more
    than a model's draft: the draft is discarded, never written over them."""
    from app.services.ai import register_provider
    from app.services.ai.providers import _instances

    report_id = await _seed_report()
    saved = dict(_instances)
    provider = _SavesTruthMidRead(lambda: _put(client, report_id))
    register_provider("openai", provider)
    try:
        queued = await _draft(client, report_id)
        assert await drain_operator_queue() == ["failed"]
    finally:
        _instances.clear()
        _instances.update(saved)

    job = await _job(client, queued.json()["job_id"])
    assert job["error"]["class"] == "TruthExists"
    assert job["model_calls_made"] == 1  # honest about what was spent
    stored = (await _truth(client, report_id)).json()
    assert stored["source"] == "operator"
    assert stored["accounts"][0]["creditor_name"] == CREDIT_ACCEPTANCE["creditor_name"]


async def test_the_free_draft_from_banked_no_longer_overwrites_stored_truth(
    client, operator_env, batch_ai,
):
    """The same rule, found on the free path while building the paid one: it
    upserted straight over whatever was stored, corrections included."""
    from app.services.batch_job import run_report_batch

    batch_ai()
    report_id = await _seed_report()
    await run_report_batch(uuid.UUID(report_id), "b0")
    await _put(client, report_id, verified=True)

    refused = await client.post(
        f"/api/operator/reports/{report_id}/batches/b0/truth/draft", headers=AGENT_HEADERS)
    assert refused.status_code == 409
    stored = (await _truth(client, report_id)).json()
    assert stored["source"] == "operator" and stored["verified"] is True


# ── A failed job leaves nothing behind ───────────────────────────────────

async def test_a_draft_written_by_a_job_that_then_fails_is_rolled_back(
    client, operator_env, batch_ai, monkeypatch,
):
    """The runner checks the call budget AFTER the handler returns. A handler
    that wrote, followed by a budget failure, used to be committed anyway by
    the runner's `finally` — a draft under a job that says it failed."""
    batch_ai()
    report_id = await _seed_report()
    # Authorised for Terra only; the job asks for Luna. The meter catches it
    # after the handler has already written the draft.
    monkeypatch.setitem(OPERATIONS, "draft_truth_batch", dataclasses.replace(
        OPERATIONS["draft_truth_batch"], models=("gpt-5.6-terra",)))

    queued = await _draft(client, report_id)
    assert await drain_operator_queue() == ["failed"]
    assert (await _job(client, queued.json()["job_id"]))["status"] == "failed"
    assert (await _truth(client, report_id)).status_code == 404


# ── Cost guardrails, as for benchmarks ───────────────────────────────────

async def test_sol_needs_an_explicit_acknowledgement(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    assert (await _draft(client, report_id, config="C")).status_code == 400
    assert (await _draft(client, report_id, config="C",
                         acknowledge_expensive=True)).status_code == 202


async def test_one_idempotency_key_buys_one_draft(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()
    first = await _draft(client, report_id, idempotency_key="b0-draft-1")
    second = await _draft(client, report_id, idempotency_key="b0-draft-1")
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["created"] is False
    await drain_operator_queue()
    assert len(provider.calls) == 1


async def test_a_batch_that_does_not_exist_is_refused_before_a_job_exists(
    client, operator_env, batch_ai,
):
    batch_ai()
    report_id = await _seed_report()
    assert (await _draft(client, report_id, batch_id="b9")).status_code == 404
    no_index = await _seed_report(with_index=False)
    assert (await _draft(client, no_index)).status_code == 409


# ── Scoring a model against truth it drafted ─────────────────────────────

async def test_corrections_keep_the_drafting_model_on_record(client, operator_env, batch_ai):
    """Correcting a draft anchors on it, so the lineage outlives the edit."""
    batch_ai()
    report_id = await _seed_report()
    await _draft(client, report_id)
    await drain_operator_queue()

    corrected = await _put(client, report_id)
    body = corrected.json()
    assert body["source"] == "operator"
    assert body["verified"] is False
    assert body["drafted_by_model"] == "gpt-5.6-luna"
    listing = (await client.get(f"/api/operator/reports/{report_id}/truth",
                                headers=AGENT_HEADERS)).json()
    assert listing[0]["drafted_by_model"] == "gpt-5.6-luna"


async def test_a_model_scored_against_its_own_draft_is_flagged(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    await _draft(client, report_id, config="A")
    await drain_operator_queue()
    await client.post(f"/api/operator/reports/{report_id}/batches/b0/truth/verify",
                      headers=AGENT_HEADERS)

    luna = await _queue(client, report_id, config="A")
    terra = await _queue(client, report_id, config="B")
    assert await drain_operator_queue() == ["succeeded", "succeeded"]

    luna_result = (await _job(client, luna.json()["job_id"]))["result"]
    terra_result = (await _job(client, terra.json()["job_id"]))["result"]
    assert luna_result["truth_drafted_by_model"] == "gpt-5.6-luna"
    assert "drafted by gpt-5.6-luna" in luna_result["anchoring_warning"]
    assert terra_result["anchoring_warning"] is None


async def test_truth_entered_by_hand_carries_no_anchoring_warning(
    client, operator_env, batch_ai,
):
    batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    queued = await _queue(client, report_id)
    await drain_operator_queue()
    result = (await _job(client, queued.json()["job_id"]))["result"]
    assert result["truth_drafted_by_model"] is None
    assert result["anchoring_warning"] is None
