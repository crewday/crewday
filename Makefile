.PHONY: lint fmt type test coverage schemathesis i18n-extract i18n-check openapi openapi-check codegen codegen-check

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy app

test:
	uv run pytest

coverage:
	uv run pytest --cov=app --cov-report=term-missing

i18n-extract:
	uv run python scripts/i18n_extract.py

i18n-check:
	uv run python scripts/i18n_extract.py
	git diff --exit-code -- app/i18n/locales app/web/src/i18n/bundles mocks/web/src/i18n/bundles

# Regenerate the committed ``docs/api/openapi.json`` from the live
# FastAPI app. ``openapi-check`` is the CI gate: fails when the
# committed file has drifted. See ``scripts/regen_openapi.py``.
openapi:
	uv run python -m scripts.regen_openapi

openapi-check:
	uv run python -m scripts.regen_openapi --check

# Regenerate ``cli/crewday/_surface.json`` + ``_surface_admin.json``
# from ``docs/api/openapi.json``. The codegen is a transform-only
# step (cd-uky5) — run ``make openapi`` first if the FastAPI surface
# has changed. ``codegen-check`` is the CI parity gate: fails when
# the committed surface files have drifted from a fresh transform.
codegen: openapi
	uv run python -m crewday._codegen

codegen-check:
	uv run python -m crewday._codegen --check

# API contract sweep (cd-3j25).
#
# Boots the FastAPI app under ``uvicorn``, mints a Bearer token via
# ``scripts/dev_login.py``, and runs ``schemathesis run`` with the
# custom checks under ``tests/contract/hooks.py`` registered through
# ``SCHEMATHESIS_HOOKS``. Spec: ``docs/specs/17-testing-quality.md``
# §"API contract" + §"Quality gates".
schemathesis:
	bash scripts/schemathesis_run.sh
