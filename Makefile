.PHONY: lint lint-fix typecheck test check

lint:
	ruff check src/ tests/

lint-fix:
	ruff check --fix src/ tests/

typecheck:
	mypy src/

test:
	pytest tests/ -v

check: lint typecheck test
