# syntax=docker/dockerfile:1.7

FROM node:24-alpine@sha256:d1b3b4da11eefd5941e7f0b9cf17783fc99d9c6fc34884a665f40a06dbdfc94f AS web-build

WORKDIR /web
COPY app/web/package.json app/web/package-lock.json ./
RUN npm ci --no-audit --no-fund --legacy-peer-deps --min-release-age=7

COPY app/web/ ./
RUN npm run build

FROM python:3.14-slim@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CREWDAY_DATABASE_URL=sqlite:////data/crewday.db \
    CREWDAY_DATA_DIR=/data \
    CREWDAY_BIND_HOST=0.0.0.0 \
    CREWDAY_BIND_PORT=8000 \
    CREWDAY_ALLOW_PUBLIC_BIND=1 \
    FASTEMBED_CACHE_PATH=/app/.cache/fastembed

RUN groupadd --system --gid 10001 crewday \
    && useradd --system --uid 10001 --gid crewday --home-dir /app --shell /usr/sbin/nologin crewday

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE.md alembic.ini ./
COPY app ./app
COPY cli ./cli
COPY migrations ./migrations
COPY deploy/single/entrypoint.sh /usr/local/bin/crewday-server
COPY --from=web-build /web/dist ./app/web/dist

# ``ffmpeg`` supports server-side media normalization; Pango/fontconfig/
# DejaVu are the runtime pieces WeasyPrint needs to render payslip PDFs.
# The asset router imports ``weasyprint`` at module load (app/api/assets/
# assets.py) and is registered unconditionally, so a missing native lib
# crashes the server at boot — the ``import weasyprint`` smoke below fails
# the build instead. ``build-essential`` is only for source wheels without
# CPython 3.14 binaries, and is removed before the layer is committed.
#
# Dependencies install from ``uv.lock`` for a reproducible, age-gated
# build: ``uv export`` materialises the locked prod set (pinned ``==``
# versions), then ``pip install --no-deps .`` layers on the project itself
# (the ``crewday`` console script + package remap) without re-resolving.
# ``uv`` is a self-contained binary, removed after the export.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        fontconfig \
        fonts-dejavu-core \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
    && python -m pip install --upgrade pip uv \
    && uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
    && python -m pip install -r /tmp/requirements.txt \
    && python -m pip install --no-deps . \
    && python -m pip uninstall -y uv \
    && python -c 'import weasyprint' \
    && python -c 'import app.main' \
    && python -m app.adapters.llm.fastembed \
    && rm -f /tmp/requirements.txt \
    && chmod 0755 /usr/local/bin/crewday-server \
    && mkdir -p /data \
    && chown -R crewday:crewday /app /data \
    && find /usr/local/bin /usr/local/lib -perm /6000 -type f -exec chmod a-s {} + \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /root/.cache /var/lib/apt/lists/*

USER crewday:crewday

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

ENTRYPOINT ["crewday-server"]
CMD ["serve"]
