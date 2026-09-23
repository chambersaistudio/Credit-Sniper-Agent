# Security and sensitive-data handling

Status as of Phase 1.5. Credit Sniper handles consumer financial data; this
document records what is protected, what isn't yet, and what must happen
before the app is exposed to anyone but its owner.

## Blocker before any public deployment

**There is no authentication.** Every API endpoint serves the single local
user (`/api/users/me`, fixed id in `backend/app/utils/default_user.py`).
Anyone who can reach the server can read the reports, accounts, and dispute
letters stored on it. CORS restricts browsers, not attackers. Run it locally
or behind a private network/VPN until real auth exists.

The code is shaped for auth to drop in: every endpoint resolves the user
through `resolve_user_id`, which becomes a dependency that reads the
authenticated principal. Ownership checks on `/api/reports/{id}`,
`/api/accounts/{id}` and `/api/cases/{id}` must be added at the same time —
today they don't verify the record belongs to the caller.

## What's stored, and where

| Data | Where | Protection today |
|---|---|---|
| Uploaded report PDFs | `UPLOAD_DIR` on local disk (`/tmp/uploads` on Vercel) | None beyond host disk encryption. Excluded from git. On Vercel `/tmp` is ephemeral, so the file is lost after the request — the extracted data survives in the database. |
| Full report text | `credit_reports.raw_text` | Database at-rest encryption from the host (Neon/Vercel Postgres encrypt at rest); no field-level encryption. Report text includes addresses, date of birth, and masked account numbers. |
| Parsed accounts and inquiries | `credit_accounts`, `credit_inquiries` | As above. Account numbers are stored as masked on the report. |
| Profile | `users` | Name, address, email, phone, date of birth. **SSN: last four digits only**, validated server-side; the full SSN is never accepted or stored. |
| Dispute packages | `cases.package` (JSON) and generated PDFs (rendered on request, not stored) | As above. Letters print the SSN as `XXX-XX-1234`. |
| AI usage log | `ai_usage_log` | Tokens, cost, model, user/case ids. No prompts or model output. |

**In transit:** TLS is provided by the host (Vercel) in front of the app.
Database connections to hosted Postgres use `ssl=require` — `postgres://…?sslmode=require`
URLs are normalized for the async driver in `backend/app/config.py`.

### Recommended before real users

1. Authentication plus per-record ownership checks (above).
2. Object storage for PDFs (S3/R2/GCS with server-side encryption and
   private buckets) instead of local disk, which also fixes Vercel losing files.
3. Application-level encryption for `credit_reports.raw_text`, `users.date_of_birth`
   and `users.ssn_last_four` (envelope encryption with a KMS-managed key), so a
   database dump alone doesn't expose them.
4. A retention policy and a "delete my data" endpoint that removes reports,
   files, and derived records together.
5. Rate limiting on upload and evaluation endpoints (evaluation spends money).

## What the AI providers receive

AI is called through `backend/app/services/ai/` only.

- **Account evaluation** (`reasoning_engine.py`) sends one account's bureau
  records and its findings: creditor, masked account number, balances, dates,
  statuses. It does **not** send the consumer's name, address, date of birth,
  or SSN digits. A test asserts this on the prompt.
- **Extraction fallback** (`ai_extraction.py`) sends the **full report text**
  — including personal identifiers — but only when the rule-based parser
  can't read a layout. Every extracted value is then checked against the
  document, so the model can't introduce data.
- Upload itself makes no AI call when the parser succeeds.

Provider-side retention follows each provider's API data policy. If a
zero-retention arrangement is needed, note that some models (for example
Claude Fable 5.1) require 30-day retention; the default tiers use models
without that requirement.

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
