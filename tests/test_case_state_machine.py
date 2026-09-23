from datetime import datetime, timedelta, timezone

import pytest

from app.services.case_state_machine import (
    TRANSITIONS, CaseStatus, InvalidTransition, compute_response_due, is_overdue, next_action, validate_transition,
)

NOW = datetime(2024, 3, 1, tzinfo=timezone.utc)


def test_happy_path_is_valid():
    path = [CaseStatus.DRAFT, CaseStatus.AWAITING_APPROVAL, CaseStatus.APPROVED, CaseStatus.SUBMITTED,
            CaseStatus.DELIVERED, CaseStatus.RESPONSE_RECEIVED, CaseStatus.DELETED, CaseStatus.MONITORING,
            CaseStatus.RESOLVED]
    for current, target in zip(path, path[1:]):
        validate_transition(current, target)


def test_cannot_skip_approval():
    with pytest.raises(InvalidTransition):
        validate_transition(CaseStatus.DRAFT, CaseStatus.SUBMITTED)
    with pytest.raises(InvalidTransition):
        validate_transition(CaseStatus.AWAITING_APPROVAL, CaseStatus.SUBMITTED)


def test_resolved_is_terminal():
    assert TRANSITIONS[CaseStatus.RESOLVED] == frozenset()


def test_every_status_has_a_next_action():
    for status in CaseStatus:
        assert next_action(status, None, NOW)


def test_due_date_runs_from_delivery_when_known():
    submitted, delivered = NOW, NOW + timedelta(days=5)
    due, basis = compute_response_due(submitted, delivered)
    assert (due, basis) == (delivered + timedelta(days=30), "delivered")


def test_due_date_from_submission_is_labeled_an_estimate():
    due, basis = compute_response_due(NOW, None)
    assert (due, basis) == (NOW + timedelta(days=30), "submitted_estimate")


def test_extended_investigation_is_45_days():
    due, _ = compute_response_due(NOW, NOW, extended=True)
    assert due == NOW + timedelta(days=45)


def test_overdue_only_while_awaiting_response():
    due = NOW - timedelta(days=1)
    assert is_overdue(CaseStatus.SUBMITTED, due, NOW)
    assert not is_overdue(CaseStatus.VERIFIED, due, NOW)
    assert not is_overdue(CaseStatus.SUBMITTED, NOW + timedelta(days=1), NOW)


def test_overdue_next_action_never_assumes_deletion():
    message = next_action(CaseStatus.SUBMITTED, NOW - timedelta(days=1), NOW)
    assert "Don't assume the item was deleted" in message
