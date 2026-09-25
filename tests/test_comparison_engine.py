from app.services.comparison_engine import compare_bureau_records
from app.services.findings import Severity


def test_no_findings_for_single_bureau():
    records = [{"bureau": "equifax", "balance": 100.0}]
    assert compare_bureau_records(records) == []


def test_no_findings_when_everything_matches():
    records = [
        {"bureau": "equifax", "balance": 100.0, "account_status": "open"},
        {"bureau": "experian", "balance": 100.0, "account_status": "open"},
    ]
    assert compare_bureau_records(records) == []


def test_penny_balance_difference_is_just_a_difference():
    records = [
        {"bureau": "equifax", "balance": 100.00},
        {"bureau": "experian", "balance": 100.50},
    ]
    findings = compare_bureau_records(records)
    assert len(findings) == 1
    assert findings[0].severity == Severity.DIFFERENCE


def test_balance_mismatch_on_closed_accounts_is_likely_inaccuracy():
    records = [
        {"bureau": "equifax", "balance": 500.0, "account_status": "charged_off"},
        {"bureau": "experian", "balance": 0.0, "account_status": "charged_off"},
    ]
    findings = compare_bureau_records(records)
    balance_finding = next(f for f in findings if f.field == "balance")
    assert balance_finding.severity == Severity.LIKELY_INACCURACY


def test_balance_mismatch_on_open_account_is_only_potential_inconsistency():
    records = [
        {"bureau": "equifax", "balance": 500.0, "account_status": "open"},
        {"bureau": "experian", "balance": 350.0, "account_status": "open"},
    ]
    findings = compare_bureau_records(records)
    balance_finding = next(f for f in findings if f.field == "balance")
    assert balance_finding.severity == Severity.POTENTIAL_INCONSISTENCY


def test_open_vs_closed_status_contradiction_is_likely_inaccuracy():
    records = [
        {"bureau": "equifax", "account_status": "open"},
        {"bureau": "experian", "account_status": "closed"},
    ]
    findings = compare_bureau_records(records)
    status_finding = next(f for f in findings if f.field == "account_status")
    assert status_finding.severity == Severity.LIKELY_INACCURACY


def test_large_dofd_gap_is_supported_dispute_ground():
    records = [
        {"bureau": "equifax", "date_of_first_delinquency": "01/2016"},
        {"bureau": "experian", "date_of_first_delinquency": "06/2018"},
    ]
    findings = compare_bureau_records(records)
    dofd_finding = next(f for f in findings if f.field == "date_of_first_delinquency")
    assert dofd_finding.severity == Severity.SUPPORTED_DISPUTE_GROUND


def test_small_dofd_gap_is_not_flagged():
    records = [
        {"bureau": "equifax", "date_of_first_delinquency": "01/15/2016"},
        {"bureau": "experian", "date_of_first_delinquency": "01/20/2016"},
    ]
    findings = compare_bureau_records(records)
    assert not any(f.field == "date_of_first_delinquency" for f in findings)


def test_findings_sorted_most_severe_first():
    records = [
        {
            "bureau": "equifax",
            "balance": 100.5,
            "account_status": "open",
            "date_of_first_delinquency": "01/2016",
        },
        {
            "bureau": "experian",
            "balance": 100.0,
            "account_status": "closed",
            "date_of_first_delinquency": "06/2018",
        },
    ]
    findings = compare_bureau_records(records)
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: list(Severity).index(s), reverse=True)
    assert severities[0] == Severity.SUPPORTED_DISPUTE_GROUND


def test_late_status_balance_gap_is_not_treated_as_terminal():
    # A 30-days-late account is still active; its balance legitimately moves.
    records = [
        {"bureau": "equifax", "balance": 500.0, "account_status": "30d_late"},
        {"bureau": "experian", "balance": 350.0, "account_status": "30d_late"},
    ]
    balance = next(f for f in compare_bureau_records(records) if f.field == "balance")
    assert balance.severity == Severity.POTENTIAL_INCONSISTENCY


def test_month_name_dates_are_compared():
    records = [
        {"bureau": "equifax", "date_of_first_delinquency": "January 2016"},
        {"bureau": "experian", "date_of_first_delinquency": "06/2018"},
    ]
    dofd = next(f for f in compare_bureau_records(records) if f.field == "date_of_first_delinquency")
    assert dofd.severity == Severity.SUPPORTED_DISPUTE_GROUND


def test_credit_limit_mismatch_flagged():
    records = [
        {"bureau": "equifax", "credit_limit": 2000.0},
        {"bureau": "experian", "credit_limit": 1500.0},
    ]
    assert [f.rule for f in compare_bureau_records(records)] == ["cross_bureau.credit_limit"]


# ── Regression: the live Capital One false positive ─────────────────────
# Clean Equifax and TransUnion disclosures BOTH printed the Capital One
# account ending 7805 as Closed. Equifax said "Pays account as agreed",
# TransUnion said "Paid or paying as agreed" — the same statement in
# different words. Normalizing those onto an open-ish `current` and a
# closed-ish `paid` produced "one bureau reports this account open while
# another reports it closed" about an account neither bureau reported open.
CAPITAL_ONE_7805 = [
    {
        "bureau": "equifax",
        "creditor_name": "CAPITAL ONE",
        "account_number": "517805XXXXXX7805",
        "open_closed": "Closed",
        "account_status_raw": "Pays account as agreed",
        "account_status": "current",
        "payment_status": "Pays account as agreed",
        "balance": 0.0,
        "date_opened": "03/2019",
    },
    {
        "bureau": "transunion",
        "creditor_name": "CAPITAL ONE",
        "account_number": "517805XXXXXX7805",
        "open_closed": "Closed",
        "account_status_raw": "Paid or paying as agreed",
        "account_status": "paid",
        "payment_status": "Paid or paying as agreed",
        "balance": 0.0,
        "date_opened": "03/2019",
    },
]


def test_capital_one_closed_as_agreed_pair_produces_no_findings():
    """Both bureaus say Closed and both say the account was paid as agreed.
    There is nothing here to dispute."""
    assert compare_bureau_records(CAPITAL_ONE_7805) == []


def test_lifecycle_is_never_inferred_from_a_payment_phrase():
    from app.services.account_semantics import AS_AGREED, CLOSED, account_lifecycle, payment_performance

    equifax, transunion = CAPITAL_ONE_7805
    # Both are CLOSED, from the report's own open/closed field.
    assert account_lifecycle(equifax) == account_lifecycle(transunion) == CLOSED
    # And both wordings mean the same standing.
    assert payment_performance(equifax) == payment_performance(transunion) == AS_AGREED


def test_differing_wording_for_the_same_standing_is_not_a_discrepancy():
    """'Pays account as agreed' and 'Paid or paying as agreed' differ only in
    wording, so payment_status must not report a contradiction."""
    findings = compare_bureau_records([
        {"bureau": "equifax", "payment_status": "Pays account as agreed"},
        {"bureau": "transunion", "payment_status": "Paid or paying as agreed"},
    ])
    assert [f.field for f in findings] == []


def test_as_agreed_against_a_derogatory_standing_is_still_flagged():
    """Normalizing wording must not blunt a real disagreement: one bureau
    saying 'as agreed' while another reports a charge-off is exactly the
    contradiction this engine exists to find."""
    findings = compare_bureau_records([
        {"bureau": "equifax", "payment_status": "Pays account as agreed"},
        {"bureau": "transunion", "payment_status": "Charged off as bad debt"},
    ])
    assert any(f.severity == Severity.LIKELY_INACCURACY for f in findings)


def test_closed_on_one_bureau_open_on_the_other_is_still_flagged():
    """The real contradiction the false positive was masquerading as."""
    findings = compare_bureau_records([
        {"bureau": "equifax", "open_closed": "Closed", "account_status_raw": "Pays account as agreed"},
        {"bureau": "transunion", "open_closed": "Open", "account_status_raw": "Paid or paying as agreed"},
    ])
    status_finding = next(f for f in findings if f.field == "account_status")
    assert status_finding.severity == Severity.LIKELY_INACCURACY
    assert set(status_finding.values.values()) == {"open", "closed"}
