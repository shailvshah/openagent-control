.PHONY: install fmt fmt-check lint typecheck quality test check up down up-persistent db-upgrade doctor test-packaging

install:
	poetry install --all-extras

fmt:
	poetry run black src tests
	poetry run ruff check --fix src tests

fmt-check:
	poetry run black --check src tests

lint:
	poetry run ruff check src tests

typecheck:
	poetry run mypy

quality: fmt-check lint typecheck

test:
	poetry run pytest

check: quality test

up:
	docker compose up --build

up-persistent:
	OAC_DATABASE_URL=postgresql+asyncpg://oac:oac@postgres:5432/oac \
	OAC_REDIS_URL=redis://redis:6379/0 \
	docker compose --profile persistence up --build

up-demo:
	docker compose --profile demo up --build

down:
	docker compose down

# Run against whatever OAC_DATABASE_URL points at (defaults to localhost:5432,
# i.e. `docker compose --profile persistence up postgres` exposed on the host).
# Goes through the CLI so this exercises the same path a pip user takes.
db-upgrade:
	OAC_DATABASE_URL=$${OAC_DATABASE_URL:-postgresql+asyncpg://oac:oac@localhost:5432/oac} \
	poetry run openagent-control migrate

doctor:
	poetry run openagent-control doctor

# Verifies the built wheel actually works from a clean venv (slow).
test-packaging:
	OAC_TEST_PACKAGING=1 poetry run pytest tests/integration/test_packaging.py -v
