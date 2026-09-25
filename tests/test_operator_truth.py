"""
Server-side benchmark truth.

Truth is real account data, and it was previously only accepted inline in the
benchmark request — which on a phone means typing four accounts of payment grid
into a textarea, and means the values pass through a clipboard and a form every
single run. It is now stored once and referenced.

Two rules carry real weight and are asserted rather than described:

* **Unverified truth cannot be benchmarked against.** A draft prefilled from a
  model's own extraction is a convenience. Scoring against it would measure
  agreement with that model rather than correctness — worse than no
  measurement, because it looks like one.
* **A job runs against the truth it was queued for.** Correcting the truth
  after queueing fails the job instead of quietly answering a different
  question.

No network, no provider spend.
"""
import uuid

import pytest

from app.services.operator import truth as truth_service
from tests.conftest import drain_operator_queue, requires_db
from tests.test_operator_api import (  # noqa: F401
    AGENT_HEADERS, B0_TRUTH, _seed_report, batch_ai, client, operator_env,
)

pytestmark = requires_db

CREDIT_ACCEPTANCE = {
    "creditor_name": "CREDIT ACCEPTANCE CORP",
    "account_number": "7788XXXX",
    # The lesson from 4a58cf3: the full printed field, both clauses.
    "status_raw": "Voluntarily surrendered. $7,684 past due as of Sep 2026.",
    "source_pages": [5],
    "payment_history": {"2026-05": "OK", "2026-04": "OK"},
}


def _truth_body(**overrides):
    body = {"accounts": [CREDIT_ACCEPTANCE]}
    body.update(overrides)
    return body


async def _put(client, report_id, batch_id="b0", **overrides):
    return await client.put(
        f"/api/operator/reports/{report_id}/batches/{batch_id}/truth",
        json=_truth_body(**overrides), headers=AGENT_HEADERS,
    )


async def _queue(client, report_id, **overrides):
    body = {"report_id": report_id, "batch_id": "b0", "config": "A"}
    body.update(overrides)
    return await client.post("/api/operator/jobs/benchmark-batch", json=body,
                            headers=AGENT_HEADERS)


# ── Validation ──────────────────────────────────────────────────────────

def test_a_misspelled_field_is_refused_rather_than_silently_unscored():
    """`ballance` would store fine and score nothing, and the benchmark would
    report the field as unmeasured rather than as a typo."""
    with pytest.raises(truth_service.TruthError) as caught:
        truth_service.validate({"accounts": [{"creditor_name": "X", "ballance": "$16"}]})
    assert "ballance" in str(caught.value)
    assert "nothing would score them" in str(caught.value)


@pytest.mark.parametrize("payload,fragment", [
    ({"accounts": []}, "at least one account"),
    ({"accounts": [{"account_number": "1"}]}, "no creditor_name"),
    ({"accounts": [{"creditor_name": "  "}]}, "no creditor_name"),
    ({"accounts": [{"creditor_name": "X", "payment_history": ["2026-01"]}]}, "YYYY-MM map"),
    ({"accounts": [{"creditor_name": "X", "payment_history": {"2026-13": "OK"}}]}, "YYYY-MM"),
    ({"accounts": [{"creditor_name": "X", "payment_history": {"Jan 2026": "OK"}}]}, "YYYY-MM"),
    ("not an object", "accounts' array"),
])
def test_malformed_truth_is_refused(payload, fragment):
    with pytest.raises(truth_service.TruthError) as caught:
        truth_service.validate(payload)
    assert fragment in str(caught.value)


def test_valid_truth_including_the_full_printed_status_field():
    validated = truth_service.validate({"accounts": [CREDIT_ACCEPTANCE]})
    assert validated["accounts"][0]["status_raw"].endswith("as of Sep 2026.")
    assert truth_service.fingerprint(validated) == truth_service.fingerprint(validated)


def test_the_fingerprint_changes_when_a_value_is_corrected():
    before = truth_service.fingerprint({"accounts": [CREDIT_ACCEPTANCE]})
    corrected = {**CREDIT_ACCEPTANCE, "status_raw": "Voluntarily surrendered."}
    assert truth_service.fingerprint({"accounts": [corrected]}) != before


# ── Storing and referencing ─────────────────────────────────────────────

async def test_truth_is_stored_once_and_then_referenced(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()

    saved = await _put(client, report_id, verified=True)
    assert saved.status_code == 200
    assert saved.json()["verified"] is True
    assert saved.json()["account_count"] == 1

    # The benchmark now needs no truth in the request at all.
    queued = await _queue(client, report_id)
    assert queued.status_code == 202, queued.text
    await drain_operator_queue()

    job = (await client.get(f"/api/operator/jobs/{queued.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["status"] == "succeeded"
    assert job["result"]["truth_label"] == "current"
    assert len(provider.calls) == 1


async def test_the_job_stores_a_reference_not_a_second_copy_of_the_values(
    client, operator_env, batch_ai
):
    """One authoritative copy. The job carries the label and a fingerprint."""
    batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    queued = await _queue(client, report_id)

    job = (await client.get(f"/api/operator/jobs/{queued.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["request"]["truth_inline"] is False
    assert job["request"]["truth_fingerprint"]
    assert "truth" not in job["request"]
    assert "7788XXXX" not in str(job["request"])


async def test_correcting_truth_resets_verification(client, operator_env):
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)

    corrected = await _put(client, report_id, accounts=[
        {**CREDIT_ACCEPTANCE, "status_raw": "Voluntarily surrendered."}
    ])
    assert corrected.status_code == 200
    # The confirmation was of the previous values.
    assert corrected.json()["verified"] is False


async def test_verifying_is_a_separate_deliberate_act(client, operator_env):
    report_id = await _seed_report()
    saved = await _put(client, report_id)
    assert saved.json()["verified"] is False

    verified = await client.post(
        f"/api/operator/reports/{report_id}/batches/b0/truth/verify",
        headers=AGENT_HEADERS)
    assert verified.status_code == 200
    assert verified.json()["verified"] is True


async def test_the_report_listing_carries_status_but_no_account_values(client, operator_env):
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)

    listing = await client.get(f"/api/operator/reports/{report_id}/truth",
                              headers=AGENT_HEADERS)
    assert listing.status_code == 200
    row = listing.json()[0]
    assert (row["batch_id"], row["account_count"], row["months"]) == ("b0", 1, 2)
    assert row["verified"] is True
    # A report overview must not carry the data it is describing.
    assert "7788XXXX" not in listing.text
    assert "Voluntarily surrendered" not in listing.text


# ── Unverified truth is refused ─────────────────────────────────────────

async def test_a_benchmark_against_unverified_truth_is_refused(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id)             # saved, not verified

    refused = await _queue(client, report_id)
    assert refused.status_code == 409
    assert "not verified" in refused.json()["detail"]
    assert provider.calls == [], "a refused request must not reach the provider"
    assert (await client.get("/api/operator/jobs", headers=AGENT_HEADERS)).json() == []


async def test_a_benchmark_with_no_stored_truth_says_so(client, operator_env, batch_ai):
    provider = batch_ai()
    report_id = await _seed_report()
    refused = await _queue(client, report_id)
    assert refused.status_code == 404
    assert "save it first" in refused.json()["detail"]
    assert provider.calls == []


# ── Drafting from a banked batch ────────────────────────────────────────

async def _bank_b0(client, report_id, batch_ai):
    from app.services.batch_job import run_report_batch

    batch_ai()
    await run_report_batch(uuid.UUID(report_id), "b0")


async def test_a_draft_prefills_the_accounts_and_starts_unverified(
    client, operator_env, batch_ai
):
    """The phone affordance: correct what the model read rather than typing
    four accounts out. Explicitly NOT truth until a human says so."""
    report_id = await _seed_report()
    await _bank_b0(client, report_id, batch_ai)

    drafted = await client.post(
        f"/api/operator/reports/{report_id}/batches/b0/truth/draft", headers=AGENT_HEADERS)
    assert drafted.status_code == 200
    body = drafted.json()
    assert body["account_count"] == 4
    assert body["verified"] is False
    assert body["source"] == "drafted_from_batch"
    assert "Correct it against the document" in body["warning"]
    assert body["accounts"][0]["creditor_name"]


async def test_a_draft_cannot_be_benchmarked_against_until_verified(
    client, operator_env, batch_ai
):
    """The circularity guard. Scoring a model against its own output measures
    self-consistency and nothing else, so the gate is structural."""
    report_id = await _seed_report()
    await _bank_b0(client, report_id, batch_ai)
    await client.post(f"/api/operator/reports/{report_id}/batches/b0/truth/draft",
                      headers=AGENT_HEADERS)

    provider = batch_ai()
    refused = await _queue(client, report_id)
    assert refused.status_code == 409
    assert "not verified" in refused.json()["detail"]
    assert provider.calls == []

    # Once a human has checked it, it runs.
    await client.post(f"/api/operator/reports/{report_id}/batches/b0/truth/verify",
                      headers=AGENT_HEADERS)
    assert (await _queue(client, report_id)).status_code == 202


async def test_drafting_from_a_batch_that_was_never_banked_is_refused(
    client, operator_env, batch_ai
):
    batch_ai()
    report_id = await _seed_report()
    drafted = await client.post(
        f"/api/operator/reports/{report_id}/batches/b0/truth/draft", headers=AGENT_HEADERS)
    assert drafted.status_code == 409
    assert "no banked extraction" in drafted.json()["detail"]


# ── A job runs against the truth it was queued for ──────────────────────

async def test_truth_corrected_after_queueing_fails_the_job(client, operator_env, batch_ai):
    """Otherwise the result would silently answer a different question than the
    one the operator asked."""
    provider = batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    queued = await _queue(client, report_id)
    assert queued.status_code == 202

    # Corrected before the worker gets to it.
    await _put(client, report_id, verified=True,
               accounts=[{**CREDIT_ACCEPTANCE, "balance": "$1"}])

    assert await drain_operator_queue() == ["failed"]
    job = (await client.get(f"/api/operator/jobs/{queued.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["error"]["class"] == "TruthChanged"
    assert "Queue a new job" in job["error"]["message"]
    assert provider.calls == []


async def test_a_corrected_truth_under_a_reused_key_is_still_a_conflict(
    client, operator_env, batch_ai
):
    """The fingerprint travels with the job, so reference-based truth keeps the
    protection that inline truth had."""
    batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    first = await _queue(client, report_id, idempotency_key="b0-luna")
    assert first.json()["created"] is True
    await drain_operator_queue()

    await _put(client, report_id, verified=True,
               accounts=[{**CREDIT_ACCEPTANCE, "status_raw": "Voluntarily surrendered."}])
    again = await _queue(client, report_id, idempotency_key="b0-luna")
    assert again.status_code == 409
    assert "different request" in again.json()["detail"]


async def test_the_same_truth_under_a_reused_key_still_reuses_the_job(
    client, operator_env, batch_ai
):
    provider = batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    first = await _queue(client, report_id, idempotency_key="b0-luna")
    await drain_operator_queue()

    again = await _queue(client, report_id, idempotency_key="b0-luna")
    assert again.status_code == 202
    assert again.json()["created"] is False
    assert again.json()["job_id"] == first.json()["job_id"]
    assert len(provider.calls) == 1


# ── Inline truth still works for the CLI ────────────────────────────────

async def test_inline_truth_remains_available(client, operator_env, batch_ai):
    """A file is the natural source on a command line; the store is for the
    phone and the agent."""
    provider = batch_ai()
    report_id = await _seed_report()
    queued = await _queue(client, report_id, truth=B0_TRUTH)
    assert queued.status_code == 202
    await drain_operator_queue()

    job = (await client.get(f"/api/operator/jobs/{queued.json()['job_id']}",
                            headers=AGENT_HEADERS)).json()
    assert job["status"] == "succeeded"
    assert job["request"]["truth_inline"] is True
    assert len(provider.calls) == 1


# ── Labels ──────────────────────────────────────────────────────────────

async def test_two_labels_can_coexist_for_one_batch(client, operator_env, batch_ai):
    batch_ai()
    report_id = await _seed_report()
    await _put(client, report_id, verified=True)
    await _put(client, report_id, label="strict", verified=True,
               accounts=[{**CREDIT_ACCEPTANCE, "payment_history_complete": True}])

    listing = (await client.get(f"/api/operator/reports/{report_id}/truth",
                                headers=AGENT_HEADERS)).json()
    assert sorted(row["label"] for row in listing) == ["current", "strict"]
    assert (await _queue(client, report_id, truth_label="strict")).status_code == 202


async def test_truth_is_scoped_to_its_report(client, operator_env):
    first, second = await _seed_report(), await _seed_report()
    await _put(client, first, verified=True)

    assert (await client.get(f"/api/operator/reports/{second}/batches/b0/truth",
                             headers=AGENT_HEADERS)).status_code == 404
    assert (await client.get(f"/api/operator/reports/{second}/truth",
                             headers=AGENT_HEADERS)).json() == []


async def test_truth_endpoints_require_the_operator_credential(client, operator_env):
    from app.config import settings

    report_id = await _seed_report()
    settings.auth_mode = "jwt"
    try:
        assert (await client.get(f"/api/operator/reports/{report_id}/truth")).status_code == 401
        assert (await client.put(
            f"/api/operator/reports/{report_id}/batches/b0/truth",
            json=_truth_body())).status_code == 401
    finally:
        settings.auth_mode = "disabled"
