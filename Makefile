.PHONY: setup test lint dataset evals evals-recorded

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

# Live evals: calls Claude (needs ANTHROPIC_API_KEY + ANTHROPIC_MODEL in .env), re-records responses.
# Save a named report with e.g. `make evals LABEL=baseline`.
evals:
	uv run python -m evals.run_evals --mode live $(if $(LABEL),--label $(LABEL))

# Replays evals/recordings/ without an API key. Fails if any wrong value is accepted (CI gate).
evals-recorded:
	uv run python -m evals.run_evals --mode recorded
