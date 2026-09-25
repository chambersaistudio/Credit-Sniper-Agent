"""
Stage 2: detailed extraction in batches, from page bundles.

Single-pass Sol died producing ~111,000 characters of JSON for 15 tradelines.
Stage 2 reads four at a time, from a transient PDF holding only the pages the
Stage-1 index says those four live on.

The properties that make this safe, all asserted here:

  * the index chooses the pages — never a text heuristic, never the document
  * the bundle is transient; the stored original is never modified
  * every page number surviving into extracted data is an ORIGINAL page number,
    translated deterministically rather than reported by the model
  * a batch banks itself, so a failure never re-buys a batch that passed
  * nothing runs more than the one batch it was asked for

No network, no provider spend.
"""
from io import BytesIO

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.services.ai import ProviderUsage, AIResponseError, register_provider
from app.services.ai.providers import ProviderResult, _instances
from app.services.document_extraction.batch_quality import assess_batch
from app.services.document_extraction.batch_schema import TradelineBatch
from app.services.document_extraction.batching import DEFAULT_BATCH_SIZE, plan_batches
from app.services.document_extraction.page_bundle import (
    PageSelectionError, build_bundle, page_count, remap_tradelines, select_pages,
)
from app.services.document_extraction.schema import (
    ExtractedTradeline, FieldEvidence, PaymentHistoryEntry,
)
from tests.conftest import requires_db
from tests.test_index_pass import GOLDEN_INDEX, _entry, golden_index


def _pdf(pages: int) -> bytes:
    """A PDF whose every page announces its own number, so a bundle's page
    order can be verified rather than assumed."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    for page in range(1, pages + 1):
        c.drawString(72, 700, f"ORIGINAL PAGE {page}")
        c.showPage()
    c.save()
    return buffer.getvalue()


def _page_text(document: bytes, page: int) -> str:
    import pdfplumber

    with pdfplumber.open(BytesIO(document)) as pdf:
        return pdf.pages[page - 1].extract_text() or ""


# ── Page selection ──────────────────────────────────────────────────────

def test_pages_come_from_the_index_and_nowhere_else():
    pages, padding = select_pages([[3], [4], [4, 5]], total_pages=31, context_pages=0)
    assert pages == (3, 4, 5)
    assert padding == ()


def test_a_tradeline_spanning_pages_contributes_all_of_them():
    pages, _ = select_pages([[9, 10]], total_pages=31, context_pages=0)
    assert pages == (9, 10)


def test_a_safety_margin_adds_neighbours_and_says_which_they_are():
    pages, padding = select_pages([[5]], total_pages=31, context_pages=1)
    assert pages == (4, 5, 6)
    assert padding == (4, 6), "padding must be distinguishable from indexed pages"


def test_the_margin_never_runs_off_the_document():
    pages, _ = select_pages([[1]], total_pages=3, context_pages=2)
    assert pages == (1, 2, 3)


def test_page_selection_is_deterministic():
    args = ([[9, 10], [3], [10]], )
    kwargs = {"total_pages": 31, "context_pages": 1}
    assert select_pages(*args, **kwargs) == select_pages(*args, **kwargs)
    # Order of the input tradelines cannot change the result.
    assert (select_pages([[3], [10], [9, 10]], **kwargs)
            == select_pages([[10], [9, 10], [3]], **kwargs))


def test_a_batch_with_no_indexed_pages_is_refused_rather_than_guessed():
    with pytest.raises(PageSelectionError):
        select_pages([[], []], total_pages=31)


# ── Building the bundle ─────────────────────────────────────────────────

def test_a_bundle_holds_exactly_the_requested_pages_in_order():
    document = _pdf(12)
    bundle = build_bundle(document, [3, 7, 8])

    assert bundle.page_count == 3
    assert bundle.pages == (3, 7, 8)
    assert bundle.source_page_count == 12
    assert page_count(bundle.pdf) == 3
    # The bundle's page 1 really is the original's page 3.
    assert "ORIGINAL PAGE 3" in _page_text(bundle.pdf, 1)
    assert "ORIGINAL PAGE 7" in _page_text(bundle.pdf, 2)
    assert "ORIGINAL PAGE 8" in _page_text(bundle.pdf, 3)


def test_building_a_bundle_never_modifies_the_original():
    document = _pdf(12)
    before = bytes(document)
    build_bundle(document, [3, 7, 8])
    assert document == before, "the authoritative original must be untouched"
    assert page_count(document) == 12


def test_the_page_map_is_total_and_refuses_to_guess():
    bundle = build_bundle(_pdf(12), [3, 7, 8])
    assert [bundle.to_original(i) for i in (1, 2, 3)] == [3, 7, 8]
    # Anything outside the bundle is unplaceable, not approximated: a wrong
    # page reference is worse than none, being indistinguishable from real
    # provenance later.
    for outside in (0, 4, 99, -1, None):
        assert bundle.to_original(outside) is None


def test_pages_outside_the_document_are_refused():
    with pytest.raises(PageSelectionError, match="outside this 5-page document"):
        build_bundle(_pdf(5), [4, 99])


def test_an_empty_bundle_is_refused():
    with pytest.raises(PageSelectionError):
        build_bundle(_pdf(5), [])


# ── Remapping provenance ────────────────────────────────────────────────

def _tradeline(pages, evidence_pages, history_pages):
    return ExtractedTradeline(
        creditor_name="CAINE & WEINER", original_creditor="PROGRESSIVE", sold_to=None,
        account_number="88XXXX2211", account_type="Collection", open_closed="Open",
        status_raw="Collection account", status_normalized="collection", payment_status=None,
        report_classification="Potentially negative", balance="$1,204",
        balance_updated="Jun 25, 2026", credit_limit=None, original_amount="$1,204",
        past_due_amount="$1,204", monthly_payment=None, high_balance=None, terms=None,
        responsibility="Individual", date_opened="Mar 3, 2024", date_closed=None,
        status_updated=None, date_first_delinquency=None, date_last_reported=None,
        date_last_payment=None, remarks=None, consumer_dispute=None, contact=None,
        payment_history=[
            PaymentHistoryEntry(year=2026, month=m, raw_status_code="COL", status_code=None,
                                balance=None, past_due=None, amount_paid=None, amount_due=None,
                                remarks=[], source_page=p)
            for m, p in enumerate(history_pages, start=1)
        ],
        source_pages=list(pages),
        identity_evidence="CAINE & WEINER",
        field_evidence=[
            FieldEvidence(field="balance", value="$1,204", page=p, excerpt="Balance $1,204")
            for p in evidence_pages
        ],
    )


def test_every_page_reference_is_translated_to_original_numbering():
    """All three places a page number hides: the tradeline, its evidence, and
    each month of payment history."""
    bundle = build_bundle(_pdf(12), [7, 8, 9])
    tradeline = _tradeline(pages=[1, 2], evidence_pages=[1, 3], history_pages=[2, 2])

    report = remap_tradelines([tradeline], bundle)

    assert tradeline.source_pages == [7, 8]
    assert [e.page for e in tradeline.field_evidence] == [7, 9]
    assert [m.source_page for m in tradeline.payment_history] == [8, 8]
    assert report.unmapped == 0
    assert report.remapped == 6


def test_a_page_the_bundle_does_not_have_is_dropped_and_reported():
    bundle = build_bundle(_pdf(12), [7, 8])
    tradeline = _tradeline(pages=[1, 9], evidence_pages=[42], history_pages=[])

    report = remap_tradelines([tradeline], bundle)

    assert tradeline.source_pages == [7]            # the bogus page is gone
    assert tradeline.field_evidence[0].page is None
    assert report.unmapped == 2
    assert sorted(report.out_of_range) == [9, 42]


def test_remapped_pages_are_deduplicated_and_ordered():
    bundle = build_bundle(_pdf(12), [7, 8, 9])
    tradeline = _tradeline(pages=[3, 1, 1, 2], evidence_pages=[], history_pages=[])
    remap_tradelines([tradeline], bundle)
    assert tradeline.source_pages == [7, 8, 9]


# ── Planning ────────────────────────────────────────────────────────────

def test_batches_hold_at_most_four_tradelines_and_cover_all_of_them():
    plans = plan_batches(golden_index())
    assert [p.size for p in plans] == [4, 4, 4, 3]
    assert sum(p.size for p in plans) == 15
    assert all(p.size <= DEFAULT_BATCH_SIZE for p in plans)
    assert [p.batch_id for p in plans] == ["b0", "b1", "b2", "b3"]


def test_planning_is_deterministic():
    first, second = plan_batches(golden_index()), plan_batches(golden_index())
    assert [p.to_dict() for p in first] == [p.to_dict() for p in second]


def test_a_batch_sends_only_its_own_pages():
    plans = plan_batches(golden_index(), context_pages=0)
    b0 = plans[0]
    # ATLAS p3, CAPITAL ONE p4, CREDIT ACCEPTANCE p4, EXTRA p5.
    assert b0.pages == (3, 4, 5)
    assert 31 not in b0.pages and 1 not in b0.pages


def test_bundling_sends_far_less_document_than_resending_the_whole_report():
    index = golden_index()
    plans = plan_batches(index, context_pages=1)
    sent = sum(len(p.pages) for p in plans)
    whole = index.total_pages * len(plans)
    assert sent < whole / 3, f"{sent} page-sends vs {whole} — bundling is not paying off"


def test_the_manifest_identifies_tradelines_without_leaking_page_numbers():
    """The model is looking at a renumbered bundle. Telling it original page
    numbers would invite it to report those instead of what it sees, breaking
    the deterministic remap."""
    plan = plan_batches(golden_index())[2]
    manifest = plan.manifest()
    assert "CAINE & WEINER" in manifest
    assert "PROGRESSIVE" in manifest           # original creditor distinguishes it
    assert "88XXXX2211" in manifest
    for page in plan.pages:
        assert f"page {page}" not in manifest.lower()


def test_batch_keys_pin_which_tradelines_a_checkpoint_belongs_to():
    plan = plan_batches(golden_index())[3]
    # Jefferson Capital twice, under different original creditors.
    assert len({tuple(k) for k in plan.keys}) == plan.size


# ── The batch gate ──────────────────────────────────────────────────────

def _batch_for(plan, bundle, *, drop=(), extra=(), pages_override=None):
    accounts = []
    for tradeline in plan.tradelines:
        if tradeline.creditor_name in drop:
            continue
        account = _tradeline(pages=pages_override or [1], evidence_pages=[], history_pages=[])
        account.creditor_name = tradeline.creditor_name
        account.original_creditor = tradeline.original_creditor
        account.account_number = tradeline.account_number
        accounts.append(account)
    for name in extra:
        account = _tradeline(pages=[1], evidence_pages=[], history_pages=[])
        account.creditor_name = name
        account.original_creditor = None
        account.account_number = "99XXXX0000"
        accounts.append(account)
    batch = TradelineBatch(accounts=accounts, missing_tradelines=[], unreadable_pages=[])
    remap_tradelines(batch.accounts, bundle)
    return batch


def test_a_complete_batch_passes():
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    quality = assess_batch(_batch_for(plan, bundle), plan, bundle)
    assert quality.ok, quality.reasons
    assert quality.matched == quality.asked == 4


def test_a_batch_that_skips_a_tradeline_fails():
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    quality = assess_batch(_batch_for(plan, bundle, drop=("EXTRA",)), plan, bundle)
    assert not quality.ok
    assert quality.missing == ["EXTRA"]
    assert any("did not return" in r for r in quality.reasons)


def test_a_batch_that_returns_an_account_it_was_not_asked_for_fails():
    """The batch model sees pages holding other accounts. Reporting one is a
    silent duplicate once every batch is merged."""
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    quality = assess_batch(_batch_for(plan, bundle, extra=("SOME OTHER BANK",)), plan, bundle)
    assert not quality.ok
    assert quality.unexpected == ["SOME OTHER BANK"]


def test_provenance_pointing_outside_the_supplied_pages_fails():
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    batch.accounts[0].source_pages = [29]      # a page this batch never saw
    quality = assess_batch(batch, plan, bundle)
    assert not quality.ok
    assert quality.pages_outside_bundle == [29]


def test_the_model_declaring_a_tradeline_absent_is_recorded_not_ignored():
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    batch.missing_tradelines = ["EXTRA"]
    quality = assess_batch(batch, plan, bundle)
    assert not quality.ok
    assert any("absent from the supplied pages" in r for r in quality.reasons)


def test_a_differently_masked_number_is_still_the_same_tradeline():
    """The index and the detailed read can legitimately disagree on masking,
    and TransUnion warns those digits may be scrambled. Name plus original
    creditor carries the identity."""
    plan = plan_batches(golden_index())[0]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    batch.accounts[0].account_number = "************9999"
    quality = assess_batch(batch, plan, bundle)
    assert quality.ok, quality.reasons


def test_a_dropped_original_creditor_is_drift_not_a_different_account():
    """Caught while rehearsing: the gate reported the same accounts as BOTH
    missing and unexpected when the detailed read omitted identity fields the
    index had recorded. It is the right account with a field lost — which is
    a quality problem worth failing on, but a different one, and it must be
    named accurately."""
    plan = plan_batches(golden_index())[2]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    for account in batch.accounts:
        account.original_creditor = None
        account.account_number = None

    quality = assess_batch(batch, plan, bundle)
    assert quality.matched == quality.asked == 4, "these are the right accounts"
    assert quality.missing == [] and quality.unexpected == []
    assert not quality.ok
    assert any("original creditor 'PROGRESSIVE'" in r for r in quality.reasons)
    assert any("account number" in r for r in quality.reasons)


def test_two_collections_from_one_agency_are_never_matched_by_name_alone():
    """Jefferson Capital appears twice under different original creditors.
    Pairing them by name would silently swap their contents, so an entry that
    keeps neither the number nor the original creditor stays unmatched."""
    plan = plan_batches(golden_index())[3]
    assert sum(1 for t in plan.tradelines if t.creditor_name == "JEFFERSON CAPITAL SYST") == 2
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    for account in batch.accounts:
        account.original_creditor = None
        account.account_number = None

    quality = assess_batch(batch, plan, bundle)
    assert not quality.ok
    # LVNV is unique by name, so it still matches; the Jeffersons do not.
    assert quality.matched == 1
    assert "JEFFERSON CAPITAL SYST" in quality.missing
    assert "JEFFERSON CAPITAL SYST" in quality.unexpected


def test_a_differently_masked_number_matches_via_the_original_creditor():
    plan = plan_batches(golden_index())[3]
    bundle = build_bundle(_pdf(31), plan.pages, padding=plan.padding)
    batch = _batch_for(plan, bundle)
    for account in batch.accounts:
        account.account_number = "****"          # masking differs from the index
    quality = assess_batch(batch, plan, bundle)
    # Both Jeffersons still resolve, because their original creditors differ.
    assert quality.matched == 3
    assert quality.missing == [] and quality.unexpected == []


# ── Running a batch ─────────────────────────────────────────────────────

def _parse_manifest(prompt: str):
    """Read back the (name, original creditor, number) the manifest asked for,
    so the fake model answers the question it was actually given."""
    import re

    rows = []
    for line in prompt.splitlines():
        match = re.match(r'\s*\d+\.\s+"([^"]+)"(.*)', line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        original = re.search(r'original creditor "([^"]+)"', rest)
        number = re.search(r"account number (\S+?)(?:,|\s*\(|$)", rest)
        rows.append((name, original.group(1) if original else None,
                     number.group(1) if number else None))
    return rows


class BatchProvider:
    name = "openai"

    def __init__(self, *, error=None, drop=(), lose_identity=False):
        self.error = error
        self.drop = drop
        # Simulates a model that reads the values but drops the identity
        # fields the index recorded.
        self.lose_identity = lose_identity
        self.calls: list[dict] = []

    async def generate(self, *a, **k):
        raise AssertionError("batch extraction must not use the text provider")

    async def generate_document(self, config, *, system, prompt, document, filename,
                                output_type, max_tokens, detail="high"):
        self.calls.append({"model": config.model, "detail": detail, "prompt": prompt,
                           "document": document, "pages": page_count(document)})
        if self.error:
            raise self.error
        assert output_type is TradelineBatch
        # Answer for whatever the manifest asked for, reading bundle page 1.
        accounts = []
        for name, original, number in _parse_manifest(prompt):
            if name in self.drop:
                continue
            account = _tradeline(pages=[1], evidence_pages=[1], history_pages=[1])
            account.creditor_name = name
            account.original_creditor = None if self.lose_identity else original
            account.account_number = None if self.lose_identity else number
            accounts.append(account)
        return ProviderResult(
            output=TradelineBatch(accounts=accounts, missing_tradelines=[], unreadable_pages=[]),
            provider=self.name, model=config.model, input_tokens=18_400, output_tokens=8_500,
            cache_read_tokens=0, cache_write_tokens=0, latency_ms=32_000.0,
        )


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


async def test_a_batch_reads_only_its_own_pages_and_gets_original_numbers_back(batch_ai):
    from app.services.batch_job import extract_batch

    provider = batch_ai()
    plan = plan_batches(golden_index(), context_pages=0)[0]
    result = await extract_batch(_pdf(31), plan)

    assert len(provider.calls) == 1, "one batch is one model call"
    call = provider.calls[0]
    # The model saw three pages, not thirty-one.
    assert call["pages"] == 3 == len(plan.pages)
    assert "3 page(s)" in call["prompt"]
    # And the provenance that came back is in the ORIGINAL report's numbering.
    assert result.accounts[0].source_pages == [plan.pages[0]]
    assert result.accounts[0].field_evidence[0].page == plan.pages[0]
    assert result.accounts[0].payment_history[0].source_page == plan.pages[0]
    assert result.quality.ok


async def test_the_bundle_really_is_a_subset_of_the_original(batch_ai):
    from app.services.batch_job import extract_batch

    provider = batch_ai()
    plan = plan_batches(golden_index(), context_pages=0)[0]
    original = _pdf(31)
    await extract_batch(original, plan)

    sent = provider.calls[0]["document"]
    assert sent != original
    assert page_count(sent) < page_count(original)
    for position, original_page in enumerate(plan.pages, start=1):
        assert f"ORIGINAL PAGE {original_page}" in _page_text(sent, position)


# ── Checkpointing ───────────────────────────────────────────────────────

@requires_db
class TestBatchCheckpoints:
    @staticmethod
    async def _report(with_index=True):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.models.user import User
        from app.services.storage import get_storage, report_key

        async with async_session_maker() as db:
            user = User(email="batch@example.com", full_name="Batch Tester")
            db.add(user)
            await db.flush()
            report = CreditReport(user_id=user.id, bureau="experian", source="manual_upload",
                                  raw_text="x", extraction_status="extraction_incomplete")
            if with_index:
                report.extraction_checkpoint = {
                    "index": golden_index().model_dump(mode="json"),
                    "index_model": "gpt-5.6-luna",
                }
            db.add(report)
            await db.flush()
            report.storage_key = report_key(user.id, report.id)
            await get_storage().put(report.storage_key, _pdf(31), "application/pdf")
            await db.commit()
            return report.id

    @staticmethod
    async def _checkpoint(report_id):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport

        async with async_session_maker() as db:
            row = await db.get(CreditReport, report_id)
            return row.extraction_checkpoint or {}

    async def test_a_batch_banks_itself(self, db_ready, batch_ai):
        from app.services.batch_job import run_report_batch

        batch_ai()
        report_id = await self._report()
        result = await run_report_batch(report_id, "b0")

        assert result.ok and result.banked
        checkpoint = await self._checkpoint(report_id)
        assert list(checkpoint["batches"]) == ["b0"]
        banked = checkpoint["batches"]["b0"]
        assert len(banked["batch"]["accounts"]) == 4
        assert banked["plan"]["pages"] == list(result.plan.pages)
        assert banked["bundle"]["pages"] == list(result.plan.pages)
        # Stage 1's index is untouched beside it.
        assert len(checkpoint["index"]["tradelines"]) == 15

    async def test_one_batch_failing_never_touches_another(self, db_ready, batch_ai):
        """The rule the whole redesign exists for."""
        from app.services.batch_job import run_report_batch

        batch_ai()
        report_id = await self._report()
        await run_report_batch(report_id, "b0")
        await run_report_batch(report_id, "b1")
        assert sorted((await self._checkpoint(report_id))["batches"]) == ["b0", "b1"]

        # b2 now fails at the provider.
        batch_ai(error=AIResponseError("truncated", usage=ProviderUsage(
            input_tokens=18_000, output_tokens=32_000, max_tokens=32_000)))
        result = await run_report_batch(report_id, "b2")

        assert not result.ok and not result.banked
        checkpoint = await self._checkpoint(report_id)
        assert sorted(checkpoint["batches"]) == ["b0", "b1"], "a failure lost banked work"
        assert len(checkpoint["batches"]["b0"]["batch"]["accounts"]) == 4

    async def test_a_banked_batch_is_reused_rather_than_re_read(self, db_ready, batch_ai):
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        await run_report_batch(report_id, "b0")
        assert len(provider.calls) == 1

        again = await run_report_batch(report_id, "b0")
        assert again.reused and again.banked
        assert len(provider.calls) == 1, "a banked batch must not be bought twice"

        forced = await run_report_batch(report_id, "b0", force=True)
        assert not forced.reused
        assert len(provider.calls) == 2

    async def test_a_batch_that_fails_its_gate_is_not_banked(self, db_ready, batch_ai):
        from app.services.batch_job import run_report_batch

        batch_ai(drop=("EXTRA",))
        report_id = await self._report()
        result = await run_report_batch(report_id, "b0")

        assert not result.ok and not result.banked
        assert result.quality.missing == ["EXTRA"]
        assert (await self._checkpoint(report_id)).get("batches") in (None, {})

    async def test_stage_two_refuses_to_run_without_a_stage_one_index(self, db_ready, batch_ai):
        """Batches exist because the index says where to look. Without one
        there is nothing to bundle, and guessing is what this replaced."""
        from app.services.batch_job import NoBankedIndex, run_report_batch

        provider = batch_ai()
        report_id = await self._report(with_index=False)
        with pytest.raises(NoBankedIndex):
            await run_report_batch(report_id, "b0")
        assert provider.calls == [], "nothing may be spent without an index"

    async def test_an_unknown_batch_id_is_refused(self, db_ready, batch_ai):
        from app.services.batch_job import run_report_batch

        provider = batch_ai()
        report_id = await self._report()
        with pytest.raises(LookupError, match="no batch"):
            await run_report_batch(report_id, "b99")
        assert provider.calls == []

    async def test_batch_status_shows_what_has_been_paid_for(self, db_ready, batch_ai):
        from app.database import async_session_maker
        from app.models.credit_report import CreditReport
        from app.services.batch_job import batch_status, run_report_batch

        batch_ai()
        report_id = await self._report()
        await run_report_batch(report_id, "b1")

        async with async_session_maker() as db:
            rows = batch_status(await db.get(CreditReport, report_id))
        assert [r["batch_id"] for r in rows] == ["b0", "b1", "b2", "b3"]
        assert [r["banked"] for r in rows] == [False, True, False, False]
        assert rows[1]["model"] == "gpt-5.6-luna" or rows[1]["model"]


# ── Scoring ─────────────────────────────────────────────────────────────

def test_batch_scoring_measures_fields_history_and_original_page_provenance():
    from app.services.benchmark.batch_scoring import score_batch

    account = _tradeline(pages=[8], evidence_pages=[], history_pages=[])
    account.creditor_name = "CAINE & WEINER"
    account.payment_history = [
        PaymentHistoryEntry(year=2026, month=5, raw_status_code="COL", status_code=None,
                            balance=None, past_due=None, amount_paid=None, amount_due=None,
                            remarks=[], source_page=8),
        PaymentHistoryEntry(year=2026, month=4, raw_status_code="OK", status_code=None,
                            balance=None, past_due=None, amount_paid=None, amount_due=None,
                            remarks=[], source_page=8),
    ]
    truth = [{
        "creditor_name": "CAINE & WEINER", "account_number": "88XXXX2211",
        "balance": "1204.00",                      # formatting must not count as a miss
        "original_creditor": "PROGRESSIVE",
        "date_opened": "2024-03-03",
        "source_pages": [8],
        "payment_history": {"2026-05": "COL", "2026-04": "COL"},
    }]

    card = score_batch("b2", "A", [account], truth)
    assert card.matched == 1 and card.missing == [] and card.spurious == []
    assert card.per_field["balance"]["correct"] == 1
    assert card.per_field["date_opened"]["correct"] == 1
    assert card.payment_history.correct == 1 and card.payment_history.total == 2
    assert card.provenance.accuracy == 1.0
    assert card.pages_wrong == []


def test_batch_scoring_flags_provenance_that_lost_the_original_page():
    """The failure bundling could introduce: correct values, wrong pages."""
    from app.services.benchmark.batch_scoring import score_batch

    account = _tradeline(pages=[1], evidence_pages=[], history_pages=[])
    account.creditor_name = "CAINE & WEINER"
    truth = [{"creditor_name": "CAINE & WEINER", "account_number": "88XXXX2211",
              "source_pages": [8]}]

    card = score_batch("b2", "A", [account], truth)
    assert card.provenance.accuracy == 0.0
    assert card.pages_wrong == [{"account": "CAINE & WEINER", "expected": [8], "got": [1]}]
