"""
Regressions for the [CODEX → QA] review of 40d461d.

Eleven findings, all verified against the code before patching. Each one is
pinned here by the behaviour it broke rather than by the line it touched, so
the test still means something after the implementation moves.

The theme across the HIGH findings is the same mistake in four places:
check-then-act across an await that costs money. Between "is this already
done?" and "record that it is done" sat a sixty-second model call, and
anything could happen in the gap.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.benchmark.batch_scoring import score_batch
from app.services.document_extraction.batching import plan_batches
from app.services.document_extraction.index_quality import assess_index
from app.services.document_extraction.page_bundle import build_bundle, remap_tradelines
from app.services.document_extraction.schema import PaymentHistoryEntry
from tests.conftest import requires_db
from tests.test_batch_extraction import _pdf, _tradeline, batch_ai  # noqa: F401
from tests.test_index_pass import GOLDEN_INDEX, _entry, golden_index, index_ai  # noqa: F401
from tests.test_operator_commands import production_index


# ── HIGH 5: the index is checked against the real document ──────────────

def test_finding_5_a_page_count_the_document_does_not_have_fails_the_gate():
    """The model's page numbers were taken on trust. They are now checked
    against a local pypdf count — the one fact in the gate that does not
    depend on the model being honest."""
    index = production_index()          # says 28 pages
    assert assess_index(index, actual_page_count=28).ok

    wrong = assess_index(index, actual_page_count=20)
    assert not wrong.ok
    assert any("it has 20" in r for r in wrong.reasons)
    assert wrong.actual_page_count == 20


def test_finding_5_a_source_page_outside_the_document_fails_the_gate():
    index = production_index()
    index.tradelines[0].source_pages = [99]
    quality = assess_index(index, actual_page_count=28)
    assert not quality.ok
    assert quality.out_of_range_pages == [99]
    assert any("outside this 28-page document" in r for r in quality.reasons)


def test_finding_5_the_gate_still_works_with_no_document_to_count():
    """Scoring a stored index, where no PDF is in hand, must not start
    failing for want of a page count."""
    quality = assess_index(production_index())
    assert quality.ok
    assert quality.actual_page_count is None


async def test_finding_5_the_index_pass_counts_the_pages_itself(index_ai):
    from app.services.index_job import index_document

    index_ai(output=golden_index(total_pages=31))   # claims 31
    result = await index_document(_pdf(28))          # document has 28
    assert not result.ok
    assert result.quality.actual_page_count == 28
    assert any("it has 28" in r for r in result.quality.reasons)


# ── HIGH 3: a batch id is an ordinal, not an identity ───────────────────

def test_finding_3_a_banked_batch_is_only_reused_for_the_tradelines_it_was_for():
    from app.services.batch_job import _entry_matches, plan_fingerprint

    plans = plan_batches(production_index())
    b0, b1 = plans[0], plans[1]

    assert _entry_matches({"plan_fingerprint": plan_fingerprint(b0)}, b0)
    # The same ordinal id, different tradelines: reusing this would attach one
    # account's balances to another.
    assert not _entry_matches({"plan_fingerprint": plan_fingerprint(b1)}, b0)
    # An entry from before fingerprints existed falls back to the stored keys.
    assert _entry_matches({"plan": b0.to_dict()}, b0)
    assert not _entry_matches({"plan": b1.to_dict()}, b0)
    # An entry carrying neither is not reusable.
    assert not _entry_matches({}, b0)


def test_finding_3_a_re_indexed_report_invalidates_the_old_fingerprint():
    from app.services.batch_job import plan_fingerprint

    before = plan_fingerprint(plan_batches(production_index())[0])
    # Re-indexing found the tradelines on different pages.
    moved = production_index()
    for tradeline in moved.tradelines:
        tradeline.source_pages = [p + 1 for p in tradeline.source_pages]
    assert plan_fingerprint(plan_batches(moved)[0]) != before


# ── MEDIUM 6: a dropped account must not improve the score ──────────────

def _account(name, pages=(3,), months=("2026-01", "2026-02")):
    account = _tradeline(pages=list(pages), evidence_pages=[], history_pages=[])
    account.creditor_name = name
    account.account_number = "1001"
    account.original_creditor = None
    account.balance = "$16"
    account.payment_history = [
        PaymentHistoryEntry(year=int(m[:4]), month=int(m[5:]), raw_status_code="OK",
                            status_code=None, balance=None, past_due=None, amount_paid=None,
                            amount_due=None, remarks=[], source_page=pages[0])
        for m in months
    ]
    return account


TWO_ACCOUNTS = [
    {"creditor_name": n, "account_number": "1001", "balance": "$16", "source_pages": [3],
     "payment_history": {"2026-01": "OK", "2026-02": "OK"}}
    for n in ("ACCOUNT A", "ACCOUNT B")
]


def test_finding_6_dropping_an_account_scores_worse_than_reading_it():
    """It scored BETTER before: only matched accounts entered the
    denominators, so a model that skipped an account skipped its penalties —
    ranking the models exactly backwards."""
    both = score_batch("b0", "read-both",
                       [_account("ACCOUNT A"), _account("ACCOUNT B")], TWO_ACCOUNTS)
    dropped = score_batch("b0", "dropped-one", [_account("ACCOUNT A")], TWO_ACCOUNTS)

    assert both.fields.accuracy == 1.0
    assert dropped.missing == ["ACCOUNT B"]
    assert dropped.fields.accuracy < both.fields.accuracy
    assert dropped.payment_history.accuracy < both.payment_history.accuracy
    assert dropped.provenance.accuracy < both.provenance.accuracy


def test_finding_6_a_missing_accounts_misses_say_the_account_was_missing():
    dropped = score_batch("b0", "A", [_account("ACCOUNT A")], TWO_ACCOUNTS)
    missing_misses = [m for m in dropped.fields.misses if m.get("account_missing")]
    assert missing_misses, "a dropped account contributes no explanation"
    assert {m["account"] for m in missing_misses} == {"ACCOUNT B"}
    # And its months are counted, so payment accuracy reflects the whole batch.
    assert dropped.months_expected == 4
    assert dropped.months_by_account["ACCOUNT B"] == {"expected": 2, "extracted": 0, "correct": 0}


# ── MEDIUM 7: duplicate and malformed months ────────────────────────────

def test_finding_7_a_month_returned_twice_is_recorded_not_collapsed():
    """Building the month map with a dict comprehension silently kept the
    last cell and discarded the rest."""
    account = _account("ACCOUNT A", months=("2026-01", "2026-01", "2026-02"))
    card = score_batch("b0", "A", [account], [TWO_ACCOUNTS[0]])

    assert card.duplicate_months == [
        {"account": "ACCOUNT A", "month": "2026-01", "first": "OK", "second": "OK"}
    ]
    assert card.payment_history.accuracy < 1.0
    assert any(m.get("malformed") for m in card.payment_history.misses)


def test_finding_7_an_impossible_month_is_malformed_not_ignored():
    account = _account("ACCOUNT A")
    account.payment_history.append(
        PaymentHistoryEntry(year=2026, month=13, raw_status_code="OK", status_code=None,
                            balance=None, past_due=None, amount_paid=None, amount_due=None,
                            remarks=[], source_page=3))
    card = score_batch("b0", "A", [account], [TWO_ACCOUNTS[0]])
    assert card.malformed_months == [
        {"account": "ACCOUNT A", "year": 2026, "month": 13, "code": "OK"}
    ]


def test_finding_7_extra_months_are_counted_and_only_penalised_when_truth_is_complete():
    """A grid can legitimately extend further back than a partial truth was
    written for, so extras are reported by default and scored only when the
    truth declares itself complete."""
    account = _account("ACCOUNT A", months=("2026-01", "2026-02", "2025-12"))

    lenient = score_batch("b0", "A", [account], [TWO_ACCOUNTS[0]])
    assert lenient.extra_months == 1
    assert lenient.payment_history.accuracy == 1.0

    strict_truth = [{**TWO_ACCOUNTS[0], "payment_history_complete": True}]
    strict = score_batch("b0", "A", [account], strict_truth)
    assert strict.payment_history.accuracy < 1.0
    assert any(m.get("not_in_truth") for m in strict.payment_history.misses)


# ── MEDIUM 8: provenance is not "any overlap" ───────────────────────────

def test_finding_8_a_correct_page_plus_invented_ones_is_not_correct_provenance():
    account = _account("ACCOUNT A", pages=(3, 17))     # 3 is right, 17 is not
    card = score_batch("b0", "A", [account], [TWO_ACCOUNTS[0]])

    assert card.provenance.accuracy == 0.0
    assert card.pages_hallucinated == [
        {"account": "ACCOUNT A", "expected": [3], "got": [3, 17], "not_in_truth": [17]}
    ]
    assert card.pages_wrong == []


def test_finding_8_wholly_wrong_pages_stay_a_different_failure_from_invented_ones():
    card = score_batch("b0", "A", [_account("ACCOUNT A", pages=(9,))], [TWO_ACCOUNTS[0]])
    assert card.provenance.accuracy == 0.0
    assert card.pages_wrong == [{"account": "ACCOUNT A", "expected": [3], "got": [9]}]
    assert card.pages_hallucinated == []


def test_finding_8_exactly_the_right_pages_still_passes():
    card = score_batch("b0", "A", [_account("ACCOUNT A", pages=(3,))], [TWO_ACCOUNTS[0]])
    assert card.provenance.accuracy == 1.0


# ── MEDIUM 9: unreadable_pages in original numbering ────────────────────

def test_finding_9_a_failed_batch_stores_original_page_numbers():
    """A failed batch is still written to the checkpoint. Leaving these
    bundle-relative stored "page 1" for what is really page 6, and nothing
    downstream could tell."""
    from app.services.document_extraction.batch_schema import TradelineBatch

    bundle = build_bundle(_pdf(28), [6, 7, 8])
    batch = TradelineBatch(accounts=[], missing_tradelines=[], unreadable_pages=[1, 3])
    remap_tradelines(batch.accounts, bundle, batch)
    assert batch.unreadable_pages == [6, 8]


def test_finding_9_an_unplaceable_unreadable_page_is_dropped_not_guessed():
    from app.services.document_extraction.batch_schema import TradelineBatch

    bundle = build_bundle(_pdf(28), [6, 7])
    batch = TradelineBatch(accounts=[], missing_tradelines=[], unreadable_pages=[1, 42])
    report = remap_tradelines(batch.accounts, bundle, batch)
    assert batch.unreadable_pages == [6]
    assert 42 in report.out_of_range


# ── MEDIUM 10: the benchmark model is an argument, not a global ─────────

def test_finding_10_nothing_in_the_request_path_mutates_global_model_settings():
    """The operator worker and the consumer extraction worker share a process.
    Choosing a model by mutating settings left a window in which an upload was
    read by whichever model a benchmark was testing."""
    from pathlib import Path

    from app.services.operator import handlers

    source = Path(handlers.__file__).read_text()
    for mutation in ("settings.ai_document_extraction_model =",
                     "settings.document_extraction_detail ="):
        assert mutation not in source, f"{mutation} is back in the request path"
    assert "model=model, detail=detail" in source


async def test_finding_10_the_override_reaches_the_provider_without_touching_settings(batch_ai):
    from app.config import settings
    from app.services.batch_job import extract_batch

    provider = batch_ai()
    before = (settings.ai_document_extraction_model, settings.document_extraction_detail)
    plan = plan_batches(production_index())[0]

    await extract_batch(_pdf(28), plan, model="gpt-5.6-terra", detail="low")

    assert provider.calls[0]["model"] == "gpt-5.6-terra"
    assert provider.calls[0]["detail"] == "low"
    # And the process-wide configuration is exactly as it was.
    assert (settings.ai_document_extraction_model, settings.document_extraction_detail) == before


async def test_finding_10_concurrent_reads_do_not_see_each_others_model(batch_ai):
    """The hazard itself: two reads in flight at once, each must get the model
    it asked for."""
    provider = batch_ai()
    plan = plan_batches(production_index())[0]
    document = _pdf(28)

    results = await asyncio.gather(
        extract_batch_wrapper(document, plan, "gpt-5.6-luna"),
        extract_batch_wrapper(document, plan, "gpt-5.6-sol"),
    )
    assert sorted(r.model for r in results) == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert sorted(c["model"] for c in provider.calls) == ["gpt-5.6-luna", "gpt-5.6-sol"]


async def extract_batch_wrapper(document, plan, model):
    from app.services.batch_job import extract_batch

    return await extract_batch(document, plan, model=model, detail="high")


# ── LOW 11: a schema failure describes the failure, not the report ──────

def test_finding_11_a_validation_failure_carries_no_model_output():
    from pydantic import BaseModel, ValidationError

    from app.services.ai.providers import describe_validation_error

    class Account(BaseModel):
        creditor_name: str
        balance: int

    # Bound outside the except block: Python unbinds `as e` on exit.
    raw = summary = None
    try:
        Account.model_validate_json(
            '{"creditor_name": "CAINE & WEINER", "balance": "$1,204 past due, JANE Q CONSUMER"}')
    except ValidationError as error:
        raw, summary = str(error), describe_validation_error(error)
    assert summary is not None, "the invalid payload validated"

    # The shape of the problem survives...
    assert "1 schema error(s)" in summary
    assert "balance" in summary
    assert "int_parsing" in summary
    # ...and the value that broke it does not, though pydantic's own string
    # includes it and that string used to be what we stored.
    for leak in ("JANE Q CONSUMER", "1,204", "$"):
        assert leak in raw, f"{leak} should appear in pydantic's own string"
        assert leak not in summary, f"{leak} survived into the stored failure"


def test_finding_11_many_errors_are_summarised_rather_than_dumped():
    from pydantic import BaseModel, ValidationError

    from app.services.ai.providers import describe_validation_error

    class Row(BaseModel):
        a: int
        b: int
        c: int
        d: int
        e: int
        f: int
        g: int
        h: int

    try:
        Row.model_validate({k: "JANE Q CONSUMER" for k in "abcdefgh"})
    except ValidationError as error:
        summary = describe_validation_error(error)

    assert "8 schema error(s)" in summary
    assert "+2 more" in summary
    assert "JANE Q CONSUMER" not in summary


# ── HIGH 1, 2, 4: claiming and lost updates (need a database) ───────────

@requires_db
class TestConcurrency:
    @staticmethod
    async def _report(with_index=True):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.storage import get_storage, report_key

        async with async_session_maker() as db:
            user = User(email=f"qa-{uuid.uuid4().hex[:8]}@example.com", full_name="QA")
            db.add(user)
            await db.flush()
            report = CreditReport(
                user_id=user.id, bureau="experian", source="manual_upload", raw_text="x",
                extraction_status="extraction_incomplete",
                extraction_checkpoint=({"index": production_index().model_dump(mode="json"),
                                        "index_model": "gpt-5.6-luna"} if with_index else None),
            )
            db.add(report)
            await db.flush()
            report.storage_key = report_key(user.id, report.id)
            await get_storage().put(report.storage_key, _pdf(28), "application/pdf")
            await db.commit()
            return report.id

    @staticmethod
    async def _checkpoint(report_id):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport

        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            return row.extraction_checkpoint or {}

    # ── Finding 1 ───────────────────────────────────────────────────────

    async def test_finding_1_a_second_worker_cannot_buy_the_same_batch(
        self, db_ready, batch_ai
    ):
        """Check-then-call: both workers saw an empty slot and both paid."""
        from app.services.batch_job import BatchAlreadyRunning, run_report_batch

        provider = batch_ai()
        report_id = await self._report()

        results = await asyncio.gather(
            run_report_batch(report_id, "b0"),
            run_report_batch(report_id, "b0"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, BatchAlreadyRunning)]
        succeeded = [r for r in results if not isinstance(r, BaseException)]

        assert len(refused) == 1, "the second worker was not refused"
        assert len(succeeded) == 1
        assert len(provider.calls) == 1, "the same batch was purchased twice"

    async def test_finding_1_a_stale_claim_does_not_block_forever(self, db_ready, batch_ai):
        """A worker that died must not strand the batch."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            row.extraction_checkpoint = {
                **row.extraction_checkpoint,
                "batches_in_flight": {"b0": {
                    "claimed_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}},
            }
            await db.commit()

        result = await run_report_batch(report_id, "b0", lease_seconds=60)
        assert result.ok
        assert len(provider.calls) == 1

    async def test_finding_1_a_failure_releases_the_claim(self, db_ready, batch_ai):
        from app.services.ai import AIProviderError
        from app.services.batch_job import run_report_batch

        batch_ai(error=AIProviderError("OpenAI API error 429: insufficient_quota"))
        report_id = await self._report()
        await run_report_batch(report_id, "b0")

        checkpoint = await self._checkpoint(report_id)
        assert not (checkpoint.get("batches_in_flight") or {}).get("b0"), \
            "a failed batch left its claim behind"

    # ── Finding 2 ───────────────────────────────────────────────────────

    async def test_finding_2_concurrent_batches_do_not_erase_each_other(
        self, db_ready, batch_ai
    ):
        """The lost update. Each call snapshotted the whole batches dict before
        its model call and wrote the snapshot back after, so whichever finished
        second erased the other's paid work."""
        batch_ai()
        report_id = await self._report()

        await asyncio.gather(
            *[__import__("app.services.batch_job", fromlist=["run_report_batch"])
              .run_report_batch(report_id, b) for b in ("b0", "b1", "b2", "b3")]
        )
        banked = (await self._checkpoint(report_id)).get("batches") or {}
        assert sorted(banked) == ["b0", "b1", "b2", "b3"], "a concurrent batch was erased"
        for entry in banked.values():
            assert entry["batch"]["accounts"]

    async def test_finding_2_banking_preserves_a_batch_committed_meanwhile(
        self, db_ready, batch_ai
    ):
        """Explicitly: b1 lands while b0 is mid-call. b0 must merge, not
        overwrite."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        original = provider.generate_document

        async def land_b1_midway(config, **kwargs):
            async with async_session_maker() as db:
                row = await db.get(CreditReport, report_id)
                checkpoint = dict(row.extraction_checkpoint or {})
                row.extraction_checkpoint = {
                    **checkpoint,
                    "batches": {**(checkpoint.get("batches") or {}),
                                "b1": {"batch": {"accounts": [{"creditor_name": "LANDED"}]},
                                       "model": "gpt-5.6-luna"}},
                }
                await db.commit()
            provider.generate_document = original
            return await original(config, **kwargs)

        provider.generate_document = land_b1_midway
        await run_report_batch(report_id, "b0")

        banked = (await self._checkpoint(report_id)).get("batches") or {}
        assert sorted(banked) == ["b0", "b1"]
        assert banked["b1"]["batch"]["accounts"][0]["creditor_name"] == "LANDED"

    # ── Finding 3, end to end ───────────────────────────────────────────

    async def test_finding_3_a_stale_batch_is_not_served_under_a_changed_index(
        self, db_ready, batch_ai
    ):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        await run_report_batch(report_id, "b0")
        assert len(provider.calls) == 1

        # Re-index: b0 now covers different tradelines.
        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            reindexed = production_index()
            reindexed.tradelines = list(reversed(reindexed.tradelines))
            row.extraction_checkpoint = {**row.extraction_checkpoint,
                                         "index": reindexed.model_dump(mode="json")}
            await db.commit()

        await run_report_batch(report_id, "b0")
        assert len(provider.calls) == 2, "stale detail was served under a changed index"

    async def test_finding_3_an_unchanged_index_still_reuses(self, db_ready, batch_ai):
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        await run_report_batch(report_id, "b0")
        again = await run_report_batch(report_id, "b0")
        assert again.reused
        assert len(provider.calls) == 1

    # ── Finding 4 ───────────────────────────────────────────────────────

    async def test_finding_4_extraction_claiming_is_atomic(self, db_ready):
        """A plain SELECT let two API instances claim the same report and both
        pay to read the same document."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.extraction_jobs import claim_next

        async with async_session_maker() as db:
            user = User(email=f"qa-{uuid.uuid4().hex[:8]}@example.com", full_name="QA")
            db.add(user)
            await db.flush()
            for _ in range(3):
                db.add(CreditReport(user_id=user.id, bureau="unknown", source="manual_upload",
                                    raw_text="x", extraction_status="extraction_incomplete",
                                    processing_stage="queued"))
            await db.commit()

        claimed = await asyncio.gather(*[claim_next() for _ in range(6)])
        taken = [c for c in claimed if c is not None]
        assert len(taken) == 3, "a report was claimed twice or not at all"
        assert len(set(taken)) == 3

    async def test_finding_4_a_claimed_report_is_not_reclaimed_within_its_lease(self, db_ready):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.extraction_jobs import claim_next

        async with async_session_maker() as db:
            user = User(email=f"qa-{uuid.uuid4().hex[:8]}@example.com", full_name="QA")
            db.add(user)
            await db.flush()
            db.add(CreditReport(user_id=user.id, bureau="unknown", source="manual_upload",
                                raw_text="x", extraction_status="extraction_incomplete",
                                processing_stage="queued"))
            await db.commit()

        assert await claim_next() is not None
        assert await claim_next() is None, "a live claim was ignored"

    async def test_finding_4_requeueing_releases_the_claim(self, db_ready):
        """Deliberately re-queueing must not have to wait out the lease."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.extraction_jobs import claim_next, enqueue

        async with async_session_maker() as db:
            user = User(email=f"qa-{uuid.uuid4().hex[:8]}@example.com", full_name="QA")
            db.add(user)
            await db.flush()
            report = CreditReport(user_id=user.id, bureau="unknown", source="manual_upload",
                                  raw_text="x", extraction_status="provider_unavailable",
                                  processing_stage="provider_unavailable")
            db.add(report)
            await db.commit()
            report_id = report.id

        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            row.processing_claimed_at = datetime.now(timezone.utc)
            enqueue(row)
            await db.commit()

        assert await claim_next() == report_id


# ── Finding 1, second half: Stage-1 index claiming ──────────────────────
# Codex's finding 1 named run_report_batch AND index_report. The first patch
# fixed only the batch path; ChatGPT's review caught that index_report was
# still check-then-call. Same hazard, same money.

@requires_db
class TestIndexClaiming:
    @staticmethod
    async def _report():
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.storage import get_storage, report_key

        async with async_session_maker() as db:
            user = User(email=f"idx-{uuid.uuid4().hex[:8]}@example.com", full_name="QA")
            db.add(user)
            await db.flush()
            report = CreditReport(user_id=user.id, bureau="experian", source="manual_upload",
                                  raw_text="x", extraction_status="extraction_incomplete")
            db.add(report)
            await db.flush()
            report.storage_key = report_key(user.id, report.id)
            await get_storage().put(report.storage_key, _pdf(28), "application/pdf")
            await db.commit()
            return report.id

    @staticmethod
    async def _checkpoint(report_id):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport

        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            return row.extraction_checkpoint or {}

    async def test_two_concurrent_index_calls_buy_exactly_one_index(self, db_ready, index_ai):
        """The review's explicit ask. Both callers saw no index and both paid."""
        from app.services.checkpoint_claim import IndexAlreadyRunning
        from app.services.index_job import index_report

        provider = index_ai(output=golden_index(total_pages=28))
        report_id = await self._report()

        results = await asyncio.gather(
            index_report(report_id, expected_tradelines=15),
            index_report(report_id, expected_tradelines=15),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, IndexAlreadyRunning)]
        succeeded = [r for r in results if not isinstance(r, BaseException)]

        assert len(refused) == 1, "the second caller was not refused"
        assert len(succeeded) == 1 and succeeded[0].ok
        assert len(provider.calls) == 1, "the index was purchased twice"

    async def test_a_failed_index_releases_its_claim(self, db_ready, index_ai):
        from app.services.ai import AIProviderError
        from app.services.checkpoint_claim import claim_state
        from app.services.index_job import index_report

        index_ai(error=AIProviderError("OpenAI API error 429: insufficient_quota"))
        report_id = await self._report()
        await index_report(report_id, expected_tradelines=15)

        from app.database import async_session_maker
        from app.models.credit_report import CreditReport

        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
        assert claim_state(row) == {}, "a failed index left its claim behind"

    async def test_a_stale_index_claim_does_not_block_forever(self, db_ready, index_ai):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.index_job import index_report

        provider = index_ai(output=golden_index(total_pages=28))
        report_id = await self._report()
        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            row.extraction_checkpoint = {"claims": {"index": {
                "claimed_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}}}
            await db.commit()

        result = await index_report(report_id, expected_tradelines=15, lease_seconds=60)
        assert result.ok and result.banked
        assert len(provider.calls) == 1

    async def test_banking_an_index_does_not_erase_a_batch_banked_meanwhile(
        self, db_ready, index_ai
    ):
        """index_report wrote a checkpoint it had read before the model call,
        so a batch that landed during indexing was erased. Same lost-update
        class as finding 2, on the other path."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.index_job import index_report

        provider = index_ai(output=golden_index(total_pages=28))
        report_id = await self._report()
        original = provider.generate_document

        async def land_a_batch_midway(config, **kwargs):
            async with async_session_maker() as db:
                row = await db.get(CreditReport, report_id)
                checkpoint = dict(row.extraction_checkpoint or {})
                row.extraction_checkpoint = {
                    **checkpoint,
                    "batches": {"b0": {"batch": {"accounts": [{"creditor_name": "LANDED"}]},
                                       "model": "gpt-5.6-sol"}},
                }
                await db.commit()
            provider.generate_document = original
            return await original(config, **kwargs)

        provider.generate_document = land_a_batch_midway
        result = await index_report(report_id, expected_tradelines=15)

        assert result.banked
        checkpoint = await self._checkpoint(report_id)
        assert checkpoint["index"]["tradelines"], "the index was not banked"
        assert checkpoint["batches"]["b0"]["batch"]["accounts"][0]["creditor_name"] == "LANDED"

    async def test_a_banked_index_is_still_reused_without_a_model_call(self, db_ready, index_ai):
        from app.services.index_job import index_report

        provider = index_ai(output=golden_index(total_pages=28))
        report_id = await self._report()
        await index_report(report_id, expected_tradelines=15)
        again = await index_report(report_id, expected_tradelines=15)

        assert again.reused and again.ok
        assert len(provider.calls) == 1


# ── One claim implementation, used by both paid passes ──────────────────

def test_both_paid_passes_use_the_same_claim_implementation():
    """Two implementations of a money-guard drift, and the one that drifts is
    the one nobody is looking at."""
    from pathlib import Path

    from app.services import batch_job, index_job

    for module in (batch_job, index_job):
        source = Path(module.__file__).read_text()
        assert "from app.services.checkpoint_claim import" in source
        assert "take_claim" in source and "drop_claim" in source and "lock_report" in source
        # No local re-implementation left behind.
        assert "def _claim_is_live" not in source
        assert "FOR UPDATE" not in source, "the lock belongs in the shared module"


def test_a_claim_with_an_unreadable_timestamp_is_treated_as_abandoned():
    """A marker nobody can parse is not protecting anything, and must not
    strand a paid pass forever."""
    from app.services.checkpoint_claim import claim_is_live

    assert not claim_is_live({"claimed_at": "not-a-date"})
    assert not claim_is_live({"claimed_at": None})
    assert not claim_is_live({})
    assert not claim_is_live(None)
    assert claim_is_live({"claimed_at": datetime.now(timezone.utc).isoformat()})


def test_a_naive_claim_timestamp_is_read_as_utc_not_crashed_on():
    from app.services.checkpoint_claim import claim_is_live

    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert claim_is_live({"claimed_at": naive})


def test_dropping_the_last_claim_leaves_no_residue():
    from app.services.checkpoint_claim import drop_claim, take_claim

    checkpoint = take_claim({"index": {"tradelines": []}}, "index")
    assert "claims" in checkpoint
    assert drop_claim(checkpoint, "index") == {"index": {"tradelines": []}}
