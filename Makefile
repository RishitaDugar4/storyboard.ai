# hbday-zee — developer entry points.
# Everything here runs without a database, an API key, or a browser.

API      := apps/api
PY       := $(API)/.venv/bin/python
PIP      := $(API)/.venv/bin/pip
FIXTURES := $(API)/tests/fixtures/demo
OUT      := $(API)/out

.PHONY: help venv up down api web migrate migration db-shell render-demo \
        render-preview render-final preflight demo-fixtures test test-api \
        test-render clean-render bakeoff-fake stack-up stack-down stack-logs \
        deploy deploy-bootstrap deploy-logs deploy-ps deploy-backup

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

venv: ## create the api venv and install dependencies
	python3 -m venv $(API)/.venv && $(PIP) install -q -r $(API)/requirements.txt
	cd apps/web && npm install

up: ## start postgres + redis in docker (app runs on the host)
	docker compose up -d db redis

down: ## stop the docker dependencies
	docker compose down

api: ## run the API on :8000 with reload
	cd $(API) && set -a && . ../../.env && set +a && \
		.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

web: ## run the Next.js app on :3000
	cd apps/web && npm run dev

migrate: ## apply migrations to the configured database
	cd $(API) && .venv/bin/alembic upgrade head

migration: ## autogenerate a migration:  make migration m="add scenes"
	cd $(API) && .venv/bin/alembic revision --autogenerate -m "$(m)"

db-shell: ## psql into the dev database
	psql -d hbday_zee_dev

demo-fixtures: ## rebuild the demo timelines from bake-off assets
	$(PY) $(API)/tools/make_demo_timeline.py

render-preview: ## render the free Ken Burns preview
	cd $(API) && .venv/bin/python -m app.render.cli \
		tests/fixtures/demo/demo-preview.json out/demo-preview.mp4

render-final: ## render the hybrid final (stills + generated clips)
	cd $(API) && .venv/bin/python -m app.render.cli \
		tests/fixtures/demo/demo-final.json out/demo-final.mp4

render-demo: demo-fixtures render-preview render-final ## M1 exit criterion: both profiles
	@echo
	@echo "  M1 demo complete:"
	@ls -lh $(OUT)/demo-preview.mp4 $(OUT)/demo-final.mp4 | awk '{print "   ", $$9, $$5}'

preflight: ## check both timelines without rendering
	cd $(API) && .venv/bin/python -m app.render.cli tests/fixtures/demo/demo-preview.json --preflight-only
	cd $(API) && .venv/bin/python -m app.render.cli tests/fixtures/demo/demo-final.json --preflight-only

test: ## run all api tests (renderer + integration)
	cd $(API) && set -a && . ../../.env && set +a && .venv/bin/python -m pytest tests -q

test-render: ## renderer tests only (no database needed)
	cd $(API) && .venv/bin/python -m pytest tests/test_render.py -q

test-api: ## api integration tests (needs postgres + migrations)
	cd $(API) && set -a && . ../../.env && set +a && \
		.venv/bin/python -m pytest tests/test_api.py -q

clean-render: ## drop rendered output and the intermediate cache
	rm -rf $(OUT) $(API)/.render-cache

bakeoff-fake: ## zero-spend rehearsal of the provider bake-off
	cd tools/bakeoff && ./.venv/bin/python run.py --fake --yes

# ---------------------------------------------------------------------------
# Full local stack in Docker (needs the Docker daemon running)
# ---------------------------------------------------------------------------
stack-up: ## build and run the whole stack locally in docker
	docker compose up -d --build
	@echo "  api  http://localhost:8000/readyz"

stack-down: ## stop the local docker stack
	docker compose down

stack-logs: ## follow local stack logs
	docker compose logs -f --tail=100

# ---------------------------------------------------------------------------
# Deployment.  Set DEPLOY_HOST=user@host (or pass it per invocation).
# ---------------------------------------------------------------------------
DEPLOY_HOST ?=
COMPOSE_REMOTE = docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml

define need_host
	@test -n "$(DEPLOY_HOST)" || { echo "set DEPLOY_HOST=user@host"; exit 2; }
endef

deploy-bootstrap: ## one-time: prepare a fresh Ubuntu box
	$(call need_host)
	scp deploy/bootstrap.sh $(DEPLOY_HOST):/tmp/bootstrap.sh
	ssh $(DEPLOY_HOST) 'bash /tmp/bootstrap.sh'

deploy: ## sync the working tree and restart the remote stack
	$(call need_host)
	./deploy/deploy.sh $(DEPLOY_HOST)

deploy-ps: ## remote container status
	$(call need_host)
	ssh $(DEPLOY_HOST) 'cd /opt/hbday-zee && $(COMPOSE_REMOTE) ps'

deploy-logs: ## follow remote logs:  make deploy-logs s=api
	$(call need_host)
	ssh -t $(DEPLOY_HOST) 'cd /opt/hbday-zee && $(COMPOSE_REMOTE) logs -f --tail=100 $(s)'

deploy-backup: ## run a database backup on the server now
	$(call need_host)
	ssh $(DEPLOY_HOST) '/opt/hbday-zee/deploy/backup.sh'
