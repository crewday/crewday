# End-to-end Playwright suite

Loaded on demand from `AGENTS.md`'s "Application-specific notes"
section. Run this when you need browser-level coverage; unit tests
under `tests/unit/` cover everything else.

## What it tests

`pytest-playwright` against the dev compose stack with e2e environment
overrides. The overrides align WebAuthn `rp_id` / `public_url` with
the loopback origin so Chromium's virtual authenticator works end to
end. See `docs/specs/17-testing-quality.md` for the spec.

The full pilot suite is fast (~10 s on Chromium).

## Bring it up

```bash
CREWDAY_PUBLIC_URL=http://localhost:8100 \
CREWDAY_WEBAUTHN_RP_ID=localhost \
CREWDAY_LLM_PROVIDER=fake \
CREWDAY_ICAL_ALLOW_PRIVATE_ADDRESSES=1 \
CREWDAY_RATE_LIMIT_ANONYMOUS_PER_MINUTE=10000 \
CREWDAY_RATE_LIMIT_TOKEN_PER_MINUTE=10000 \
CREWDAY_RATE_LIMIT_PERSONAL_ME_PER_MINUTE=10000 \
docker compose -f docker-compose.dev.yml up -d --build
uv run playwright install chromium webkit   # one-time
uv run pytest tests/e2e -v -n0 \
    --tracing=retain-on-failure \
    --video=retain-on-failure \
    --screenshot=only-on-failure
```

The suite skips with a focused message if `/healthz` is unreachable —
bring compose up first.

Use the compose command above for passkey tests. `./scripts/dev-stack-up.sh`
starts the normal dev app stack only; if that stack advertises an RP ID
that does not match `localhost`, the e2e preflight skips with the same
environment-variable command when `CREWDAY_E2E_BASE_URL` is
`http://localhost:8100`.

## Why `localhost`, not `127.0.0.1`

Default `CREWDAY_E2E_BASE_URL` is `http://localhost:8100`. Chromium
rejects IP literals as WebAuthn RP IDs; `localhost` resolves to the
same 127.0.0.1-bound Vite port. Override only when pointing at a
non-default host.

## WebKit auto-skip

Hosts missing libicu74 cannot launch WebKit. The suite surfaces this
as a focused `pytest.skip` rather than an opaque crash. The Chromium
path covers everything CI gates on.

## Artefacts

The `--tracing` / `--video` / `--screenshot` flags above are what
gate trace.zip, video, and screenshot drops. Without them
`pytest-playwright`'s CLI defaults are all `off` and you get
nothing. Keep the flags on for any non-trivial run.

- `tests/e2e/_artifacts/` — traces, videos, failure screenshots.
- `tests/e2e/_diff/` — visual-regression diffs.

Both directories are gitignored.

The visual baseline at `tests/e2e/_baselines/` **is** committed and
reviewed manually — never regenerate it without eyeballing the
result.

## Auth helper

For login flows the suite uses
`tests/e2e/_helpers/auth.py::login_with_dev_session`. See
`docs/dev/playwright-auth.md` for why the dev-login alias cookie
exists and how it differs from the curl-shaped session cookie.
