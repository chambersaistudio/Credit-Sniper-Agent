"""
What a configuration got right.

Every metric is computed against a human-confirmed ground truth, and every one
is reported as counts rather than a single blended score: a config that reads
14 of 15 accounts and one that invents a 16th are both "93%" and are not the
same failure.

Nothing here calls a model or touches the network.
"""
from dataclasses import dataclass, field
from typing import Any

from app.services.benchmark.groundtruth import (
    INQUIRY_FIELDS, SCORED_FIELDS, GroundTruth, account_key, normalize_text, values_match,
)
from app.services.document_extraction.pipeline import reconcile, triage_findings
from app.services.document_extraction.schema import AuditReport, CreditReportExtraction
from app.services.document_extraction.status import ExtractionStatus
from app.services.extraction_quality import assess_accounts
from app.services.document_extraction.mapping import account_row


@dataclass
class Tally:
    """Correct out of comparable, with the misses kept for reading."""

    correct: int = 0
    total: int = 0
    misses: list[dict[str, Any]] = field(default_factory=list)

    def record(self, ok: bool, detail: dict[str, Any] | None = None) -> None:
        self.total += 1
        if ok:
            self.correct += 1
        elif detail is not None:
            self.misses.append(detail)

    @property
    def accuracy(self) -> float | None:
        return round(self.correct / self.total, 4) if self.total else None

    def to_dict(self, max_misses: int = 25) -> dict[str, Any]:
        return {"correct": self.correct, "total": self.total, "accuracy": self.accuracy,
                "misses": self.misses[:max_misses]}


@dataclass
class Scorecard:
    document: str
    config: str
    # Structure
    expected_accounts: int = 0
    extracted_accounts: int = 0
    matched_accounts: int = 0
    missing_accounts: list[str] = field(default_factory=list)
    spurious_accounts: list[str] = field(default_factory=list)
    # Identification
    bureau_correct: bool | None = None
    report_date_correct: bool | None = None
    score_correct: bool | None = None
    score_type_correct: bool | None = None
    # Content
    fields: Tally = field(default_factory=Tally)
    per_field: dict[str, dict[str, int]] = field(default_factory=dict)
    payment_history: Tally = field(default_factory=Tally)
    payment_history_months_expected: int = 0
    payment_history_months_extracted: int = 0
    inquiries: Tally = field(default_factory=Tally)
    inquiry_count_correct: bool | None = None
    hard_inquiries_overcounted: int = 0
    provenance: Tally = field(default_factory=Tally)
    # Audit behaviour
    audit_findings: int = 0
    audit_blocking: int = 0
    audit_set_aside: int = 0
    audit_false_positives: list[dict[str, Any]] = field(default_factory=list)
    audit_true_positives: int = 0
    # Gate
    final_status: str = ExtractionStatus.FAILED.value
    gate_reasons: list[str] = field(default_factory=list)
    quality_complete: bool = False

    @property
    def account_count_correct(self) -> bool:
        return self.extracted_accounts == self.expected_accounts

    @property
    def audit_false_positive_count(self) -> int:
        return len(self.audit_false_positives)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "config": self.config,
            "accounts": {
                "expected": self.expected_accounts,
                "extracted": self.extracted_accounts,
                "matched": self.matched_accounts,
                "count_correct": self.account_count_correct,
                "missing": self.missing_accounts,
                "spurious": self.spurious_accounts,
            },
            "identification": {
                "bureau_correct": self.bureau_correct,
                "report_date_correct": self.report_date_correct,
                "score_correct": self.score_correct,
                "score_type_correct": self.score_type_correct,
            },
            "field_accuracy": self.fields.to_dict(),
            "per_field": self.per_field,
            "payment_history": {
                **self.payment_history.to_dict(),
                "months_expected": self.payment_history_months_expected,
                "months_extracted": self.payment_history_months_extracted,
            },
            "inquiries": {
                **self.inquiries.to_dict(),
                "count_correct": self.inquiry_count_correct,
                "hard_overcounted": self.hard_inquiries_overcounted,
            },
            "provenance": self.provenance.to_dict(),
            "audit": {
                "findings": self.audit_findings,
                "blocking": self.audit_blocking,
                "set_aside": self.audit_set_aside,
                "false_positives": self.audit_false_positives,
                "true_positives": self.audit_true_positives,
            },
            "gate": {
                "final_status": self.final_status,
                "verified": self.final_status == ExtractionStatus.VERIFIED.value,
                "quality_complete": self.quality_complete,
                "reasons": self.gate_reasons,
            },
        }


def _account_index(records, name_of, number_of) -> dict[tuple[str, str], list[Any]]:
    index: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        index.setdefault(account_key(name_of(record), number_of(record)), []).append(record)
    return index


def _match_accounts(truth: GroundTruth, extraction: CreditReportExtraction):
    """Pair extracted tradelines with their ground truth, and name what didn't
    pair up. Falls back to the creditor name alone when the masked number
    differs — a scrambled number is a field miss, not a missing account."""
    expected = _account_index(truth.accounts, lambda a: a.get("creditor_name"), lambda a: a.get("account_number"))
    remaining = {key: list(values) for key, values in expected.items()}
    pairs, spurious = [], []

    for tradeline in extraction.accounts:
        key = account_key(tradeline.creditor_name, tradeline.account_number)
        bucket = remaining.get(key)
        if not bucket:
            # Same furnisher, different masked digits: still that account.
            loose = [k for k, v in remaining.items() if v and k[0] == key[0]]
            bucket = remaining[loose[0]] if loose else None
        if bucket:
            pairs.append((bucket.pop(0), tradeline))
        else:
            spurious.append(tradeline.creditor_name)

    missing = [a.get("creditor_name") for values in remaining.values() for a in values]
    return pairs, missing, spurious


def _score_fields(scorecard: Scorecard, pairs) -> None:
    for expected, tradeline in pairs:
        for name in SCORED_FIELDS:
            if name not in expected:
                continue  # a partial truth scores only what it states
            ok = values_match(name, expected[name], getattr(tradeline, name, None))
            counts = scorecard.per_field.setdefault(name, {"correct": 0, "total": 0})
            counts["total"] += 1
            counts["correct"] += int(ok)
            scorecard.fields.record(ok, {
                "account": tradeline.creditor_name, "field": name,
                "expected": expected[name], "got": getattr(tradeline, name, None),
            })


def _score_payment_history(scorecard: Scorecard, pairs) -> None:
    """Month-level accuracy. A month the truth records and the extraction
    doesn't is a miss; extra months are reported but not counted against the
    codes, since the grid may legitimately extend further back."""
    for expected, tradeline in pairs:
        truth_months = expected.get("payment_history")
        got = {f"{e.year}-{e.month:02d}": e.raw_status_code for e in tradeline.payment_history}
        scorecard.payment_history_months_extracted += len(got)
        if not isinstance(truth_months, dict):
            continue
        scorecard.payment_history_months_expected += len(truth_months)
        for month, code in truth_months.items():
            ok = normalize_text(got.get(month)) == normalize_text(code)
            scorecard.payment_history.record(ok, {
                "account": tradeline.creditor_name, "month": month,
                "expected": code, "got": got.get(month),
            })


def _score_inquiries(scorecard: Scorecard, truth: GroundTruth, extraction: CreditReportExtraction) -> None:
    scorecard.inquiry_count_correct = len(extraction.inquiries) == len(truth.inquiries)
    expected = {normalize_text(i.get("creditor_name")): i for i in truth.inquiries}
    for inquiry in extraction.inquiries:
        match = expected.get(normalize_text(inquiry.creditor_name))
        if match is None:
            scorecard.inquiries.record(False, {"inquiry": inquiry.creditor_name, "field": "creditor_name",
                                               "expected": None, "got": inquiry.creditor_name})
            continue
        for name in INQUIRY_FIELDS:
            if name not in match:
                continue
            ok = values_match(name, match[name], getattr(inquiry, name, None))
            scorecard.inquiries.record(ok, {
                "inquiry": inquiry.creditor_name, "field": name,
                "expected": match[name], "got": getattr(inquiry, name, None),
            })
        # Calling a promotional or account-review inquiry hard is the specific
        # error that would inflate a consumer's apparent inquiry damage.
        if (normalize_text(inquiry.inquiry_type) == "hard"
                and normalize_text(match.get("inquiry_type")) in (None, "soft")):
            scorecard.hard_inquiries_overcounted += 1


def _score_provenance(scorecard: Scorecard, truth: GroundTruth, pairs) -> None:
    """Can each value be traced back to a page? Without provenance the
    extraction can't be audited later, whatever else it got right."""
    if not truth.expects_provenance:
        return
    for expected, tradeline in pairs:
        pages = list(tradeline.source_pages or [])
        wanted = expected.get("source_pages")
        if wanted:
            ok = bool(set(pages) & set(wanted))
            detail = {"account": tradeline.creditor_name, "expected_pages": wanted, "got_pages": pages}
        else:
            ok = bool(pages)
            detail = {"account": tradeline.creditor_name, "expected_pages": "any", "got_pages": pages}
        scorecard.provenance.record(ok, detail)


def _score_audit(scorecard: Scorecard, truth: GroundTruth, extraction: CreditReportExtraction,
                 audit: AuditReport | None, pairs) -> None:
    """Did the auditor object to things that were actually right?

    A blocking finding against a field the extraction got right, per the ground
    truth, is a false positive: it holds a correct report out of dispute
    analysis. This is the metric that decides whether a cheaper auditor is
    usable at all."""
    if audit is None:
        return
    scorecard.audit_findings = len(audit.findings)
    benign, blocking = triage_findings(extraction, audit)
    scorecard.audit_set_aside = len(benign)
    scorecard.audit_blocking = len(blocking)

    by_name = {normalize_text(t.creditor_name): expected for expected, t in pairs}
    for finding, _ in blocking:
        expected = by_name.get(normalize_text(finding.account_name))
        field_name = finding.field
        if expected is not None and field_name in expected:
            extracted = next(
                (getattr(t, field_name, None) for e, t in pairs
                 if e is expected and hasattr(t, field_name)), None
            )
            if values_match(field_name, expected[field_name], extracted):
                scorecard.audit_false_positives.append({
                    "account": finding.account_name, "field": field_name,
                    "kind": finding.kind, "extracted": extracted,
                    "auditor_wanted": finding.correct_value,
                    "ground_truth": expected[field_name],
                })
                continue
        scorecard.audit_true_positives += 1

    # Miscounting the tradelines is an audit error in its own right.
    if audit.account_count_in_document is not None and truth.account_count:
        if audit.account_count_in_document != truth.account_count:
            scorecard.audit_false_positives.append({
                "account": None, "field": "account_count", "kind": "count",
                "extracted": len(extraction.accounts),
                "auditor_wanted": audit.account_count_in_document,
                "ground_truth": truth.account_count,
            })


def score_extraction(
    truth: GroundTruth, config_name: str,
    extraction: CreditReportExtraction | None, audit: AuditReport | None,
) -> Scorecard:
    """Score one (document, config) run. Never calls a model."""
    scorecard = Scorecard(document=truth.name, config=config_name,
                          expected_accounts=truth.account_count)
    if extraction is None:
        scorecard.gate_reasons = ["No extraction was produced."]
        return scorecard

    scorecard.extracted_accounts = len(extraction.accounts)
    pairs, missing, spurious = _match_accounts(truth, extraction)
    scorecard.matched_accounts = len(pairs)
    scorecard.missing_accounts = missing
    scorecard.spurious_accounts = spurious

    if truth.bureau:
        scorecard.bureau_correct = normalize_text(extraction.bureau) == truth.bureau
    if truth.report_date:
        stated = extraction.document_created_date or extraction.report_date
        scorecard.report_date_correct = values_match("date_opened", truth.report_date, stated)
    if truth.score is not None:
        scorecard.score_correct = extraction.score == truth.score
    if truth.score_type:
        scorecard.score_type_correct = normalize_text(extraction.score_type) == normalize_text(truth.score_type)

    _score_fields(scorecard, pairs)
    _score_payment_history(scorecard, pairs)
    _score_inquiries(scorecard, truth, extraction)
    _score_provenance(scorecard, truth, pairs)
    _score_audit(scorecard, truth, extraction, audit, pairs)

    status, reasons = reconcile(extraction, audit)
    quality = assess_accounts([account_row(t) for t in extraction.accounts])
    scorecard.quality_complete = quality.complete
    # Same cap ingestion applies: a clean gate over rows that don't look like
    # tradelines is not a verified report.
    if status is ExtractionStatus.VERIFIED and not quality.complete:
        status = ExtractionStatus.EXTRACTION_INCOMPLETE
        reasons = [*reasons, *quality.reasons]
    scorecard.final_status = status.value
    scorecard.gate_reasons = reasons
    return scorecard
