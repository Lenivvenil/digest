.PHONY: lint lint-fix typecheck test check install install-dev

lint:
	ruff check digest/ tests/

lint-fix:
	ruff check --fix digest/ tests/
	ruff format digest/ tests/

typecheck:
	mypy digest/

test:
	pytest tests/ -v

check: lint typecheck test

install:
	pip install --no-deps -r requirements.txt

install-dev:
	pip install --no-deps -r requirements-dev.txt
