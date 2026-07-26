# Multi-stage: poetry and build tooling stay in the builder; the runtime image
# carries only the virtualenv and application files.
FROM python:3.11-slim AS builder

WORKDIR /app
ENV POETRY_VIRTUALENVS_IN_PROJECT=1

RUN pip install --no-cache-dir poetry==2.3.2

COPY pyproject.toml poetry.lock README.md LICENSE ./
COPY src ./src
# Install with the persistence extra so the same image can run either mode;
# the code lazy-imports that stack, so leaving it unused costs image size only,
# not runtime memory.
RUN poetry install --only main --extras persistence

FROM python:3.11-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv ./.venv
COPY src ./src
COPY registry ./registry
COPY policies ./policies
COPY migrations ./migrations
COPY alembic.ini ./
# The compose stack runs examples/enterprise_scenario/serve.py as the governed
# downstream system (real MCP server + authorization server). Needs no extra
# dependencies — only stdlib, pyjwt, and cryptography, all already installed.
COPY examples ./examples
ENV PYTHONPATH=/app

EXPOSE 8000
CMD ["uvicorn", "openagent_control.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
