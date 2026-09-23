from app.services.comparison_engine import compare_canonical_account, Severity


def test_no_findings_for_single_bureau():
    records = [{"bureau": "equifax", "balance": 100.0}]
    assert compare_canonical_account(records) == []


def test_no_findings_when_everything_matches():
    records = [
        {"bureau": "equifax", "balance": 100.0, "account_status": "open"},
        {"bureau": "experian", "balance": 100.0, "account_status": "open"},
    ]
    assert compare_canonical_account(records) == []


def test_penny_balance_difference_is_just_a_difference():
    records = [
        {"bureau": "equifax", "balance": 100.00},
        {"bureau": "experian", "balance": 100.50},
    ]
    findings = compare_canonical_account(records)
    assert len(findings) == 1
    assert findings[0].severity == Severity.DIFFERENCE


def test_balance_mismatch_on_closed_accounts_is_likely_inaccuracy():
    records = [
        {"bureau": "equifax", "balance": 500.0, "account_status": "charged_off"},
        {"bureau": "experian", "balance": 0.0, "account_status": "charged_off"},
    ]
    findings = compare_canonical_account(records)
    balance_finding = next(f for f in findings if f.field == "balance")
    assert balance_finding.severity == Severity.LIKELY_INACCURACY


def test_balance_mismatch_on_open_account_is_only_potential_inconsistency():
    records = [
        {"bureau": "equifax", "balance": 500.0, "account_status": "open"},
        {"bureau": "experian", "balance": 350.0, "account_status": "open"},
    ]
    findings = compare_canonical_account(records)
    balance_finding = next(f for f in findings if f.field == "balance")
    assert balance_finding.severity == Severity.POTENTIAL_INCONSISTENCY


def test_open_vs_closed_status_contradiction_is_likely_inaccuracy():
    records = [
        {"bureau": "equifax", "account_status": "open"},
        {"bureau": "experian", "account_status": "closed"},
    ]
    findings = compare_canonical_account(records)
    status_finding = next(f for f in findings if f.field == "account_status")
    assert status_finding.severity == Severity.LIKELY_INACCURACY


def test_large_dofd_gap_is_supported_dispute_ground():
    records = [
        {"bureau": "equifax", "date_of_first_delinquency": "01/2016"},
        {"bureau": "experian", "date_of_first_delinquency": "06/2018"},
    ]
    findings = compare_canonical_account(records)
    dofd_finding = next(f for f in findings if f.field == "date_of_first_delinquency")
    assert dofd_finding.severity == Severity.SUPPORTED_DISPUTE_GROUND


def test_small_dofd_gap_is_not_flagged():
    records = [
        {"bureau": "equifax", "date_of_first_delinquency": "01/15/2016"},
        {"bureau": "experian", "date_of_first_delinquency": "01/20/2016"},
    ]
    findings = compare_canonical_account(records)
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
    findings = compare_canonical_account(records)
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: list(Severity).index(s), reverse=True)
    assert severities[0] == Severity.SUPPORTED_DISPUTE_GROUND
