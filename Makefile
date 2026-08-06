# f126-race-engineer — developer + operator entry points.
# Everything here is a thin, readable wrapper: no target hides a decision you'd want to see.
# Full runbook (console setup, first-time deploy, troubleshooting): README.md

# Local, environment-specific configuration: kube context, dashboard host, node IP, image
# repo. Gitignored — this repo is public and names no real infrastructure. Create it with
# `cp deploy/.env.example deploy/.env`. The leading `-` means "optional": local-only targets
# (dev, replay, test, lint, frontend) work in a fresh clone without it.
-include deploy/.env

# Anything below with `?=` can be overridden by deploy/.env, by the environment, or inline
# on the command line (`make deploy IMAGE_TAG=sha-abc1234`). The vars with no default here
# — KUBE_CONTEXT, IMAGE — have none on purpose: guessing them is how you deploy to the
# wrong cluster.
NAMESPACE    ?= f126
IMAGE_TAG    ?= latest
PLATFORM     ?= linux/amd64
SPEED        ?= 1
UDP_PORT     ?= 20777

# Fall back to the current kubectl context rather than emitting `--context ''` when unset;
# the check-cluster guard is what actually stops cluster targets from running blind.
KUBECTL := kubectl $(if $(KUBE_CONTEXT),--context $(KUBE_CONTEXT) )--namespace $(NAMESPACE)

# Prefer the gitignored overlay (real host) over the tracked base (placeholder host).
# See deploy/k8s/README.md.
KUSTOMIZE_DIR := $(if $(wildcard deploy/local/kustomization.yaml),deploy/local,deploy/k8s)

.DEFAULT_GOAL := help
.PHONY: help config dev replay test test-backend test-frontend lint lint-backend lint-frontend \
        frontend image push deploy logs db-init check-cluster check-image

help: ## Show this help
	@echo "f126-race-engineer"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Vars: IMAGE_TAG=$(IMAGE_TAG)  PLATFORM=$(PLATFORM)  KUBE_CONTEXT=$(KUBE_CONTEXT)"
	@echo "Local config: deploy/.env $(if $(wildcard deploy/.env),(found),(MISSING — cp deploy/.env.example deploy/.env))"

config: ## Print the resolved local config (what to type into the console's telemetry menu)
	@echo "Local configuration — from deploy/.env (gitignored)"
	@echo
	@echo "  KUBE_CONTEXT    $(if $(KUBE_CONTEXT),$(KUBE_CONTEXT),<unset>)"
	@echo "  NAMESPACE       $(NAMESPACE)"
	@echo "  DASHBOARD_HOST  $(if $(DASHBOARD_HOST),$(DASHBOARD_HOST),<unset>)"
	@echo "  NODE_IP         $(if $(NODE_IP),$(NODE_IP),<unset>)"
	@echo "  UDP_PORT        $(UDP_PORT)"
	@echo "  IMAGE           $(if $(IMAGE),$(IMAGE),<unset>)"
	@echo "  kustomize dir   $(KUSTOMIZE_DIR)"
	@echo
	@echo "Console -> Settings -> Telemetry Settings:"
	@echo "  UDP Telemetry     On"
	@echo "  UDP Broadcast     Off"
	@echo "  UDP IP Address    $(if $(NODE_IP),$(NODE_IP),<NODE_IP — set it in deploy/.env>)"
	@echo "  UDP Port          $(UDP_PORT)"
	@echo "  UDP Send Rate     60 Hz"
	@echo "  UDP Format        2026 Season Pack"
	@echo "  Your Telemetry    Public"
	@echo
	@echo "Dashboard: $(if $(DASHBOARD_HOST),https://$(DASHBOARD_HOST),<DASHBOARD_HOST — set it in deploy/.env>)"

# --- guards -----------------------------------------------------------------
# Cluster and registry targets refuse to run on guesses. Local-only targets never
# depend on these.

check-cluster:
	@missing=""; \
	[ -n "$(KUBE_CONTEXT)" ] || missing="$$missing KUBE_CONTEXT"; \
	[ -n "$(NAMESPACE)" ]    || missing="$$missing NAMESPACE"; \
	if [ -n "$$missing" ]; then \
		echo "error: cluster target needs:$$missing"; \
		echo; \
		echo "These live in deploy/.env, which is gitignored and not in a fresh clone:"; \
		echo "    cp deploy/.env.example deploy/.env"; \
		echo "    \$$EDITOR deploy/.env"; \
		echo; \
		echo "Or pass them inline:"; \
		echo "    make deploy KUBE_CONTEXT=\$$(kubectl config current-context)"; \
		echo; \
		echo "See deploy/.env.example for what each variable means."; \
		exit 1; \
	fi

check-image:
	@if [ -z "$(IMAGE)" ]; then \
		echo "error: IMAGE is not set (e.g. ghcr.io/<user>/f126-race-engineer)."; \
		echo; \
		echo "Set it in deploy/.env (cp deploy/.env.example deploy/.env), or inline:"; \
		echo "    make push IMAGE=ghcr.io/<user>/f126-race-engineer"; \
		exit 1; \
	fi

# --- local development ------------------------------------------------------

dev: ## Run capture + dashboard locally (UDP :20777, HTTP :8000)
	uv run f126 serve

replay: ## Replay a capture: make replay FILE=data/raw/x.f1raw [SPEED=1|max] [LOOP=1]
	@test -n "$(FILE)" || { \
		echo "usage: make replay FILE=data/raw/<session>.f1raw [SPEED=1|max] [LOOP=1]"; \
		echo "hint:  ls data/raw/"; \
		exit 1; }
	@test -f "$(FILE)" || { echo "no such capture: $(FILE)"; exit 1; }
	uv run f126 replay "$(FILE)" --speed $(SPEED) $(if $(LOOP),--loop,)

frontend: ## Production build of the Svelte app into frontend/dist
	cd frontend && npm run build

# --- quality ----------------------------------------------------------------

test: test-backend test-frontend ## Run backend + frontend test suites

test-backend:
	uv run pytest -q

test-frontend:
	cd frontend && npm test

lint: lint-backend lint-frontend ## Lint backend (ruff) + frontend (svelte-check)

lint-backend:
	uv run ruff check .

lint-frontend:
	cd frontend && npm run check

# --- image ------------------------------------------------------------------

image: check-image ## Build the container image locally (linux/amd64, loaded into the local daemon)
	docker buildx build \
		--platform $(PLATFORM) \
		-f deploy/Dockerfile \
		-t $(IMAGE):$(IMAGE_TAG) \
		--load \
		.

push: check-image ## Build and push the image to the registry (CD does this on merge to main)
	docker buildx build \
		--platform $(PLATFORM) \
		-f deploy/Dockerfile \
		-t $(IMAGE):$(IMAGE_TAG) \
		--push \
		.

# --- cluster ----------------------------------------------------------------

deploy: check-cluster check-image ## Apply manifests to the cluster and wait for the rollout
ifeq ($(KUSTOMIZE_DIR),deploy/k8s)
	@echo ">> WARNING: deploy/local/ not found — applying the tracked BASE (deploy/k8s)."
	@echo ">> The base ingress host is the placeholder race.example.com and will not serve"
	@echo ">> your domain. Create the overlay first: see deploy/k8s/README.md."
endif
	$(KUBECTL) apply -k $(KUSTOMIZE_DIR)
ifeq ($(IMAGE_TAG),latest)
	@# Re-applying an unchanged manifest does not restart the pod, so a freshly pushed
	@# :latest would never be pulled. Force it. Pin IMAGE_TAG=sha-<shortsha> to avoid this
	@# ambiguity entirely — that path rolls out because the pod spec actually changes.
	@echo ">> IMAGE_TAG=latest — forcing a restart so the new image is pulled"
	$(KUBECTL) rollout restart deployment/f126
else
	$(KUBECTL) set image deployment/f126 f126=$(IMAGE):$(IMAGE_TAG)
endif
	$(KUBECTL) rollout status deployment/f126 --timeout=180s

logs: check-cluster ## Tail the running pod's logs
	$(KUBECTL) logs -f deployment/f126 --tail=100

db-init: check-cluster ## Print the one-time Postgres role/database bootstrap commands (does NOT run them)
	@echo "One-time setup on a Postgres reachable from the cluster (namespace: postgres)."
	@echo "Deliberately not automated — it creates a role with a password you choose and"
	@echo "must not be re-run blindly. Copy/paste from README.md > 'First-time deploy'."
	@echo
	@echo "  PGPOD=\$$(kubectl --context $(KUBE_CONTEXT) -n postgres get pod -l app=postgres -o name | head -1)"
	@echo "  kubectl --context $(KUBE_CONTEXT) -n postgres exec -it \"\$$PGPOD\" -- \\"
	@echo "    psql -U postgres -c \"CREATE ROLE f126 LOGIN PASSWORD '<PASSWORD>';\" \\"
	@echo "                     -c \"CREATE DATABASE f126 OWNER f126;\""
	@echo
	@echo "Then create the secret the deployment reads (key: url):"
	@echo "  kubectl --context $(KUBE_CONTEXT) -n $(NAMESPACE) create secret generic f126-db \\"
	@echo "    --from-literal=url='postgresql://f126:<PASSWORD>@postgres.postgres.svc.cluster.local:5432/f126'"
