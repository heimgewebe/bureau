.PHONY: install validate test lint

install:
	python -m pip install -e '.[dev]'

validate: lint test
	python -m bureau.cli --root . check

test:
	pytest

lint:
	ruff check src tests
