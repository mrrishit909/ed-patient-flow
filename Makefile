export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55220/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8220

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m ed.seed --if-empty
dev:            ## API with reload on :8220, jobs run by a local worker
	($(PY) -m core.jobs ed.api &) && $(PY) -m uvicorn ed.api:app --reload --port 8220
test:
	$(PY) -m pytest --cov=ed --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the demo scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first
	$(PY) -m core.loadtest $(BASE) hosp-ops_director-demo 32 10 "GET /v1/capacity/current" "GET /v1/forecast/arrivals?hours=24" "GET /v1/forecast/beds" 
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
