.PHONY: install validate test lint bridge-import-policy

install:
	python -m pip install -e '.[dev]'

validate: lint test bridge-import-policy
	python -m bureau.cli --root . check

bridge-import-policy:
	bureau-cabinet-bridge-import-policy --json >/dev/null

test:
	pytest

lint:
	ruff check src tests
