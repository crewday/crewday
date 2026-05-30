# crew.day legacy mock backend

`mocks/app/` is retained as legacy preview data and API context while the
remaining mock-retirement tasks are closed. The parallel React mock SPA has
been removed; production UI and visual-regression coverage now live under
`app/web/`, especially `/styleguide` and `/styleguide/*`.

The primary development stack is the repo-root `docker-compose.dev.yml`.
It starts the real FastAPI app, the `app/web` Vite dev server, and Mailpit.
It no longer starts a mock web service or routes a `/mocks` frontend.

## Files

- `app/main.py` — legacy mock FastAPI app and in-memory routes.
- `app/mock_data.py` — fake household data used by the mock backend.
- `Dockerfile.mocks-api` — API-only image for ad hoc legacy mock backend
  smoke checks outside the primary dev stack.
- `Dockerfile.app-api` — dev-stack image for the real repo-root `app/`
  package.
- `docker-compose.e2e.yml` — e2e-only overrides for the real app stack.

See `docs/specs/14-web-frontend.md` for the app-owned frontend contract and
mock-retirement notes.
