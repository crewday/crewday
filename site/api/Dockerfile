FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /srv/site-api
COPY pyproject.toml README.md ./
COPY site_api ./site_api
RUN python -m pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 --user-group siteapi

USER 10001:10001
EXPOSE 8001
CMD ["sh", "-c", "uvicorn site_api.main:app --host ${SITE_BIND_HOST:-0.0.0.0} --port ${SITE_BIND_PORT:-8001}"]
