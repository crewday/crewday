# Playwright auth on the dev loopback

How to attach a dev session cookie to a Playwright browser that is
talking to `http://127.0.0.1:8100`. Loaded on demand from
`AGENTS.md`'s "Dev verification helpers" section.

## Why this is its own page

The session cookie the API issues is `__Host-crewday_session`. The
`__Host-` prefix mandates `Secure`, which Chromium's cookie API
either rejects outright or silently drops on plain-HTTP requests.
So you cannot just paste the curl cookie into
`page.context().addCookies()`.

`scripts.dev_login` knows about this and emits a dev-only **alias**
cookie (`crewday_session`, no prefix, `secure: false`) when asked
for `--output playwright`. The API accepts both names; the alias
exists only because of this loopback constraint.

## One-step recipe

1. Mint the cookie inside the dev compose stack:

   ```bash
   docker compose -f mocks/docker-compose.yml exec app-api \
     python -m scripts.dev_login --email me@dev.local \
       --workspace smoke --output playwright
   ```

2. Paste the JSON object straight into Playwright:

   ```js
   const cookie = /* paste the JSON object from --output playwright */;
   await page.context().addCookies([cookie]);
   await page.goto("http://127.0.0.1:8100/w/smoke/...");
   ```

The shape `dev_login` emits looks like this (do not hand-roll it
unless you know what you're doing):

```js
await page.context().addCookies([{
  name: "crewday_session",
  value: "<same opaque session value>",
  url: "http://127.0.0.1:8100",
  httpOnly: true,
  secure: false,
  sameSite: "Lax",
}]);
```

## For the pytest e2e suite

Use `tests/e2e/_helpers/auth.py::login_with_dev_session` — it
performs the exact alias injection above and is the only path
exercised in CI. Don't reinvent it inside individual tests.

## For curl / scripted multi-step

The default `dev_login` invocation (no `--output`) prints
`__Host-crewday_session=<value>` on stdout — feed it straight to
curl:

```bash
docker compose -f mocks/docker-compose.yml exec app-api \
  python -m scripts.dev_login --email me@dev.local --workspace smoke
# -> __Host-crewday_session=eyJ...

curl -b "__Host-crewday_session=eyJ..." \
  http://127.0.0.1:8100/w/smoke/api/v1/...
```

Or use the host-side wrapper (requires `uv sync` or
`pip install -e .` first so SQLAlchemy/click are importable). Its
default `--output curl` prints a `-b '__Host-…'` flag to stdout,
ready for shell capture:

```bash
cookie="$(CREWDAY_DEV_AUTH=1 ./scripts/dev-login.sh me@dev.local dev)"
curl -sS $cookie http://127.0.0.1:8100/w/dev/api/v1/...
```

For most read-only smoke checks `./scripts/agent-curl.sh` is
simpler — it caches the cookie per workspace+email and refreshes
on stale sessions.
