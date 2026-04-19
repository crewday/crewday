.PHONY: lint fmt type test

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy app

test:
	uv run pytest
