"""
Case lifecycle rules — deterministic, no AI.

Deadlines come from what actually happened, not a fixed round spacing: the
reinvestigation clock runs from when the recipient *received* the dispute
(15 U.S.C. § 1681i(a)(1); 12 C.F.R. § 1022.43(e) for direct furnisher
disputes). When only the send date is known, the due date is an estimate
and labeled as such. A missed deadline recommends a follow-up — it is never
treated as an automatic deletion.
"""
from datetime import datetime, timedelta, timezone
from enum import Enum


class CaseStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    DELIVERED = "delivered"
    INVESTIGATION_ACTIVE = "investigation_active"
    RESPONSE_RECEIVED = "response_received"
    CORRECTED = "corrected"
    DELETED = "deleted"
    VERIFIED = "verified"
    MORE_INFORMATION_REQUIRED = "more_information_required"
    FOLLOWUP_RECOMMENDED = "followup_recommended"
    RESOLVED = "resolved"
    MONITORING = "monitoring"


S = CaseStatus
TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    S.DRAFT: frozenset({S.AWAITING_APPROVAL, S.RESOLVED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.DRAFT, S.RESOLVED}),
    S.APPROVED: frozenset({S.SUBMITTED, S.DRAFT}),
    S.SUBMITTED: frozenset({S.DELIVERED, S.INVESTIGATION_ACTIVE, S.RESPONSE_RECEIVED}),
    S.DELIVERED: frozenset({S.INVESTIGATION_ACTIVE, S.RESPONSE_RECEIVED}),
    S.INVESTIGATION_ACTIVE: frozenset({S.RESPONSE_RECEIVED, S.MORE_INFORMATION_REQUIRED}),
    S.RESPONSE_RECEIVED: frozenset({S.CORRECTED, S.DELETED, S.VERIFIED, S.MORE_INFORMATION_REQUIRED}),
    S.CORRECTED: frozenset({S.MONITORING, S.FOLLOWUP_RECOMMENDED, S.RESOLVED}),
    S.DELETED: frozenset({S.MONITORING, S.RESOLVED}),
    S.VERIFIED: frozenset({S.FOLLOWUP_RECOMMENDED, S.RESOLVED}),
    S.MORE_INFORMATION_REQUIRED: frozenset({S.DRAFT, S.AWAITING_APPROVAL, S.RESOLVED}),
    S.FOLLOWUP_RECOMMENDED: frozenset({S.DRAFT, S.RESOLVED}),
    S.MONITORING: frozenset({S.FOLLOWUP_RECOMMENDED, S.RESOLVED}),
    S.RESOLVED: frozenset(),
}

# Statuses where the recipient owes a response.
AWAITING_RESPONSE = frozenset({S.SUBMITTED, S.DELIVERED, S.INVESTIGATION_ACTIVE})
# Statuses that need the consumer to do something.
NEEDS_USER = frozenset({S.DRAFT, S.AWAITING_APPROVAL, S.APPROVED, S.MORE_INFORMATION_REQUIRED, S.FOLLOWUP_RECOMMENDED, S.VERIFIED})

REINVESTIGATION_DAYS = 30
EXTENDED_REINVESTIGATION_DAYS = 45

OUTCOMES = {
    "corrected": S.CORRECTED,
    "deleted": S.DELETED,
    "verified": S.VERIFIED,
    "more_information_required": S.MORE_INFORMATION_REQUIRED,
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: CaseStatus, target: CaseStatus) -> None:
    if target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[current])) or "none (terminal)"
        raise InvalidTransition(f"Can't move a case from {current.value} to {target.value}. Allowed: {allowed}.")


def compute_response_due(
    submitted_at: datetime | None, delivered_at: datetime | None, extended: bool = False
) -> tuple[datetime | None, str | None]:
    """(due date, basis). Basis is "delivered" when receipt is known,
    "submitted_estimate" when only the send date is — the real clock may
    start later, when the recipient receives it."""
    start, basis = (delivered_at, "delivered") if delivered_at else (submitted_at, "submitted_estimate")
    if start is None:
        return None, None
    days = EXTENDED_REINVESTIGATION_DAYS if extended else REINVESTIGATION_DAYS
    return start + timedelta(days=days), basis


def is_overdue(status: CaseStatus, response_due_at: datetime | None, now: datetime | None = None) -> bool:
    if status not in AWAITING_RESPONSE or response_due_at is None:
        return False
    return (now or datetime.now(timezone.utc)) > response_due_at


def next_action(status: CaseStatus, response_due_at: datetime | None, now: datetime | None = None) -> str:
    """Deterministic plain-language next step for the consumer."""
    if is_overdue(status, response_due_at, now):
        return (
            f"The response was due {response_due_at.date().isoformat()} and nothing has been recorded. "
            "If you haven't received results, follow up with the recipient and keep proof of your original "
            "submission. Don't assume the item was deleted — check your updated report."
        )
    return {
        S.DRAFT: "Generate the dispute package to review it.",
        S.AWAITING_APPROVAL: "Review the dispute package and approve it, or send it back for changes.",
        S.APPROVED: "Send the package, then record how and when you sent it.",
        S.SUBMITTED: "Waiting for the recipient. Record the delivery date when you have it — the investigation clock runs from receipt.",
        S.DELIVERED: "Waiting for the investigation results.",
        S.INVESTIGATION_ACTIVE: "Waiting for the investigation results.",
        S.RESPONSE_RECEIVED: "Record what the response says: corrected, deleted, verified, or more information requested.",
        S.CORRECTED: "Check your next report to confirm the correction, then resolve or keep monitoring.",
        S.DELETED: "Watch future reports to make sure the item isn't reinserted.",
        S.VERIFIED: "The item was verified. Review whether new evidence supports a follow-up, or request the method of verification.",
        S.MORE_INFORMATION_REQUIRED: "Gather the information requested and revise the package.",
        S.FOLLOWUP_RECOMMENDED: "Start a revised package with any new evidence, or resolve the case.",
        S.MONITORING: "Monitoring future reports for this account.",
        S.RESOLVED: "No action needed.",
    }[status]
