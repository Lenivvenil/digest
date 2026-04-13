.PHONY: lint lint-fix typecheck test check install install-dev

lint:
	ruff check src/ tests/

lint-fix:
	ruff check --fix src/ tests/
	ruff format src/ tests/

typecheck:
	mypy src/

test:
	pytest tests/ -v

check: lint typecheck test

install:
	pip install --no-deps -r requirements.txt

install-dev:
	pip install --no-deps -r requirements-dev.txt
