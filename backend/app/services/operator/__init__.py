"""
The operator control plane.

Production QA driven over HTTPS by an agent or a phone, instead of a shell on
the deployed box. Its shape is deliberately narrow:

    client ──HTTPS──▶ /api/operator/*  (allowlisted operations only)
                          │
                          ▼
                  operator_jobs (Postgres)
                          │
                          ▼
              worker inside the Railway backend
                          │
                          ▼
        the index / batch / diagnostic services we already have

There is no endpoint here that runs a command, evaluates code, reads an
environment variable or accepts SQL. What the credential grants is the list in
`registry.OPERATIONS` and nothing adjacent to it.

Importing this package registers the handlers; it starts nothing. The worker
runs only when `main` starts it under OPERATOR_WORKER_ENABLED.
"""
from app.services.operator import handlers  # noqa: F401  (registers the handlers)
from app.services.operator.auth import OperatorPrincipal, operator_request
from app.services.operator.jobs import enqueue, job_to_dict, run_job, worker_loop
from app.services.operator.registry import OPERATIONS, describe

__all__ = [
    "OPERATIONS", "OperatorPrincipal", "describe", "enqueue", "job_to_dict",
    "operator_request", "run_job", "worker_loop",
]
