.PHONY: install validate test lint bridge-import-policy

install:
	python -m pip install -e '.[dev]'

validate: lint test bridge-import-policy
	PYTHONPATH=src python -m bureau.cli --root . check

bridge-import-policy:
	PYTHONPATH=src python -m bureau.cabinet_bridge_import_policy --json >/dev/null

test:
	PYTHONPATH=src pytest

lint:
	ruff check src tests
