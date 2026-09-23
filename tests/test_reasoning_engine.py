import uuid
from types import SimpleNamespace

from app.services.ai import ModelTier
from app.services.credit_profile import AccountView
from app.services.findings import Finding, Severity
from app.services.reasoning_engine import ClaimProposalOut, evaluate_account

DOFD = Finding("cross_bureau.dofd", "date_of_first_delinquency", Severity.SUPPORTED_DISPUTE_GROUND,
               "DOFD differs by 880 days", {"equifax": "01/2016", "experian": "06/2018"})
BALANCE = Finding("cross_bureau.balance", "balance", Severity.POTENTIAL_INCONSISTENCY,
                  "Balances differ", {"equifax": 500.0, "experian": 350.0})


def _view(findings):
    canonical = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), creditor_name="Capital One", account_type="Credit Card")
    records = [{"bureau": "equifax", "balance": 500.0}, {"bureau": "experian", "balance": 350.0}]
    return AccountView(canonical=canonical, records=records, history_count=2, findings=findings)


def _answer(**overrides):
    base = dict(
        has_dispute_ground=True, reasoning="DOFD conflict.", supporting_finding_ids=["F1"],
        disputed_fields=["date_of_first_delinquency"], recipients=["equifax", "experian"],
        legal_basis=[{"reference_id": "fcra_605_obsolete", "applies_because": "DOFD sets the period"}],
        requested_remedy="Correct the DOFD or delete.", additional_evidence_needed=[],
        recommended_action="dispute_bureau", confidence=0.85,
    )
    return ClaimProposalOut(**{**base, **overrides})


async def test_grounded_proposal_passes_through(fake_ai):
    provider = fake_ai(lambda *_: _answer())
    proposal = await evaluate_account(_view([DOFD]))
    assert proposal.has_dispute_ground
    assert [f.rule for f in proposal.supporting_findings] == ["cross_bureau.dofd"]
    assert proposal.tier == ModelTier.REASONING
    assert len(provider.calls) == 1


async def test_prompt_contains_no_consumer_identity(fake_ai):
    provider = fake_ai(lambda *_: _answer())
    await evaluate_account(_view([DOFD]))
    prompt = provider.calls[0]["prompt"]
    assert "Capital One" in prompt and "F1" in prompt
    assert "ssn" not in prompt.lower()


async def test_dispute_without_real_findings_is_downgraded(fake_ai):
    fake_ai(lambda *_: _answer(supporting_finding_ids=["F9"]))
    proposal = await evaluate_account(_view([BALANCE]))
    assert not proposal.has_dispute_ground
    assert proposal.recommended_action == "need_more_evidence"
    assert proposal.validation_notes


async def test_uncatalogued_citations_and_unreporting_bureaus_are_dropped(fake_ai):
    fake_ai(lambda *_: _answer(
        recipients=["equifax", "transunion"],
        legal_basis=[{"reference_id": "made_up_statute", "applies_because": "x"},
                     {"reference_id": "fcra_605_obsolete", "applies_because": "y"}],
    ))
    proposal = await evaluate_account(_view([DOFD]))
    assert proposal.recipients == ["equifax"]  # TransUnion doesn't report this account
    assert list(proposal.legal_explanations) == ["fcra_605_obsolete"]


async def test_low_confidence_escalates(fake_ai):
    provider = fake_ai(lambda _t, _p, config: _answer(confidence=0.3 if config.effort == "high" else 0.8))
    proposal = await evaluate_account(_view([DOFD]))
    assert [c["config"].effort for c in provider.calls] == ["high", "max"]
    assert proposal.tier == ModelTier.ESCALATION
    assert proposal.confidence == 0.8


async def test_model_rejecting_a_supported_ground_escalates(fake_ai):
    no_ground = _answer(has_dispute_ground=False, supporting_finding_ids=[], recipients=[], legal_basis=[],
                        recommended_action="no_dispute", confidence=0.9)
    provider = fake_ai(lambda *_: no_ground)
    proposal = await evaluate_account(_view([DOFD]))
    assert len(provider.calls) == 2
    assert not proposal.has_dispute_ground


async def test_no_findings_no_dispute_no_escalation(fake_ai):
    provider = fake_ai(lambda *_: _answer(has_dispute_ground=False, supporting_finding_ids=[], recipients=[],
                                          legal_basis=[], recommended_action="no_dispute", confidence=0.95))
    proposal = await evaluate_account(_view([]))
    assert proposal.recommended_action == "no_dispute"
    assert len(provider.calls) == 1
