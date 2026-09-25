"""
The quality gate for one extracted batch.

A batch is trusted only when it answers exactly the question it was asked:
the tradelines the index named, all of them, none of them substituted, and
every page reference resolving to a real page of the original report.

The identity check matters more here than it did for single-pass extraction.
A batch model sees four accounts on a handful of pages, several of which may
be collections from the same agency — the failure to catch is a plausible
wrong answer, not an obviously empty one.

Nothing here calls a model.
"""
from dataclasses import dataclass, field
from typing import Any

from app.services.document_extraction.index_quality import identity_key
from app.services.document_extraction.page_bundle import RemapReport


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).strip().lower()


def _account_key(creditor_name, account_number, original_creditor) -> tuple[str, str, str]:
    """Same identity rule the index uses, applied to an extracted tradeline."""
    import re

    digits = re.sub(r"\D", "", str(account_number or ""))
    return (_norm(creditor_name), digits[-4:] if len(digits) >= 4 else digits,
            _norm(original_creditor))


def _match(asked: dict, accounts: list, plan) -> tuple[dict, list[str], list[str]]:
    """Pair returned accounts with the tradelines the batch asked for.

    Matched in tiers, strongest identity first, because the two reads can
    legitimately disagree on some fields and not others. Masked digits differ
    between the index and a detailed read — TransUnion warns they can be
    scrambled — and a detailed read can omit an original creditor the index
    recorded. Neither makes it a different account.

    Name alone is a last resort, and ONLY when that name appears once in the
    batch. Jefferson Capital appears twice under different original creditors,
    so matching it by name would pair the two arbitrarily and silently swap
    their contents.

    Returns (pairs, unexpected names, identity-drift reasons).
    """
    by_name: dict[str, int] = {}
    for key in asked:
        by_name[key[0]] = by_name.get(key[0], 0) + 1

    pairs: dict[Any, Any] = {}
    leftovers: list[Any] = []
    indexed = {key: t for key, t in zip(asked, plan.tradelines)}

    def claim(key, account) -> bool:
        if key is None or key in pairs:
            return False
        pairs[key] = account
        return True

    for account in accounts:
        key = _account_key(account.creditor_name, account.account_number,
                           account.original_creditor)
        if claim(key if key in asked else None, account):
            continue
        leftovers.append((key, account))

    # Tier 2: same name and masked digits; the original creditor was dropped.
    # Tier 3: same name and original creditor; the masking differs.
    # Tier 4: the name is unique in this batch, so nothing else can be meant.
    for tier in (
        lambda k, c: k[0] == c[0] and k[1] and k[1] == c[1],
        lambda k, c: k[0] == c[0] and k[2] and k[2] == c[2],
        lambda k, c: k[0] == c[0] and by_name.get(c[0], 0) == 1,
    ):
        still: list[Any] = []
        for key, account in leftovers:
            candidate = next((k for k in asked if k not in pairs and tier(k, key)), None)
            if not claim(candidate, account):
                still.append((key, account))
        leftovers = still

    drift: list[str] = []
    for key, account in pairs.items():
        entry = indexed.get(key)
        if entry is None:
            continue
        if entry.original_creditor and not account.original_creditor:
            drift.append(
                f"{entry.creditor_name}: the index recorded original creditor "
                f"'{entry.original_creditor}' but the detailed read returned none."
            )
        if entry.account_number and not account.account_number:
            drift.append(
                f"{entry.creditor_name}: the index recorded an account number but the "
                f"detailed read returned none."
            )
    return pairs, [a.creditor_name for _, a in leftovers], drift


@dataclass
class BatchQuality:
    ok: bool
    asked: int
    returned: int
    matched: int
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    without_pages: list[str] = field(default_factory=list)
    pages_outside_bundle: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "asked": self.asked, "returned": self.returned,
            "matched": self.matched, "reasons": self.reasons, "missing": self.missing,
            "unexpected": self.unexpected, "without_pages": self.without_pages,
            "pages_outside_bundle": self.pages_outside_bundle,
        }


def assess_batch(batch, plan, bundle, remap: RemapReport | None = None) -> BatchQuality:
    """Did this batch return the tradelines it was asked for, provably located?

    `batch` has already had its page references translated to original pages,
    so the page checks here are against the real report's numbering.
    """
    asked = {identity_key(t): t.creditor_name for t in plan.tradelines}
    accounts = list(batch.accounts or []) if batch is not None else []
    reasons: list[str] = []

    if batch is None:
        return BatchQuality(ok=False, asked=len(asked), returned=0, matched=0,
                            reasons=["No batch was produced."])

    returned_keys, unexpected, drift = _match(asked, accounts, plan)
    missing = [name for key, name in asked.items() if key not in returned_keys]
    reasons.extend(drift)
    # The model saying so itself is the honest failure, and is reported as
    # missing rather than as a separate category.
    declared_missing = [n for n in (batch.missing_tradelines or [])]

    without_pages = [a.creditor_name for a in returned_keys.values() if not a.source_pages]
    outside = [p for a in returned_keys.values() for p in (a.source_pages or [])
               if p not in bundle.pages]

    if missing:
        reasons.append(f"Batch did not return: {', '.join(sorted(set(missing)))}.")
    if unexpected:
        reasons.append(
            f"Batch returned tradelines it was not asked for: {', '.join(sorted(set(unexpected)))}."
        )
    if declared_missing:
        reasons.append(
            f"Model reported these tradelines absent from the supplied pages: "
            f"{', '.join(sorted(set(declared_missing)))}."
        )
    if without_pages:
        reasons.append(f"{len(without_pages)} tradeline(s) have no source page.")
    if outside:
        # Provenance pointing at a page the batch never saw is not provenance.
        reasons.append(
            f"Page references outside the supplied pages: {sorted(set(outside))}."
        )
    if remap and remap.out_of_range:
        reasons.append(
            f"Model reported page numbers this bundle does not have: "
            f"{sorted(set(remap.out_of_range))}."
        )
    if batch.unreadable_pages:
        # Already original page numbers by the time the gate sees them.
        reasons.append(f"Pages could not be read reliably: {sorted(batch.unreadable_pages)}.")

    return BatchQuality(
        ok=not reasons,
        asked=len(asked),
        returned=len(accounts),
        matched=len(returned_keys),
        reasons=reasons,
        missing=sorted(set(missing)),
        unexpected=sorted(set(unexpected)),
        without_pages=without_pages,
        pages_outside_bundle=sorted(set(outside)),
    )
