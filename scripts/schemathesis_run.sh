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
# ``--exclude-path-regex`` keeps the SSE transport endpoint out of
# the sweep — its ``text/event-stream`` body is an open-ended pipe
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
    # ``auth.logout`` is intentionally NOT included — invoking it
    # invalidates the seeded dev session, which then breaks every
    # subsequent session-cookie-authed op in the same run (esp.
    # ``auth.me.get``). Re-enable once the runner can re-mint the
    # session between operations, or once the gate stops sharing one
    # seed across the suite. Tracked under cd-rfda7.
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

set +e
${SCHEMATHESIS_BIN} run \
    "http://${HOST}:${PORT}/api/openapi.json" \
    --header "Authorization: Bearer ${TOKEN}" \
    --header "Cookie: __Host-crewday_session=${SESSION}; crewday_csrf=schemathesis" \
    --header "X-CSRF: schemathesis" \
    --checks all \
    --mode all \
    --max-examples "${MAX_EXAMPLES}" \
    --workers "${WORKERS}" \
    --exclude-path-regex '^/events$' \
    --generation-codec ascii \
    --suppress-health-check filter_too_much,data_too_large,too_slow \
    "${INCLUDE_ARGS[@]}" \
    "${EXCLUDE_CHECKS_ARGS[@]}" \
    --report junit \
    --report-dir "${REPORT_DIR}" \
    "$@"
RC=$?
set -e

if [[ ${RC} -ne 0 ]]; then
    echo "schemathesis: run failed (exit ${RC}); uvicorn log:" >&2
    cat "${LOG_PATH}" >&2 || true
fi

exit "${RC}"
