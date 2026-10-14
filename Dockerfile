# syntax=docker/dockerfile:1

# ---- base: dependencies first (cached), then the app -------------------------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY src/ src/
COPY input/ input/
COPY reviews/ reviews/

# ---- test: the image only builds if the test suite passes ----------------------
FROM base AS test
COPY tests/ tests/
RUN python -m unittest discover -s tests && touch /tmp/tests-passed

# ---- runtime: non-root; results go to /app/output (mount it) -------------------
FROM base AS runtime
LABEL org.opencontainers.image.title="fs-statement-agent" \
      org.opencontainers.image.description="Balance Sheet + P&L generator with a self-verifying review loop"
COPY --from=test /tmp/tests-passed /tmp/tests-passed
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/output && chown app /app/output
USER app
ENTRYPOINT ["python", "src/pipeline.py"]
