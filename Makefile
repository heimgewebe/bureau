.PHONY: install validate validate-admission test lint systemkatalog-boundary

install:
	python -m pip install -e '.[dev]'

validate: lint test systemkatalog-boundary
	PYTHONPATH=src python -m bureau.cli --root . check

# Fast, substantive PR admission; merge-group and main still run validate.
validate-admission: lint systemkatalog-boundary
	PYTHONPATH=src python -m bureau.cli --root . check
	PYTHONPATH=src pytest tests/test_bureau.py tests/test_github_workflows.py tests/test_validation_scope.py

systemkatalog-boundary:
	PYTHONPATH=src python -m bureau.systemkatalog_boundary --root . --json >/dev/null

test:
	PYTHONPATH=src pytest

lint:
	ruff check src tests
