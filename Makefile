.PHONY: setup test lint benchmark benchmark-suite services dashboard go-test rust-test clean

setup:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -e '.[dev,api]'

test:
	PYTHONPATH=python .venv/bin/pytest --cov=conflictgraph --cov-report=term-missing

lint:
	.venv/bin/ruff check python tests benchmark
	.venv/bin/mypy python/conflictgraph
	cd dashboard && npm run typecheck

benchmark:
	PYTHONPATH=python .venv/bin/python -m conflictgraph.cli benchmark --profile quick

benchmark-suite:
	PYTHONPATH=python .venv/bin/pytest -c benchmark/pyproject.toml benchmark/suite

services:
	docker compose up --build postgres api ml-api dashboard prometheus grafana

dashboard:
	cd dashboard && npm install && npm run dev

go-test:
	cd controlplane && go test -race ./...

rust-test:
	cd tracer && cargo test --all-targets

clean:
	find python tests benchmark -type d -name __pycache__ -prune -exec rm -r {} +
