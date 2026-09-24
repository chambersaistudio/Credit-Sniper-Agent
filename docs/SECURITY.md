# Security and sensitive-data handling

Status as of Phase 1.5. Credit Sniper handles consumer financial data; this
document records what is protected, what isn't yet, and what must happen
before the app is exposed to anyone but its owner.

## Authentication and tenant isolation

**Authentication is enforced in hosted mode.** A managed provider (Clerk;
any OIDC/JWKS issuer works) signs a short JWT for the signed-in user; the
frontend sends it as `Authorization: Bearer …`. The backend
(`backend/app/auth.py`) verifies it against the provider's **public** JWKS —
signature (RS256), issuer, audience if configured, and expiry — so no
provider secret and no password store live in this app. The token's stable
`sub` claim maps to one `User` row (`users.auth_subject`, unique), created on
first sign-in. A user id is **never** read from a query string, form field,
or body — identity comes only from the verified token.

Two modes (`AUTH_MODE`):
- `jwt` — required for any hosted/multi-user deployment; the above holds.
- `disabled` — local dev/tests only: no token required, every request
  resolves to one local user (`backend/app/utils/default_user.py`). The app
  logs a loud warning at startup in this mode, and `/api/health/ready`
  reports `"auth_mode"` so a deploy check can confirm `jwt` before real data
  is uploaded. **Never expose a `disabled` deployment to the internet.**

**Per-user isolation.** Every data route filters by the authenticated user,
and every `/{id}` route verifies the record belongs to the caller. A record
owned by someone else returns the same **404** as a nonexistent one, so a
response never discloses that another user's record exists (no 403/404
oracle). This is enforced for reports, report files, accounts, evaluations,
claims, cases, packages, case actions, activity, and the dashboard.
`tests/test_auth.py` proves cross-user reads and writes fail (IDOR/BOLA),
that unauthenticated and invalid/expired tokens are rejected, and that the
full lifecycle still works under auth.

## What's stored, and where

| Data | Where | Protection today |
|---|---|---|
| Uploaded report PDFs | Document storage (`backend/app/services/storage.py`): a local directory, or a **private Cloudflare R2 bucket** (`STORAGE_BACKEND=r2`) | R2 encrypts at rest and the bucket has no public access. A stored file is reachable only through `GET /api/reports/{id}/file`, which first verifies the caller owns the report; on R2 it then returns a **120-second presigned URL** (the browser fetches the object directly — the R2 credentials never reach the client), on local disk it streams the bytes. No object is ever public. Keys are namespaced `users/<id>/…` so a user's files can be deleted by prefix. With local storage on a PaaS without a volume, files are lost on redeploy (extracted data stays in Postgres). |
| Full report text | `credit_reports.raw_text` | Whatever at-rest encryption the database host provides — confirm Railway's current guarantees for its Postgres volumes before storing real reports; no field-level encryption yet. Report text includes addresses, date of birth, and masked account numbers. |
| Parsed accounts and inquiries | `credit_accounts`, `credit_inquiries` | As above. Account numbers are stored as masked on the report. |
| Profile | `users` | Name, address, email, phone, date of birth. **SSN: last four digits only**, validated server-side; the full SSN is never accepted or stored. |
| Dispute packages | `cases.package` (JSON); on approval an immutable PDF of exactly what was approved goes to document storage | As above. Letters print the SSN as `XXX-XX-1234`. |
| AI usage log | `ai_usage_log` | Tokens, cost, model, user/case ids. No prompts or model output. |

**In transit:** TLS terminates at Vercel (frontend) and Railway's edge (API). The API reaches Railway Postgres over Railway's private network.
Database connections to hosted Postgres use `ssl=require` — `postgres://…?sslmode=require`
URLs are normalized for the async driver in `backend/app/config.py`.

### Security limitations that remain (before other people's data)

Done: authentication, per-record ownership/tenant isolation, and
document access behind authorization (above). Still open:

1. Use `STORAGE_BACKEND=r2` (private bucket, scoped API token) in any hosted
   environment instead of local disk.
2. Application-level encryption for `credit_reports.raw_text`, `users.date_of_birth`
   and `users.ssn_last_four` (envelope encryption with a KMS-managed key), so a
   database dump alone doesn't expose them.
3. A retention policy and a "delete my data" endpoint that removes reports,
   files, and derived records together.
4. Rate limiting on the upload and evaluation endpoints (evaluation spends
   money). Not yet implemented; a per-user/IP limiter (e.g. slowapi, or a
   gateway rule) is the intended home since these routes are authenticated.

## What the AI providers receive

AI is called through `backend/app/services/ai/` only.

**Document understanding (ingestion).** The original uploaded PDF is sent,
complete and unredacted, to the configured document provider (OpenAI
Responses API). This is a deliberate, authorized trade: local text extraction
misread a real Experian report before any model saw it, so the model must
read the authoritative document. Controls on that path:

- The original stays private in R2; there is no public object URL, and the
  bytes handed to the provider are the ones read back from private storage.
- `store=false` on every request and no Conversation object, so the provider
  retains no copy of the report.
- The PDF is sent inline as base64 in the request — not uploaded as a
  persistent Files object — and is held in memory, not written to disk.
- Nothing about the document is logged: not the bytes, the base64 payload,
  the request body, or model inputs. Provider errors log status and message
  only. `tests/test_document_extraction.py` asserts this.
- API keys stay backend-only.
- Optional privacy-preserving modes (redacted or zero-retention ingestion)
  can be added later; they are deliberately absent here because a degraded
  document produces a degraded reading.

**Everything downstream** still passes through deterministic redaction
(`backend/app/services/redaction.py` — regex and string matching, no model)
before it leaves the server.

- **Extraction fallback** (`ai_extraction.py`, only when the rule-based
  parser can't read a layout): the report text is redacted first. Removed:
  the consumer's name in the spellings reports use ("SMITH, JOHN M"), SSNs
  (full and masked), date of birth, street addresses and PO boxes,
  city/state/ZIP, phone numbers, email addresses, and labeled identity lines
  (also-known-as, previous addresses, employer, driver's license…). Kept:
  bureau, creditor/furnisher names, masked account numbers, balances,
  statuses, account dates, payment history, remarks. Redaction uses both
  patterns and the consumer's known identity (profile + report header).
  Model output is then checked against the *redacted* text, and redaction
  placeholders are rejected as values.
- **Account evaluation** (`reasoning_engine.py`) sends one account's
  tradeline fields and findings — never profile identity — and its free-text
  values go through the same redaction.
- Tests (`tests/test_redaction.py`, `tests/test_api_flow.py`) assert that
  representative PII is absent from the exact request built for the provider
  while the tradeline data is present.

Known limits: an unmasked account number written as `123-456-7890` or
`123-45-6789` is indistinguishable from a phone number or SSN and is
redacted; a creditor's own street address and phone are removed too
(extraction doesn't need them).

Provider-side retention follows each provider's API data policy. Some models
(for example Claude Fable 5.1) require 30-day retention; the default tiers
don't use them.

## Secrets

- API keys live only in server environment variables (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`). Nothing in `frontend/` references a key; the browser
  talks only to `/api`. With `ANTHROPIC_API_KEY` unset, the SDK can use an
  `ant auth login` profile instead of a raw key.
- `.env` is gitignored; `.env.example` holds no values.
- `POST /api/migrate` is disabled unless `ADMIN_TOKEN` is set, and then
  requires it in `X-Admin-Token` (constant-time comparison).
- **No bureau or furnisher credentials are stored.** Phase 2 browser
  automation will need them; they must go in a secrets vault with per-user
  encryption, never in the application database, and never be logged.

## Untrusted input

- Uploads must start with the PDF magic bytes, are size-capped
  (`MAX_FILE_SIZE_MB`), and page-capped (150) before text extraction. Parsing
  runs in a worker thread so one large file can't stall the event loop.
- Model output is never trusted as application state: it's schema-validated,
  and the reasoning engine's cited findings, recipients, fields, and legal
  references are checked against real data and a vetted catalog. Case status
  changes only through the deterministic state machine.
- Logs record errors and AI usage metadata, not report contents.

## Reporting a problem

This is a personal project; open a private issue or contact the repository
owner directly rather than filing a public issue with details.
