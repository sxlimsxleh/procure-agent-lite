.PHONY: setup test lint dataset evals evals-recorded erp demo ui

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

# Mock SAP purchase order API on :8001. FLAKY_RATE (default 0.3) and FLAKY_SEED control faults.
erp:
	uv run uvicorn mock_erp.app:app --port 8001

# End to end on PR-0001: starts the mock ERP (FLAKY_RATE=0.3), proposes from recorded
# extractions, asks for approval on the command line, writes the PO, prints the audit trail.
# Live extraction instead: make demo SOURCE=live (needs ANTHROPIC_API_KEY + ANTHROPIC_MODEL).
SOURCE ?= recorded
demo:
	@FLAKY_RATE=$${FLAKY_RATE:-0.3} uv run uvicorn mock_erp.app:app --port 8001 --log-level warning & \
	ERP_PID=$$!; trap 'kill $$ERP_PID' EXIT; \
	uv run python -m procure.pipeline demo PR-0001 --source $(SOURCE)

# Streamlit approval queue on :8501, with the mock ERP running alongside.
ui:
	@FLAKY_RATE=$${FLAKY_RATE:-0.3} uv run uvicorn mock_erp.app:app --port 8001 --log-level warning & \
	ERP_PID=$$!; trap 'kill $$ERP_PID' EXIT; \
	uv run streamlit run ui/approve.py --server.headless true
