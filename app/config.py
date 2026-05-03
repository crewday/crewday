"""Pydantic-settings config loader.

Every adapter, worker, and API router imports ``get_settings`` (or the
module-level ``settings`` proxy) from here; nothing else reads
``os.environ`` directly. Values come from process environment variables
prefixed ``CREWDAY_`` with an optional ``.env`` file at the repo root
— see ``.env.example`` for the full template.

See ``docs/specs/01-architecture.md`` §"Runtime invariants" and
``docs/specs/16-deployment-operations.md`` §"Environment variables".
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, IPvAnyNetwork, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_SECRET_MASK = "***"


class Settings(BaseSettings):
    """Process-wide configuration, loaded from env + optional ``.env``.

    Secrets are wrapped in :class:`pydantic.SecretStr` so they never
    appear in ``repr()`` or default serialisation. Use
    :meth:`safe_dump` when emitting settings to logs.
    """

    model_config = SettingsConfigDict(
        env_prefix="CREWDAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required ---
    database_url: str

    # --- Paths ---
    data_dir: Path = Path("./data")

    # --- Bind guard (see docs/specs/01 §"Runtime invariants" + §16) ---
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    trusted_interfaces: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["tailscale*"],
    )
    allow_public_bind: bool = False

    # --- Public URL ---
    public_url: str | None = None

    # --- WebAuthn (optional override; derived from public_url otherwise) ---
    # Only needed when the rp_id should differ from the origin's hostname —
    # e.g. hosting on ``app.example.com`` but scoping passkeys to the parent
    # ``example.com`` so they work on sibling subdomains too. See
    # ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics".
    webauthn_rp_id: str | None = None

    # --- SMTP (optional; see §10 messaging-notifications) ---
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    # Envelope sender for every outgoing message. ``None`` when SMTP
    # isn't configured; the :class:`app.adapters.mail.smtp.SMTPMailer`
    # requires it at construction time and will refuse to start without
    # one — the spec (§10) treats a message with no From as a bug.
    smtp_from: str | None = None
    # Whether STARTTLS (port 587) or implicit TLS (port 465) is attempted.
    # Plain-port 25 always skips TLS regardless. Operators who front the
    # relay over a trusted socket (localhost, unix socket bridge) can
    # flip this off; in every other deployment it must stay ``True``.
    smtp_use_tls: bool = True
    # Socket timeout (seconds) passed to ``smtplib.SMTP`` / ``SMTP_SSL``.
    # Applies to the initial connection and every subsequent I/O.
    smtp_timeout: int = 10
    # Domain used to build the ``Return-Path: bounce+<token>@<domain>``
    # header for future bounce-webhook correlation (§10). When ``None``,
    # the SMTPMailer falls back to the domain parsed from ``smtp_from``.
    smtp_bounce_domain: str | None = None

    # --- Chat gateway inbound webhooks (§23) ---
    # Deployment-scoped provider webhooks arrive on the bare host at
    # ``/webhooks/chat/{provider}``. ``chat_gateway_workspace_id`` names
    # the workspace that receives first-contact auto-created bindings
    # for the deployment-default provider account; per-provider secrets
    # verify the native webhook signature before any row is written.
    chat_gateway_workspace_id: str | None = None
    chat_gateway_twilio_secret: SecretStr | None = None
    chat_gateway_meta_whatsapp_secret: SecretStr | None = None
    chat_gateway_postmark_secret: SecretStr | None = None

    # --- LLM (optional; see §11 llm-and-agents) ---
    openrouter_api_key: SecretStr | None = None
    # Runtime LLM provider selector. ``None`` (default) preserves the
    # historical behaviour: the factory wires :class:`OpenRouterClient`
    # iff an API key is reachable, or returns ``None`` when both env
    # and root-key are unset. ``"openrouter"`` is the explicit prod
    # form. ``"fake"`` swaps in the in-process
    # :class:`~app.adapters.llm.fake.FakeLLMClient` so e2e / dev stacks
    # exercise the LLM path without an upstream key — gated to dev/e2e
    # via ``mocks/docker-compose.e2e.yml`` (see §16 "Environment
    # variables"). Production deployments leave this unset; the §11
    # ``fake`` provider type is dev/test only.
    llm_provider: Literal["openrouter", "fake"] | None = None
    # Model id used by :mod:`app.domain.expenses.autofill` for receipt
    # OCR + structured extraction. ``None`` disables the capability at
    # the deployment level: :func:`~app.domain.expenses.claims.attach_receipt`
    # skips the runner entirely (the fields stay empty for manual
    # entry) and ``POST /expenses/scan`` returns 503
    # ``scan_not_configured``. Set this to the wire model id the
    # caller's :class:`~app.adapters.llm.ports.LLMClient` is willing to
    # serve (``google/gemma-3-27b-it``, ``openai/gpt-4o-mini``, …).
    # The capability→model registry (§11 "Model assignment") is the
    # authoritative source once it lands; this setting is the v1
    # short-circuit while the §11 model router is still being plumbed
    # through (see cd-95zb's spec note on the schema remap from
    # ``Receipt.ocr_json`` to ``ExpenseClaim.llm_autofill_json``).
    llm_ocr_model: str | None = None

    # --- Signing / tokens ---
    root_key: SecretStr | None = None

    # --- Sessions (§03 "Sessions"; cd-cyq) ---
    # Session lifetime (days) for users who hold a ``manager`` surface
    # grant on any scope **or** are members of any ``owners`` permission
    # group — the "has_owner_grant" population. Everyone else gets the
    # longer :attr:`session_user_ttl_days` window. Recomputed on login,
    # not mid-session: a user who gains a manager grant mid-session keeps
    # their longer lifetime until the next sign-in. Mid-request refreshes
    # extend the existing value past half-life; see
    # :mod:`app.auth.session`.
    session_owner_ttl_days: int = 7
    # Session lifetime (days) for worker / client / guest users who hold
    # no manager surface grant and no owners-group membership anywhere.
    session_user_ttl_days: int = 30

    # --- API rate limiting (§12 "Rate limiting"; cd-uxdk) ---
    # ``memory`` is the single-worker/dev backend. Multi-process
    # deployments use ``postgres`` so bucket state is shared through the
    # deployment-wide ``rate_limit_bucket`` table.
    rate_limit_backend: Literal["memory", "postgres"] = "memory"
    rate_limit_token_per_minute: int = Field(default=60, ge=1)
    rate_limit_personal_me_per_minute: int = Field(default=600, ge=1)
    rate_limit_anonymous_per_minute: int = Field(default=30, ge=1)

    # --- Signup abuse mitigations (§15 "Self-serve abuse mitigations"; cd-055) ---
    # Cloudflare Turnstile server-side secret. ``None`` means "test /
    # offline mode": the CAPTCHA verifier accepts the fixed token
    # ``"test-pass"`` and rejects ``"test-fail"`` so unit tests never
    # hit the network. Operators running on the SaaS deployment set
    # this to the real Turnstile secret; the deployment setting
    # ``captcha_required`` then governs whether a token is mandatory
    # at all (spec §15 "Self-serve abuse mitigations"). The Turnstile
    # endpoint URL is pinned (not configurable) — changing the
    # provider is a code diff, not an ops switch.
    captcha_turnstile_secret: SecretStr | None = None

    # --- Runtime ---
    demo_mode: bool = False
    # Signed-cookie key for the ephemeral demo binding cookie (§24).
    # Operators should set a dedicated 32-byte base64-ish secret via
    # CREWDAY_DEMO_COOKIE_KEY; code falls back to root_key only for
    # local/test settings that predate the dedicated knob.
    demo_cookie_key: SecretStr | None = None
    demo_db_denylist: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
    )
    # Whitelist of top-frame origins the demo app is willing to be
    # embedded from (§15 "CSP on demo"). Whitespace-separated, pasted
    # verbatim into the ``frame-ancestors`` CSP directive when
    # :attr:`demo_mode` is ``True``. ``None`` / empty = the demo runs
    # stand-alone (no embedding), matching the §15 default. Ignored
    # outside demo mode — prod always keeps ``frame-ancestors 'none'``.
    demo_frame_ancestors: str | None = None
    demo_global_daily_usd_cap: float = Field(default=5.0, ge=0)
    demo_block_cidr: Annotated[list[IPvAnyNetwork], NoDecode] = Field(
        default_factory=list,
    )
    demo_mints_per_ip_per_hour: int = Field(default=10, ge=1)
    demo_mutations_per_workspace_per_minute: int = Field(default=60, ge=1)
    demo_llm_turns_per_workspace_per_minute: int = Field(default=10, ge=1)
    demo_max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    demo_upload_bytes_per_ip_per_day: int = Field(default=25 * 1024 * 1024, ge=1)
    demo_uploads_per_workspace_lifetime: int = Field(default=10, ge=1)
    demo_max_payload_bytes: int = Field(default=32 * 1024, ge=1)
    # Emit ``Strict-Transport-Security`` on every response (§15 "HTTP
    # security headers"). Default **off** so a fresh deployment that
    # has not yet provisioned TLS doesn't accidentally send a 2-year
    # HSTS pin over HTTP — which a browser will cache and then refuse
    # to downgrade on the next request, bricking the operator's own
    # dev loop. Operators flip this to ``True`` once their TLS cert
    # is live and verified.
    hsts_enabled: bool = False
    worker: Literal["internal", "external"] = "internal"
    storage_backend: Literal["localfs", "s3"] = "localfs"
    # Cross-worker SSE event relay (cd-nusy). The in-process bus only
    # reaches subscribers in the publishing worker; under
    # ``uvicorn --workers N`` an event published on worker A never
    # reaches an SSE client connected to worker B without a relay.
    #
    # * ``auto`` (default): Postgres → ``LISTEN/NOTIFY`` relay,
    #   SQLite → no-op (single-worker dev only).
    # * ``in_process``: force the no-op relay even on Postgres. Used
    #   in tests + an operator escape hatch for a single-worker PG
    #   deploy that wants to skip the dedicated LISTEN connection.
    # * ``postgres``: force the LISTEN/NOTIFY relay; refuses to start
    #   if the active dialect is anything other than PostgreSQL.
    #
    # See ``docs/specs/16-deployment-operations.md`` §"Multi-worker
    # behaviour" and :mod:`app.events.relay`.
    events_relay: Literal["auto", "in_process", "postgres"] = "auto"
    # Deployment profile selector for the SPA-serving seam (cd-q1be).
    # ``prod`` serves the built ``app/web/dist/`` via :class:`StaticFiles`
    # + SPA catch-all — the shape every self-hosted / SaaS box runs.
    # ``dev`` forwards non-API GETs to the Vite dev server at
    # :attr:`vite_dev_url` so HMR keeps working while an engineer edits
    # the SPA under ``app/web/src/``. The dev path is strictly a local
    # development affordance — the bind guard + public-interface rule
    # still apply (§16 "Binding policy"), and the proxy never runs in
    # prod-shaped deployments.
    profile: Literal["prod", "dev"] = "prod"
    # Base URL of the Vite dev server the ``dev`` profile proxies to.
    # Loopback by default so a misconfigured dev box does not end up
    # forwarding to an externally reachable process. Changing the port
    # in ``app/web/vite.config.ts`` requires updating this value too.
    vite_dev_url: str = "http://127.0.0.1:5173"
    # Root logger level applied by :func:`app.util.logging.setup_logging`
    # at factory boot. Kept as a string literal so ``CREWDAY_LOG_LEVEL``
    # maps one-to-one onto the stdlib names; ``DEBUG`` is deliberately
    # available as an ops lever without a code change.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Additional CORS origins allowed past the v1 "same-origin only"
    # default. Empty in every production deployment; dev work behind a
    # Vite proxy on a different port / host populates this with the
    # dev origin (e.g. ``http://127.0.0.1:5173``). Never wildcard —
    # the CORS middleware is ``allow_credentials=False``, so echoing
    # an untrusted Origin would still leak nothing sensitive, but a
    # mis-set wildcard here is a privacy regression waiting to
    # happen. Comma-separated at the env layer, via
    # :meth:`_split_cors_origins`.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
    )

    # --- iCal SSRF guard (cd-xr652) ---
    # **Dev / e2e ONLY — never enable in production.** Disables the
    # §04 "SSRF guard" private-address rejection inside
    # :class:`app.adapters.ical.validator.IcalValidatorConfig`, so a
    # feed URL whose host resolves to loopback / RFC 1918 / link-local
    # passes registration. The sole supported caller is
    # ``mocks/docker-compose.e2e.yml`` — Playwright's GA journey 3
    # (cd-zxvk) needs to point a feed at an in-cluster ICS server. No
    # other validator gate (scheme, DNS-rebind pin, redirects, size,
    # timeout) is loosened by this knob; only the public-IP filter.
    # Default ``False`` everywhere else; the production app refuses
    # any private-IP feed URL on registration regardless of how the
    # operator's compose file is shaped.
    ical_allow_private_addresses: bool = False

    # --- Tenancy (cd-iwsv, cd-9il) ---
    # Gates the Phase-0 ``X-Test-Workspace-Id`` header path inside
    # :mod:`app.tenancy.middleware`. Default **off** in every
    # production deployment — a client that supplies the header on a
    # binary with the flag on can mint any :class:`WorkspaceContext`,
    # so the flag exists purely for the unit-test seam while the real
    # resolver lands (cd-9il keeps the header path for the rare test
    # that needs to bypass DB lookups). Set to ``True`` only in a
    # sandbox where every caller is trusted.
    phase0_stub_enabled: bool = False

    # --- Native push notifications (§02 "user_push_token"; cd-nq9s) ---
    # Single boolean gate for the bare-host
    # ``POST /api/v1/me/push-tokens`` registration surface. Default
    # **off** because the v1 deployment has not yet provisioned FCM /
    # APNS credentials — the router maps the gate to ``501
    # push_unavailable`` so a native shell can probe capability without
    # having to consult a second feature flag. ``GET`` and ``DELETE``
    # are always live regardless of this flag so a sign-out can prune a
    # stale row even on a deployment with native push delivery off
    # (spec §02 "user_push_token" §"Surface" — sign-out cleanup must
    # not depend on the registration gate). Provisioning the
    # actual FCM / APNS credentials is tracked as a follow-up Beads
    # task; this knob only governs the HTTP surface visibility.
    native_push_enabled: bool = False

    # --- Observability (§16 "Observability / Metrics", cd-24tp) ---
    # Hard kill switch for the ``GET /metrics`` Prometheus endpoint.
    # Default **off** so a self-hosted box does not expose a metrics
    # surface unless the operator opts in. Recipe D's compose flips
    # it on alongside the in-cluster Prometheus scraper. When ``False``
    # the route returns 404 (not 403) so a scanner cannot distinguish
    # "metrics off" from "no such image".
    metrics_enabled: bool = False
    # CIDR allowlist for ``/metrics``. The endpoint matches the
    # request's TCP source IP against every entry; a non-match
    # returns 403. Empty falls back to the loopback + Tailscale CGNAT
    # default in :func:`app.observability.endpoint._resolve_allow_cidrs`.
    # Comma-separated at the env layer (see
    # :meth:`_split_metrics_allow_cidr`).
    metrics_allow_cidr: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
    )

    # --- Trusted reverse proxies (§16 "Reverse-proxy caveat", cd-ca0u) ---
    # CIDR list of reverse proxies whose ``X-Forwarded-For`` we are
    # willing to honour when resolving a request's source IP — wired
    # through :mod:`app.util.forwarded`. Empty (default) means no
    # proxies are trusted: every consumer (today: the ``/metrics``
    # CIDR gate) treats the TCP peer as the source IP and ignores XFF
    # entirely. Operators behind a reverse proxy populate this with
    # the proxy's CIDR — Recipe A (Caddy on host) sets
    # ``CREWDAY_TRUSTED_PROXIES=127.0.0.1/32``; compose stacks set the
    # Docker bridge CIDR. v4 and v6 entries mix freely. Comma-separated
    # at the env layer (see :meth:`_split_trusted_proxies`).
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
    )

    @field_validator("trusted_interfaces", mode="before")
    @classmethod
    def _split_trusted_interfaces(cls, value: object) -> object:
        """Parse comma-separated env input into a list.

        ``pydantic-settings`` would otherwise try to decode the raw
        env value as JSON for a ``list[str]`` field, which makes the
        natural ``CREWDAY_TRUSTED_INTERFACES=tailscale*,wg*`` form
        fail. Whitespace-only entries are dropped so a trailing comma
        doesn't turn into an empty-string glob.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        """Parse comma-separated env input into a list (see above)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("metrics_allow_cidr", mode="before")
    @classmethod
    def _split_metrics_allow_cidr(cls, value: object) -> object:
        """Parse comma-separated env input into a list (see above)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_trusted_proxies(cls, value: object) -> object:
        """Parse comma-separated env input into a list (see above)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("demo_db_denylist", mode="before")
    @classmethod
    def _split_demo_db_denylist(cls, value: object) -> object:
        """Parse comma-separated demo DB denylist URLs into a list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("demo_block_cidr", mode="before")
    @classmethod
    def _split_demo_block_cidr(cls, value: object) -> object:
        """Parse comma-separated demo IP deny-list CIDRs."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_rate_limit_backend(self) -> Settings:
        if self.rate_limit_backend == "postgres" and self.root_key is None:
            raise ValueError("CREWDAY_ROOT_KEY is required for postgres rate limiting")
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Return a dict with every :class:`SecretStr` masked.

        ``"***"`` for populated secrets, ``None`` for unset ones;
        non-secret fields pass through unchanged. Safe to log.
        """
        out: dict[str, Any] = {}
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = _SECRET_MASK if value.get_secret_value() else None
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    Cached so repeated calls are free and every caller sees the same
    object. Tests that mutate env between cases must call
    ``get_settings.cache_clear()``.
    """
    return Settings()


def __getattr__(name: str) -> Any:
    """Lazy module attribute for ``from app.config import settings``.

    Deferring construction until first access keeps the module
    importable in test collection even when required env vars haven't
    been set yet — mirrors the ``get_settings()`` contract.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
