# Staging deployment: Vercel (frontend) → Railway (API + Postgres) → Cloudflare R2

**This is staging / personal-test infrastructure, not production.**
Authentication is now enforced (Clerk-issued JWTs, verified server-side) and
every record is scoped to its owner, so the app is safe for the owner to test
with their own reports **once `AUTH_MODE=jwt` is set and verified**. It is not
yet hardened for other people's data — see "Security limitations that remain"
in `docs/SECURITY.md` (no field-level encryption, no rate limiting yet).

```
iPhone ──▶ Clerk (hosted sign-in) ──issues JWT──┐
   │                                            │
   └──HTTPS──▶ Vercel (static React build) ◀────┘
        │
        └──HTTPS (CORS, Bearer JWT)──▶ Railway "api" service (FastAPI, Dockerfile)
                 │  verifies JWT against Clerk's public JWKS
                 ├── private network ──▶ Railway Postgres
                 └── HTTPS (S3 API) ──▶ Cloudflare R2 bucket (private, optional)
```

The backend verifies tokens with Clerk's **public** keys, so the only auth
value the frontend needs is Clerk's **publishable** key (browser-safe), and
the backend needs **no** Clerk secret at all.

## Why the Vercel preview returned 500 and 404

Both were confirmed by reproduction, not assumed:

- **500 on every API call** (including `/api/health`): `api/index.py`
  exported `handler = Mangum(app)`. Vercel's Python runtime treats a
  top-level `handler` as a `BaseHTTPRequestHandler` *class* and checks it
  with `issubclass()`; a Mangum *instance* raises
  `TypeError: issubclass() arg 1 must be a class`, so the function failed
  before any app code ran. Behind that, there was also no database
  configured for the function and no step that ran migrations, so it
  would have failed even with the entrypoint fixed.
- **404 on page loads and refreshes** (`/reports`, `/cases/…`): `vercel.json`
  only rewrote `/api/*`. The app routes in the browser, so tapping between
  tabs worked, but loading or refreshing any path other than `/` asked
  Vercel for a file that doesn't exist.

Fix (this commit): the API no longer deploys to Vercel at all. `api/` is
removed, `vercel.json` builds only the frontend and rewrites every non-asset
path to `index.html`, and the frontend calls the API at `VITE_API_URL`.

## 0. Clerk (authentication provider)

We use [Clerk](https://clerk.com) as the managed auth provider: hosted
sign-in/up, email verification and password recovery, sessions, and
mobile/PWA support, with no password storage of our own. Any OIDC/JWKS
issuer (Auth0, Cognito, …) works with the same backend — only these values
change.

1. Create a Clerk application. Enable the sign-in methods you want (email +
   password and/or email code are enough to start).
2. From the Clerk dashboard note:
   - **Publishable key** (`pk_test_…` / `pk_live_…`) — public, goes in Vercel.
   - **Frontend API URL** (e.g. `https://xxxx.clerk.accounts.dev`, or your
     custom domain in production). This is the token **issuer**; the **JWKS
     URL** is that same URL + `/.well-known/jwks.json`.
3. Add your Vercel origin(s) to Clerk's allowed origins so sign-in loads there.

No Clerk **secret** key is needed anywhere in this app — the backend verifies
tokens with Clerk's public JWKS.

## 1. Railway: Postgres

1. Create a Railway project → **+ New → Database → PostgreSQL**.
2. Leave it **private**: don't enable a public TCP proxy. The API reaches it
   over Railway's private network.

## 2. Railway: API service

**+ New → GitHub Repo →** `chambersaistudio/Credit-Sniper-Agent`, then in the
service's **Settings**:

| Setting | Value |
|---|---|
| Source branch | the branch you're testing (e.g. `claude/credit-dispute-agent-u0yyq3`) |
| Root Directory | `backend` |
| Config file path | `/backend/railway.json` (Railway doesn't resolve this relative to Root Directory) |
| Builder | Dockerfile (set by `railway.json`) |
| Build command | none — the Dockerfile installs `requirements.txt` |
| Start command | leave empty — the Dockerfile runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Pre-deploy command | `alembic upgrade head` (set by `railway.json`) |
| Health check path | `/api/health/ready` (set by `railway.json`; checks Postgres and the migration revision) |
| Networking | **Generate Domain** under Public Networking. Railway injects `PORT`; no port setting needed. |
| Serverless / app sleeping | Off |
| Replicas | 1 |

**Variables** (service → Variables):

| Variable | Value | Required |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference variable; the private URL) | yes |
| `AUTH_MODE` | `jwt` — **required for any hosted deployment.** Leaving it `disabled` serves every user's data to anyone with the URL. | yes |
| `AUTH_JWKS_URL` | `<Clerk Frontend API URL>/.well-known/jwks.json` | yes (with `jwt`) |
| `AUTH_ISSUER` | `<Clerk Frontend API URL>` (e.g. `https://xxxx.clerk.accounts.dev`) | yes (with `jwt`) |
| `AUTH_AUDIENCE` | leave empty unless you configured a custom audience claim | no |
| `RUN_MIGRATIONS_ON_STARTUP` | `false` (the pre-deploy command migrates) | yes |
| `ALLOWED_ORIGINS` | your Vercel production origin, e.g. `https://credit-sniper-agent.vercel.app` (no trailing slash) | yes |
| `ALLOWED_ORIGIN_REGEX` | preview URLs, e.g. `^https://credit-sniper-agent-[a-z0-9-]+-<your-team-slug>\.vercel\.app$` (your team slug is the part before `.vercel.app` on preview URLs, ending in `-projects`) | if you use previews |
| `ANTHROPIC_API_KEY` | your key | for evaluation and the extraction fallback |
| `STORAGE_BACKEND` | `local` for the first smoke test, `r2` once R2 is set up | yes |
| `ADMIN_TOKEN` | a random value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) | optional — enables `POST /api/migrate` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | from step 3 | only with `STORAGE_BACKEND=r2` |
| `OPENAI_API_KEY` | your key | for AI-native document extraction |
| `EXTRACTION_WORKER_ENABLED` | `true` (default). Set `false` only if a separate process drains the queue — with it off, uploads stay queued forever. | no |
| `EXTRACTION_WORKER_POLL_SECONDS` | `2` (default) | no |
| `AI_DOCUMENT_EXTRACTION_MODEL` / `AI_DOCUMENT_AUDIT_MODEL` | empty = the pinned defaults. Change only on benchmark evidence (see below). | no |
| `DOCUMENT_EXTRACTION_DETAIL` | `high` (default) | no |

**Background extraction:** reading a report is a durable job, not part of the
upload request. `POST /api/reports/upload` returns **202** with a report id and
`status_url`; the client polls `GET /api/reports/{id}/status`. Each expensive
pass is checkpointed on the report row, so a restart resumes rather than
re-paying: a failed audit never re-runs the extractor, and a failed
persistence re-runs neither model. The queue is Postgres, so the worker is
safe to restart at any time.

With more than one API instance, the worker runs in each of them and they can
pick up the same report. Run exactly one instance, or set
`EXTRACTION_WORKER_ENABLED=false` on all but one.

**Choosing extraction models:** do not change the document tiers by intuition.
`scripts/benchmark_extraction.py` scores candidate configurations against
human-confirmed ground truth for real reports and reports quality, latency,
tokens and cost per report side by side — including what "run the cheap
config, escalate to Sol only on disagreement or a failed quality gate" would
actually cost. See `tests/fixtures/benchmark/README.md` for the golden-set
format; keep real reports and their ground truth out of git.

**Migrations:** each deploy runs `alembic upgrade head` before the new
container starts; a failed migration stops the deploy. To run one by hand:
`railway run --service <api> alembic upgrade head` from `backend/`.

## 3. Cloudflare R2 (not required for the first deployment test)

The first smoke test works with `STORAGE_BACKEND=local`: report PDFs and
approved letters go to the container's disk and **are lost on redeploy**.
The extracted accounts, cases, and packages live in Postgres and survive.
Set up R2 before storing anything you want to keep:

1. Cloudflare → R2 → **Create bucket** (e.g. `credit-sniper-staging`). Leave
   public access **disabled** and add no custom domain.
2. R2 → **Manage API tokens → Create API token**: permission *Object Read &
   Write*, scoped to that bucket only.
3. On the Railway API service, set `STORAGE_BACKEND=r2`, `R2_ACCOUNT_ID`
   (shown on the R2 overview), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
   `R2_BUCKET`, and redeploy.

Keys are laid out as `users/<user>/reports/<report>.pdf` and
`users/<user>/cases/<case>/approved-package-<timestamp>.pdf`; responses and
evidence uploads will use `…/responses/` and `…/evidence/` when those
features land.

## 4. Vercel: frontend

1. Project → Settings → **General**: Root Directory = repository root (the
   repo's `vercel.json` builds `frontend/`). Framework preset: Other.
2. Settings → **Environment Variables** (Production **and** Preview):
   - `VITE_API_URL` = `https://<your-railway-domain>` (no `/api`, no trailing slash)
   - `VITE_CLERK_PUBLISHABLE_KEY` = Clerk's publishable key (`pk_…`). This is
     a public browser credential; leaving it unset makes the frontend run in
     no-auth dev mode, which must never point at a hosted `jwt` backend.
3. **Remove** any leftover variables from the old setup (`ANTHROPIC_API_KEY`,
   `POSTGRES_URL*`, `SECRET_KEY`, `ADMIN_TOKEN`). The frontend needs no
   secrets; nothing that isn't prefixed `VITE_` reaches the browser, and even
   the `VITE_` values here are public by design.
4. Redeploy. `VITE_*` values are baked in at build time, so changing either
   always needs a new deployment.

## 5. Verify Vercel → Clerk → Railway → Postgres

Run the automated smoke test from a machine that can reach the API (steps
1–13, uploading only a synthetic PDF):

```bash
API_BASE=https://<railway-domain> \
TOKEN_A=<Clerk JWT from `await window.Clerk.session.getToken()` in the browser console> \
TOKEN_B=<a second signed-in account's JWT, optional> \
WEB_ORIGIN=https://<your-vercel-domain> \
./scripts/smoke_test.sh
```

Or check by hand:

1. `https://<railway-domain>/api/health` → `{"status":"ok",…}`: the process is up.
2. `https://<railway-domain>/api/health/ready` → `"database":"ok"`,
   `"schema_revision":"0003"`, `"storage_backend"`, `"ai_configured":true`,
   **`"auth_mode":"jwt"`**. A 503 means `DATABASE_URL`/migrations are wrong.
   If `auth_mode` is `disabled`, stop — the deployment is unprotected.
3. `curl https://<railway-domain>/api/users/me` with no token → **401**. This
   confirms auth is actually enforced (not just configured).
4. Open the Vercel URL on your phone. You should get Clerk's **sign-in
   screen**. Sign up / sign in; Home then loads. If sign-in doesn't appear,
   check `VITE_CLERK_PUBLISHABLE_KEY` and that your Vercel origin is allowed
   in Clerk. If Home shows an API error after sign-in, check `VITE_API_URL`,
   `AUTH_ISSUER`/`AUTH_JWKS_URL`, and `ALLOWED_ORIGINS` / `ALLOWED_ORIGIN_REGEX`
   (they must match the exact origin in Safari's address bar).
5. Pull to refresh on `/reports`: it must reload, not 404.
6. Upload a **synthetic** report first, confirm it appears under
   Reports → Accounts. Optionally sign in as a second account and confirm it
   sees none of the first account's data.

## Checklist before uploading your first REAL report from your iPhone

Do all of these against the **hosted** deployment (or use the local option
below):

- [ ] `GET /api/health/ready` shows `"auth_mode":"jwt"` and `"schema_revision":"0003"`.
- [ ] `GET /api/users/me` with no token returns **401**.
- [ ] The Vercel URL shows Clerk sign-in before any data screen.
- [ ] You can sign in, and Home loads over HTTPS (lock icon in Safari).
- [ ] `STORAGE_BACKEND=r2` with a **private** bucket (so PDFs persist and
      stay private) — or accept that local storage loses PDFs on redeploy.
- [ ] A second test account sees none of your data (quick IDOR sanity check).
- [ ] You understand the remaining limits in `docs/SECURITY.md`
      (no field-level DB encryption, no rate limiting yet). Only your own
      data should be on this deployment.

## Local alternative (no public exposure)

To run entirely on your own machine and network — no Clerk, no internet
exposure — keep `AUTH_MODE=disabled` (single local user) and omit the Clerk
key:

```bash
docker compose up db -d
cd backend && pip install -r requirements.txt
DATABASE_URL=postgresql://creditsniper:password@localhost:5432/creditsniper \
AUTH_MODE=disabled ANTHROPIC_API_KEY=... uvicorn app.main:app --host 0.0.0.0 --port 8000
cd frontend && npm ci && npm run dev -- --host   # no VITE_CLERK_PUBLISHABLE_KEY
```

Then open `http://<your-computer's-LAN-IP>:5173` on an iPhone on the same
Wi-Fi. Nothing is reachable from the internet, but traffic on your LAN is
unencrypted, so only do this on a network you trust.
