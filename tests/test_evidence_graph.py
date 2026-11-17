"""Synthetic M4 golden case; no consumer data or provider calls."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.services.evidence_graph import build_evidence_graph, relationships_for

OWNER = uuid.uuid4()


def view(name, bureau, **fields):
    record = {
        'creditor_name': name, 'bureau': bureau, 'credit_account_id': str(uuid.uuid4()),
        'report_id': str(uuid.uuid4()), 'extraction_status': 'verified', 'as_of': '2026-11-01',
        'account_type': 'Credit card', 'source_pages': [3], 'field_evidence': [], **fields,
    }
    record['field_evidence'] = [
        {'field': key, 'value': str(record[key]), 'page': 3, 'excerpt': 'Synthetic fixture'}
        for key in ('creditor_name', 'original_creditor', 'sold_to', 'account_number') if record.get(key)
    ]
    return SimpleNamespace(canonical=SimpleNamespace(id=uuid.uuid4(), user_id=OWNER, creditor_name=name),
                           records=[record])


def golden():
    return [view('MISSION LANE TAB BANK', 'equifax', sold_to='JEFFERSON CAPITAL SYSTEMS LLC', account_number='XXXX1111'),
            view('JEFFERSON CAPITAL SYST', 'transunion', original_creditor='MISSION LANE CREDIT CARD',
                 account_type='Collection', account_number='XXXX1111')]


def test_mission_lane_jefferson_link_preserves_distinct_nodes_and_source_evidence():
    views = golden()
    before = deepcopy(views)
    graph = build_evidence_graph(views)
    assert len(graph['nodes']) == 2
    edge, = graph['relationships']
    assert edge['from'] == str(views[0].canonical.id)
    assert edge['to'] == str(views[1].canonical.id)
    assert edge['status'] == 'needs_review' and edge['dispute_ground'] is False
    assert edge['supports'][0]['signals'] == {'named_original_creditor': True,
        'named_debt_buyer': True, 'masked_suffix_agrees': True}
    assert all(s['pages'] == [3] and s['report_id'] for s in edge['supports'][0]['evidence'])
    assert views == before
    assert relationships_for(graph, edge['from'])[0]['related_account']['id'] == edge['to']
    assert relationships_for(graph, edge['to'])[0]['related_account']['id'] == edge['from']


def test_other_jefferson_debt_does_not_link_by_collector_or_balance_alone():
    views = golden()
    views[1].records[0]['original_creditor'] = 'T-MOBILE'
    assert build_evidence_graph(views)['relationships'] == []


def test_name_only_relationship_is_a_lead_without_invented_account_number():
    views = golden()
    views[0].records[0].update(sold_to=None, account_number=None)
    edge, = build_evidence_graph(views)['relationships']
    assert not edge['supports'][0]['signals']['masked_suffix_agrees']
    assert edge['status'] == 'needs_review'


@pytest.mark.parametrize('status', ['needs_audit', 'extraction_incomplete', None])
def test_unverified_records_cannot_supply_relationship_evidence(status):
    views = golden()
    views[1].records[0]['extraction_status'] = status
    assert not build_evidence_graph(views)['relationships']


def test_same_creditor_multiple_original_accounts_stay_ambiguous():
    views = golden()
    views.append(view('MISSION LANE', 'experian', account_number='XXXX2222'))
    edges = build_evidence_graph(views)['relationships']
    assert len(edges) == 2
    assert all(e['ambiguous'] for e in edges)


def test_missing_field_page_is_explicit_and_never_guessed_from_account_pages():
    views = golden()
    for v in views:
        v.records[0]['field_evidence'] = []
    edge, = build_evidence_graph(views)['relationships']
    assert all(s['pages'] == [] and s['provenance'] == 'unavailable'
               for s in edge['supports'][0]['evidence'])


def test_mixed_tenants_refused():
    views = golden()
    views[1].canonical.user_id = uuid.uuid4()
    with pytest.raises(ValueError, match='one owner'):
        build_evidence_graph(views)


def test_graph_is_stable_when_database_order_changes():
    views = golden()
    assert build_evidence_graph(views) == build_evidence_graph(list(reversed(views)))
