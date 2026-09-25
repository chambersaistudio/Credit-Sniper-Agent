"""
Scoring one extracted batch against confirmed ground truth.

Reuses the report-level benchmark's comparison rules exactly — same field
list, same money/date normalization, same matching — so a batch result and a
whole-report result are measured on the same terms and can be compared.

Nothing here calls a model.
"""
from dataclasses import dataclass, field
from typing import Any

from app.services.benchmark.groundtruth import (
    SCORED_FIELDS, account_key, normalize_text, values_match,
)
from app.services.benchmark.scoring import Tally


@dataclass
class BatchScorecard:
    batch_id: str
    config: str
    asked: int = 0
    returned: int = 0
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    fields: Tally = field(default_factory=Tally)
    per_field: dict[str, dict[str, int]] = field(default_factory=dict)
    payment_history: Tally = field(default_factory=Tally)
    months_expected: int = 0
    months_extracted: int = 0
    provenance: Tally = field(default_factory=Tally)
    pages_wrong: list[dict[str, Any]] = field(default_factory=list)
    gate_ok: bool = False
    gate_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "config": self.config,
            "accounts": {"asked": self.asked, "returned": self.returned,
                         "matched": self.matched, "missing": self.missing,
                         "spurious": self.spurious},
            "field_accuracy": self.fields.to_dict(),
            "per_field": self.per_field,
            "payment_history": {**self.payment_history.to_dict(),
                                "months_expected": self.months_expected,
                                "months_extracted": self.months_extracted},
            "provenance": {**self.provenance.to_dict(), "pages_wrong": self.pages_wrong},
            "gate": {"ok": self.gate_ok, "reasons": self.gate_reasons},
        }


def score_batch(batch_id: str, config: str, accounts: list, truth_accounts: list[dict],
                quality=None) -> BatchScorecard:
    """Score the tradelines a batch returned against the truth for that batch.

    `accounts` must already have had their page references remapped to
    ORIGINAL page numbers, because that is what the ground truth records.
    """
    card = BatchScorecard(batch_id=batch_id, config=config,
                          asked=len(truth_accounts), returned=len(accounts))
    if quality is not None:
        card.gate_ok = quality.ok
        card.gate_reasons = quality.reasons

    remaining = {}
    for expected in truth_accounts:
        remaining.setdefault(
            account_key(expected.get("creditor_name"), expected.get("account_number")), []
        ).append(expected)

    pairs = []
    for account in accounts:
        key = account_key(account.creditor_name, account.account_number)
        bucket = remaining.get(key)
        if not bucket:
            loose = [k for k, v in remaining.items() if v and k[0] == key[0]]
            bucket = remaining[loose[0]] if loose else None
        if bucket:
            pairs.append((bucket.pop(0), account))
        else:
            card.spurious.append(account.creditor_name)
    card.matched = len(pairs)
    card.missing = [a.get("creditor_name") for v in remaining.values() for a in v]

    for expected, account in pairs:
        for name in SCORED_FIELDS:
            if name not in expected:
                continue
            ok = values_match(name, expected[name], getattr(account, name, None))
            counts = card.per_field.setdefault(name, {"correct": 0, "total": 0})
            counts["total"] += 1
            counts["correct"] += int(ok)
            card.fields.record(ok, {"account": account.creditor_name, "field": name,
                                    "expected": expected[name],
                                    "got": getattr(account, name, None)})

        truth_months = expected.get("payment_history")
        got = {f"{e.year}-{e.month:02d}": e.raw_status_code for e in account.payment_history}
        card.months_extracted += len(got)
        if isinstance(truth_months, dict):
            card.months_expected += len(truth_months)
            for month, code in truth_months.items():
                ok = normalize_text(got.get(month)) == normalize_text(code)
                card.payment_history.record(ok, {"account": account.creditor_name,
                                                 "month": month, "expected": code,
                                                 "got": got.get(month)})

        # Provenance is scored against ORIGINAL page numbers — the whole point
        # of bundling is that batching must not disturb them.
        wanted = expected.get("source_pages")
        pages = list(account.source_pages or [])
        if wanted:
            ok = bool(set(pages) & set(wanted))
            if not ok:
                card.pages_wrong.append({"account": account.creditor_name,
                                         "expected": wanted, "got": pages})
        else:
            ok = bool(pages)
        card.provenance.record(ok, {"account": account.creditor_name,
                                    "expected_pages": wanted or "any", "got_pages": pages})
    return card
