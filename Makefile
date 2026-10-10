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
	python -m pip install -r requirements.txt .

install-dev:
	python -m pip install -r requirements-dev.txt .
