# Everything the project needs, in dependency order:
#   make install && make up && make deploy && make smoke
PY := .venv/bin/python
TF := terraform -chdir=terraform

.DEFAULT_GOAL := help
.PHONY: help install test cov lint fmt package up down logs wait deploy plan destroy outputs seed smoke clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install dev dependencies
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt

test: ## Run the unit tests
	$(PY) -m pytest

cov: ## Run the unit tests with a coverage report
	$(PY) -m pytest --cov=src --cov-report=term-missing

lint: ## Lint the source and tests
	$(PY) -m ruff check src tests scripts
	$(TF) fmt -check -recursive

fmt: ## Format the source and Terraform
	$(PY) -m ruff check --fix src tests scripts
	$(PY) -m ruff format src tests scripts
	$(TF) fmt -recursive

package: ## Build the Lambda deployment zip
	./scripts/package.sh

up: ## Start LocalStack
	docker compose up -d
	@$(MAKE) --no-print-directory wait

wait: ## Block until LocalStack is healthy
	@printf 'waiting for localstack'
	@for i in $$(seq 1 60); do \
		if curl -sf http://localhost:4566/_localstack/health >/dev/null 2>&1; then echo ' ready'; exit 0; fi; \
		printf '.'; sleep 2; \
	done; echo ' timed out'; exit 1

down: ## Stop LocalStack and discard its state
	docker compose down -v

logs: ## Tail LocalStack logs
	docker compose logs -f localstack

plan: package ## Show the Terraform plan
	$(TF) init -input=false -upgrade=false
	$(TF) plan

deploy: package ## Create the local AWS infrastructure
	$(TF) init -input=false -upgrade=false
	$(TF) apply -auto-approve
	@echo
	@echo "API base URL: $$($(TF) output -raw api_base_url)"

destroy: ## Tear the local infrastructure down
	$(TF) destroy -auto-approve

outputs: ## Print the Terraform outputs
	$(TF) output

seed: ## Load sample images through the deployed API
	$(PY) scripts/seed_local.py

smoke: ## Run the end-to-end check against the deployed API
	$(PY) scripts/smoke_test.py

clean: ## Remove build artefacts and caches
	rm -rf build .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
