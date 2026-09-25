from typing import List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

LOCAL_DATABASE_URL = "postgresql+asyncpg://creditsniper:password@localhost:5432/creditsniper"


def normalize_database_url(url: str) -> str:
    """Hosted Postgres (Railway/Neon/Supabase) hands out postgres:// or
    postgresql:// URLs with libpq's ?sslmode=. SQLAlchemy's async engine needs
    the +asyncpg driver, and asyncpg spells that option ?ssl=."""
    if not url:
        return url
    parts = urlsplit(url)
    scheme = "postgresql+asyncpg" if parts.scheme in ("postgres", "postgresql") else parts.scheme
    query = [("ssl", v) if k == "sslmode" else (k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DEFAULT_ORIGINS = "http://localhost:3000,http://localhost:5173,http://localhost:8080"


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Per-tier AI routing overrides — empty means the tier's default in
    # app/services/ai/config.py.
    ai_fast_provider: str = ""
    ai_fast_model: str = ""
    ai_fast_effort: str = ""
    ai_reasoning_provider: str = ""
    ai_reasoning_model: str = ""
    ai_reasoning_effort: str = ""
    ai_escalation_provider: str = ""
    ai_escalation_model: str = ""
    ai_escalation_effort: str = ""
    ai_document_extraction_provider: str = ""
    ai_document_extraction_model: str = ""
    ai_document_extraction_effort: str = ""
    ai_document_audit_provider: str = ""
    ai_document_audit_model: str = ""
    ai_document_audit_effort: str = ""
    ai_document_index_provider: str = ""
    ai_document_index_model: str = ""
    ai_document_index_effort: str = ""

    # AI-native document understanding: the extractor reads the ORIGINAL PDF.
    # Empty = auto (on when a provider credential is configured). "off" keeps
    # the deterministic parser, which can then never reach VERIFIED.
    document_extraction_mode: str = ""
    # How much rendering detail the provider gives each PDF page. Credit
    # reports have small text, two-column grids and payment-history tables.
    document_extraction_detail: str = "high"
    # The index pass only has to find account headings and page numbers, not
    # read small print in a payment grid, so it renders at low detail.
    document_index_detail: str = "low"
    # Run the independent second-pass auditor over the same original PDF.
    document_audit_enabled: bool = True
    # Run the in-process extraction worker. Extraction is a durable background
    # job: turning this off leaves uploads queued (useful for tests, or when a
    # separate worker process drains the queue instead).
    extraction_worker_enabled: bool = True
    extraction_worker_poll_seconds: float = 2.0
    # How long a worker's claim on a report is honoured. A report whose worker
    # died is re-claimable after this; until then a second worker leaves it
    # alone rather than paying to read the same document again.
    extraction_claim_lease_seconds: int = 1800

    # ── Operator control plane ───────────────────────────────────
    # Production QA driven over HTTPS instead of a shell on the box. Every
    # operation is allowlisted by name; there is no command, SQL or code path.
    #
    # The machine credential for an operator agent. A secret: it lives only in
    # the environment, is compared in constant time, is never logged and is
    # never returned by any endpoint. It is valid ONLY for /api/operator/*.
    # Empty disables machine access entirely, leaving signed-in admins only.
    operator_agent_token: str = ""
    # A label for that credential in audit records, so a job says who ran it
    # without any part of the token appearing anywhere.
    operator_agent_label: str = "agent"
    # Who may drive the operator surface from a signed-in session, as a
    # comma-separated list of email addresses. It is an allowlist, not a
    # secret: the operator surface spends money and reads every report's
    # telemetry, so "signed in" is not the same as "operator". Empty means no
    # signed-in user qualifies while auth is on — the deployment is then
    # reachable only with the machine credential. (With auth disabled there is
    # one fixed local user and nobody to keep out, so the list is not applied.)
    operator_admin_emails: str = ""
    # Runs the operator job worker in this process. Off in tests, which drive
    # the queue explicitly.
    operator_worker_enabled: bool = True
    operator_worker_poll_seconds: float = 2.0
    # Requests per minute per principal against /api/operator/*.
    operator_rate_limit_per_minute: int = 60
    # Paid jobs per hour per principal — a second, slower limit, because the
    # cost of a runaway loop is money rather than load.
    operator_paid_rate_limit_per_hour: int = 30
    # A job claimed but never finished (the worker was killed mid-flight) is
    # reclaimed after this long. A PAID job is failed rather than re-run.
    operator_job_lease_seconds: int = 900

    database_url: str = LOCAL_DATABASE_URL
    # Apply pending migrations when the app boots. Safe with one instance;
    # with several, run `alembic upgrade head` as a pre-deploy step instead
    # and turn this off.
    run_migrations_on_startup: bool = True
    # Required header value for POST /api/migrate; the endpoint is disabled while empty.
    admin_token: str = ""
    debug: bool = False

    # ── Authentication ───────────────────────────────────────────
    # "jwt"      — every data request must carry a bearer token that this
    #              server verifies against the provider's JWKS. The identity
    #              is the token's subject; the fixed local user is never used.
    #              REQUIRED for any hosted/multi-user deployment.
    # "disabled" — no auth; all requests resolve to one local user. Local
    #              development and tests only. Never expose such a deployment
    #              to the internet with real data on it.
    auth_mode: str = "disabled"
    # OIDC/JWKS verification inputs (from the managed auth provider, e.g.
    # Clerk/Auth0). The JWKS URL and issuer are public, not secrets. Backend
    # verification uses the provider's public keys, so no provider secret key
    # is needed here at all.
    auth_jwks_url: str = ""
    auth_issuer: str = ""
    # Optional. Set when the provider stamps an audience claim you want checked.
    auth_audience: str = ""

    # CORS. Exact origins, comma-separated (kept as a plain string: pydantic-
    # settings would try to JSON-decode a List[str] env var). The regex is for
    # Vercel preview URLs, which change on every deployment.
    allowed_origins_raw: str = Field(default=DEFAULT_ORIGINS, validation_alias="ALLOWED_ORIGINS")
    allowed_origin_regex: str = ""

    # Document storage: "local" (a directory — mount a volume in production)
    # or "r2" (private Cloudflare R2 bucket).
    storage_backend: str = "local"
    upload_dir: str = "./uploads"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    max_file_size_mb: int = 50

    model_config = {"env_file": ".env", "case_sensitive": False, "populate_by_name": True}

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode == "jwt"

    @property
    def operator_admins(self) -> frozenset[str]:
        """Lower-cased operator emails, so the comparison is case-insensitive
        the way email is."""
        return frozenset(
            e.strip().lower() for e in self.operator_admin_emails.split(",") if e.strip()
        )

    @property
    def document_extraction_enabled(self) -> bool:
        """AI-native ingestion is on unless explicitly disabled, and needs a
        credential for the provider that serves the document tiers."""
        if self.document_extraction_mode.lower() in ("off", "false", "0", "parser"):
            return False
        if self.document_extraction_mode.lower() in ("on", "true", "1", "ai"):
            return True
        return bool(self.openai_api_key)


settings = Settings()
