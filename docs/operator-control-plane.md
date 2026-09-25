# Operator control plane

Production QA over HTTPS — driven by a review agent, read on a phone —
instead of a shell on the deployed box.

```
CLIENT / CODEX
      │ HTTPS  (X-Operator-Token)
      ▼
/api/operator/*          allowlisted operations only
      │
      ▼
operator_jobs            durable queue in Postgres
      │
      ▼
worker inside the Railway backend   (OPERATOR_WORKER_ENABLED)
      │
      ▼
the index / batch / diagnostic services we already have
```

It is **not** a command endpoint. There is no route that takes a shell
command, Python, SQL, a filesystem path or an environment variable name. What
the credential grants is the list in
`backend/app/services/operator/registry.py` and nothing adjacent to it.

## Endpoints

| Method | Path | Cost | Purpose |
|---|---|---|---|
| GET | `/api/operator/operations` | free | The allowlist, with each operation's cost policy |
| GET | `/api/operator/whoami` | free | Confirm a credential works |
| GET | `/api/operator/reports?limit=` | free | Recent reports and extraction state |
| GET | `/api/operator/reports/{id}/batch-plan` | free | Batches a banked index produces, and which are banked |
| GET | `/api/operator/reports/{id}/checkpoint` | free | What each checkpoint holds, by shape |
| GET | `/api/operator/reports/{id}/diagnosis` | free | Processing history, classified failure, attributable AI spend |
| POST | `/api/operator/jobs/benchmark-batch` | **paid** | Queue ONE benchmark of ONE batch under ONE model → 202 + job id |
| GET | `/api/operator/jobs/{job_id}` | free | Job state and sanitized result |
| GET | `/api/operator/jobs?report_id=&operation=&limit=` | free | Recent jobs |

Free reads answer directly. The one paid operation becomes a durable job.

## Auth

Two principals, one surface:

- **agent** — `OPERATOR_AGENT_TOKEN`, held by the review agent. Sent as
  `X-Operator-Token` (preferred) or `Authorization: Bearer`.
- **admin** — an ordinary signed-in user, for the mobile operator page.

Properties, each asserted by a test:

- compared with `hmac.compare_digest`, so a near-miss costs the same time as
  a wild guess
- never logged, never returned; `whoami` reports `agent:codex`, not the token
- an unset credential never matches, including against an empty string
- **valid only for `/api/operator/*`** — presenting it to `/api/reports/`,
  `/api/accounts/`, `/api/cases/`, `/api/users/me` or `/api/dashboard` is a 401
- grants no user identity: the agent is not a user and holds nobody's data
- 401s are uniformly `Not authenticated`, so the endpoint cannot be used to
  test candidate secrets by their error message

Rate limits, per principal, in process:
`OPERATOR_RATE_LIMIT_PER_MINUTE` (default 60) on every operator request, and a
separate slower `OPERATOR_PAID_RATE_LIMIT_PER_HOUR` (default 30), because the
cost of too many paid jobs is money rather than load.

## Cost guardrails

Every operation declares its policy in the registry *before* it runs:

```python
Operation(name="benchmark_batch", paid=True, max_model_calls=1,
          models=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"),
          resumable=False)
```

- **One provider call per benchmark job.** Counted by the runner, not trusted
  to the handler: a handler that called twice fails the job.
- **No silent escalation.** The runner checks the model each call actually
  reached against the operation's list. Luna cannot become Terra; Terra cannot
  become Sol.
- **One config per job.** `config` is `A | B | C` — a literal, so `"all"`,
  `["A","B"]` and `"A,B"` are all 422. No code path iterates configs.
- **Sol needs saying so.** Config C without `acknowledge_expensive: true` is a
  400 and no job is created.
- **One idempotency key buys one job.** The same key with the same request
  returns the original job (`created: false`). The same key with a *different*
  request is a 409 — corrected truth is a different question and must not be
  answered from the old run.

## Durability

`operator_jobs` is the queue. A worker claims with a single
`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`, so two
workers can never take the same row.

Recovery after a worker dies mid-flight is asymmetric on purpose:

- a **free** job is requeued
- a **paid** job is **failed** with `WorkerInterrupted` — the provider call may
  already have happened and been billed, and re-running it would buy the same
  work twice. Resubmitting is the operator's decision.

## What a benchmark cannot do

- **Bank.** It calls `extract_batch` (no database) and never
  `run_report_batch` (which banks). It hashes the report's banked batches
  before and after and returns `banked_batches_unchanged`.
- **Change production defaults.** Model and detail are set for the duration
  and restored in a `finally` — asserted for the failure path too, which is
  the one that matters.

## Data rules

Every operator payload passes through `services/operator/sanitize.py` on the
way out, including results read back from the database, so a result stored
before a rule existed is still filtered when served.

**Allowed:** masked account identifiers, creditor names, extraction state,
model telemetry, benchmark diagnostics, page numbers.

**Never:** the report PDF or any part of it, the consumer's name, address,
date of birth, SSN or phone, storage keys, any credential. Stripped by key
(including every `*api_key*`, `*secret*`, `*token*`, `*credential*`) and by
value (PDF magic, `data:` URIs, SSN patterns, `sk-…` style secrets).

Failures record a class and a message *we* wrote. An unrecognised failure gets
a generic message on purpose: an unvetted message is exactly the one likely to
carry a provider's wording, a file path or part of the request.

Benchmark truth holds real account values, so it is never logged and never
committed. Seed it through the API.

## Railway environment

| Variable | Value | Required |
|---|---|---|
| `OPERATOR_AGENT_TOKEN` | a long random secret, e.g. `python -c "import secrets;print('op_'+secrets.token_urlsafe(48))"` | yes, for agent access |
| `OPERATOR_AGENT_LABEL` | `codex` — appears in audit records, not a secret | no |
| `OPERATOR_WORKER_ENABLED` | `true` (default). With it off, jobs queue forever | no |
| `OPERATOR_WORKER_POLL_SECONDS` | `2` (default) | no |
| `OPERATOR_RATE_LIMIT_PER_MINUTE` | `60` (default) | no |
| `OPERATOR_PAID_RATE_LIMIT_PER_HOUR` | `30` (default) | no |
| `OPERATOR_JOB_LEASE_SECONDS` | `900` (default) | no |

Leaving `OPERATOR_AGENT_TOKEN` unset disables machine access entirely; signed-in
admins still reach the page.

With more than one API instance the operator worker runs in each. Claiming is
safe (`SKIP LOCKED`), so that is correct but unnecessary — run one, or set
`OPERATOR_WORKER_ENABLED=false` on all but one.

## Codex configuration

Base URL: the Railway API origin. Header on every call:

```
X-Operator-Token: <OPERATOR_AGENT_TOKEN>
Content-Type: application/json
```

Discover the allowlist first — it is the contract:

```
GET /api/operator/operations
```

Queue the b0 Luna benchmark:

```
POST /api/operator/jobs/benchmark-batch
{
  "report_id": "<uuid>",
  "batch_id": "b0",
  "config": "A",
  "idempotency_key": "b0-luna-2026-09-25",
  "truth": {"accounts": [
    {"creditor_name": "CREDIT ACCEPTANCE CORP",
     "account_number": "7788XXXX",
     "status_raw": "Voluntarily surrendered. $7,684 past due as of Sep 2026.",
     "source_pages": [5],
     "payment_history": {"2026-05": "OK"}}
  ]}
}
```

→ `202 {"job_id": "...", "status": "queued", "created": true}`, then poll
`GET /api/operator/jobs/{job_id}` until `status` is `succeeded` or `failed`.

Always send an `idempotency_key`. A retry on a dropped connection then costs
nothing, which is the difference between a flaky network and a double charge.

For Sol add `"acknowledge_expensive": true`.

Record each truth field as the document **prints** it, in full — Experian's
Status reads `"Voluntarily surrendered. $7,684 past due as of Sep 2026."`, and
a truth holding only the first clause scores a correct extraction as a miss.
Comparison is exact on purpose: accepting a superset would also accept a model
that invented the extra text.

## Mobile admin page

`/operator` in the frontend. No navigation entry — reachable by URL, for
internal use. Shows recent reports, extraction state, the banked index, the
batch plan, banked batches and recent jobs; per batch it offers Luna, Terra and
Sol, each behind an explicit confirmation, with Sol's saying why it is
different. Results render matched accounts, field/payment/provenance accuracy,
the gate, tokens, latency, cost and every payment-history miss.
