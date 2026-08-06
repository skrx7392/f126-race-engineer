# f126-race-engineer — developer + operator entry points.
# Everything here is a thin, readable wrapper: no target hides a decision you'd want to see.
# Full runbook (PS5 setup, first-time deploy, troubleshooting): README.md

KUBE_CONTEXT ?= k3s-cluster-lan
NAMESPACE    ?= f126
IMAGE        ?= ghcr.io/skrx7392/f126-race-engineer
IMAGE_TAG    ?= latest
PLATFORM     ?= linux/amd64
SPEED        ?= 1

KUBECTL := kubectl --context $(KUBE_CONTEXT) --namespace $(NAMESPACE)

.DEFAULT_GOAL := help
.PHONY: help dev replay test test-backend test-frontend lint lint-backend lint-frontend \
        frontend image push deploy logs db-init

help: ## Show this help
	@echo "f126-race-engineer"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Vars: IMAGE_TAG=$(IMAGE_TAG)  PLATFORM=$(PLATFORM)  KUBE_CONTEXT=$(KUBE_CONTEXT)"

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

image: ## Build the container image locally (linux/amd64, loaded into the local daemon)
	docker buildx build \
		--platform $(PLATFORM) \
		-f deploy/Dockerfile \
		-t $(IMAGE):$(IMAGE_TAG) \
		--load \
		.

push: ## Build and push the image to GHCR (CD does this automatically on merge to main)
	docker buildx build \
		--platform $(PLATFORM) \
		-f deploy/Dockerfile \
		-t $(IMAGE):$(IMAGE_TAG) \
		--push \
		.

# --- cluster ----------------------------------------------------------------

deploy: ## Apply manifests to cluster and wait for the rollout
	$(KUBECTL) apply -k deploy/k8s
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

logs: ## Tail the running pod's logs
	$(KUBECTL) logs -f deployment/f126 --tail=100

db-init: ## Print the one-time Postgres role/database bootstrap commands (does NOT run them)
	@echo "One-time setup on the shared Postgres (namespace: postgres)."
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
