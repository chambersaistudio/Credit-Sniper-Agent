"""
Stage 1: the report index.

Single-pass Sol extraction failed on a 15-tradeline Experian report by
producing ~111,000 characters of JSON before being cut off mid-string — twice,
at ~$1 an attempt, banking nothing. The index pass exists to make the
expensive work bounded: it locates tradelines instead of transcribing them,
at roughly 77 output tokens each rather than ~2,100.

Asserted here: the index is cheap by construction (it cannot ask for the
fields that caused the truncation), it is only trusted when it can account for
every tradeline and find each one again, and it is banked independently so
nothing ever pays to index the same document twice.

No network, no provider spend.
"""
import pytest

from app.services.ai import ProviderUsage, AIResponseError, register_provider
from app.services.ai.config import ModelTier, resolve_tier
from app.services.ai.providers import ProviderResult, _instances
from app.services.document_extraction.index_quality import assess_index, identity_key
from app.services.document_extraction.index_schema import IndexedTradeline, ReportIndex
from app.services.document_extraction.schema import AuditReport, CreditReportExtraction
from tests.conftest import requires_db

# The 15 tradelines of the real Experian report, sanitized. Two pairs share a
# furnisher, which is exactly what naive de-duplication gets wrong.
GOLDEN_INDEX = [
    ("ATLAS", None, "5299XXXXXXXX1234", [3]),
    ("CAPITAL ONE", None, "517805XXXXXX8842", [4]),
    ("CREDIT ACCEPTANCE CORP", None, "7788XXXX", [4]),
    ("EXTRA", None, "40011XXXXXXX2200", [5]),
    ("MISSION LANE TAB BANK", None, "5178XXXXXXXX9901", [5]),
    ("NAVY FEDERAL CR UNION", None, "4441XXXXXXXX1020", [6]),
    ("NAVY FEDERAL CR UNION", None, "9002XXXX", [6]),
    ("SBNASELFLNDR", None, "3301XXXX", [7]),
    ("SELF FINANCIAL/LEAD BA", None, "3302XXXX", [7]),
    ("CAINE & WEINER", "PROGRESSIVE", "88XXXX2211", [8]),
    ("CREDENCE RESOURCE MANA", "AT T", "77XXXX9310", [8]),
    ("CREDIT COLLECTION SERV", "PROGRESSIVE", "66XXXX1188", [9]),
    ("JEFFERSON CAPITAL SYST", "MISSION LANE CREDIT CARD", "55XXXX7742", [9, 10]),
    ("JEFFERSON CAPITAL SYST", "T-MOBILE", "55XXXX7743", [10]),
    ("LVNV FUNDING LLC", "CAPITAL BANK OPEN SKY", "44XXXX2001", [10]),
]


def _entry(name, original, number, pages):
    return IndexedTradeline(
        creditor_name=name, original_creditor=original, account_number=number,
        account_type="Collection" if original else "Credit card", source_pages=pages,
        heading_excerpt=f"{name} Account #{number}",
    )


def golden_index(**overrides) -> ReportIndex:
    fields = dict(
        bureau="experian", document_created_date="Sep 24, 2026", report_date=None,
        score=580, score_type="FICO Score 8", tradeline_count=15,
        tradelines=[_entry(*row) for row in GOLDEN_INDEX],
        total_pages=31, unreadable_pages=[],
    )
    fields.update(overrides)
    return ReportIndex(**fields)


# ── The index is cheap by construction ──────────────────────────────────

def test_the_index_cannot_ask_for_the_fields_that_caused_the_truncation():
    """The schema is the guarantee. If balances or payment history could be
    requested here, Stage 1 would drift back toward Stage 2's cost."""
    fields = set(IndexedTradeline.model_fields)
    for expensive in ("balance", "credit_limit", "past_due_amount", "payment_history",
                      "field_evidence", "status_raw", "date_opened", "remarks"):
        assert expensive not in fields, f"{expensive} would make the index expensive"
    assert fields == {"creditor_name", "original_creditor", "account_number",
                      "account_type", "source_pages", "heading_excerpt"}


def test_an_index_entry_costs_a_fraction_of_a_detailed_tradeline():
    """The number the whole design rests on."""
    index = golden_index()
    per_entry = len(index.model_dump_json()) / len(index.tradelines) / 3.6
    assert per_entry < 150, f"~{per_entry:.0f} tokens/entry — the index is not cheap enough"


def test_the_indexer_prompt_forbids_transcription():
    from app.services.document_extraction.pipeline import INDEXER_SYSTEM

    lowered = INDEXER_SYSTEM.lower()
    assert "do not return balances" in lowered
    assert "source_pages` is required" in INDEXER_SYSTEM
    # The two ways an index silently loses accounts.
    assert "exactly once" in lowered
    assert "closed" in lowered and "collection" in lowered


def test_the_index_tier_defaults_to_the_cheapest_model_at_low_detail():
    from app.config import settings

    config = resolve_tier(ModelTier.DOCUMENT_INDEX)
    assert config.model == "gpt-5.6-luna"
    assert settings.document_index_detail == "low"
    # Generous ceiling on purpose: max_output_tokens is a cap, not a
    # reservation, so headroom is free and a tight cap is what truncates.
    assert config.max_tokens >= 16000
    # Production defaults for the detailed passes are untouched.
    assert resolve_tier(ModelTier.DOCUMENT_EXTRACTION).model == "gpt-5.6-sol"
    assert settings.document_extraction_detail == "high"


# ── The quality gate ────────────────────────────────────────────────────

def test_a_complete_index_passes():
    quality = assess_index(golden_index(), expected_tradelines=15)
    assert quality.ok, quality.reasons
    assert (quality.listed, quality.declared, quality.distinct) == (15, 15, 15)
    assert quality.with_pages == 15


def test_the_same_furnisher_twice_is_two_tradelines_not_a_duplicate():
    """Navy Federal appears twice with different account numbers, and Jefferson
    Capital twice under different original creditors. Both are real, and an
    index that merged them would lose an account from every later batch."""
    quality = assess_index(golden_index(), expected_tradelines=15)
    assert quality.duplicates == []
    assert quality.distinct == 15

    navy = [t for t in golden_index().tradelines if t.creditor_name == "NAVY FEDERAL CR UNION"]
    assert identity_key(navy[0]) != identity_key(navy[1])
    jefferson = [t for t in golden_index().tradelines if t.creditor_name == "JEFFERSON CAPITAL SYST"]
    assert identity_key(jefferson[0]) != identity_key(jefferson[1])


def test_a_genuinely_repeated_tradeline_is_caught():
    index = golden_index()
    index.tradelines.append(_entry(*GOLDEN_INDEX[0]))
    index.tradeline_count = 16
    quality = assess_index(index)
    assert not quality.ok
    assert quality.duplicates == ["ATLAS"]
    assert any("more than once" in r for r in quality.reasons)


def test_a_listing_that_disagrees_with_its_own_count_is_rejected():
    """The signature of a truncated or abandoned list — and the reason the
    schema asks for the total separately."""
    index = golden_index(tradelines=[_entry(*row) for row in GOLDEN_INDEX[:9]])
    quality = assess_index(index)
    assert not quality.ok
    assert any("declares 15 tradelines but lists 9" in r for r in quality.reasons)


def test_a_tradeline_with_no_page_reference_is_rejected():
    """Provenance is not optional: an entry nobody can locate cannot be read
    in detail later, or audited afterwards."""
    index = golden_index()
    index.tradelines[3].source_pages = []
    quality = assess_index(index)
    assert not quality.ok
    assert quality.with_pages == 14
    assert quality.missing_pages == ["EXTRA"]


@pytest.mark.parametrize("overrides,fragment", [
    ({"bureau": None}, "Bureau not identified"),
    ({"bureau": "experion"}, "Bureau not identified"),
    ({"tradelines": []}, "lists no tradelines"),
    ({"unreadable_pages": [12, 13]}, "could not be read reliably"),
])
def test_index_gate_rejections(overrides, fragment):
    if "tradelines" in overrides:
        overrides["tradeline_count"] = 0
    quality = assess_index(golden_index(**overrides))
    assert not quality.ok
    assert any(fragment in r for r in quality.reasons), quality.reasons


def test_a_confirmed_count_is_checked_when_one_is_given():
    """Validation against a human-confirmed total — the success criterion for
    the real Experian report."""
    short = golden_index(tradelines=[_entry(*r) for r in GOLDEN_INDEX[:14]], tradeline_count=14)
    assert assess_index(short).ok                       # self-consistent
    quality = assess_index(short, expected_tradelines=15)
    assert not quality.ok                                # but wrong about the document
    assert any("Expected 15 tradelines, indexed 14" in r for r in quality.reasons)


def test_no_index_at_all_is_a_failure_not_an_empty_pass():
    quality = assess_index(None)
    assert not quality.ok and quality.listed == 0


# ── Running the pass ────────────────────────────────────────────────────

class IndexProvider:
    """Records which tier and detail each call used."""

    name = "openai"

    def __init__(self, output=None, error=None):
        self.output = output if output is not None else golden_index()
        self.error = error
        self.calls: list[dict] = []

    async def generate(self, *args, **kwargs):
        raise AssertionError("the index pass must not use the text provider")

    async def generate_document(self, config, *, system, prompt, document, filename,
                                output_type, max_tokens, detail="high"):
        self.calls.append({"model": config.model, "detail": detail, "output_type": output_type,
                           "max_tokens": max_tokens, "system": system})
        if self.error:
            raise self.error
        assert output_type is ReportIndex, "the index pass asked for the wrong schema"
        return ProviderResult(output=self.output, provider=self.name, model=config.model,
                              input_tokens=41_200, output_tokens=1_158, cache_read_tokens=0,
                              cache_write_tokens=0, latency_ms=8_400.0)


@pytest.fixture
def index_ai():
    saved = dict(_instances)
    installed = {}

    def install(output=None, error=None):
        installed["p"] = IndexProvider(output, error)
        register_provider("openai", installed["p"])
        return installed["p"]

    yield install
    _instances.clear()
    _instances.update(saved)


async def test_the_index_pass_is_one_cheap_call_and_nothing_else(index_ai):
    from app.services.index_job import index_document

    provider = index_ai()
    result = await index_document(b"%PDF-1.4 fake", expected_tradelines=15)

    assert len(provider.calls) == 1, "indexing must cost exactly one model call"
    call = provider.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["detail"] == "low"
    assert call["output_type"] is ReportIndex
    # It cannot have run either expensive pass.
    assert CreditReportExtraction not in [c["output_type"] for c in provider.calls]
    assert AuditReport not in [c["output_type"] for c in provider.calls]

    assert result.ok
    assert result.quality.listed == 15
    assert result.index.bureau == "experian"
    assert result.model == "gpt-5.6-luna"


async def test_a_failed_index_reports_its_failure_class_and_cost(index_ai):
    from app.services.index_job import index_document

    index_ai(error=AIResponseError(
        "Model output failed schema validation after 4,102 chars (max_output_tokens=16000)",
        usage=ProviderUsage(input_tokens=41_200, output_tokens=16_000, max_tokens=16_000,
                            response_id="resp_idx", latency_ms=30_000.0),
    ))
    result = await index_document(b"%PDF-1.4 fake", expected_tradelines=15)

    assert not result.ok
    assert result.index is None
    assert result.failure.error_class == "AIResponseError"
    assert result.failure.status.value == "model_response_failed"
    assert result.failure.billed is True
    assert result.failure.output_tokens == 16_000
    assert result.quality.reasons == ["No index was produced."]


# ── Checkpointing ───────────────────────────────────────────────────────

@requires_db
class TestIndexCheckpoint:
    """The index is banked independently, so it is never re-bought."""

    @staticmethod
    async def _report(db_ready):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.storage import get_storage, report_key

        async with async_session_maker() as db:
            user = User(email="idx@example.com", full_name="Index Tester")
            db.add(user)
            await db.flush()
            report = CreditReport(user_id=user.id, bureau="unknown", source="manual_upload",
                                  raw_text="x", extraction_status="extraction_incomplete")
            db.add(report)
            await db.flush()
            report.storage_key = report_key(user.id, report.id)
            await get_storage().put(report.storage_key, b"%PDF-1.4 fake", "application/pdf")
            await db.commit()
            return report.id

    @staticmethod
    async def _row(report_id):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport

        async with async_session_maker() as db:
            return await db.get(CreditReport, report_id)

    async def test_a_passing_index_is_banked(self, db_ready, index_ai):
        from app.services.index_job import index_report

        index_ai()
        report_id = await self._report(db_ready)
        result = await index_report(report_id, expected_tradelines=15)

        assert result.ok and result.banked
        checkpoint = (await self._row(report_id)).extraction_checkpoint
        assert len(checkpoint["index"]["tradelines"]) == 15
        assert checkpoint["index_model"] == "gpt-5.6-luna"
        assert checkpoint["index_quality"]["ok"] is True
        assert checkpoint["indexed_at"]
        # Stage 1 alone: no extraction has been paid for.
        assert "extraction" not in checkpoint

    async def test_a_failing_index_is_not_banked(self, db_ready, index_ai):
        """A checkpoint promises the work behind it need not be repeated. An
        index that failed its gate is not work to build on."""
        from app.services.index_job import index_report

        index_ai(output=golden_index(tradelines=[_entry(*r) for r in GOLDEN_INDEX[:9]]))
        report_id = await self._report(db_ready)
        result = await index_report(report_id, expected_tradelines=15)

        assert not result.ok and not result.banked
        # Nothing about a failed index is kept, and no bookkeeping residue
        # either — the claim is released cleanly.
        checkpoint = (await self._row(report_id)).extraction_checkpoint or {}
        assert "index" not in checkpoint
        assert checkpoint == {}

    async def test_banking_an_index_preserves_an_existing_extraction_checkpoint(
        self, db_ready, index_ai
    ):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.index_job import index_report

        index_ai()
        report_id = await self._report(db_ready)
        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            row.extraction_checkpoint = {"extraction": {"accounts": [1, 2, 3]},
                                         "extractor_model": "gpt-5.6-sol"}
            await db.commit()

        await index_report(report_id, expected_tradelines=15)
        checkpoint = (await self._row(report_id)).extraction_checkpoint
        assert checkpoint["extraction"] == {"accounts": [1, 2, 3]}   # not clobbered
        assert checkpoint["extractor_model"] == "gpt-5.6-sol"
        assert len(checkpoint["index"]["tradelines"]) == 15

    async def test_the_index_pass_records_its_own_cost(self, db_ready, index_ai):
        from sqlalchemy import select

        from app.database import async_session_maker
        from app.models.ai_usage import AIUsageLog
        from app.services.ai import add_usage_listener, clear_usage_listeners
        from app.services.index_job import index_report
        from app.services.usage_sink import persist_usage

        index_ai()
        report_id = await self._report(db_ready)
        clear_usage_listeners()
        add_usage_listener(persist_usage)
        try:
            await index_report(report_id, expected_tradelines=15)
        finally:
            clear_usage_listeners()

        async with async_session_maker() as db:
            row = (await db.execute(select(AIUsageLog).where(
                AIUsageLog.context["report_id"].astext == str(report_id)
            ))).scalar_one()
        assert row.task == "index_report_document"
        assert row.model == "gpt-5.6-luna"
        assert row.success is True
        assert (row.input_tokens, row.output_tokens) == (41_200, 1_158)
        # luna at $0.20/$1.20 per 1M: 41,200×0.20 + 1,158×1.20
        assert row.estimated_cost_usd == pytest.approx(0.00963, abs=1e-5)
        # Two orders of magnitude under the $1.0173 a failed Sol attempt burned.
        assert row.estimated_cost_usd < 0.05


# ── Stage 1 idempotency ─────────────────────────────────────────────────

@requires_db
class TestIndexIdempotency:
    async def test_indexing_twice_does_not_buy_the_index_twice(self, db_ready, index_ai):
        """Invoking Stage 1 by hand a second time must reuse what is banked."""
        from app.services.index_job import index_report

        provider = index_ai()
        report_id = await TestIndexCheckpoint._report(db_ready)

        first = await index_report(report_id, expected_tradelines=15)
        assert first.ok and first.banked and not first.reused
        assert len(provider.calls) == 1

        second = await index_report(report_id, expected_tradelines=15)
        assert second.ok and second.reused
        assert len(provider.calls) == 1, "the same index was purchased twice"
        assert second.quality.listed == 15

        forced = await index_report(report_id, expected_tradelines=15, force=True)
        assert not forced.reused
        assert len(provider.calls) == 2

    async def test_a_failed_index_is_not_treated_as_reusable(self, db_ready, index_ai):
        """Nothing banks a failing index, but an older row could carry one —
        reusing it would propagate a bad page map into every batch."""
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.index_job import index_report

        provider = index_ai()
        report_id = await TestIndexCheckpoint._report(db_ready)
        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            short = golden_index(tradelines=[_entry(*r) for r in GOLDEN_INDEX[:9]])
            row.extraction_checkpoint = {"index": short.model_dump(mode="json")}
            await db.commit()

        result = await index_report(report_id, expected_tradelines=15)
        assert not result.reused, "a failing index must not be reused"
        assert len(provider.calls) == 1
