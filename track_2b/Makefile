ENV_FILE ?= $(if $(wildcard .env.docker),.env.docker,.env.docker.example)
COMPOSE = docker compose --env-file "$(ENV_FILE)" -f compose.yaml
LOCAL = $(COMPOSE) -f compose.local.yaml --profile local
PYTHON ?= python

.PHONY: run run-gpu run-local prepare-model stop logs smoke smoke-live package
run:
	$(COMPOSE) up --build --remove-orphans app

run-gpu:
	$(COMPOSE) -f compose.helpers-gpu.yaml up --build --remove-orphans app

run-local:
	$(LOCAL) up --build --remove-orphans app

prepare-model:
	$(LOCAL) --profile prepare run --rm --no-deps prepare-model

stop:
	$(LOCAL) --profile prepare down

logs:
	$(LOCAL) logs --tail 100 -f

smoke:
	$(COMPOSE) exec -T app python scripts/smoke_deployment.py

# Uses the configured real model on a temporary world, never the active save.
smoke-live:
	$(COMPOSE) exec -T app python scripts/smoke_deployment.py --live

package:
	$(PYTHON) src/scripts/package_submission.py
