"""
Tests for dispute timing and strategy logic.
"""
import pytest
from datetime import datetime, timezone, timedelta
from app.services.dispute_tracker import (
    calculate_next_round_date,
    is_safe_to_send_next_round,
    _determine_next_step,
    MINIMUM_ROUND_SPACING_DAYS,
    BUREAU_REINVESTIGATION_DAYS,
)


def test_next_round_date_calculation():
    submitted = datetime(2024, 1, 1, tzinfo=timezone.utc)
    next_date = calculate_next_round_date(submitted)
    expected = submitted + timedelta(days=MINIMUM_ROUND_SPACING_DAYS)
    assert next_date == expected


def test_safe_to_send_recent():
    # Submitted 5 days ago — not safe yet
    submitted = datetime.now(timezone.utc) - timedelta(days=5)
    assert is_safe_to_send_next_round(submitted) is False


def test_safe_to_send_old():
    # Submitted 45 days ago — safe
    submitted = datetime.now(timezone.utc) - timedelta(days=45)
    assert is_safe_to_send_next_round(submitted) is True


def test_next_step_removed():
    result = _determine_next_step("removed", 1)
    assert result["action_required"] is False
    assert "monitor" in result["strategy"]


def test_next_step_no_response():
    result = _determine_next_step("no_response", 1)
    assert result["action_required"] is True
    assert result.get("urgent") is True
    assert "deletion" in result["strategy"]


def test_next_step_verified_round1():
    result = _determine_next_step("verified", 1)
    assert result["action_required"] is True
    assert "609" in result["strategy"]


def test_next_step_verified_round2():
    result = _determine_next_step("verified", 2)
    assert result["action_required"] is True
    assert "furnisher" in result["strategy"].lower()


def test_next_step_verified_round3():
    result = _determine_next_step("verified", 3)
    assert result["action_required"] is True
    assert "legal" in result["strategy"].lower()


def test_next_step_denied():
    result = _determine_next_step("denied", 1)
    assert result["action_required"] is True
    assert "new_angle" in result["strategy"]
