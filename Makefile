.PHONY: install validate validate-admission validate-main-reuse test lint systemkatalog-boundary

install:
	python -m pip install -e '.[dev]'

validate: lint test systemkatalog-boundary
	PYTHONPATH=src python -m bureau.cli --root . check

# Fast, substantive PR admission; merge-group still runs validate.
validate-admission: lint systemkatalog-boundary
	PYTHONPATH=src python -m bureau.cli --root . check
	PYTHONPATH=src pytest tests/test_bureau.py tests/test_github_workflows.py tests/test_validation_scope.py

# Reuse is selected only after a proven exact full merge-group run.
# Keep actual installation/smoke checks and the accepted-main history invariant.
validate-main-reuse: validate-admission
	PYTHONPATH=src pytest tests/test_runtime_refresh.py::test_registry_source_precondition_authorities_have_complete_no_run_acceptance_contracts

systemkatalog-boundary:
	PYTHONPATH=src python -m bureau.systemkatalog_boundary --root . --json >/dev/null

test:
	PYTHONPATH=src pytest

lint:
	ruff check src tests
