.PHONY: install run project wallet lint format test build

install:
	uv sync

run: wallet

project:
	uv run project

wallet:
	uv run wallet

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

test:
	uv run python -m unittest discover -s tests -v

build:
	uv build
