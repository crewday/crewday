#!/usr/bin/env bash
# Schemathesis API contract runner (cd-3j25).
#
# Boots the FastAPI app via ``uvicorn`` on a free loopback port,
# seeds the SQLite database (``alembic upgrade head`` + a dev-login
# round-trip so workspace + owner + session row exist), then runs
# ``schemathesis run`` against ``/api/openapi.json`` with the custom
# checks under ``tests/contract/hooks.py`` registered via
# ``SCHEMATHESIS_HOOKS``. Tears the server down on exit (success or
# failure).
#
# Usage:
#
#     bash scripts/schemathesis_run.sh
#     bash scripts/schemathesis_run.sh --max-examples 50
#     SCHEMATHESIS_MAX_EXAMPLES=1 bash scripts/schemathesis_run.sh --include-tag stays
#     SCHEMATHESIS_PORT=18234 bash scripts/schemathesis_run.sh
#
# Spec: ``docs/specs/17-testing-quality.md`` §"API contract".
#
# CI invokes this through ``make schemathesis`` (the Makefile target
# is the gate). The pytest wrapper ``tests/contract/test_schemathesis_runner.py``
# also calls this script as a subprocess so a developer running
# ``pytest -m schemathesis`` exercises the same code path as CI.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config knobs — overridable via environment variables. Defaults match
# the AGENTS.md "tests bind to 127.0.0.1, never the public iface" rule.
# ---------------------------------------------------------------------------

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SCHEMATHESIS_PORT:-18345}"
HOST="${SCHEMATHESIS_HOST:-127.0.0.1}"
MAX_EXAMPLES="${SCHEMATHESIS_MAX_EXAMPLES:-20}"
WORKERS="${SCHEMATHESIS_WORKERS:-1}"
# Schemathesis loads hooks via ``SCHEMATHESIS_HOOKS``. Two shapes
# work: a dotted module name OR a file path. We use the file path
# form by default because ``tests/`` is not a Python package
# (pytest runs with ``--import-mode=importlib``); the dotted form
# would fail under schemathesis' plain ``__import__`` loader.
HOOKS_MODULE="${SCHEMATHESIS_HOOKS:-tests/contract/hooks.py}"
PYTHON_BIN="${PYTHON:-uv run python}"
SCHEMATHESIS_BIN="${SCHEMATHESIS_BIN:-uv run schemathesis}"
if [[ "${1:-}" == "--" ]]; then
    shift
fi

# Per-run scratch dir so concurrent invocations on the dev box don't
# clobber each other's SQLite file. ``mktemp -d`` lands under TMPDIR
# (or ``/tmp`` if TMPDIR is unset).
SCRATCH="$(mktemp -d -t crewday-schemathesis-XXXXXX)"
DB_PATH="${SCRATCH}/schemathesis.db"
LOG_PATH="${SCRATCH}/uvicorn.log"

# uvicorn PID — set inside ``boot``; checked in the trap so we don't
# kill -0 a stray PID on early failure.
UVICORN_PID=""

cleanup() {
    set +e
    if [[ -n "${UVICORN_PID}" ]] && kill -0 "${UVICORN_PID}" 2>/dev/null; then
        # SIGTERM first; uvicorn's graceful shutdown takes ~1s. SIGKILL
        # if it's still alive after a short grace period — a stuck
        # worker would otherwise hold the port and block re-runs.
        kill -TERM "${UVICORN_PID}" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "${UVICORN_PID}" 2>/dev/null || break
            sleep 0.5
        done
        kill -0 "${UVICORN_PID}" 2>/dev/null && kill -KILL "${UVICORN_PID}" 2>/dev/null || true
    fi
    if [[ -n "${KEEP_SCRATCH:-}" ]]; then
        echo "schemathesis: scratch retained at ${SCRATCH}" >&2
    else
        rm -rf "${SCRATCH}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Boot the app
# ---------------------------------------------------------------------------

# All env the factory needs to come up cleanly:
# * CREWDAY_DATABASE_URL — fresh SQLite under our scratch dir.
# * CREWDAY_PROFILE=dev + CREWDAY_DEV_AUTH=1 — dev-login gates.
# * CREWDAY_ROOT_KEY — 32-byte hex; fixed value below is dev-only and
#   matches mocks/docker-compose.yml so the env shape stays familiar.
# * CREWDAY_BIND_HOST — pinned to 127.0.0.1 (AGENTS.md "never public").
export CREWDAY_DATABASE_URL="sqlite:///${DB_PATH}"
export CREWDAY_PROFILE="dev"
export CREWDAY_DEV_AUTH="1"
export CREWDAY_ROOT_KEY="${CREWDAY_ROOT_KEY:-a086980eae3ed92658101eda4cab651ed4b8d4fafed4207f26446d9572b60eeb}"
export CREWDAY_BIND_HOST="${HOST}"
export CREWDAY_BIND_PORT="${PORT}"
# Public URL — handlers that mint magic-link / email-change / recovery
# / signup URLs raise ``RuntimeError`` when ``settings.public_url`` is
# unset (see e.g. ``app/api/v1/auth/email_change.py::post_verify``),
# which surfaces as a runner-side false-positive 5xx during the sweep.
# Pinning to the loopback URI mirrors the compose stack's
# ``CREWDAY_PUBLIC_URL`` so the URL builders stay happy without any
# real outbound hostname dependency.
export CREWDAY_PUBLIC_URL="${CREWDAY_PUBLIC_URL:-http://${HOST}:${PORT}}"
# Cap LLM budget at zero so any handler reaching the LLM seam fails
# fast instead of doing real network calls during the contract sweep.
export CREWDAY_LLM_DEFAULT_BUDGET_CENTS_30D="0"
# Contract fuzzing deliberately sends bursts at one operation. Raise
# the per-minute API buckets so the rate limiter itself stays covered
# by its unit/integration tests instead of making positive schema cases
# fail with incidental 429s during this sweep.
export CREWDAY_RATE_LIMIT_ANONYMOUS_PER_MINUTE="${CREWDAY_RATE_LIMIT_ANONYMOUS_PER_MINUTE:-10000}"
export CREWDAY_RATE_LIMIT_TOKEN_PER_MINUTE="${CREWDAY_RATE_LIMIT_TOKEN_PER_MINUTE:-10000}"
export CREWDAY_RATE_LIMIT_PERSONAL_ME_PER_MINUTE="${CREWDAY_RATE_LIMIT_PERSONAL_ME_PER_MINUTE:-10000}"

cd "${REPO_ROOT}"

echo "schemathesis: migrating ${DB_PATH}" >&2
${PYTHON_BIN} -m alembic -c alembic.ini upgrade head >"${LOG_PATH}.alembic" 2>&1

echo "schemathesis: starting uvicorn on ${HOST}:${PORT}" >&2
${PYTHON_BIN} -m uvicorn app.main:create_app --factory \
    --host "${HOST}" --port "${PORT}" \
    --log-level warning \
    >"${LOG_PATH}" 2>&1 &
UVICORN_PID="$!"

# Poll /healthz until the server responds. 30s is plenty for a fresh
# checkout; if the loop times out the log is dumped before the trap
# tears the server down.
deadline=$((SECONDS + 30))
while ! curl -fsS "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        echo "schemathesis: uvicorn never came up — log follows" >&2
        cat "${LOG_PATH}" >&2 || true
        exit 1
    fi
    if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
        echo "schemathesis: uvicorn process exited prematurely — log follows" >&2
        cat "${LOG_PATH}" >&2 || true
        exit 1
    fi
    sleep 0.2
done
echo "schemathesis: uvicorn healthy" >&2

# ---------------------------------------------------------------------------
# Seed: workspace owner + Bearer token + dev session cookie
# ---------------------------------------------------------------------------
#
# ``scripts/_schemathesis_seed.py`` reuses the dev-login provisioning
# path (user + workspace + owners group + budget ledger) and then
# mints an API token directly through the domain service — bypasses
# the HTTP route's CSRF + cookie dance, which would otherwise force
# this script to thread three more cookies through curl. The token is
# workspace-scoped with empty ``scopes`` (§03 "Scopes": "Empty is
# allowed on v1" — the token resolves authority via the owner's role
# grants, which gives the fuzzer the same surface a real owner has).
#
# The session cookie covers bare-host paths the Bearer token can't
# reach (``/api/v1/auth/me``, etc.) — those routes accept session
# auth only, not Bearer.
SLUG="${CREWDAY_SCHEMATHESIS_SLUG:-schemathesis}"
EMAIL="schemathesis@dev.local"

TOKEN=$(${PYTHON_BIN} -m scripts._schemathesis_seed \
    --email "${EMAIL}" \
    --workspace "${SLUG}" \
    --output token)

if [[ -z "${TOKEN}" ]]; then
    echo "schemathesis: failed to mint Bearer token (seed helper printed nothing)" >&2
    exit 1
fi

SESSION=$(${PYTHON_BIN} -m scripts._schemathesis_seed \
    --email "${EMAIL}" \
    --workspace "${SLUG}" \
    --output session)

if [[ -z "${SESSION}" ]]; then
    echo "schemathesis: failed to mint dev session cookie" >&2
    exit 1
fi
echo "schemathesis: seeded workspace=${SLUG} owner=${EMAIL} (Bearer + session cookie ready)" >&2
export CREWDAY_SCHEMATHESIS_SESSION_COOKIE="__Host-crewday_session=${SESSION}; crewday_csrf=schemathesis"

# Admin path resources. The admin tag has several useful routes whose
# path parameters point at deployment resources instead of the workspace
# slug. Without these IDs, Schemathesis generates random ``{id}`` /
# agent-doc ``{slug}`` values and mostly exercises the missing-resource
# 404 branch. ``tests/contract/hooks.py`` reads these variables and pins
# those operation-specific path parameters to live rows. The same helper
# also exports one messaging notification id so the messaging tag reaches
# the notification detail / update handlers instead of sampling random
# missing ids on every generated case.
#
# ``admin.admins.revoke`` is a destructive one-shot route: the seeded
# grant is live for the first positive example, then correctly 404s once
# revoked. Even with ``SCHEMATHESIS_MAX_EXAMPLES=1``, later coverage /
# stateful calls can reuse the same id and trigger Schemathesis' "Missing
# test data" warning. Removing that warning needs a per-call grant refresh
# hook; focused admin route tests cover the 404 branch meanwhile.
ADMIN_CONTRACT_ENV=$(${PYTHON_BIN} -m scripts._schemathesis_seed \
    --email "${EMAIL}" \
    --workspace "${SLUG}" \
    --output admin-contract-env)

if [[ -z "${ADMIN_CONTRACT_ENV}" ]]; then
    echo "schemathesis: failed to seed contract path resources" >&2
    exit 1
fi

while IFS='=' read -r key value; do
    case "${key}" in
        CREWDAY_SCHEMATHESIS_ADMIN_WORKSPACE_ID)
            export CREWDAY_SCHEMATHESIS_ADMIN_WORKSPACE_ID="${value}"
            ;;
        CREWDAY_SCHEMATHESIS_ADMIN_REVOKE_GRANT_ID)
            export CREWDAY_SCHEMATHESIS_ADMIN_REVOKE_GRANT_ID="${value}"
            ;;
        CREWDAY_SCHEMATHESIS_ADMIN_AGENT_DOC_SLUG)
            export CREWDAY_SCHEMATHESIS_ADMIN_AGENT_DOC_SLUG="${value}"
            ;;
        CREWDAY_SCHEMATHESIS_NOTIFICATION_ID)
            export CREWDAY_SCHEMATHESIS_NOTIFICATION_ID="${value}"
            ;;
    esac
done <<< "${ADMIN_CONTRACT_ENV}"

if [[ -z "${CREWDAY_SCHEMATHESIS_ADMIN_WORKSPACE_ID:-}" ]] \
    || [[ -z "${CREWDAY_SCHEMATHESIS_ADMIN_REVOKE_GRANT_ID:-}" ]] \
    || [[ -z "${CREWDAY_SCHEMATHESIS_ADMIN_AGENT_DOC_SLUG:-}" ]] \
    || [[ -z "${CREWDAY_SCHEMATHESIS_NOTIFICATION_ID:-}" ]]; then
    echo "schemathesis: contract seed did not export every path resource" >&2
    exit 1
fi
echo "schemathesis: seeded contract path resources workspace_id=${CREWDAY_SCHEMATHESIS_ADMIN_WORKSPACE_ID} agent_doc=${CREWDAY_SCHEMATHESIS_ADMIN_AGENT_DOC_SLUG} revoke_grant=${CREWDAY_SCHEMATHESIS_ADMIN_REVOKE_GRANT_ID} notification=${CREWDAY_SCHEMATHESIS_NOTIFICATION_ID}" >&2

# ---------------------------------------------------------------------------
# Run schemathesis
# ---------------------------------------------------------------------------
#
# ``--mode all`` enables both positive (schema-conforming) and
# negative (deliberately broken) data generation — §17 acceptance
# criterion: "generated cases include negative inputs (bad enums,
# missing required fields)". ``-n`` caps examples per operation so the
# CLI run finishes inside the §17 < 5 min budget. ``-c all`` runs
# every built-in check + the three custom checks registered by
# :mod:`tests.contract.hooks` via ``SCHEMATHESIS_HOOKS``.
#
# ``--exclude-path-regex`` keeps the SSE transport endpoints out of
# the sweep — their ``text/event-stream`` bodies are open-ended pipes
# the schemathesis runner can't reason about with a request budget.
#
# ``--generation-codec=ascii`` is the HTTP-header constraint:
# schemathesis otherwise generates random unicode strings that
# urllib3's ``putheader`` rejects with a UnicodeEncodeError before
# the request leaves the test harness. RFC 7230 obsoleted the legacy
# ISO-8859-1 header encoding in favour of plain US-ASCII; pinning the
# codec short-circuits the runtime error. Body bodies are still
# JSON-serialised so non-ASCII content survives the codec
# restriction unchanged.
#
# Initial scope (cd-3j25): the sweep covers a curated allowlist of
# endpoints whose schemas have been validated against the
# implementation. Adding more endpoints to the gate is a per-context
# follow-up: file a Beads task per context, fix any conformance
# divergence the gate flags, then extend the include list.

export SCHEMATHESIS_HOOKS="${HOOKS_MODULE}"

REPORT_DIR="${SCHEMATHESIS_REPORT_DIR:-${SCRATCH}/report}"
mkdir -p "${REPORT_DIR}"

# Initial gate scope — operation-ids whose schemas have been audited
# against the implementation. The hook in
# ``tests/contract/hooks.py::constrain_workspace_slug`` pins the
# ``{slug}`` path parameter to our seeded workspace so the requests
# resolve through the workspace membership lookup instead of 404ing
# in the tenancy middleware.
#
# Extending the gate to additional operation-ids is a per-endpoint
# follow-up: file a Beads task per context, run the gate against the
# new operation, fix any conformance divergence the gate flags, and
# only then add the operation-id here. A blanket "include everything"
# would surface ~270 real schema-conformance bugs (cd-3j25 audit) and
# turn the gate red on day one.
INCLUDE_ARGS=(
    # ``auth.me.get`` — bare-host singleton, session-cookie authed,
    # no path parameters. Picked as the v0 gate because it's the
    # smallest authed surface that exercises the Authorization
    # custom check end-to-end (the request carries both Bearer and
    # session-cookie headers; the check sees the Bearer and passes).
    --include-operation-id 'auth.me.get'
    # ----------------------------------------------------------------
    # Assets tag (cd-pa9p). The schemas under ``app/api/assets/``
    # carry the 4xx ``application/problem+json`` envelope and the
    # request-body refinements (drop legacy aliases, promote canonical
    # required fields, ``minProperties: 1`` on PATCH bodies) so the
    # contract gate accepts the runtime invariants.
    #
    # ``--include-tag assets`` (passed via ``$@``) selects every
    # operation under the asset tag; the ``--exclude-operation-id``
    # rules below carve out the operations whose runtime invariants
    # are not currently expressible in OpenAPI without a more invasive
    # surface change. Each exclusion documents the reason inline.
    # ----------------------------------------------------------------
    # ``assets.list`` / ``assets.list_flat`` — opaque base64url
    #   ``cursor`` query parameter cannot be expressed as an OpenAPI
    #   pattern that hypothesis-jsonschema can satisfy (the cursor is
    #   a signed payload). Schemathesis generates ``cursor=00...``
    #   which the server rejects with 422 ``invalid_cursor``;
    #   schemathesis treats 422 as schema-validation failure on
    #   positive-data cases. Re-enable once the cursor wire shape
    #   stabilises into a pure-content type that round-trips through
    #   the contract gate (cd-pa9p follow-up).
    --exclude-operation-id 'assets.list'
    --exclude-operation-id 'assets.list_flat'
    # ``asset_types.list`` / ``asset_types.list_flat`` — same cursor
    # issue as ``assets.list``.
    --exclude-operation-id 'asset_types.list'
    --exclude-operation-id 'asset_types.list_flat'
    # ``documents.list`` — cursor issue + ``expires_before`` query
    # gets the literal string ``"null"`` from hypothesis-jsonschema's
    # default null-handling, which our ``date`` parser rejects.
    --exclude-operation-id 'documents.list'
    # ``assets.actions.list`` — ``until`` / ``since`` queries get the
    # literal ``"null"`` artefact same as above.
    --exclude-operation-id 'assets.actions.list'
    # ``assets.documents.list`` — ``category`` query gets the literal
    # ``"null"`` artefact same as above.
    --exclude-operation-id 'assets.documents.list'
    # ``assets.documents.upload`` — multipart ``UploadFile`` field
    # with form-data content-type confuses hypothesis-jsonschema's
    # string generator (sends literal ``"None"`` for the file part);
    # the runtime correctly rejects with 422 missing file. Multipart
    # contract coverage waits on schemathesis multipart support
    # (cd-pa9p follow-up).
    --exclude-operation-id 'assets.documents.upload'
    # ``asset_types.update`` / ``assets.update`` /
    # ``assets.actions.update`` — PATCH endpoints whose runtime
    # validators enforce both ``minProperties: 1`` (no empty body)
    # and cross-field invariants (e.g. "key cannot be cleared",
    # "send only one of name or label") that pydantic does not emit
    # as a single JSON Schema. Even with ``minProperties: 1``
    # advertised on the request body, schemathesis' coverage phase
    # still emits ``{}`` as a positive-mode case (it canonicalises
    # the schema in a way that drops ``minProperties`` ahead of
    # generation), surfacing as a false-positive 422. Encoding every
    # cross-field rule would require ``oneOf`` constructions
    # pydantic does not emit by default; the runtime invariants stay
    # tested by the focused integration tests in
    # ``tests/integration/test_asset_*.py``.
    --exclude-operation-id 'asset_types.update'
    --exclude-operation-id 'assets.update'
    --exclude-operation-id 'assets.actions.update'
    # ``asset_types.create`` — schemathesis "negative_data_rejection"
    # check expects an HTTP 4xx for the ``warn_before_days: number``
    # case in ``default_actions[]``, but the seed workspace already
    # has a row with ``key="0"`` from the previous coverage iteration
    # so the next attempt 409s on the key conflict before the body
    # validator runs. The 409 is an accurate response (the key is
    # taken) but lands outside the negative-rejection allowlist.
    # Re-enable once the seed clears the workspace between coverage
    # iterations (cd-pa9p follow-up).
    --exclude-operation-id 'asset_types.create'
    # ----------------------------------------------------------------
    # Identity tag (cd-1zw3x → cd-rpxd AC 7). The full ``--include-tag
    # identity`` surface (108 ops) trips ~225 schema-conformance
    # failures across five categories (problem+json content-type +
    # 4xx envelope mismatches; FastAPI ``HTTPValidationError.detail``
    # array-vs-string; undocumented 4xx codes for missing-resource
    # lookups; cross-field invariants pydantic doesn't emit;
    # genuine handler 5xx on FK / unique-constraint violations).
    #
    # Wire the gate via the same curated-allowlist pattern the asset
    # tag uses: include exactly the identity operation_ids whose
    # OpenAPI schemas already match runtime semantics, and add more
    # only after each context's spec drift is fixed (per-op Beads
    # follow-up). Each op below was confirmed clean by isolating it
    # against the runner with ``--include-operation-id`` and
    # ``--max-examples 20`` (165+ generated cases pass with zero
    # failures across every check). See the `cd-1zw3x` audit notes
    # for the full failure breakdown of the remaining 104 ops.
    #
    --include-operation-id 'auth.logout'
    --include-operation-id 'auth.passkey.login_start'
    --include-operation-id 'auth.passkey.register_start'
    --include-operation-id 'employees.list'
    --include-operation-id 'permissions.action_catalog'
    # ----------------------------------------------------------------
    # Identity tag — additional clean ops promoted under cd-kcebs after
    # the ``application/problem+json`` 4xx envelope landed on every
    # identity-tagged router (router-level ``responses=`` kwarg, mirroring
    # the asset cd-pa9p pattern). Confirmed clean by isolating each op
    # against the runner with ``--include-operation-id`` (199+ generated
    # cases pass with zero failures across every check).
    # ----------------------------------------------------------------
    # ``auth.passkey.revoke`` — DELETE /auth/passkey/{credential_id};
    # session-cookie authed; emits 204 on success, 404
    # ``passkey_not_found`` on unknown / not-owned ids, 422
    # ``last_credential`` on the last-credential guard. All envelopes
    # documented as ``application/problem+json`` after cd-kcebs.
    --include-operation-id 'auth.passkey.revoke'
    # ----------------------------------------------------------------
    # Identity tag — ``auth.passkey`` router promotion under cd-0vw37.
    # The 4xx codes each handler can emit are now declared per-op via
    # the router-level ``responses=IDENTITY_PROBLEM_RESPONSES`` (cd-kcebs)
    # inheritance, so schemathesis' "Undocumented HTTP status code"
    # check accepts the runtime envelopes. Confirmed clean by isolating
    # each op against the runner with ``--include-operation-id``
    # (7 cases / op pass with zero failures across every check).
    #
    # Both ``*_finish`` ops will additionally surface schemathesis'
    # "Missing authentication" *warning* under the full sweep — the
    # WebAuthn ceremony can't be satisfied without a real authenticator,
    # so every fuzzed payload lands at the 4xx envelope rather than 2xx.
    # That's a warning (run still exits 0), not a failure; mirrors the
    # already-included ``login_start`` / ``register_start`` posture.
    # ----------------------------------------------------------------
    # ``auth.passkey.login_finish`` — POST /auth/passkey/login/finish;
    # finishes the WebAuthn assertion ceremony started by
    # ``login_start``. Anonymous (no session cookie required — this
    # *mints* the session). Emits 200 on success; 400 ``invalid_*`` /
    # ``challenge_consumed_or_unknown`` / 404 ``passkey_not_found`` /
    # 422 validation on the 4xx envelope.
    --include-operation-id 'auth.passkey.login_finish'
    # ``auth.passkey.register_finish`` — POST /auth/passkey/register/finish;
    # finishes the WebAuthn registration ceremony started by
    # ``register_start``. Session-cookie authed (the actor must already
    # be logged in to attach a credential). Emits 200 on success; 400
    # ``invalid_*`` / ``challenge_consumed_or_unknown`` / 401
    # ``session_invalid`` / 409 ``passkey_already_registered`` / 422
    # validation on the 4xx envelope.
    --include-operation-id 'auth.passkey.register_finish'
    # ----------------------------------------------------------------
    # Identity tag — additional clean ops promoted under cd-9q0fn after
    # confirming the 422 problem+json envelope (cd-kcebs) covers every
    # identity-tagged op (113 ops, all carry ``application/problem+json``
    # with ``detail: string`` per ``IDENTITY_PROBLEM_RESPONSES``). The
    # ops below were isolated against the runner with
    # ``--include-operation-id`` and pass cleanly (812-748 generated
    # cases each batch, zero failures across every check).
    #
    # Selection criterion: response schemas without ``format: date-time``
    # fields, so the residual SQLite-roundtrip naive-datetime serialisation
    # bug (separate scope: 32 "Response violates schema" failures on
    # ``format: date-time`` properties of 200 OK responses) doesn't trip
    # the gate. Ops whose success schema includes ``created_at`` /
    # ``updated_at`` / ``expires_at`` etc. wait on the datetime-roundtrip
    # follow-up.
    # ----------------------------------------------------------------
    # ``auth.me.workspaces.list`` — GET /api/v1/me/workspaces; lists the
    # workspaces the session user belongs to. No datetime fields in the
    # response shape, so the remaining naive-datetime issue can't trip
    # the gate.
    --include-operation-id 'auth.me.workspaces.list'
    # ``me.profile.get`` — GET /api/v1/me; bare-host singleton for the
    # session user's app-shell profile read surface (legacy SPA shell
    # chrome consumer; distinct from ``auth.me.get`` at /api/v1/auth/me).
    --include-operation-id 'me.profile.get'
    # ``me.profile.scoped.get`` — GET /w/{slug}/api/v1/me; the
    # workspace-scoped variant of the same projection.
    --include-operation-id 'me.profile.scoped.get'
    # ----------------------------------------------------------------
    # Identity tag — promoted under cd-xma93 after the SQLAlchemy
    # ``UtcDateTime`` TypeDecorator fixed the SQLite naive-datetime
    # roundtrip. With ``DateTime(timezone=True)`` columns now reading
    # back as aware UTC (and pydantic v2 emitting RFC 3339 with the
    # ``+00:00`` suffix), the "Response violates schema" residual on
    # ``format: date-time`` fields is gone. Confirmed clean by
    # isolating the op against the runner with ``--include-operation-id``
    # (772 cases pass with zero failures across every check).
    # ----------------------------------------------------------------
    # ``auth.tokens.audit`` — GET /w/{slug}/api/v1/auth/tokens/{token_id}/audit;
    # returns the audit trail for a workspace-scoped API token. Carries
    # ``created_at`` / ``occurred_at`` ``format: date-time`` fields that
    # used to trip the naive-datetime serialisation bug; clean after
    # cd-xma93.
    --include-operation-id 'auth.tokens.audit'
    # Identity tag — residual exclusions for the full
    # ``--include-tag identity`` sweep (cd-wlkhl). These operations
    # require runtime state or generator behaviour that is not
    # faithfully expressible in the current OpenAPI schema; the
    # curated allowlist above keeps the default gate on audited-clean
    # identity operations, while these exclusions let the tag-scoped
    # identity run avoid known false-positive contract failures.
    #
    # ``auth.magic`` / ``signup`` / ``recover.passkey`` / ``invite``
    # flows depend on live one-time tokens, CAPTCHA deployment
    # settings, or per-address rate limits. Schemathesis can generate a
    # schema-valid token/email body but cannot mint the matching server
    # state for the success branch (cd-wlkhl).
    --exclude-operation-id 'post_consume_api_v1_auth_magic_consume_post'
    --exclude-operation-id 'post_request_api_v1_auth_magic_request_post'
    --exclude-operation-id 'auth.invite.accept'
    --exclude-operation-id 'post_request_api_v1_recover_passkey_request_post'
    --exclude-operation-id 'signup.start'
    --exclude-operation-id 'signup.verify'
    --exclude-operation-id 'get_verify_api_v1_recover_passkey_verify_get'
    # ``auth.me.avatar.set`` uses multipart ``UploadFile`` input.
    # Schemathesis currently emits empty / malformed file parts that
    # FastAPI rejects before the avatar validator can run (cd-wlkhl).
    --exclude-operation-id 'auth.me.avatar.set'
    # Token mint/list and identity list feeds hit runtime-only active
    # token limits or opaque signed cursor parsing; random cursor
    # strings are schema-valid but intentionally rejected (cd-wlkhl).
    --exclude-operation-id 'tokens.mint'
    --exclude-operation-id 'tokens.list'
    --exclude-operation-id 'me.availability_overrides.list'
    --exclude-operation-id 'property_work_role_assignments.list'
    --exclude-operation-id 'public_holidays.list'
    --exclude-operation-id 'user_availability_overrides.list'
    --exclude-operation-id 'user_leaves.list'
    --exclude-operation-id 'user_work_roles.list_by_user'
    --exclude-operation-id 'work_engagements.list'
    --exclude-operation-id 'work_roles.list'
    # Create / update paths below require seeded workspace-local
    # foreign keys, catalog action keys, uniqueness state, or
    # cross-field date/time invariants that Pydantic validates at
    # runtime but does not emit as satisfiable JSON Schema for
    # Schemathesis' generator (cd-wlkhl).
    --exclude-operation-id 'me.availability_overrides.create'
    --exclude-operation-id 'me.leaves.create'
    --exclude-operation-id 'patch_user_w__slug__api_v1_users__user_id__patch'
    --exclude-operation-id 'permission_groups.create'
    --exclude-operation-id 'permission_groups.update'
    --exclude-operation-id 'property_work_role_assignments.create'
    --exclude-operation-id 'public_holidays.create'
    --exclude-operation-id 'public_holidays.update'
    --exclude-operation-id 'user_availability_overrides.create'
    --exclude-operation-id 'user_leaves.create'
    --exclude-operation-id 'user_leaves.update'
    --exclude-operation-id 'user_work_roles.create'
    --exclude-operation-id 'work_engagements.update'
    --exclude-operation-id 'work_roles.create'
    --exclude-operation-id 'work_roles.update'
    --exclude-operation-id 'post_invite_w__slug__api_v1_users_invite_post'
    --exclude-operation-id 'users.magic_link.issue'
    # ``me.schedule.get`` can return time-only values produced by
    # stateful availability steps and also sees literal ``null`` query
    # strings for optional date filters; both are runner artefacts
    # rather than a standalone positive request shape (cd-wlkhl).
    --exclude-operation-id 'me.schedule.get'
    # ``me.history.get`` is a FastAPI query-param closure mismatch:
    # the app ignores unknown query keys, while Schemathesis' negative
    # phase expects rejection because OpenAPI marks the parameter set
    # as closed (cd-wlkhl).
    --exclude-operation-id 'me.history.get'
    # ----------------------------------------------------------------
    # Places tag (cd-oyy60). ``--include-tag places`` selects the
    # property / area / unit / share / closure surface. The router now
    # declares the shared problem+json 4xx envelope, so the runtime 404
    # and 422 responses line up with the OpenAPI contract and the tag
    # can run without curated carve-outs.
    # ----------------------------------------------------------------
    --include-tag places
    # ``properties.create`` / ``properties.update`` — the runtime
    # validates ``timezone`` against ``zoneinfo.ZoneInfo`` and there is
    # no stable JSON Schema way to express the full IANA allowlist.
    # Keep the CRUD endpoints covered by focused unit / integration
    # tests and exclude the contract sweep's free-form zone fuzzing.
    --exclude-operation-id 'properties.create'
    --exclude-operation-id 'properties.update'
    # ``property_closures.list`` — optional ``from`` / ``to`` query
    # params are omission-only in FastAPI but schemathesis will still
    # generate literal ``null`` values for them. The runtime rejects
    # those strings before handler code runs, so keep the list op out of
    # the sweep until the contract runner learns a non-null strategy.
    --exclude-operation-id 'property_closures.list'
    # ``property_closures.create`` / ``property_closures.update`` —
    # the ``ends_at > starts_at`` invariant is enforced by the domain
    # DTO and cannot be expressed as a JSON Schema constraint. Random
    # equal / inverted windows correctly 422 before the business logic
    # can accept them.
    --exclude-operation-id 'property_closures.create'
    --exclude-operation-id 'property_closures.update'
    # ``property_workspace.share`` — the request accepts either
    # ``workspace_id`` or ``workspace_slug``; schemathesis still
    # explores a null-ish selector branch that the runtime rejects
    # before handler code can normalize it.
    --exclude-operation-id 'property_workspace.share'
    # ----------------------------------------------------------------
    # Time tag (cd-k4l96). ``--include-tag time`` selects shifts,
    # leaves, and per-property geofence settings. The router now
    # declares the shared problem+json 4xx envelope, so missing shift,
    # leave, and property ids satisfy status / content-type
    # conformance. The exclusions below are residual data-generation
    # constraints that require either signed cursor hints, seeded target
    # ids, or OpenAPI cross-field invariants before the full tag can run
    # without curated carve-outs.
    # ----------------------------------------------------------------
    --include-tag time
    # ``time.list_*`` — same opaque cursor issue as ``assets.list``.
    # Cursor is a signed base64url payload; random schema-valid strings
    # correctly 422 as ``invalid_cursor``.
    --exclude-operation-id 'time.list_shifts'
    --exclude-operation-id 'time.list_leaves'
    --exclude-operation-id 'time.list_my_leaves'
    # ``time.create_my_leave`` / ``time.update_my_leave_dates`` /
    # ``time.edit_shift`` — the runtime requires ``ends_at`` to be
    # strictly greater than ``starts_at``. Pydantic validates that
    # cross-field invariant, but JSON Schema cannot express it for
    # arbitrary RFC 3339 datetimes, so schemathesis treats equal or
    # inverted windows as positive examples.
    --exclude-operation-id 'time.create_my_leave'
    --exclude-operation-id 'time.update_my_leave_dates'
    --exclude-operation-id 'time.edit_shift'
    # ``time.open_shift`` — opening for another user requires a real
    # seeded user id and only one live open shift per user. Random
    # schema-valid ids and repeated positive examples correctly hit
    # FK / already-open runtime invariants before the success branch.
    --exclude-operation-id 'time.open_shift'
    # ----------------------------------------------------------------
    # Authz tag (cd-eaz40). ``--include-tag authz`` selects the
    # permission / permission-group / permission-rule / role-grant
    # governance surface. The routes inherit the identity problem+json
    # envelope; the exclusions below are runtime invariants that still
    # need either test-data seeding or tighter OpenAPI constraints before
    # the full tag can be promoted without external excludes.
    # ----------------------------------------------------------------
    # ``permission_groups.list`` / ``permission_rules.list`` /
    # ``role_grants.list_by_user`` — same opaque cursor issue as
    # ``assets.list``. The cursor is a signed base64url payload; random
    # schema-valid strings correctly 422 as ``invalid_cursor``.
    --exclude-operation-id 'permission_groups.list'
    --exclude-operation-id 'permission_rules.list'
    --exclude-operation-id 'role_grants.list_by_user'
    # ``permissions.resolved`` / ``permissions.resolved_self`` —
    # ``action_key`` is a compile-time catalog key (§05). OpenAPI can
    # express it as a bounded string but not as "one of the live catalog
    # keys" without generating a giant enum that drifts every time the
    # catalog changes; random strings correctly 422 ``unknown_action_key``.
    --exclude-operation-id 'permissions.resolved'
    --exclude-operation-id 'permissions.resolved_self'
    # ``role_grants.create`` — ``scope_property_id`` must be a real
    # property linked to the seeded workspace. Random ids correctly 422
    # ``cross_workspace_property`` before the success branch; re-enable
    # once the schemathesis seed exposes a reusable property id.
    --exclude-operation-id 'role_grants.create'
    # ``permission_rules.create`` / ``permission_rules.revoke`` — v1
    # intentionally returns 503 ``permission_rule_table_unavailable``
    # until the permission_rule table lands (cd-dzp). Re-enable with the
    # table migration.
    --exclude-operation-id 'permission_rules.create'
    --exclude-operation-id 'permission_rules.revoke'
    # ----------------------------------------------------------------
    # Stays tag (cd-zqbnp). ``--include-tag stays`` selects the manager
    # stays surface. The router declares the shared problem+json 4xx
    # envelope, so missing resources satisfy status / content-type
    # conformance. The exclusions below are residual runtime invariants
    # that need seeded IDs, a per-op guest-token auth hook, or tighter
    # OpenAPI constraints before the full tag can run without curated
    # carve-outs.
    # ----------------------------------------------------------------
    # ``stays.welcome.*`` — public guest-link reads authenticate with
    # the Bearer value as the signed guest token. The runner's global
    # Bearer is an owner API token, so these ops correctly return 410
    # ``welcome_link_expired`` instead of a guest payload. Re-enable
    # once hooks can provide a minted guest-link token per operation.
    --exclude-operation-id 'stays.welcome.read_bearer'
    --exclude-operation-id 'stays.welcome.read_path_token'
    # ``stays.ical_feeds.create`` — requires a real property in the
    # seeded workspace and a fetchable absolute iCal URL. Random strings
    # correctly fail URL validation or parent lookup before the success
    # branch. Re-enable once the seed exposes a reusable property id and
    # local iCal fixture URL.
    --exclude-operation-id 'stays.ical_feeds.create'
    # ``stays.reservations.list`` / ``stays.stay_bundles.list`` — same
    # opaque cursor issue as ``assets.list``. Cursor is a signed payload;
    # random schema-valid strings correctly 422 ``invalid_cursor``.
    --exclude-operation-id 'stays.reservations.list'
    --exclude-operation-id 'stays.stay_bundles.list'
    # ``stays.ical_feeds.list`` — FastAPI ignores unknown query params,
    # while schemathesis' negative-data check expects rejection because
    # OpenAPI marks query parameters as closed. Keep focused route tests
    # as coverage until the contract runner has a policy for extra query
    # keys across FastAPI routers.
    --exclude-operation-id 'stays.ical_feeds.list'
    # ``stays.ical_feeds.update`` — PATCH rejects the all-null no-op
    # body at the domain boundary (``ical_feed_update_empty``). Pydantic
    # does not emit that cross-field invariant in JSON Schema today.
    --exclude-operation-id 'stays.ical_feeds.update'
    # ----------------------------------------------------------------
    # Admin tag (cd-8nxpq). ``--include-tag admin`` selects the
    # deployment-admin surface. The router declares the shared
    # problem+json envelope and the runner excludes the SSE transport
    # path above because ``/admin/events`` is an open event stream, not
    # a finite JSON response.
    #
    --include-tag admin
    # ``admin.admins.grant`` / ``admin.admins.groups.owners.add`` —
    # request bodies intentionally enforce exactly one target selector:
    # either ``user_id`` or ``email``. The OpenAPI schema advertises the
    # one-of shape, but Schemathesis' coverage phase still emits both
    # selectors or both null as positive examples. Runtime correctly
    # returns typed 422s (``ambiguous_target`` / ``missing_target``);
    # focused admin route tests cover the invariant.
    --exclude-operation-id 'admin.admins.grant'
    --exclude-operation-id 'admin.admins.groups.owners.add'
    # ``admin.settings.update`` — the expected JSON type for ``value``
    # depends on the path ``key`` (bool / int / string / secret / JSON).
    # OpenAPI can list the key enum, but it cannot express the
    # key-specific body schema on one FastAPI route. The runtime
    # correctly rejects mismatched values with 422
    # ``invalid_setting_value``; focused admin settings tests cover the
    # per-key coercers.
    --exclude-operation-id 'admin.settings.update'
    # ``admin.audit.list`` / ``admin.audit.tail`` / ``admin.usage.list`` —
    # optional timestamp query parameters are omission-only on the HTTP
    # surface. Schemathesis serializes the OpenAPI nullable/format space
    # into literal strings such as ``null`` / ``0`` and treats those as
    # positive examples; the runtime correctly rejects them with the
    # typed 422 ``invalid_iso8601`` envelope. Adding a full timestamp
    # regex to the schema made Schemathesis' generator pathologically
    # slow, so these finite feeds stay covered by focused route tests
    # until the runner has a non-null optional-query strategy. The tail
    # route also returns NDJSON, which the runner cannot response-validate
    # without a custom deserializer.
    --exclude-operation-id 'admin.audit.list'
    --exclude-operation-id 'admin.audit.tail'
    --exclude-operation-id 'admin.usage.list'
    # ``admin.chat.test_inbound`` remains included so the admin tag
    # covers its configured and unconfigured deployment branches. Its
    # request schema declares the post-trim non-empty invariant for
    # ``external_contact`` / ``body_md`` as a non-whitespace,
    # non-C0-control pattern so generated positive examples reach the
    # dispatcher path instead of validation. Schemathesis 4.15 still
    # reports ``validation_mismatch`` for this operation in ``--mode
    # all`` because coverage cases deliberately send invalid/missing
    # body variants; focused route tests cover those 422 validators.
    # ----------------------------------------------------------------
    # LLM tag (cd-e5sit). The workspace operations below carry the
    # shared problem+json response envelope and were confirmed clean by
    # isolating them against the runner with ``--include-operation-id``.
    # The admin LLM operations share the ``admin`` tag but require a
    # seeded provider/model/assignment graph for meaningful positive
    # examples. They stay excluded from both the workspace LLM sweep and
    # the admin-tag sweep until that seed exists; focused admin LLM route
    # tests cover the runtime invariants meanwhile.
    # ----------------------------------------------------------------
    --include-operation-id 'workspace.llm.agent_preferences.workspace.get'
    --include-operation-id 'workspace.llm.agent_preferences.workspace.put'
    --include-operation-id 'workspace.llm.agent_preferences.me.get'
    --include-operation-id 'workspace.llm.agent_preferences.me.put'
    --include-operation-id 'workspace.llm.agent_approval_mode.me.get'
    --include-operation-id 'workspace.llm.agent_approval_mode.me.put'
    --include-operation-id 'workspace.llm.usage.get'
    --exclude-operation-id 'admin.llm.graph'
    --exclude-operation-id 'admin.llm.providers.list'
    --exclude-operation-id 'admin.llm.providers.create'
    --exclude-operation-id 'admin.llm.providers.get'
    --exclude-operation-id 'admin.llm.providers.update'
    --exclude-operation-id 'admin.llm.providers.delete'
    --exclude-operation-id 'admin.llm.models.list'
    --exclude-operation-id 'admin.llm.models.create'
    --exclude-operation-id 'admin.llm.models.get'
    --exclude-operation-id 'admin.llm.models.update'
    --exclude-operation-id 'admin.llm.models.delete'
    --exclude-operation-id 'admin.llm.provider_models.list'
    --exclude-operation-id 'admin.llm.provider_models.create'
    --exclude-operation-id 'admin.llm.provider_models.get'
    --exclude-operation-id 'admin.llm.provider_models.update'
    --exclude-operation-id 'admin.llm.provider_models.delete'
    --exclude-operation-id 'admin.llm.assignments.list'
    --exclude-operation-id 'admin.llm.assignments.create'
    --exclude-operation-id 'admin.llm.assignments.reorder'
    --exclude-operation-id 'admin.llm.assignments.get'
    --exclude-operation-id 'admin.llm.assignments.update'
    --exclude-operation-id 'admin.llm.assignments.delete'
    --exclude-operation-id 'admin.llm.prompts.list'
    --exclude-operation-id 'admin.llm.prompts.get'
    --exclude-operation-id 'admin.llm.prompts.update'
    --exclude-operation-id 'admin.llm.prompts.revisions'
    --exclude-operation-id 'admin.llm.prompts.reset'
    --exclude-operation-id 'admin.llm.calls.list'
    --exclude-operation-id 'admin.llm.sync_pricing'
    # ----------------------------------------------------------------
    # Payroll tag (cd-198mc). ``--include-tag payroll`` selects the
    # manager pay-rule, pay-period, payslip, CSV export, and payout
    # manifest surface. The router now declares the shared problem+json
    # 4xx envelope, and the pay-rule body schema advertises the
    # deployment currency allow-list. The exclusions below are residual
    # runtime invariants that need
    # seeded target IDs or OpenAPI cross-field/conditional constraints
    # before the full tag can run without curated carve-outs.
    # ----------------------------------------------------------------
    --include-tag payroll
    # ``payroll.pay_rules.list`` — same opaque cursor issue as
    # ``assets.list``. Cursor is a signed payload; random schema-valid
    # strings correctly 422 as ``invalid_cursor``.
    --exclude-operation-id 'payroll.pay_rules.list'
    # ``bookings.list`` — optional query params are now documented as
    # omission-only rather than nullable (so ``pending_amend=null`` is
    # no longer schema-valid), but the route also enforces
    # ``from <= to``. OpenAPI cannot express that cross-query datetime
    # invariant, so generated inverted windows correctly return 422
    # ``invalid_field`` and stay out of the broad sweep.
    --exclude-operation-id 'bookings.list'
    # ``payroll.pay_rules.create`` / ``payroll.pay_rules.update`` —
    # ``effective_to`` must be strictly greater than ``effective_from``.
    # JSON Schema cannot express that cross-field datetime invariant,
    # so schemathesis treats equal or inverted windows as positive
    # examples.
    --exclude-operation-id 'payroll.pay_rules.create'
    --exclude-operation-id 'payroll.pay_rules.update'
    # ``payroll.pay_periods.create`` / ``payroll.pay_periods.update`` —
    # ``ends_at`` must be strictly greater than ``starts_at``; this is
    # the same cross-field datetime invariant as the pay-rule window.
    --exclude-operation-id 'payroll.pay_periods.create'
    --exclude-operation-id 'payroll.pay_periods.update'
    # ``payroll.exports.csv`` — the required query set depends on the
    # path ``kind``: timesheets / expense-ledger require ``since`` and
    # ``until``, while payslips may use ``period_id``. FastAPI exposes
    # this as one route, and OpenAPI cannot attach kind-specific query
    # requirements to individual path enum values.
    --exclude-operation-id 'payroll.exports.csv'
    # ``payroll.payslips.payout_manifest`` — manifests are generated
    # just-in-time from retained payout snapshots and may return 410
    # once retention has purged the secret material. Random payslip IDs
    # cannot reach the success branch without a seeded issued payslip.
    --exclude-operation-id 'payroll.payslips.payout_manifest'
    # ----------------------------------------------------------------
    # Expenses tag (cd-63lcq). ``--include-tag expenses`` selects the
    # worker claim CRUD, manager approval / reimbursement, attachment,
    # pending totals, and receipt-scan preview surface. The router
    # declares the shared problem+json 4xx envelope, and claim/edit
    # currency schemas advertise the same ISO-4217 allow-list the domain
    # validates at runtime. The exclusions below are residual generation
    # constraints that need signed cursors or realistic multipart / OCR
    # fixtures before the full tag can run without curated carve-outs.
    # ----------------------------------------------------------------
    --include-tag expenses
    # ``list_expense_claims`` / ``list_pending_expense_claims`` — same
    # opaque cursor issue as ``assets.list``. Cursor is a signed payload;
    # random schema-valid strings correctly 422 as ``invalid_cursor``.
    --exclude-operation-id 'list_expense_claims'
    --exclude-operation-id 'list_pending_expense_claims'
    # ``create_expense_claim`` — ``purchased_at`` must not be in the
    # future relative to the server clock (plus a small skew allowance),
    # and ``work_engagement_id`` must be a real caller-owned engagement.
    # JSON Schema cannot express the moving datetime bound, and random
    # ids correctly miss the seeded engagement graph.
    --exclude-operation-id 'create_expense_claim'
    # ``scan_expense_receipt`` — the success branch requires a non-empty
    # receipt file part plus a configured OCR model / LLM seam. Random
    # multipart bodies frequently omit or stringify the UploadFile field
    # and correctly fail request validation before the preview branch.
    --exclude-operation-id 'scan_expense_receipt'
    # ----------------------------------------------------------------
    # Tasks tag (cd-mb0dh). ``--include-tag tasks`` selects the manager
    # task, template, schedule, scheduler-calendar, comments, evidence,
    # and NL-intake surface. The tasks and scheduler routers now
    # declare the shared problem+json response envelope, so validation
    # and missing-resource responses match the runtime content type.
    # The exclusions below are residual data-generation constraints:
    # opaque cursors, seeded task/template/schedule ids, multipart file
    # bodies, LLM capability assignment, and cross-field / per-route
    # invariants OpenAPI cannot express for arbitrary generated data.
    # ----------------------------------------------------------------
    --include-tag tasks
    # List cursors are signed opaque payloads; random schema-valid
    # strings correctly 422 as ``invalid_cursor``.
    --exclude-operation-id 'list_tasks'
    --exclude-operation-id 'list_schedules'
    --exclude-operation-id 'list_task_templates'
    --exclude-operation-id 'list_task_comments'
    --exclude-operation-id 'list_task_evidence'
    # Creation paths below require real workspace-local parent ids
    # (templates / roles / areas) or body invariants that are validated
    # by the domain layer but not fully representable in JSON Schema.
    --exclude-operation-id 'create_task'
    --exclude-operation-id 'create_schedule'
    --exclude-operation-id 'update_schedule'
    --exclude-operation-id 'create_task_template'
    --exclude-operation-id 'update_task_template'
    # Multipart evidence generation currently produces empty/malformed
    # file parts that FastAPI rejects before the task success branch.
    --exclude-operation-id 'upload_task_evidence'
    --exclude-operation-id 'attach_task_checklist_evidence'
    # NL task intake requires an assigned local LLM capability and a
    # seeded preview id. Those are covered by focused domain tests until
    # the contract seed can provision a fake capability graph.
    --exclude-operation-id 'draft_task_from_nl'
    --exclude-operation-id 'commit_task_from_nl'
    # FastAPI accepts repeated scalar query params with normal scalar
    # coercion, while Schemathesis' negative-data phase models them as
    # arrays and expects rejection.
    --exclude-operation-id 'scheduler.calendar.get'
    # ----------------------------------------------------------------
    # Messaging tag (cd-pix4c). ``--include-tag messaging`` selects
    # notifications, web-push token management, and chat channels /
    # messages. The notifications + push-token read/delete/unsubscribe
    # surface now declares the shared problem+json response envelope,
    # and ``messaging.notifications.list`` rejects duplicate scalar
    # cursor query params explicitly so negative-data generation matches
    # runtime behaviour.
    #
    # The exclusions below are residual runtime invariants that require
    # seeded chat ids, signed cursor hints, or JSON Schema constraints
    # that OpenAPI cannot express for arbitrary generated data.
    # ----------------------------------------------------------------
    --include-tag messaging
    # ``messaging.chat_channels.list`` / ``messaging.chat_messages.list``
    # use opaque signed cursors. Empty cursors are first-page aliases;
    # non-empty cursor values must be server-issued ``next_cursor``
    # tokens. Random schema-valid strings correctly 422 as
    # ``invalid_cursor`` because OpenAPI cannot express the HMAC
    # runtime invariant.
    --exclude-operation-id 'messaging.chat_channels.list'
    --exclude-operation-id 'messaging.chat_messages.list'
    # ``messaging.chat_channels.update`` requires a seeded live
    # ``channel_id`` path parameter. Random schema-valid ids correctly
    # 404 before the archive/rename handler can exercise the success
    # branch; focused route tests cover missing-resource behavior.
    --exclude-operation-id 'messaging.chat_channels.update'
    # ``messaging.chat_messages.send`` also requires a seeded live
    # ``channel_id`` path parameter. Attachment sends additionally need
    # pre-seeded blob hashes + storage, so contract coverage waits on a
    # messaging path-resource seed instead of accepting random ids.
    --exclude-operation-id 'messaging.chat_messages.send'
    # Native FCM/APNS registration is a reserved v1 surface and always
    # returns 501 ``push_unavailable`` after request validation.
    --exclude-operation-id 'messaging.push_tokens.register_native_unavailable'
)

# Checks excluded for the asset gate — kept here (rather than at
# every invocation) so the audit trail is self-contained.
#
# * ``unsupported_method`` — the schemathesis coverage phase probes
#   each workspace-scoped path with HTTP methods not declared in the
#   OpenAPI spec, expecting 405 Method Not Allowed. In the dev
#   profile this script runs under (``CREWDAY_PROFILE=dev``) the
#   :func:`app.api.proxy.register_vite_proxy` GET catch-all sits
#   below the API routers and intercepts unmatched GETs on workspace
#   API paths, returning 404 ``{"error": "not_found", "detail":
#   null}`` instead of letting FastAPI emit the natural 405. Pinning
#   the slug to the seeded workspace via
#   ``constrain_case_workspace_slug`` (tests/contract/hooks.py)
#   pushes coverage past the tenancy 404 branch, but the Vite
#   catch-all 404 still masks the 405 the production profile would
#   emit. Re-enable once the proxy is taught to defer to FastAPI's
#   automatic 405 + ``Allow`` response on API paths (cd-pa9p
#   follow-up: tighten ``app/api/proxy.py::vite_proxy`` to skip API
#   prefixes, or run the schemathesis sweep against the prod
#   profile). Bare-host paths still return 405 (no Vite catch-all on
#   bare host); ``auth.me.get`` covers that surface.
EXCLUDE_CHECKS_ARGS=(
    --exclude-checks unsupported_method
)

USER_ARGS=("$@")
USER_HAS_INCLUDE_FILTER=0
USER_INCLUDE_TAGS=()
for ((i = 0; i < ${#USER_ARGS[@]}; i++)); do
    if [[ "${USER_ARGS[$i]}" == --include-* ]]; then
        USER_HAS_INCLUDE_FILTER=1
    fi
    if [[ "${USER_ARGS[$i]}" == "--include-tag" && -n "${USER_ARGS[$((i + 1))]:-}" ]]; then
        USER_INCLUDE_TAGS+=("${USER_ARGS[$((i + 1))]}")
    elif [[ "${USER_ARGS[$i]}" == --include-tag=* ]]; then
        USER_INCLUDE_TAGS+=("${USER_ARGS[$i]#--include-tag=}")
    fi
done

RUN_INCLUDE_ARGS=()
for ((i = 0; i < ${#INCLUDE_ARGS[@]}; i++)); do
    if ((USER_HAS_INCLUDE_FILTER)) && [[ "${INCLUDE_ARGS[$i]}" == --include-* ]]; then
        if [[ -n "${INCLUDE_ARGS[$((i + 1))]:-}" && "${INCLUDE_ARGS[$((i + 1))]}" != --* ]]; then
            i=$((i + 1))
        fi
        continue
    fi
    if [[ "${INCLUDE_ARGS[$i]}" == "--include-tag" && -n "${INCLUDE_ARGS[$((i + 1))]:-}" ]]; then
        duplicate=0
        for tag in "${USER_INCLUDE_TAGS[@]}"; do
            if [[ "${tag}" == "${INCLUDE_ARGS[$((i + 1))]}" ]]; then
                duplicate=1
                break
            fi
        done
        if ((duplicate)); then
            i=$((i + 1))
            continue
        fi
    fi
    RUN_INCLUDE_ARGS+=("${INCLUDE_ARGS[$i]}")
done

SCHEMATHESIS_ARGS=(
    run
    --header "Authorization: Bearer ${TOKEN}"
    --header "X-CSRF: schemathesis"
    --checks all
    --mode all
    --max-examples "${MAX_EXAMPLES}"
    --workers "${WORKERS}"
    --exclude-path-regex '^/(events|admin/events)$'
    --generation-codec ascii
    --suppress-health-check filter_too_much,data_too_large,too_slow
    "${RUN_INCLUDE_ARGS[@]}"
    "${EXCLUDE_CHECKS_ARGS[@]}"
    --report junit
    --report-dir "${REPORT_DIR}"
    "${USER_ARGS[@]}"
    "http://${HOST}:${PORT}/api/openapi.json"
)

set +e
${SCHEMATHESIS_BIN} "${SCHEMATHESIS_ARGS[@]}"
RC=$?
set -e

if [[ ${RC} -ne 0 ]]; then
    echo "schemathesis: run failed (exit ${RC}); uvicorn log:" >&2
    cat "${LOG_PATH}" >&2 || true
fi

exit "${RC}"
