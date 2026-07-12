.PHONY: install validate test lint systemkatalog-boundary

install:
	python -m pip install -e '.[dev]'

validate: lint test systemkatalog-boundary
	PYTHONPATH=src python -m bureau.cli --root . check

systemkatalog-boundary:
	PYTHONPATH=src python -m bureau.systemkatalog_boundary --root . --json >/dev/null

test:
	PYTHONPATH=src pytest

lint:
	ruff check src tests
