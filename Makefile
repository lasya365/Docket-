VENV ?= ./.venv
PY   := $(VENV)/bin/python
PORT ?= 8000

# Docket reads its secrets from the process environment only (settings.py).
# Every target that starts Docket sources ./.env first, if it exists.
LOAD_ENV := set -a; [ -f ./.env ] && . ./.env; set +a;

.PHONY: help install env test fixtures serve replay demo reset mcp doctor doctor-offline check clean

help:               ## list the targets
	@grep -E '^[a-z]+:.*##' Makefile | sed 's/:.*##/ -/'

install:            ## create the venv and install Docket (needs Python 3.11+)
	@python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Docket needs Python 3.11 or newer; found " + sys.version.split()[0])'
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e '.[dev]'
	@echo "installed. next: make env && make test && make serve"

env:                ## create .env from .env.example, with a random ingest token
	@if [ -f .env ]; then echo ".env already exists, leaving it alone"; else \
	  token=$$($(PY) -c 'import secrets; print(secrets.token_urlsafe(24))'); \
	  sed "s|^DOCKET_INGEST_TOKEN=change-me|DOCKET_INGEST_TOKEN=$$token|" .env.example > .env; \
	  echo "wrote .env with a random DOCKET_INGEST_TOKEN — put the same value in agent-setup/docket-env.sh"; fi

test:               ## the whole suite, offline
	$(PY) -m pytest backend/tests -q

fixtures:           ## rebuild tests/fixtures/data and demo/seed from the specs
	$(PY) backend/tests/fixtures/build_fixtures.py

serve:              ## run Docket on $(PORT), reading ./.env
	$(LOAD_ENV) $(PY) -m uvicorn "docket.server:get_app" --factory --port $(PORT)

replay:             ## run Docket in replay mode: POST /run/{pr} uses demo/seed, never GitHub
	$(LOAD_ENV) DOCKET_MODE=replay $(PY) -m uvicorn "docket.server:get_app" --factory --port $(PORT)

demo:               ## load the four seeded cases into a running Docket
	@$(LOAD_ENV) curl -s -XPOST localhost:$(PORT)/demo/seed \
	  $${DOCKET_API_TOKEN:+-H "Authorization: Bearer $$DOCKET_API_TOKEN"} | $(PY) -m json.tool

reset:              ## clear runs and tickets from a running Docket (keeps sessions and the ledger)
	@$(LOAD_ENV) curl -s -XPOST localhost:$(PORT)/demo/reset \
	  $${DOCKET_API_TOKEN:+-H "Authorization: Bearer $$DOCKET_API_TOKEN"}; echo

mcp:                ## run the read-only MCP server on stdio, reading ./.env
	@$(LOAD_ENV) $(PY) -m docket.mcp_server

doctor:             ## check every configured integration and say what is wrong (reads .env)
	@$(LOAD_ENV) $(PY) -m docket.doctor

doctor-offline:     ## the same, without contacting anything
	@$(LOAD_ENV) $(PY) -m docket.doctor --offline

check:              ## is a running Docket healthy?
	@curl -s localhost:$(PORT)/healthz | $(PY) -m json.tool

clean:              ## remove caches, dry-run output and the local database
	rm -rf .pytest_cache out docket.sqlite
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
