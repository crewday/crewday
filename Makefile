.PHONY: lint fmt type test coverage

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
