import uuid
from types import SimpleNamespace

from app.services.dispute_package import build_package, render_statement
from app.services.package_pdf import render_package_pdf

USER = SimpleNamespace(full_name="Jane Q Consumer", address="1 Main St", city="Anytown", state="CA",
                       zip_code="90210", date_of_birth="01/01/1980", ssn_last_four="1234")
BALANCE = {"rule": "cross_bureau.balance", "field": "balance", "severity": "likely_inaccuracy",
           "rationale": "x", "values": {"equifax": 3450.0, "experian": 3200.0}, "bureau": None}
EXPERIAN_ONLY = {"rule": "record.obsolete_reporting", "field": "date_of_first_delinquency",
                 "severity": "supported_dispute_ground", "rationale": "x", "bureau": "experian",
                 "values": {"date_of_first_delinquency": "2015-01-01", "reporting_period_end": "2022-06-30"}}


def _claim(*findings, remedy="Correct the balance."):
    evidence = [SimpleNamespace(id=uuid.uuid4(), source_type="finding", bureau=f.get("bureau"), data=f) for f in findings]
    return SimpleNamespace(id=uuid.uuid4(), evidence=evidence, requested_remedy=remedy,
                           legal_reference_ids=["fcra_611_reinvestigation", "fcra_623a_furnisher_accuracy", "not_real"])


def _case(recipient_type="bureau", recipient_name="equifax", address=None):
    return SimpleNamespace(recipient_type=recipient_type, recipient_name=recipient_name, recipient_address=address)


def test_statement_states_each_bureaus_value():
    text = render_statement(BALANCE)
    assert "$3,450.00" in text and "$3,200.00" in text and "Equifax" in text


def test_bureau_letter_excludes_other_bureaus_single_record_findings():
    package = build_package(_case(), [_claim(BALANCE, EXPERIAN_ONLY)], USER, "Capital One", "XXXX-4521")
    assert len(package["statements"]) == 1
    assert "ending 4521" in package["subject"]


def test_only_catalogued_citations_for_the_recipient_type():
    package = build_package(_case(), [_claim(BALANCE)], USER, "Capital One", "4521")
    ids = [r["id"] for r in package["legal_references"]]
    assert ids == ["fcra_611_reinvestigation"]  # furnisher-only and unknown ids dropped


def test_ready_requires_complete_profile():
    incomplete = SimpleNamespace(**{**USER.__dict__, "address": None})
    package = build_package(_case(), [_claim(BALANCE)], incomplete, "Capital One", "4521")
    assert not package["ready"]
    assert any("missing address" in w for w in package["warnings"])


def test_furnisher_package_needs_an_address():
    package = build_package(_case("furnisher", "Capital One"), [_claim(BALANCE)], USER, "Capital One", "4521")
    assert not package["ready"]
    ready = build_package(_case("furnisher", "Capital One", "PO Box 1\nRichmond, VA"), [_claim(BALANCE)], USER, "Capital One", "4521")
    assert ready["ready"]
    assert any("1022.43" in r for r in ready["requests"])


def test_letter_never_contains_full_ssn_field():
    package = build_package(_case(), [_claim(BALANCE)], USER, "Capital One", "4521")
    assert "XXX-XX-1234" in package["body"]


def test_pdf_renders():
    package = build_package(_case(), [_claim(BALANCE)], USER, "Capital One & Sons <LLC>", "4521")
    assert render_package_pdf(package).startswith(b"%PDF")
