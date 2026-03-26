# syntax=docker/dockerfile:1

# ---- builder: resolve and install dependencies into /app/.venv ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv
WORKDIR /app
# Dependencies first, so this layer is cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --extra postgres --no-install-project
COPY README.md ./
COPY app ./app
RUN uv sync --no-dev --extra postgres

# ---- runtime: no compilers, non-root user ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=7860
WORKDIR /app
RUN useradd --create-home --uid 1000 appuser
COPY --from=builder --chown=appuser:appuser /app /app
RUN mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser

# SQLite file and Chroma persistence live here; mount a volume to keep them.
VOLUME ["/app/data"]
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860', timeout=3)"
CMD ["python", "-m", "app.main"]
