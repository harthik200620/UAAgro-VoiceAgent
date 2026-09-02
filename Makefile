# UA Agro voice platform.
#
# On Windows these targets run from a WSL shell: `make` does not exist in
# PowerShell, and Docker Desktop exposes the daemon inside WSL once
# Settings -> Resources -> WSL Integration is enabled for the distro.

COMPOSE := docker compose -f infra/docker/docker-compose.yml
UV      := uv run
PKGS    := packages apps tests

.DEFAULT_GOAL := help
.PHONY: help dev down logs test test-unit lint types fmt check eval load deploy \
        db-migrate db-seed db-partitions db-check db-reset sim ci \
        models kb-ingest kb-status kb-retrieval \
        latency observability admin-test admin-build

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack

dev:  ## bring up the whole local stack (§20)
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example -- fill the FILL_ME values")
	$(COMPOSE) up --build -d
	@echo
	@echo "  admin panel   http://localhost:3000"
	@echo "  api           http://localhost:8000/docs"
	@echo "  voice worker  ws://localhost:8080/ws/voice"
	@echo "  llm gateway   http://localhost:4000"
	@echo "  prometheus    http://localhost:9090"
	@echo "  grafana       http://localhost:3001"
	@echo "  minio console http://localhost:9001"
	@echo
	@echo "  run 'make sim' to place a simulated call."
	@echo "  the agent will not answer until an inbound flow is published"
	@echo "  in the panel -- it refuses to run an unapproved prompt (§15.1)."

dev-local:  ## bring up the stack WITHOUT Docker (embedded Postgres)
	uv run python scripts/dev_local.py
	@echo
	@echo "  now, in three terminals:"
	@echo "    1)  set -a; . ./.localdev/env; set +a; uv run uvicorn api.main:app --port 8000"
	@echo "    2)  cd apps/admin && npx next dev -p 3000"
	@echo "    3)  open http://localhost:3000/hi/login"
	@echo
	@echo "  sign in as admin@uaagro.in / DevOnly!Passw0rd"
	@echo "  two-factor is mandatory (§17); 'make totp' prints the code."

totp:  ## print the current two-factor code for the local admin account
	@uv run python scripts/dev_totp.py

down:  ## stop the stack, keep volumes
	$(COMPOSE) down

logs:  ## follow logs for every service
	$(COMPOSE) logs -f

# ---------------------------------------------------------------- quality

lint:  ## ruff check + format check
	$(UV) ruff check $(PKGS)
	$(UV) ruff format --check $(PKGS)

fmt:  ## apply formatting and safe fixes
	$(UV) ruff check --fix $(PKGS)
	$(UV) ruff format $(PKGS)

types:  ## mypy --strict across the workspace
	$(UV) mypy packages/domain/src/uaagro_domain packages/db/src/uaagro_db \
	           apps/api/src/api apps/voice-worker/src/voice_worker apps/worker/src/worker

secrets:  ## refuse any key, token or password in the tree (§17); also runs on every commit
	$(UV) python scripts/secret_scan.py apps packages tests scripts config infra docs .env.example

hooks:  ## install the pre-commit hooks (secret scan, ruff) into this clone
	$(UV) pre-commit install

test:  ## run the test suite (starts its own embedded Postgres; no Docker needed)
	$(UV) pytest

test-unit:  ## unit tests only, skipping anything that starts a database
	$(UV) pytest -m "not integration"

check: lint types secrets test  ## everything CI runs

ci: check  ## alias used by the pipeline

# ---------------------------------------------------------------- database

db-migrate:  ## apply migrations
	$(UV) uaagro-db migrate

db-seed:  ## load deterministic development seed data
	$(UV) uaagro-db seed

db-partitions:  ## create any missing monthly call partitions
	$(UV) uaagro-db partitions

db-check:  ## report server version and installed extensions
	$(UV) uaagro-db check

db-reset:  ## DESTRUCTIVE: drop volumes and rebuild the database from scratch
	$(COMPOSE) down -v
	$(COMPOSE) up -d postgres redis minio
	$(COMPOSE) up migrate

# ---------------------------------------------------------------- voice

sim:  ## replay a WAV fixture at the voice worker over the Exotel protocol
	$(UV) uaagro-sim --wav fixtures/audio/synthetic_speech_8k.wav \
	                 --url ws://localhost:8080/ws/voice \
	                 --out fixtures/audio/out.wav

eval:  ## voice evaluation harness (Phase 3+)
	$(UV) pytest packages/evals -m "not vendor"

# ---------------------------------------------------------------- knowledge

models:  ## fetch the local model weights (about 1.1 GB, gitignored)
	$(UV) python scripts/fetch_models.py

kb-ingest:  ## chunk, embed and store the seed knowledge base (does NOT publish)
	$(UV) uaagro-kb ingest UA_AGRO_KNOWLEDGE_BASE.md

kb-status:  ## documents, chunks, vectors and the unapproved-content count
	$(UV) uaagro-kb status

kb-retrieval:  ## the 20 seeded retrieval queries that gate Phase 3 (§21)
	$(UV) pytest tests/test_knowledge.py -k twenty_seeded -q

load:  ## sustained concurrent-call load test + ramp (§19, §21 Phase 8)
	$(UV) pytest tests/load -m load -s

latency:  ## §7's regression gate. CI fails if p95 exceeds the budget.
	$(UV) pytest -m latency

observability:  ## alert rules and dashboards match the constants they came from
	$(UV) pytest tests/test_observability.py -q

admin-test:  ## admin panel: the server-only boundary and the RBAC matrix
	cd apps/admin && npm run typecheck && npm run test

admin-build:  ## admin panel production build
	cd apps/admin && npm run build

# ---------------------------------------------------------------- deploy

deploy:  ## terraform plan for ap-south-1 (Phase 8)
	cd infra/terraform && terraform init -input=false && terraform plan
