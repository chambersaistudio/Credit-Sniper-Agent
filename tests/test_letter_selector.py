"""
Tests for letter type selection logic.
"""
import pytest
from app.services.letter_generator import select_letter_type


def test_select_metro2_when_violations_exist():
    analysis = {
        "violations": [{"violation_type": "metro2", "specific_violation": "Field 26 DOFD"}],
        "primary_strategy": "factual_dispute",
    }
    assert select_letter_type(analysis, round_number=1) == "metro2_compliance"


def test_select_fcra_when_no_metro2():
    analysis = {
        "violations": [{"violation_type": "fcra", "specific_violation": "15 U.S.C. § 1681c"}],
        "primary_strategy": "factual_dispute",
    }
    assert select_letter_type(analysis, round_number=1) == "fcra_violation"


def test_select_primary_strategy_fallback():
    analysis = {
        "violations": [],
        "primary_strategy": "section_609",
    }
    assert select_letter_type(analysis, round_number=1) == "section_609"


def test_round2_escalation_from_factual():
    analysis = {
        "violations": [],
        "primary_strategy": "factual_dispute",
    }
    assert select_letter_type(analysis, round_number=2) == "section_609"


def test_round2_escalation_from_609():
    analysis = {
        "violations": [],
        "primary_strategy": "section_609",
    }
    assert select_letter_type(analysis, round_number=2) == "section_611"


def test_round3_always_fcra():
    analysis = {"violations": [], "primary_strategy": "factual_dispute"}
    assert select_letter_type(analysis, round_number=3) == "fcra_violation"
