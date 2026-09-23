from datetime import date

from app.services.account_rules import evaluate_record, is_negative, reporting_period_end
from app.services.findings import Severity


def _rules(record, as_of=date(2024, 1, 1)):
    return {f.rule: f for f in evaluate_record({"bureau": "equifax", **record}, as_of)}


def test_reporting_period_is_seven_years_after_180_days():
    assert reporting_period_end(date(2016, 1, 1)) == date(2023, 6, 29)


def test_obsolete_negative_item_is_supported_ground():
    found = _rules({"account_status": "charged_off", "date_of_first_delinquency": "01/2016"})
    assert found["record.obsolete_reporting"].severity == Severity.SUPPORTED_DISPUTE_GROUND


def test_recent_negative_item_is_not_obsolete():
    assert "record.obsolete_reporting" not in _rules({"account_status": "collection", "date_of_first_delinquency": "01/2020"})


def test_positive_old_account_is_never_obsolete():
    assert _rules({"account_status": "open", "date_of_first_delinquency": "01/2010"}) == {}


def test_negative_without_dofd_is_only_a_question():
    finding = _rules({"account_status": "collection"})["record.missing_dofd"]
    assert finding.severity == Severity.POTENTIAL_INCONSISTENCY


def test_impossible_dates_are_likely_inaccuracies():
    found = _rules({
        "account_status": "charged_off", "date_opened": "05/2018",
        "date_of_first_delinquency": "01/2018", "date_closed": "01/2017",
    })
    assert found["record.dofd_before_opened"].severity == Severity.LIKELY_INACCURACY
    assert found["record.closed_before_opened"].severity == Severity.LIKELY_INACCURACY


def test_is_negative_uses_payment_status_too():
    assert is_negative({"account_status": "open", "payment_status": "120 days past due"})
    assert not is_negative({"account_status": "open", "payment_status": "Pays as agreed"})
