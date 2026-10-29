import os
import logging
import re
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


def _load_development_dotenv() -> None:
    """Load a dotenv file for local development only, and only when it is named.

    There is no implicit search: python-dotenv's default walks up from this file
    to the repository root, whose .env may hold production values (and the
    Compose stack bind-mounts the repository at /app). The file must be named by
    PEREGRINE_DOTENV_PATH while ENVIRONMENT is development; python-dotenv itself
    honours PYTHON_DOTENV_DISABLED. Variables already in the process win.
    """
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    path = os.getenv("PEREGRINE_DOTENV_PATH", "").strip()
    if environment not in ("development", "dev") or not path:
        return
    if not os.path.isfile(path):
        raise RuntimeError(f"PEREGRINE_DOTENV_PATH does not name a readable file: {path}")
    load_dotenv(dotenv_path=path, override=False)


_load_development_dotenv()

_config_logger = logging.getLogger("peregrine.config")


# The only environment names that get development behaviour: ephemeral secrets,
# locally minted session cookies, open debug endpoints, the in-process cache and
# a disabled upload scanner. Every other name, "local" included, is production
# everywhere — an environment is never dev for one control and prod for another.
DEV_ENVIRONMENTS = frozenset({"development", "dev", "test"})


def is_dev_environment(environment: str | None) -> bool:
    """True only for development/dev/test.

    Callers pass their own ``settings.environment``: a module keeps the Settings
    instance it imported, so reading this module's global here could disagree
    with the caller's view after a config reload.
    """
    return (environment or "").lower() in DEV_ENVIRONMENTS


# PLAN-EXEMPT: P0 prod hotfix — OBJECT_STORAGE_PRESIGN_TTL_SECONDS=auto (ops env mis-entry)
# crash-loops web+worker+beat at import via a bare int(os.getenv(...)) field-default.
# This helper degrades a malformed int env to its default instead of taking down the platform.
def _env_int(name: str, default: int) -> int:
    """Parse an integer env var, degrading to ``default`` instead of raising.

    These settings are evaluated at class-definition (import) time, so a bare
    ``int(os.getenv(...))`` on a malformed value raises during ``import
    app.config`` and crash-loops EVERY process that imports it (web, worker,
    beat). A missing/blank/non-integer value is logged loudly and falls back to
    the default — a misconfigured knob must never take down the platform.
    """
    raw = os.getenv(name)  # PYS-OK: safe-parse helper hardening existing int field-defaults, not a new untyped Settings field
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        _config_logger.warning(
            "config: %s=%r is not a valid integer; falling back to default %d",
            name,
            raw,
            default,
        )
        return default


def _strict_boolean(value, name: str) -> bool:
    """Deployment switches accept only booleans or explicit true/false strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be true or false")


def normalize_database_url(url: str) -> str:
    """Rewrite the legacy ``postgres://`` scheme to ``postgresql://``.

    Render/Heroku-style connection strings may use ``postgres://``, which
    SQLAlchemy 2.0 rejects (it has no ``postgres`` dialect). Settings applies
    this once so the runtime engine, the deployment preflight and Alembic
    (migrations/env.py) all see the same URL.
    """
    url = (url or "").strip()
    scheme, separator, rest = url.partition("://")
    if separator and (scheme == "postgres" or scheme.startswith("postgres+")):
        return "postgresql" + scheme[len("postgres"):] + separator + rest
    return url


# Single source of truth for default model IDs (#10). Referenced in the os.getenv defaults below
# and mirrored in .env.example / .env.local.example / README.md, so the default never drifts across
# config, env templates, and docs. Each MUST have a matching MODEL_PRICING_PER_MILLION row
# (app/agent/performance.py) — a default that is not priced silently drops cost attribution
# (AP-LAO-03), so update both in lockstep.
DEFAULT_TASK_MODEL = "claude-sonnet-4-6"
DEFAULT_FAST_MODEL = "claude-haiku-4-5"
DEFAULT_OPENAI_ANALYSIS_MODEL = "gpt-4o"
DEFAULT_OPENAI_CONTEXT_MODEL = "gpt-4o-mini"

# Model ids known to be deprecated / removed (return 404). A configured value in this set is a
# fail-fast in non-dev (raise) and a loud warning in dev — see ``_validate_required_secrets``.
_KNOWN_STALE_MODELS = frozenset({"claude-sonnet-4-5-20250514"})

# ── Per-model context windows (tokens) — C7 context budgeting ────────────────
# Substring-keyed like MODEL_PRICING_PER_MILLION (app/agent/performance.py): the
# longest key that is a substring of the model id wins. Values are deliberately
# CONSERVATIVE class-level defaults, not per-variant precision: every selectable
# claude-* id is a 200k-class window; gpt-4o / gpt-4o-mini are 128k. A model id
# matching neither falls back to DEFAULT_CONTEXT_WINDOW_TOKENS — the most
# conservative table value — rather than an invented precise number. Extend or
# override at runtime via AGENT_MODEL_CONTEXT_WINDOWS ("substring:tokens,…"),
# parsed in app/agent/runtime/model_loop.py.
DEFAULT_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gpt-4o": 128_000,
}
# Conservative fallback for a model id in neither the table nor the env override.
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


class Settings(BaseModel):
    qbo_client_id: str = os.getenv("QBO_CLIENT_ID", "")
    qbo_client_secret: str = os.getenv("QBO_CLIENT_SECRET", "")
    qbo_redirect_uri: str = os.getenv("QBO_REDIRECT_URI", "")
    qbo_environment: str = os.getenv("QBO_ENVIRONMENT", "sandbox")
    # Prefix on every agent-drafted journal-entry memo. This string is written
    # into the CLIENT'S general ledger as PrivateNote — never put a product
    # name or an internal codename here. Empty means no prefix.
    je_memo_prefix: str = os.getenv("JE_MEMO_PREFIX", "[AI]")
    database_url: str = Field(default=os.getenv("DATABASE_URL", ""), validate_default=True)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    redis_url: str = os.getenv("REDIS_URL", "").strip()
    cache_serialization_mode: str = os.getenv("CACHE_SERIALIZATION_MODE", "json").strip().lower()
    celery_broker_url: str = (
        os.getenv("CELERY_BROKER_URL")
        or os.getenv("REDIS_URL")
        or "redis://localhost:6379/0"
    ).strip()
    celery_result_backend: str = (
        os.getenv("CELERY_RESULT_BACKEND")
        or os.getenv("REDIS_URL")
        or "redis://localhost:6379/0"
    ).strip()

    local_deployment_preflight: bool = _strict_boolean(os.getenv("LOCAL_DEPLOYMENT_PREFLIGHT", "false"), "LOCAL_DEPLOYMENT_PREFLIGHT")
    document_originals_required: bool = _strict_boolean(os.getenv("DOCUMENT_ORIGINALS_REQUIRED", "false"), "DOCUMENT_ORIGINALS_REQUIRED")
    monthly_review_enabled: bool = _strict_boolean(os.getenv("MONTHLY_REVIEW_ENABLED", "false"), "MONTHLY_REVIEW_ENABLED")
    # W2 read infrastructure. Source authoring does not grant live activation.
    accounting_source_feeds_enabled: bool = _strict_boolean(os.getenv("ACCOUNTING_SOURCE_FEEDS_ENABLED", "false"), "ACCOUNTING_SOURCE_FEEDS_ENABLED")
    accounting_source_live_reads_enabled: bool = _strict_boolean(os.getenv("ACCOUNTING_SOURCE_LIVE_READS_ENABLED", "false"), "ACCOUNTING_SOURCE_LIVE_READS_ENABLED")
    accounting_source_worker_enabled: bool = _strict_boolean(os.getenv("ACCOUNTING_SOURCE_WORKER_ENABLED", "false"), "ACCOUNTING_SOURCE_WORKER_ENABLED")
    accounting_source_runtime_role: str = os.getenv("ACCOUNTING_SOURCE_RUNTIME_ROLE", "").strip()
    accounting_source_partition_seconds: int = 600
    accounting_source_max_requests: int = 1000
    accounting_source_max_pages: int = 1000
    accounting_source_max_rows: int = 100000
    accounting_source_max_response_bytes: int = 8388608
    accounting_source_max_retained_bytes: int = 67108864
    accounting_source_max_diagnostic_bytes: int = 524288
    accounting_source_max_partitions: int = 32
    celery_beat_schedule_allowlist: str = os.getenv("CELERY_BEAT_SCHEDULE_ALLOWLIST", "")
    # Firm-operations filing reconciliation has not passed acceptance. Off by
    # default: every /firm-operations route 404s until this is explicitly true.
    firm_operations_enabled: bool = _strict_boolean(os.getenv("FIRM_OPERATIONS_ENABLED", "false"), "FIRM_OPERATIONS_ENABLED")
    upload_malware_scanner_readiness_url: str = os.getenv("UPLOAD_MALWARE_SCANNER_READINESS_URL", "").strip()

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("local_deployment_preflight", "document_originals_required", "firm_operations_enabled", mode="before")
    @classmethod
    def _deployment_boolean(cls, value, info):
        return _strict_boolean(value, info.field_name.upper())

    # JWT signing key — no fallback default; must be set explicitly
    secret_key: str = os.getenv("SECRET_KEY", "")
    secret_encryption_key: str = os.getenv("SECRET_ENCRYPTION_KEY", "")

    # Previous SECRET_ENCRYPTION_KEY value(s), retained during a key rotation so that
    # ciphertext written under the old key still DECRYPTS while the re-encrypt sweep
    # (scripts/ops/reencrypt_secrets.py) runs. Comma-separated, most-recent-first.
    # Each entry is validated as an AES-length key by app/services/crypto.py exactly
    # like the primary. Empty/unset == today's single-key behavior exactly; drop it
    # again once the sweep reports no ciphertext remains under a previous key.
    secret_encryption_key_previous: str = os.getenv("SECRET_ENCRYPTION_KEY_PREVIOUS", "")

    # Object storage (S3-compatible; Cloudflare R2 by default) for original
    # uploaded documents. UNCONFIGURED-SAFE: when endpoint/bucket are empty the
    # object_storage service no-ops (uploads still succeed, text-only) — never
    # raises. Keys/secret are read here but never logged (log-redaction rule).
    object_storage_endpoint_url: str = os.getenv("OBJECT_STORAGE_ENDPOINT_URL", "").strip()
    object_storage_bucket: str = os.getenv("OBJECT_STORAGE_BUCKET", "").strip()
    object_storage_access_key_id: str = os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    object_storage_secret_access_key: str = os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    object_storage_region: str = os.getenv("OBJECT_STORAGE_REGION", "auto").strip()
    object_storage_presign_ttl_seconds: int = _env_int("OBJECT_STORAGE_PRESIGN_TTL_SECONDS", 300)  # PLAN-EXEMPT: P0 prod hotfix (see _env_int above) — malformed int env must not crash-loop the platform

    auth_cookie_name: str = os.getenv("AUTH_COOKIE_NAME", "access_token")
    auth_cookie_domain: str = os.getenv("AUTH_COOKIE_DOMAIN", "").strip()
    auth_session_minutes: int = int(os.getenv("AUTH_SESSION_MINUTES", "1440"))
    csrf_cookie_name: str = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
    csrf_header_name: str = os.getenv("CSRF_HEADER_NAME", "X-CSRF-Token")
    auth_enable_browser_bearer_fallback: bool = os.getenv("AUTH_ENABLE_BROWSER_BEARER_FALLBACK", "false").lower() == "true"
    auth_priority_admin_email: str = os.getenv("AUTH_PRIORITY_ADMIN_EMAIL", "")
    auth_platform_admin_emails: str = os.getenv("AUTH_PLATFORM_ADMIN_EMAILS", "")
    # Shared with the frontend service: the /bff proxy signs the client IP it
    # observed with this secret and app/rate_limit.py keys unauthenticated
    # limits on that IP only when the signature verifies. Unset, those limits
    # key on the socket peer (the proxy), shared by every browser.
    bff_client_ip_secret: str = os.getenv("BFF_CLIENT_IP_SECRET", "").strip()
    auth_allow_legacy_fallback_local_only: bool = os.getenv("AUTH_ALLOW_LEGACY_FALLBACK_LOCAL_ONLY", "true").lower() == "true"
    auth_disable_legacy_password_login_in_production: bool = os.getenv("AUTH_DISABLE_LEGACY_PASSWORD_LOGIN_IN_PRODUCTION", "true").lower() == "true"
    password_reset_redirect_url: str = os.getenv("PASSWORD_RESET_REDIRECT_URL", "").rstrip("/")
    email_change_redirect_url: str = os.getenv("EMAIL_CHANGE_REDIRECT_URL", "").rstrip("/")

    # Tenant data lifecycle (export/deletion). The export bundle is written to
    # local object storage under this directory; a hashed, expiring signed link
    # gates download. Grace window before a confirmed deletion may hard-delete.
    tenant_export_storage_dir: str = os.getenv("TENANT_EXPORT_STORAGE_DIR", "/tmp/peregrine_tenant_exports").rstrip("/")
    tenant_export_link_ttl_seconds: int = int(os.getenv("TENANT_EXPORT_LINK_TTL_SECONDS", str(60 * 60 * 24 * 3)))
    tenant_deletion_grace_days: int = int(os.getenv("TENANT_DELETION_GRACE_DAYS", "7"))

    # Stytch B2B
    stytch_project_id: str = os.getenv("STYTCH_PROJECT_ID", "")
    stytch_secret: str = os.getenv("STYTCH_SECRET", "")
    stytch_public_token: str = os.getenv("STYTCH_PUBLIC_TOKEN", "")
    stytch_environment: str = os.getenv("STYTCH_ENVIRONMENT", "test")
    stytch_base_url: str = os.getenv("STYTCH_BASE_URL", "")
    stytch_default_mfa_policy: str = os.getenv("STYTCH_DEFAULT_MFA_POLICY", "OPTIONAL")
    stytch_default_auth_methods: str = os.getenv("STYTCH_DEFAULT_AUTH_METHODS", "password,email_otp,magic_link")
    stytch_default_allowed_mfa_methods: str = os.getenv("STYTCH_DEFAULT_ALLOWED_MFA_METHODS", "sms_otp")
    stytch_passwords_across_organizations: bool = os.getenv("STYTCH_PASSWORDS_ACROSS_ORGANIZATIONS", "false").lower() == "true"

    # QBO webhooks
    qbo_webhook_verifier_token: str = os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()

    # Direct QBO write endpoints (/accounting/journal-entry, /accounting/transaction/*)
    # bypass the ProposedAction approval flow. They are deprecated and disabled by
    # default; the supported write path is /actions/propose/* -> approve -> execute.
    allow_direct_accounting_writes: bool = os.getenv("ALLOW_DIRECT_ACCOUNTING_WRITES", "false").lower() == "true"

    # Sentry internal-integration webhooks
    sentry_webhook_secret: str = os.getenv("SENTRY_WEBHOOK_SECRET", "").strip()

    # Upload malware scanning. Local/test can disable this for fast iteration;
    # non-dev environments default to fail-closed required scanning.
    upload_malware_scanner_mode: str = os.getenv("UPLOAD_MALWARE_SCANNER_MODE", "").strip().lower()
    upload_malware_scanner_provider: str = os.getenv("UPLOAD_MALWARE_SCANNER_PROVIDER", "").strip().lower()
    upload_malware_scanner_url: str = (
        os.getenv("UPLOAD_MALWARE_SCANNER_URL")
        or os.getenv("UPLOAD_MALWARE_SCANNER")
        or ""
    ).strip()
    upload_malware_scanner_hostport: str = os.getenv("UPLOAD_MALWARE_SCANNER_HOSTPORT", "").strip()
    upload_malware_scanner_scheme: str = os.getenv("UPLOAD_MALWARE_SCANNER_SCHEME", "http").strip().lower()
    upload_malware_scanner_path: str = os.getenv("UPLOAD_MALWARE_SCANNER_PATH", "/scan").strip()
    # PLAN-EXEMPT: AC-3.3 scanner-timeout default alignment (one-line default bump)
    # Default >= the scanner's SCAN_TIMEOUT_SECONDS (30) so the client never aborts a
    # healthy scan before the scanner's own timeout returns a fail-closed error.
    upload_malware_scanner_timeout_seconds: float = float(os.getenv("UPLOAD_MALWARE_SCANNER_TIMEOUT_SECONDS", "45"))  # SETTINGS-OK: legacy S6 os.getenv default value bump, not a new field

    # SQLAlchemy production pool controls
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    db_pool_pre_ping: bool = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"

    # Connection-pooling strategy. "queue" (DEFAULT) = today's in-process QueuePool
    # sized by the db_pool_* knobs above. "transaction" = SQLAlchemy NullPool, for use
    # ONLY behind a transaction-pooling PgBouncer (the engine opens one server
    # connection per checkout and lets PgBouncer multiplex; the app pool no longer
    # multiplies against Postgres max_connections). NullPool on a DIRECT DB connection
    # opens a fresh Postgres connection per checkout and can exhaust max_connections
    # under load — never set "transaction" until PgBouncer is actually in front. The
    # prod cutover (provision PgBouncer + rewire DATABASE_URL) is a separate ops step.
    db_pooler: str = os.getenv("DB_POOLER", "queue").strip().lower()  # PYS-OK: legacy S6 os.getenv field-default shape matching sibling db_pool_* fields; pool-strategy selector, not a secret

    # Agent runtime efficiency / safety controls
    agent_max_request_chars: int = int(os.getenv("AGENT_MAX_REQUEST_CHARS", "12000"))
    agent_max_prompt_chars: int = int(os.getenv("AGENT_MAX_PROMPT_CHARS", "60000"))
    agent_conversation_history_limit: int = int(os.getenv("AGENT_CONVERSATION_HISTORY_LIMIT", "12"))
    agent_duplicate_run_window_seconds: int = int(os.getenv("AGENT_DUPLICATE_RUN_WINDOW_SECONDS", "120"))  # SETTINGS-OK: legacy S6 os.getenv shape, untouched sibling line reproduced for anchor only
    # PLAN-EXEMPT: plan 20260711-agentic-workflow-hardening R4 (round-2, PR #381 remediation row):
    # wall-clock ceiling for one whole tool-loop run. Iteration/message caps bound COUNT, not
    # TIME — a run of slow tool calls could previously run unbounded. On expiry the loop stops
    # calling the model and composes an honest partial answer from the tool results so far.
    agent_loop_deadline_seconds: int = int(os.getenv("AGENT_LOOP_DEADLINE_SECONDS", "420"))  # SETTINGS-OK: matches the legacy S6 sibling agent_* field shape; typed-BaseSettings cutover is the S6 remediation wave, not this line

    # ── Token-cost controls (LLM optimization roadmap) ──────────────────────
    # Tiered routing: start a run on a cheap "fast" model and escalate to the
    # default (frontier) model the moment the model decides to call a tool.
    # Pure-chat turns and the low-stakes "should I call a tool" decision run
    # cheap; every tool-result *synthesis* turn (where financial reasoning
    # happens) runs on the frontier model. Toggle off with AGENT_FAST_FIRST_TURN
    # =false to restore single-model behaviour instantly.
    agent_fast_first_turn: bool = os.getenv("AGENT_FAST_FIRST_TURN", "true").lower() == "true"
    # PYS-OK: single-source model-default constant (#10); legacy os.getenv shape unchanged pending S6
    agent_fast_model: str = os.getenv("AGENT_FAST_MODEL", DEFAULT_FAST_MODEL)
    agent_escalate_after_tool: bool = os.getenv("AGENT_ESCALATE_AFTER_TOOL", "true").lower() == "true"

    # Intent classification: keyword rules are always on (free, deterministic).
    # Enabling this adds one cheap-lane LLM call to refine ONLY requests the
    # keyword rules can't place — better tool selection / routing on ambiguous
    # asks, at the cost of a little hot-path latency. Off by default.
    agent_llm_intent_classifier: bool = os.getenv("AGENT_LLM_INTENT_CLASSIFIER", "false").lower() == "true"

    # In-loop context budget. The agent tool loop resends the growing message
    # transcript every iteration (quadratic token growth). When the cumulative
    # transcript exceeds the budget, older tool-result payloads are trimmed in
    # place (recent turns stay full-resolution) and the loop is hard-capped at a
    # message ceiling so a pathological run cannot run away.
    agent_loop_tool_result_budget_chars: int = int(os.getenv("AGENT_LOOP_TOOL_RESULT_BUDGET_CHARS", "60000"))
    agent_loop_compacted_result_chars: int = int(os.getenv("AGENT_LOOP_COMPACTED_RESULT_CHARS", "800"))

    # ── Nightly per-transaction scrape cap (AP-CEL-9 token bound) ────────────
    # The nightly GL scrape (app/tasks/proactive.py _scrape_gl_for_new_schedules)
    # calls detect_with_ai once per candidate transaction. Without a ceiling a
    # single high-volume tenant balloons the per-realm run (LLM cost + runtime).
    # This caps the number of detector/LLM calls per realm per nightly run; when
    # the cap is hit the scrape logs and stops rather than silently truncating.
    # SETTINGS-OK: mirrors the file's prevailing os.getenv default shape (the S6
    # BaseModel form is tolerated diff-vs-HEAD); a lone BaseSettings field here
    # would be inconsistent and would not read from env like its 90 siblings.
    nightly_scrape_max_detector_calls: int = int(os.getenv("NIGHTLY_SCRAPE_MAX_DETECTOR_CALLS", "200"))
    agent_loop_keep_recent_tool_turns: int = int(os.getenv("AGENT_LOOP_KEEP_RECENT_TOOL_TURNS", "2"))
    agent_loop_max_messages: int = int(os.getenv("AGENT_LOOP_MAX_MESSAGES", "60"))

    # ── Token-accurate context budgeting (C7 AC-1) ───────────────────────────
    # Budget = context window (DEFAULT_MODEL_CONTEXT_WINDOWS / this override) −
    # output headroom − estimation margin. The model loop enforces it as a
    # FEEDBACK budget: the provider's ACTUAL usage.input_tokens from the prior
    # call plus a chars-per-token estimate of the delta; the first call is
    # estimated at chars/4 (no tiktoken dependency by design).
    # Override/extend the window table with "substring:tokens" pairs, e.g.
    # "claude:200000,gpt-4o:128000,o4-mini:200000".
    # PYS-OK: mirrors the file's prevailing os.getenv field shape pending the S6 BaseSettings migration; a lone BaseSettings field here would not read env like its ~90 siblings
    agent_model_context_windows: str = os.getenv("AGENT_MODEL_CONTEXT_WINDOWS", "").strip()
    agent_output_headroom_tokens: int = _env_int("AGENT_OUTPUT_HEADROOM_TOKENS", 8192)
    agent_context_budget_margin_tokens: int = _env_int("AGENT_CONTEXT_BUDGET_MARGIN_TOKENS", 2048)
    agent_token_estimate_chars_per_token: int = _env_int("AGENT_TOKEN_ESTIMATE_CHARS_PER_TOKEN", 4)

    # ── Rolling conversation summarization (C7 AC-2) ─────────────────────────
    # The newest agent_history_keep_verbatim turns stay verbatim; up to
    # agent_history_summary_max_turns older turns compress into ONE deterministic
    # structured digest block (no in-turn LLM call — the S5 C3-D latency lesson)
    # that preserves decisions, approvals/rejections + ProposedAction ids,
    # standing constraints, open proposal ids, and cited figures verbatim.
    agent_history_keep_verbatim: int = _env_int("AGENT_HISTORY_KEEP_VERBATIM", 12)
    agent_history_summary_max_turns: int = _env_int("AGENT_HISTORY_SUMMARY_MAX_TURNS", 40)
    # Promoted from the hardcoded per-history-message 4000-char clip
    # (context_packet.py) — also caps the rolling-summary digest size.
    agent_history_message_clip_chars: int = _env_int("AGENT_HISTORY_MESSAGE_CLIP_CHARS", 4000)

    # ── Structure-aware prompt truncation (C7 AC-3) ──────────────────────────
    # Promoted from the hardcoded per-block 1800-char clip and 6-blocks-per-
    # section cap (prompt_builder.py). Truncation drops whole lowest-priority
    # volatile blocks; it never slices inside a wrapped <untrusted> block.
    agent_context_block_clip_chars: int = _env_int("AGENT_CONTEXT_BLOCK_CLIP_CHARS", 1800)
    agent_context_blocks_per_section: int = _env_int("AGENT_CONTEXT_BLOCKS_PER_SECTION", 6)

    # ── Episodic memory retrieval (Wave 3.3 LG-4) ────────────────────────────
    # Run-start similarity recall of past investigations (agent_episodes), rendered
    # as ONE fenced, NON-AUTHORITATIVE volatile block. top_k is kept tight (episodes
    # are short "investigated/concluded/evidence" summaries) so the block cannot
    # crowd out financial context; its own clip mirrors agent_context_block_clip_chars.
    agent_episodic_top_k: int = _env_int("AGENT_EPISODIC_TOP_K", 3)
    agent_episodic_block_clip_chars: int = _env_int("AGENT_EPISODIC_BLOCK_CLIP_CHARS", 1200)

    # ── Tool-failure memory TTL (Wave 3.3 LG-5) ──────────────────────────────
    # A recorded (org, realm, tool, error_class) failure slides its expires_at to
    # now + this many days on every re-failure; retrieval surfaces only rows whose
    # expires_at is still in the future (and resolved_at IS NULL), so an abandoned
    # broken tool's caveat ages out with NO beat — a transient failure never becomes
    # a permanent "broken" caveat.
    tool_failure_ttl_days: int = _env_int("TOOL_FAILURE_TTL_DAYS", 7)

    # Deprecated global LLM settings. Lane-specific settings below are the source of truth.
    llm_provider: str = os.getenv("LLM_PROVIDER", "claude")
    # PYS-OK: dedupe the model-default literal into the single-source constant (#10); the legacy os.getenv shape is unchanged pending the S6 BaseSettings migration
    llm_model: str = os.getenv("LLM_MODEL", DEFAULT_TASK_MODEL)
    llm_fast_model: str = os.getenv("LLM_FAST_MODEL", "")
    llm_reasoning_model: str = os.getenv("LLM_REASONING_MODEL", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    enable_dual_llm_orchestration: bool = os.getenv("ENABLE_DUAL_LLM_ORCHESTRATION", "true").lower() == "true"
    llm_verbose_routing_logs: bool = os.getenv("LLM_VERBOSE_ROUTING_LOGS", "false").lower() == "true"

    # LLM transport hardening (audit items Q2/Q4). Provider SDK clients are
    # reused as module-level singletons; these bound per-call timeout and retry
    # behaviour so a stuck provider can't hang on the SDK's 600s default or
    # trigger silent retry storms.
    llm_request_timeout_seconds: float = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "45"))
    # PLAN-EXEMPT: P0 hotfix (JE drafting 529 resilience) per Steven directive 2026-06-23.
    # The Anthropic/OpenAI SDK clients are built with max_retries=this and auto-retry
    # overloaded_error (529) / 429 / 5xx with exponential backoff. A live JE-draft run failed
    # outright on a transient "Overloaded" with only 1 retry; 3 lets the SDK ride out a brief
    # provider overload before the run surfaces a model-provider error.
    # SETTINGS-OK: legacy os.getenv shape unchanged; only the default value is raised 1 -> 3
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    gemini_request_timeout_seconds: float = float(os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "45"))

    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-1 (F-1 close hardening).
    # Per-close-run LLM token budget (input+output, summed over the run's ledger
    # rows). Enforced FAIL-LOUD in the wrapper (llm_task_context.enforce_close_run_budget):
    # a close run at/over budget raises CloseBudgetExceededError BEFORE the next
    # provider call — it never silently continues. Overridable per realm via
    # ReviewPolicy.close_run_token_budget.
    close_run_token_budget: int = int(os.getenv("CLOSE_RUN_TOKEN_BUDGET", "500000"))
    # TTL for the per-realm close single-flight lease (close_run_locks). The
    # walk heartbeats the lease on every step commit; a lease not heartbeat-
    # refreshed within this window is stealable/releasable, so a dead or wedged
    # session cannot hold a realm longer than this (default 30 min).
    close_lock_ttl_seconds: int = int(os.getenv("CLOSE_LOCK_TTL_SECONDS", "1800"))

    # PLAN-EXEMPT: ratified plan 20260702-qa-remediation W-7 (board pack, AC-2).
    # Hard token cap for the board_pack report's ONE LLM commentary pass — a
    # deterministic pre-call bound on the estimated prompt size (~4 chars/token
    # heuristic over system+user prompts). At/over the cap the call is NOT made
    # and the report renders with an honest per-section placeholder instead; the
    # pass is also ledger-metered as task_type="board_report" (the W-1 seam).
    board_report_commentary_token_cap: int = _env_int("BOARD_REPORT_COMMENTARY_TOKEN_CAP", 8000)

    task_llm_provider: str = os.getenv("TASK_LLM_PROVIDER") or os.getenv("LLM_PROVIDER", "claude")
    # PYS-OK: single-source model-default constant (#10); legacy os.getenv shape unchanged pending S6
    task_llm_model: str = os.getenv("TASK_LLM_MODEL") or os.getenv("LLM_MODEL", DEFAULT_TASK_MODEL)
    task_llm_fallback_provider: str = os.getenv("TASK_LLM_FALLBACK_PROVIDER", "")
    task_llm_fallback_model: str = os.getenv("TASK_LLM_FALLBACK_MODEL", "")
    analysis_llm_provider: str = os.getenv("ANALYSIS_LLM_PROVIDER") or ("openai" if os.getenv("OPENAI_API_KEY") else os.getenv("LLM_PROVIDER", "claude"))
    # PYS-OK: single-source model-default constants (#10); legacy os.getenv shape unchanged pending S6
    analysis_llm_model: str = os.getenv("ANALYSIS_LLM_MODEL") or (DEFAULT_OPENAI_ANALYSIS_MODEL if os.getenv("OPENAI_API_KEY") else os.getenv("LLM_MODEL", DEFAULT_TASK_MODEL))
    context_llm_provider: str = os.getenv("CONTEXT_LLM_PROVIDER") or ("openai" if os.getenv("OPENAI_API_KEY") else os.getenv("LLM_PROVIDER", "claude"))
    # PYS-OK: single-source model-default constants (#10); legacy os.getenv shape unchanged pending S6
    context_llm_model: str = os.getenv("CONTEXT_LLM_MODEL") or (DEFAULT_OPENAI_CONTEXT_MODEL if os.getenv("OPENAI_API_KEY") else os.getenv("LLM_MODEL", DEFAULT_TASK_MODEL))

    # Onboarding hot-path optimization (audit items Q1/Q3). The KPI/covenant
    # structured-extraction drafts have deterministic local fallbacks and run on
    # the user-facing GET /onboarding/plan path, so they should not occupy the
    # frontier analysis lane. Route them to the cheaper/faster lane by default
    # (env-overridable). Drafts are also fanned out concurrently; this bounds the
    # worker pool so a burst of drafts can't exhaust threads.
    onboarding_draft_lane: str = os.getenv("ONBOARDING_DRAFT_LANE", "context")
    onboarding_draft_max_workers: int = int(os.getenv("ONBOARDING_DRAFT_MAX_WORKERS", "5"))

    # ── Autonomy roadmap (Phase 2): confidence calibration + shadow-mode ──────
    # Per-tenant categorization confidence threshold a calibrated score must meet
    # to be considered would-be-auto. Default-only (not user-facing yet); PR2's
    # TenantTaskAutonomy.confidence_threshold overrides per tenant.
    autonomy_categorization_confidence_threshold: float = float(os.getenv("AUTONOMY_CATEGORIZATION_CONFIDENCE_THRESHOLD", "0.9"))
    # Calibration reliability curve: number of equal-width raw-confidence buckets,
    # and the minimum samples required before a bucket / a tenant is treated as
    # calibrated. Below these, calibration returns "uncalibrated" and is never
    # treated as high-confidence.
    autonomy_calibration_buckets: int = int(os.getenv("AUTONOMY_CALIBRATION_BUCKETS", "10"))
    autonomy_calibration_min_bucket_samples: int = int(os.getenv("AUTONOMY_CALIBRATION_MIN_BUCKET_SAMPLES", "20"))
    autonomy_calibration_min_total_samples: int = int(os.getenv("AUTONOMY_CALIBRATION_MIN_TOTAL_SAMPLES", "50"))
    # Promotion gate bar: a task earns promotion only with precision on would-be-
    # auto items >= this, zero high-severity misses, and >= N samples over >= M
    # distinct cycles (days with would-be-auto activity).
    autonomy_promotion_min_precision: float = float(os.getenv("AUTONOMY_PROMOTION_MIN_PRECISION", "0.95"))
    autonomy_promotion_min_samples: int = int(os.getenv("AUTONOMY_PROMOTION_MIN_SAMPLES", "200"))
    autonomy_promotion_min_cycles: int = int(os.getenv("AUTONOMY_PROMOTION_MIN_CYCLES", "5"))
    # Circuit breaker (PR2): demote an L3 task to proposal_first if the override
    # rate over the recent window exceeds this, or on ANY high-severity miss.
    autonomy_breaker_override_rate: float = float(os.getenv("AUTONOMY_BREAKER_OVERRIDE_RATE", "0.1"))
    autonomy_breaker_min_samples: int = int(os.getenv("AUTONOMY_BREAKER_MIN_SAMPLES", "20"))
    # Auto-execute caps (PR2): max auto-executed categorizations per single
    # proposal action and per ingestion run.
    autonomy_max_autoexec_per_action: int = int(os.getenv("AUTONOMY_MAX_AUTOEXEC_PER_ACTION", "1"))
    autonomy_max_autoexec_per_run: int = int(os.getenv("AUTONOMY_MAX_AUTOEXEC_PER_RUN", "50"))

    # Shadow backfill (Phase 3): the cheap model the backfill predictor pins for
    # categorization prediction over historical GL lines. Pinned explicitly (not
    # the configured lane model) so the one-time backfill is always cheap.
    autonomy_backfill_provider: str = os.getenv("AUTONOMY_BACKFILL_PROVIDER", "claude")
    # PYS-OK: single-source model-default constant (#10); legacy os.getenv shape unchanged pending S6
    autonomy_backfill_model: str = os.getenv("AUTONOMY_BACKFILL_MODEL", DEFAULT_FAST_MODEL)
    autonomy_backfill_history_examples: int = int(os.getenv("AUTONOMY_BACKFILL_HISTORY_EXAMPLES", "5"))
    # Durability: commit every N scored lines so an interrupted run is durable
    # and resumable at line granularity (both the free measurement and any LLM run).
    autonomy_backfill_commit_every: int = int(os.getenv("AUTONOMY_BACKFILL_COMMIT_EVERY", "100"))
    # Free learned-model default operating point (the residual split). The free
    # measurement reports a full precision/coverage curve; this is just the
    # default "confident bucket" cutoff used for the gate's would_autoexecute.
    autonomy_free_confidence_threshold: float = float(os.getenv("AUTONOMY_FREE_CONFIDENCE_THRESHOLD", "0.8"))
    autonomy_free_min_support: int = int(os.getenv("AUTONOMY_FREE_MIN_SUPPORT", "3"))

    # Server throughput (audit item S5). The hot assistant/onboarding routes are
    # sync `def` endpoints; FastAPI runs each in the shared anyio threadpool and
    # holds a worker for the route's entire multi-second, multi-LLM-call duration.
    # The anyio default cap is 40 workers, which saturates under concurrent load
    # even though the work is almost entirely I/O wait (provider HTTP calls). We
    # raise the cap to a higher value sized for an I/O-bound LLM service. This is
    # purely a concurrency ceiling for blocking offload threads — it does not add
    # CPU parallelism and does not change behaviour, so 120 is a safe default that
    # roughly triples headroom while staying well under typical OS thread limits.
    # Env-overridable; set to 40 to restore the framework default.
    server_threadpool_max_workers: int = int(os.getenv("SERVER_THREADPOOL_MAX_WORKERS", "120"))

    # Billing / Stripe
    # Offer ACH (us_bank_account) alongside card in Checkout when a company has
    # no explicit payment_method_kinds override. Requires ACH enabled on the
    # Stripe account. Toggle off to keep card-only.
    stripe_enable_ach: bool = os.getenv("STRIPE_ENABLE_ACH", "true").lower() == "true"

    # Production Settings
    environment: str = os.getenv("ENVIRONMENT", "development")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    cors_origins: str = os.getenv("CORS_ORIGINS", "http://localhost:3000")  # Comma separated
    cors_origin_regex: str = os.getenv("CORS_ORIGIN_REGEX", "").strip()

    @model_validator(mode="after")
    def _validate_required_secrets(self) -> "Settings":
        """Fail fast in non-dev environments when critical secrets are missing."""
        is_dev = is_dev_environment(self.environment)

        if self.document_originals_required:
            from urllib.parse import urlsplit

            endpoint = urlsplit(self.object_storage_endpoint_url)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                    or not all((self.object_storage_bucket, self.object_storage_access_key_id,
                                self.object_storage_secret_access_key))):
                raise ValueError("DOCUMENT_ORIGINALS_REQUIRED needs valid complete object storage configuration")

        # #10: fail-fast on a configured model id known to be stale/removed (returns 404) so a stale
        # value cannot silently brick every LLM call in prod. Checked FIRST so it surfaces even if
        # another required secret is also absent. Warn (not raise) in dev.
        for _model_id in {
            self.task_llm_model,
            self.task_llm_fallback_model,
            self.analysis_llm_model,
            self.context_llm_model,
            self.agent_fast_model,
            self.llm_model,
        }:
            if _model_id and _model_id in _KNOWN_STALE_MODELS:
                stale_msg = (
                    f"Configured model '{_model_id}' is known-stale/removed (returns 404). "
                    "Update it to a current model id (see DEFAULT_* in app/config.py)."
                )
                if is_dev:
                    _config_logger.warning(stale_msg)
                else:
                    raise ValueError(stale_msg)

        if not self.secret_key:
            if is_dev:
                # Generate a random key for local dev so it still boots
                import secrets as _s
                self.secret_key = _s.token_urlsafe(48)
                _config_logger.warning(
                    "SECRET_KEY not set — using random ephemeral key (dev only). "
                    "Sessions will not survive restarts."
                )
            else:
                raise ValueError(
                    "SECRET_KEY environment variable is required in non-development environments."
                )

        if not self.database_url and not is_dev:
            raise ValueError(
                "DATABASE_URL environment variable is required in non-development environments."
            )

        if not self.secret_encryption_key and not is_dev:
            raise ValueError(
                "SECRET_ENCRYPTION_KEY environment variable is required in non-development environments."
            )

        if not self.redis_url and not is_dev:
            raise ValueError(
                "REDIS_URL environment variable is required in non-development environments."
            )

        if self.bff_client_ip_secret and len(self.bff_client_ip_secret) < 32:
            raise ValueError("BFF_CLIENT_IP_SECRET must be at least 32 characters.")
        if not self.bff_client_ip_secret and not is_dev:
            _config_logger.warning(
                "BFF_CLIENT_IP_SECRET is not set: unauthenticated rate limits key on the proxy "
                "address, so every browser behind the frontend shares one bucket."
            )

        if not is_dev and not self.auth_disable_legacy_password_login_in_production:
            raise ValueError(
                "AUTH_DISABLE_LEGACY_PASSWORD_LOGIN_IN_PRODUCTION must be true in non-development environments."
            )

        if not self.upload_malware_scanner_mode:
            self.upload_malware_scanner_mode = "disabled" if is_dev else "required"

        valid_scanner_modes = {"disabled", "off", "false", "required", "enforce", "fail_closed", "log_only", "monitor"}
        if self.upload_malware_scanner_mode not in valid_scanner_modes:
            raise ValueError("UPLOAD_MALWARE_SCANNER_MODE is invalid.")

        scanner_provider = (self.upload_malware_scanner_provider or "").strip().lower()
        if scanner_provider and scanner_provider not in {"http", "clamav_http", "external_http", "eicar_test", "test"}:
            raise ValueError("UPLOAD_MALWARE_SCANNER_PROVIDER is invalid.")
        if self.upload_malware_scanner_scheme not in {"http", "https"}:
            raise ValueError("UPLOAD_MALWARE_SCANNER_SCHEME must be http or https.")
        if not self.upload_malware_scanner_url and os.getenv("UPLOAD_MALWARE_SCANNER"):
            self.upload_malware_scanner_url = os.getenv("UPLOAD_MALWARE_SCANNER", "").strip()
        if self.upload_malware_scanner_url and "://" not in self.upload_malware_scanner_url:
            self.upload_malware_scanner_url = f"{self.upload_malware_scanner_scheme}://{self.upload_malware_scanner_url}"
        if self.upload_malware_scanner_hostport and not self.upload_malware_scanner_url:
            path = self.upload_malware_scanner_path or "/scan"
            if not path.startswith("/"):
                path = f"/{path}"
            self.upload_malware_scanner_path = path
            self.upload_malware_scanner_url = (
                f"{self.upload_malware_scanner_scheme}://{self.upload_malware_scanner_hostport}{path}"
            )
        if self.upload_malware_scanner_timeout_seconds <= 0:
            raise ValueError("UPLOAD_MALWARE_SCANNER_TIMEOUT_SECONDS must be greater than 0.")

        if not is_dev:
            if self.upload_malware_scanner_mode not in {"required", "enforce", "fail_closed"}:
                raise ValueError(
                    "UPLOAD_MALWARE_SCANNER_MODE must be required in non-development environments."
                )
            if scanner_provider in {"eicar_test", "test"}:
                raise ValueError(
                    "UPLOAD_MALWARE_SCANNER_PROVIDER cannot use the test scanner in non-development environments."
                )
            if scanner_provider not in {"http", "clamav_http", "external_http"}:
                raise ValueError(
                    "UPLOAD_MALWARE_SCANNER_PROVIDER must be http, clamav_http, or external_http in non-development environments."
                )
            if not self.upload_malware_scanner_url:
                raise ValueError(
                    "UPLOAD_MALWARE_SCANNER_URL environment variable is required in non-development environments."
                )

        if self.cache_serialization_mode != "json":
            if not is_dev:
                raise ValueError("CACHE_SERIALIZATION_MODE must be json in non-development environments.")
            _config_logger.warning(
                "CACHE_SERIALIZATION_MODE=%s is allowed only in dev/test; production cache serialization is JSON-only.",
                self.cache_serialization_mode,
            )

        if not self.qbo_webhook_verifier_token and not is_dev:
            raise ValueError(
                "QBO_WEBHOOK_VERIFIER_TOKEN environment variable is required in non-development environments."
            )

        if self.cors_origin_regex:
            try:
                re.compile(self.cors_origin_regex)
            except re.error as exc:
                raise ValueError(f"CORS_ORIGIN_REGEX is not a valid regular expression: {exc}") from exc

        if not self.stytch_project_id and not is_dev:
            raise ValueError(
                "STYTCH_PROJECT_ID environment variable is required in non-development environments."
            )

        if not self.stytch_secret and not is_dev:
            raise ValueError(
                "STYTCH_SECRET environment variable is required in non-development environments."
            )

        for lane_name, provider_attr, model_attr in (
            ("task", "task_llm_provider", "task_llm_model"),
            ("analysis", "analysis_llm_provider", "analysis_llm_model"),
            ("context", "context_llm_provider", "context_llm_model"),
        ):
            provider = (getattr(self, provider_attr) or "").strip().lower()
            model = (getattr(self, model_attr) or "").strip()
            if not provider:
                raise ValueError(f"{provider_attr.upper()} must be set for the {lane_name} lane.")
            if not model:
                raise ValueError(f"{model_attr.upper()} must be set for the {lane_name} lane.")
            if provider in {"openai", "gpt"} and not self.openai_api_key:
                if not is_dev:
                    raise ValueError(f"{lane_name} lane is configured for OpenAI but OPENAI_API_KEY is missing.")
                _config_logger.warning("%s lane is configured for OpenAI but OPENAI_API_KEY is missing.", lane_name)
            if provider in {"claude", "anthropic"} and not self.anthropic_api_key:
                if not is_dev:
                    raise ValueError(f"{lane_name} lane is configured for Anthropic but ANTHROPIC_API_KEY is missing.")
                _config_logger.warning("%s lane is configured for Anthropic but ANTHROPIC_API_KEY is missing.", lane_name)

        fallback_provider = (self.task_llm_fallback_provider or "").strip().lower()
        fallback_model = (self.task_llm_fallback_model or "").strip()
        if fallback_provider and not fallback_model:
            raise ValueError("TASK_LLM_FALLBACK_MODEL must be set when TASK_LLM_FALLBACK_PROVIDER is set.")
        if fallback_provider in {"openai", "gpt"} and not self.openai_api_key:
            if not is_dev:
                raise ValueError("Task fallback lane is configured for OpenAI but OPENAI_API_KEY is missing.")
            _config_logger.warning("task fallback lane is configured for OpenAI but OPENAI_API_KEY is missing.")
        if fallback_provider in {"claude", "anthropic"} and not self.anthropic_api_key:
            if not is_dev:
                raise ValueError("Task fallback lane is configured for Anthropic but ANTHROPIC_API_KEY is missing.")
            _config_logger.warning("task fallback lane is configured for Anthropic but ANTHROPIC_API_KEY is missing.")

        if os.getenv("LLM_PROVIDER") or os.getenv("LLM_MODEL"):
            _config_logger.warning(
                "LLM_PROVIDER / LLM_MODEL are deprecated. TASK_*, ANALYSIS_*, and CONTEXT_* settings now control routing."
            )

        return self

    @property
    def intuit_auth_base(self):
        return "https://appcenter.intuit.com/connect/oauth2"

    @property
    def intuit_token_endpoint(self):
        return "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

    @property
    def intuit_revoke_endpoint(self):
        return "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

    @property
    def stytch_api_base(self) -> str:
        if self.stytch_base_url:
            return self.stytch_base_url.rstrip("/")
        env = self.stytch_environment.lower()
        return "https://test.stytch.com" if env == "test" else "https://api.stytch.com"

    class Config:
        extra = "ignore"

settings = Settings()
