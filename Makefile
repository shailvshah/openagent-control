.PHONY: install fmt fmt-check lint typecheck quality test check up down

install:
	poetry install

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

down:
	docker compose down
