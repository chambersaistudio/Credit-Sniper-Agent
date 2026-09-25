"""
The allowlist.

An operator request names an operation from this registry. It cannot name a
command, a module, a function, a query or a file. Adding a capability means
adding an entry here with its cost policy written down beside it, which is the
point: what the control plane can do is a short, readable list rather than an
emergent property of what happens to be importable.

Each entry declares, before anything runs:

    paid             does this spend provider money
    max_model_calls  the hard ceiling on provider calls for one job
    models           the exact models it may use — no escalation path
    requires_ack     must the caller explicitly acknowledge the expense

`max_model_calls` is enforced by the runner counting actual provider calls,
not by trusting the handler to behave.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Operation:
    name: str
    summary: str
    paid: bool = False
    max_model_calls: int = 0
    # Exactly which models this operation may reach. A one-element tuple is
    # the mechanism that stops Luna quietly becoming Terra: the runner asserts
    # the model actually used is in here.
    models: tuple[str, ...] = ()
    requires_ack: bool = False
    # Whether a job interrupted mid-flight may be picked up again. False for
    # anything paid: re-running it would buy the same work twice, so it is
    # failed instead and the operator decides.
    resumable: bool = True


# Benchmark configurations. One config per job — there is deliberately no
# entry that runs more than one, and no code path that iterates them.
BENCHMARK_CONFIGS: dict[str, tuple[str, str]] = {
    "A": ("gpt-5.6-luna", "high"),
    "B": ("gpt-5.6-terra", "high"),
    "C": ("gpt-5.6-sol", "high"),
}
# Sol is the expensive escalation target. Asking for it is allowed, but only
# deliberately: the request must say so.
CONFIGS_REQUIRING_ACK = frozenset({"C"})


OPERATIONS: dict[str, Operation] = {
    "benchmark_batch": Operation(
        name="benchmark_batch",
        summary="Read one batch under one model and score it against confirmed truth. "
                "Banks nothing and changes no production defaults.",
        paid=True,
        max_model_calls=1,
        models=tuple(model for model, _ in BENCHMARK_CONFIGS.values()),
        requires_ack=False,  # per-config; see CONFIGS_REQUIRING_ACK
        resumable=False,
    ),
    "diagnose_report": Operation(
        name="diagnose_report",
        summary="Processing history, classified failure, checkpoint shape and AI usage "
                "for one report. Free.",
    ),
    "inspect_checkpoint": Operation(
        name="inspect_checkpoint",
        summary="What each checkpoint holds, by shape rather than contents. Free.",
    ),
    "batch_plan": Operation(
        name="batch_plan",
        summary="The batches a banked index produces and which are already banked. Free.",
    ),
    "extraction_status": Operation(
        name="extraction_status",
        summary="Extraction state for recent reports. Free.",
    ),
}


class UnknownOperation(ValueError):
    """An operation that is not on the allowlist."""


def get_operation(name: str) -> Operation:
    try:
        return OPERATIONS[name]
    except KeyError:
        # The message lists what IS allowed rather than echoing what was
        # asked for, so the endpoint cannot be used to probe for hidden names.
        raise UnknownOperation(
            f"unknown operation; allowed: {sorted(OPERATIONS)}"
        ) from None


def config_model(config: str) -> tuple[str, str]:
    """(model, detail) for a benchmark config letter."""
    try:
        return BENCHMARK_CONFIGS[config]
    except KeyError:
        raise ValueError(f"unknown config {config!r}; expected one of {sorted(BENCHMARK_CONFIGS)}") from None


def describe() -> list[dict]:
    """The catalogue, for an operator deciding what to run."""
    return [
        {
            "operation": op.name,
            "summary": op.summary,
            "paid": op.paid,
            "max_model_calls": op.max_model_calls,
            "models": list(op.models),
            "resumable": op.resumable,
        }
        for op in OPERATIONS.values()
    ]
