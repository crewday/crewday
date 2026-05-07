# crew.day public site

This deployable owns the public `crew.day` surface: static marketing
pages, the future suggestion box, and a small API for site-local
features. It is intentionally separate from the app and demo stacks.

## Layout

```text
site/
├── README.md
├── docker-compose.yml
├── Caddyfile
├── web/     # Astro static site
└── api/     # FastAPI site backend
```

`site/web` has its own `package.json` and lockfile. `site/api` has its
own `pyproject.toml`. Neither package imports from or shares build
configuration with `app/` or `mocks/`.

## Local checks

```bash
npm run build
npm run typecheck
```

Run those from `site/web`.

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy --strict site_api
uv run pytest -q
```

Run those from `site/api`.

## Local compose smoke

```bash
docker compose -f site/docker-compose.yml up --build
curl -i http://127.0.0.1:18080/
curl -i http://127.0.0.1:18080/api/healthz
```

The compose stack binds Caddy to `127.0.0.1:18080` by default. Override the
host port with `SITE_HTTP_PORT=<port>` when needed. `site-api` is only
exposed on the internal Docker network.
