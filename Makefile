.PHONY: setup test lint dataset

setup:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

# Regenerates data/inbox from scripts/make_dataset.py (output is committed; byte-for-byte deterministic).
dataset:
	uv run python scripts/make_dataset.py
