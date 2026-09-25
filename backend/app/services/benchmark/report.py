"""Render a benchmark result as Markdown an operator can act on."""
from typing import Any


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.4f}"


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_markdown(result: dict[str, Any]) -> str:
    configs = result["configs"]
    totals = result["totals"]
    lines: list[str] = [
        "# Document extraction benchmark",
        "",
        "Scored against human-confirmed ground truth for each document. "
        "Production defaults are unchanged by this run.",
        "",
        "## Configurations",
        "",
        _row(["Config", "Extractor / auditor / detail", "Role"]),
        _row(["---", "---", "---"]),
    ]
    for config in configs:
        role = "baseline + escalation target" if config["baseline"] else config["note"]
        lines.append(_row([config["name"], config["label"].split("(", 1)[1].rstrip(")"), role]))

    lines += ["", "## Quality", "", _row([
        "Config", "Verified", "Account count exact", "Bureau", "Field acc.",
        "Payment history", "Inquiries", "Provenance", "Hard-inq. overcount",
        "Audit false pos.", "Missing", "Spurious",
    ]), _row(["---"] * 12)]
    for config in configs:
        t = totals.get(config["name"]) or {}
        if not t:
            continue
        lines.append(_row([
            config["name"],
            f"{t['verified']}/{t['documents']}",
            f"{t['account_count_exact']}/{t['documents']}",
            f"{t['bureau_correct']}/{t['documents']}",
            _pct(t["field_accuracy"]),
            _pct(t["payment_history_accuracy"]),
            _pct(t["inquiry_accuracy"]),
            _pct(t["provenance_accuracy"]),
            str(t["hard_inquiries_overcounted"]),
            str(t["audit_false_positives"]),
            str(t["missing_accounts"]),
            str(t["spurious_accounts"]),
        ]))

    lines += ["", "## Cost and latency", "",
              _row(["Config", "Cost / report", "Total cost", "Tokens", "Mean latency"]),
              _row(["---"] * 5)]
    for config in configs:
        t = totals.get(config["name"]) or {}
        if not t:
            continue
        lines.append(_row([
            config["name"], _usd(t["cost_per_report_usd"]), _usd(t["total_cost_usd"]),
            f"{t['total_tokens']:,}", f"{t['mean_latency_ms'] / 1000:.1f}s",
        ]))

    lines += [
        "", "## With Sol as escalation only", "",
        "What each candidate would cost in production under the policy: run the candidate, "
        "escalate to the baseline only when the two candidates disagree about the document or "
        "the quality gate refuses the reading.",
        "",
        _row(["Candidate", "Escalated", "Rate", "Verified", "Cost / report"]),
        _row(["---"] * 5),
    ]
    for candidate, t in (result.get("policy_totals") or {}).items():
        if not t:
            continue
        lines.append(_row([
            f"{candidate} → escalate to baseline",
            f"{t['escalated']}/{t['documents']}",
            _pct(t["escalation_rate"]),
            f"{t['verified']}/{t['documents']}",
            _usd(t["cost_per_report_usd"]),
        ]))
    baseline = next((c["name"] for c in configs if c["baseline"]), None)
    if baseline and totals.get(baseline):
        lines.append(_row([
            f"{baseline} always (today's default)", "—", "—",
            f"{totals[baseline]['verified']}/{totals[baseline]['documents']}",
            _usd(totals[baseline]["cost_per_report_usd"]),
        ]))

    lines += ["", "## Per document", ""]
    for run in result["runs"]:
        gate = run["gate"]
        lines += [
            f"### {run['document']} — config {run['config']}",
            "",
            f"- status: **{gate['final_status']}**"
            + (f" — {'; '.join(gate['reasons'])}" if gate["reasons"] else ""),
            f"- accounts: {run['accounts']['extracted']} extracted, "
            f"{run['accounts']['expected']} expected, {run['accounts']['matched']} matched",
            f"- field accuracy: {_pct(run['field_accuracy']['accuracy'])} "
            f"({run['field_accuracy']['correct']}/{run['field_accuracy']['total']})",
            f"- payment history: {_pct(run['payment_history']['accuracy'])}",
            f"- audit: {run['audit']['blocking']} blocking, {run['audit']['set_aside']} set aside, "
            f"{len(run['audit']['false_positives'])} false positive(s)",
            f"- cost: {_usd(run['cost']['total_usd'])}, "
            f"{run['cost']['total_tokens']:,} tokens, "
            f"{run['cost']['total_latency_ms'] / 1000:.1f}s",
        ]
        if run["accounts"]["missing"]:
            lines.append(f"- missing: {', '.join(str(a) for a in run['accounts']['missing'])}")
        if run["accounts"]["spurious"]:
            lines.append(f"- spurious: {', '.join(str(a) for a in run['accounts']['spurious'])}")
        misses = run["field_accuracy"]["misses"]
        if misses:
            lines.append("- field misses:")
            for miss in misses[:10]:
                lines.append(
                    f"  - {miss['account']} · {miss['field']}: "
                    f"expected `{miss['expected']}`, got `{miss['got']}`"
                )
        for fp in run["audit"]["false_positives"][:10]:
            lines.append(
                f"- audit false positive: {fp['account']} · {fp['field']} — "
                f"auditor wanted `{fp['auditor_wanted']}`, document says `{fp['ground_truth']}`"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
