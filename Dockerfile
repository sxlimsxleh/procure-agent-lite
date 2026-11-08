# One image for all services (mock ERP, approval UI, CLI demo); docker-compose.yml picks the command.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# Pinned uv from PyPI (same version as development) so the image installs exactly uv.lock.
RUN pip install --no-cache-dir uv==0.11.32

WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# Our code. Installed editable, so procure.pipeline finds data/, config/ etc. relative to /app.
COPY src ./src
RUN uv sync --locked --no-dev

COPY mock_erp ./mock_erp
COPY ui ./ui
COPY config ./config
COPY data ./data
COPY evals/recordings ./evals/recordings

# Run as a normal user. /app/var holds the SQLite audit log (a volume in docker-compose.yml).
RUN useradd --create-home --uid 1000 app && mkdir -p /app/var && chown app /app/var
USER app

EXPOSE 8001 8501
