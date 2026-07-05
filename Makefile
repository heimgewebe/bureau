.PHONY: install validate test lint bridge-import-policy

install:
	python -m pip install -e '.[dev]'

validate: lint test bridge-import-policy
	python -m bureau.cli --root . check

bridge-import-policy:
	python -c 'import json; p=json.load(open("docs/cabinet-bridge-import-review-contract-v0.policy.json")); r=p["requiredReceipt"]; assert r["importAllowed"] is False; assert r["importReviewRequired"] is True; assert all(v is False for v in p["nonEffects"].values())'

test:
	pytest

lint:
	ruff check src tests
