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
site/scripts/quality.sh
```

Run the site quality command from the repository root. It installs the
isolated site dependencies, checks design-token/icon drift, runs any
`site/web` lint/test scripts that exist, typechecks and builds `site/web`,
then formats, lints, typechecks, and tests `site/api`.

```bash
SITE_QUALITY_INSTALL=0 site/scripts/quality.sh
```

Use `SITE_QUALITY_INSTALL=0` after dependencies are already installed.

## Site release lane

`.github/workflows/site-ci.yml` is scoped to public-site changes under
`site/**`, site specs, the site workflow itself, and the app token source
that feeds the design drift check. The lane runs `site/scripts/quality.sh`
and builds the two site images independently from the app images. On
`main` pushes, it publishes both images to GHCR with commit-SHA tags:

```text
ghcr.io/<owner>/crewday-site-web:<commit-sha>
ghcr.io/<owner>/crewday-site-api:<commit-sha>
```

## Local compose smoke

```bash
docker compose -f site/docker-compose.yml up --build
curl -i http://127.0.0.1:18080/
curl -i http://127.0.0.1:18080/api/healthz
```

The compose stack binds Caddy to `127.0.0.1:18080` by default. Override the
host port with `SITE_HTTP_PORT=<port>` when needed. `site-api` is only
exposed on the internal Docker network.
