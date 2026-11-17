"""Versioned, read-only evidence graph for one consumer's current profile.

Identity links and debt relationships are different: a collection never
becomes the original creditor's account. Relationships are review leads,
not dispute grounds or proof of ownership. No model, writes or provider calls.
"""
from __future__ import annotations

from typing import Any

from app.services.account_matcher import account_number_suffix, is_collection, normalize_creditor_name

GRAPH_VERSION = "1"


def _party(value: str | None) -> str:
    name = normalize_creditor_name(value)
    words = name.replace("credit card", "").split()
    normalized = " ".join("systems" if word == "syst" else word for word in words)
    # A documented brand/issuer label, for review leads only. This does not
    # alias TAB Bank generally, or change canonical account identity matching.
    # Source: https://www.missionlane.com/legal (reviewed 2026-11-17).
    return {"mission lane tab": "mission lane"}.get(normalized, normalized)


def _same_party(left: str | None, right: str | None) -> bool:
    return bool(_party(left)) and _party(left) == _party(right)


def _source(record: dict[str, Any], field: str) -> dict[str, Any]:
    # References and values, never consumer contact details or free-text excerpts.
    aliases = {"date_of_first_delinquency": "date_first_delinquency"}
    evidence = [e for e in record.get("field_evidence") or []
                if e.get("field") in {field, aliases.get(field, field)}]
    pages = sorted({e["page"] for e in evidence
                    if isinstance(e.get("page"), int) and e["page"] > 0})
    return {
        "credit_account_id": record["credit_account_id"], "report_id": record["report_id"],
        "bureau": record["bureau"], "as_of": record.get("as_of"),
        "field": field, "value": record.get(field),
        "pages": pages,
        "provenance": "field" if pages else "unavailable",
    }


def build_evidence_graph(views: list) -> dict[str, Any]:
    """The caller must load an owned profile. Refuse mixed-tenant inputs too."""
    if len({v.canonical.user_id for v in views}) > 1:
        raise ValueError("Evidence graph requires one owner")
    ordered = sorted(views, key=lambda v: str(v.canonical.id))
    nodes = [{
        "id": str(v.canonical.id), "creditor_name": v.canonical.creditor_name,
        "bureaus": sorted(r["bureau"] for r in v.records),
        "record_ids": sorted(r["credit_account_id"] for r in v.records),
    } for v in ordered]
    edges = []
    for original in ordered:
        for collection in ordered:
            if original is collection:
                continue
            supports = []
            for a in original.records:
                if is_collection(a) or a.get("extraction_status") != "verified":
                    continue
                for b in collection.records:
                    if not is_collection(b) or b.get("extraction_status") != "verified":
                        continue
                    named_original = _same_party(b.get("original_creditor"), a.get("creditor_name"))
                    named_buyer = _same_party(a.get("sold_to"), b.get("creditor_name"))
                    if b.get("original_creditor") and not named_original:
                        # One debt buyer can own multiple unrelated debts.
                        # A conflicting named original creditor outweighs
                        # knowing that this buyer acquired some other account.
                        continue
                    if not (named_original or named_buyer):
                        continue
                    evidence = []
                    if named_original:
                        evidence += [_source(b, "original_creditor"), _source(a, "creditor_name")]
                    if named_buyer:
                        evidence += [_source(a, "sold_to"), _source(b, "creditor_name")]
                    suffix = account_number_suffix(a.get("account_number"))
                    number_agrees = bool(suffix and len(suffix) == 4
                                         and suffix == account_number_suffix(b.get("account_number")))
                    if number_agrees:
                        evidence += [_source(a, "account_number"), _source(b, "account_number")]
                    supports.append({
                        "signals": {"named_original_creditor": named_original,
                                    "named_debt_buyer": named_buyer,
                                    "masked_suffix_agrees": number_agrees},
                        "evidence": evidence,
                    })
            if supports:
                edges.append({
                    "id": f"debt:{original.canonical.id}:{collection.canonical.id}",
                    "type": "original_creditor_collection", "from": str(original.canonical.id),
                    "to": str(collection.canonical.id), "status": "needs_review",
                    "supports": supports, "dispute_ground": False,
                    "explanation": "These separate tradelines may concern the same underlying debt. "
                                   "Confirm the account identity and ownership or transfer documents. "
                                   "Both appearing on a report does not by itself establish an error.",
                })
    # More than one original account can have the same creditor. Preserve all
    # candidates and name the ambiguity; never pick one by input order.
    targets = [e["to"] for e in edges]
    for edge in edges:
        edge["ambiguous"] = targets.count(edge["to"]) > 1
    return {"version": GRAPH_VERSION, "nodes": nodes, "relationships": edges}


def relationships_for(graph: dict[str, Any], account_id: str) -> list[dict[str, Any]]:
    nodes = {n["id"]: n for n in graph["nodes"]}
    return [{**edge, "related_account": nodes[edge["to"] if edge["from"] == account_id else edge["from"]]}
            for edge in graph["relationships"] if account_id in (edge["from"], edge["to"])]
