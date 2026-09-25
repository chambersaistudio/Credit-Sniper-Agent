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
    # Per account: how many months the truth records, how many the model
    # returned, and how many agreed. An overall percentage hides whether one
    # account was misread or every account lost the same month.
    months_by_account: dict[str, dict[str, int]] = field(default_factory=dict)
    provenance: Tally = field(default_factory=Tally)
    pages_wrong: list[dict[str, Any]] = field(default_factory=list)
    # Pages an account claimed that the truth does not list. Correct pages
    # plus invented ones is not correct provenance.
    pages_hallucinated: list[dict[str, Any]] = field(default_factory=list)
    # Months returned twice for one account, or with an impossible month
    # number. Both are malformed output, not merely inaccurate.
    duplicate_months: list[dict[str, Any]] = field(default_factory=list)
    malformed_months: list[dict[str, Any]] = field(default_factory=list)
    extra_months: int = 0
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
            # Every payment-history miss is kept: which months a model read
            # wrong is the comparison, not a footnote to it.
            "payment_history": {**self.payment_history.to_dict(max_misses=None),
                                "months_expected": self.months_expected,
                                "months_extracted": self.months_extracted,
                                "by_account": self.months_by_account},
            "provenance": {**self.provenance.to_dict(), "pages_wrong": self.pages_wrong,
                           "pages_hallucinated": self.pages_hallucinated},
            "malformed": {"duplicate_months": self.duplicate_months,
                          "malformed_months": self.malformed_months,
                          "extra_months": self.extra_months},
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
        got: dict[str, str] = {}
        for entry in account.payment_history or []:
            if not isinstance(entry.month, int) or not 1 <= entry.month <= 12:
                # An impossible month is malformed output, and collapsing it
                # into a dict would hide it entirely.
                card.malformed_months.append({"account": account.creditor_name,
                                              "year": entry.year, "month": entry.month,
                                              "code": entry.raw_status_code})
                continue
            key = f"{entry.year}-{entry.month:02d}"
            if key in got:
                card.duplicate_months.append({"account": account.creditor_name, "month": key,
                                              "first": got[key], "second": entry.raw_status_code})
                continue  # the first cell stands; the repeat is recorded
            got[key] = entry.raw_status_code
        card.months_extracted += len(got)
        per_account = card.months_by_account.setdefault(
            account.creditor_name, {"expected": 0, "extracted": len(got), "correct": 0}
        )
        per_account["extracted"] = len(got)
        if isinstance(truth_months, dict):
            card.months_expected += len(truth_months)
            per_account["expected"] = len(truth_months)
            # Months the model returned that the truth does not record. Only a
            # miss when the truth declares itself complete — a grid can
            # legitimately extend further back than the truth was written for.
            extras = sorted(set(got) - set(truth_months))
            card.extra_months += len(extras)
            if expected.get("payment_history_complete") and extras:
                for month in extras:
                    card.payment_history.record(False, {
                        "account": account.creditor_name, "month": month,
                        "expected": None, "got": got[month], "missing": False,
                        "not_in_truth": True,
                    })
            for month in sorted(truth_months):
                code = truth_months[month]
                ok = normalize_text(got.get(month)) == normalize_text(code)
                per_account["correct"] += int(ok)
                card.payment_history.record(ok, {
                    "account": account.creditor_name,
                    "month": month,
                    "expected": code,
                    # None means the model returned no cell for that month at
                    # all, which is a different failure from reading it wrong.
                    "got": got.get(month),
                    "missing": month not in got,
                })

        # Provenance is scored against ORIGINAL page numbers — the whole point
        # of bundling is that batching must not disturb them.
        wanted = expected.get("source_pages")
        pages = list(account.source_pages or [])
        if wanted:
            # Every page claimed must be a page the truth lists, and at least
            # one must be. Scoring an overlap as correct would pass an account
            # that cited the right page AND three invented ones — provenance
            # nobody could audit.
            overlap = set(pages) & set(wanted)
            hallucinated = sorted(set(pages) - set(wanted))
            ok = bool(overlap) and not hallucinated
            if not overlap:
                # Nothing it cited is a page the truth lists: wholly wrong.
                card.pages_wrong.append({"account": account.creditor_name,
                                         "expected": wanted, "got": pages})
            elif hallucinated:
                # The right page AND invented ones — provenance nobody could
                # audit, and a different failure from citing the wrong page.
                card.pages_hallucinated.append({"account": account.creditor_name,
                                                "expected": wanted, "got": pages,
                                                "not_in_truth": hallucinated})
        else:
            ok = bool(pages)
        card.provenance.record(ok, {"account": account.creditor_name,
                                    "expected_pages": wanted or "any", "got_pages": pages})
    # A tradeline the model never returned must not simply shrink the
    # denominator. Scoring only matched accounts meant a model that dropped an
    # account scored HIGHER than one that read it imperfectly — the ranking
    # exactly backwards. Every field and month the truth states for a missing
    # account is counted as a miss.
    for expected in (a for v in remaining.values() for a in v):
        name = expected.get("creditor_name")
        for field_name in SCORED_FIELDS:
            if field_name not in expected:
                continue
            counts = card.per_field.setdefault(field_name, {"correct": 0, "total": 0})
            counts["total"] += 1
            card.fields.record(False, {"account": name, "field": field_name,
                                       "expected": expected[field_name], "got": None,
                                       "account_missing": True})
        truth_months = expected.get("payment_history")
        if isinstance(truth_months, dict):
            card.months_expected += len(truth_months)
            card.months_by_account[name] = {"expected": len(truth_months),
                                            "extracted": 0, "correct": 0}
            for month in sorted(truth_months):
                card.payment_history.record(False, {
                    "account": name, "month": month, "expected": truth_months[month],
                    "got": None, "missing": True, "account_missing": True,
                })
        card.provenance.record(False, {"account": name,
                                       "expected_pages": expected.get("source_pages") or "any",
                                       "got_pages": [], "account_missing": True})

    for bad in card.duplicate_months + card.malformed_months:
        card.payment_history.record(False, {
            "account": bad["account"],
            "month": bad.get("month") or f"{bad.get('year')}-{bad.get('month')}",
            "expected": "one well-formed cell", "got": "duplicate or malformed",
            "missing": False, "malformed": True,
        })
    return card
