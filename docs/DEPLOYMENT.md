# Staging deployment: Vercel (frontend) → Railway (API + Postgres) → Cloudflare R2

**This is staging / personal-test infrastructure, not production.** There is
no authentication: anyone with the API URL can read everything stored there.
Don't upload real credit reports to a publicly reachable deployment until
access control exists (see `docs/SECURITY.md`).

```
iPhone ──HTTPS──▶ Vercel (static React build)
   │
   └──HTTPS (CORS)──▶ Railway "api" service (FastAPI, Dockerfile)
                        ├── private network ──▶ Railway Postgres
                        └── HTTPS (S3 API) ──▶ Cloudflare R2 bucket (private, optional)
```

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
| `RUN_MIGRATIONS_ON_STARTUP` | `false` (the pre-deploy command migrates) | yes |
| `ALLOWED_ORIGINS` | your Vercel production origin, e.g. `https://credit-sniper-agent.vercel.app` (no trailing slash) | yes |
| `ALLOWED_ORIGIN_REGEX` | preview URLs, e.g. `^https://credit-sniper-agent-[a-z0-9-]+-<your-team-slug>\.vercel\.app$` (your team slug is the part before `.vercel.app` on preview URLs, ending in `-projects`) | if you use previews |
| `ANTHROPIC_API_KEY` | your key | for evaluation and the extraction fallback |
| `STORAGE_BACKEND` | `local` for the first smoke test, `r2` once R2 is set up | yes |
| `ADMIN_TOKEN` | a random value (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) | optional — enables `POST /api/migrate` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | from step 3 | only with `STORAGE_BACKEND=r2` |

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
2. Settings → **Environment Variables**: add `VITE_API_URL` =
   `https://<your-railway-domain>` (no `/api`, no trailing slash) for
   Production and Preview.
3. **Remove** any leftover variables from the old setup (`ANTHROPIC_API_KEY`,
   `POSTGRES_URL*`, `SECRET_KEY`, `ADMIN_TOKEN`). The frontend needs no
   secrets, and nothing that isn't prefixed `VITE_` reaches the browser —
   but there's no reason for keys to sit on Vercel at all.
4. Redeploy. `VITE_API_URL` is baked in at build time, so changing it
   always needs a new deployment.

## 5. Verify Vercel → Railway → Postgres

1. `https://<railway-domain>/api/health` → `{"status":"ok",…}`: the process is up.
2. `https://<railway-domain>/api/health/ready` → `"database":"ok"`,
   `"schema_revision":"0002"`, `"storage_backend"`, `"ai_configured":true`.
   A 503 means `DATABASE_URL` or migrations are wrong; see the deploy logs.
3. Open the Vercel URL on your phone. Home should say "Start with your credit
   reports". An error box instead means the browser couldn't reach the API:
   check `VITE_API_URL` (then redeploy) and `ALLOWED_ORIGINS` /
   `ALLOWED_ORIGIN_REGEX` (they must match the exact origin in Safari's
   address bar).
4. Pull to refresh on `/reports`: it must reload, not 404.
5. Upload a **synthetic** report (not a real one — see the warning at top),
   confirm it appears under Reports → Accounts, and reload `/api/health/ready`.

## Local alternative (no public exposure)

To try real reports before access control exists, keep everything on your
own machine and network:

```bash
docker compose up db -d
cd backend && pip install -r requirements.txt
DATABASE_URL=postgresql://creditsniper:password@localhost:5432/creditsniper \
ANTHROPIC_API_KEY=... uvicorn app.main:app --host 0.0.0.0 --port 8000
cd frontend && npm ci && npm run dev -- --host
```

Then open `http://<your-computer's-LAN-IP>:5173` on an iPhone on the same
Wi-Fi. Nothing is reachable from the internet, but traffic on your LAN is
unencrypted, so only do this on a network you trust.
