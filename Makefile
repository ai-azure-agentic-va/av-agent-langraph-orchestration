# Makefile for langraph-agent-orchestration (deepagents backend).
#
# One-step deploy:  make deploy   (runs preflight, then builds in ACR + deploys to ACA)
# Prerequisites:    az login      (with access to subscription $(SUBSCRIPTION))
#
# The deployed container's NON-SECRET env vars come from infra/.env.deploy (the
# single source of truth); SECRETS come from Key Vault. `make preflight` checks
# both are in place before deploying. See infra/README.md for the full story.

# --- Azure / deploy config (override on the command line, e.g. `make deploy RG=...`) ---
RG          ?= example-chat-agent-dev-rg
APP         ?= example-deepagents-dev
SUBSCRIPTION ?= 22222222-2222-2222-2222-222222222222
KV          ?= example-chat-kv-dev

# Key Vault secrets that main.bicep references. They MUST exist before deploying,
# or the container app's Key Vault references won't resolve. entra-client-secret
# is the easy-to-miss one: deploy.sh wires ENTRA_CLIENT_SECRET to it, so if it is
# absent OBO group resolution silently goes dormant (or the revision fails).
KV_SECRETS  = azure-openai-api-key azure-ai-search-api-key langsmith-api-key entra-client-secret

# --- Local test config (mock ServiceNow + dummy Azure creds; no network at import) ---
TEST_ENV = PYTHONPATH=src SERVICENOW_MODE=mock PERSISTENCE_BACKEND=memory \
	AGENT_USE_MANAGED_IDENTITY=false \
	AZURE_OPENAI_ENDPOINT=https://dummy.openai.azure.com AZURE_OPENAI_API_KEY=dummy-key \
	AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-test AZURE_OPENAI_API_VERSION=2024-10-21
PY = .venv/bin/python
TESTS = src/v1/test/v1/utils/test_servicenow_intents.py \
	src/v1/test/v1/utils/test_servicenow.py \
	src/v1/test/v1/utils/test_agent_recursion.py \
	src/v1/test/v1/utils/test_graph_groups.py

.DEFAULT_GOAL := help
.PHONY: help preflight deploy redeploy test health url logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

preflight: ## Verify deploy prerequisites (az login, subscription, env file, KV secrets)
	@command -v az >/dev/null 2>&1 || { echo "✗ Azure CLI (az) not found — install: https://aka.ms/azure-cli"; exit 1; }
	@az account show >/dev/null 2>&1 || { echo "✗ Not logged in to Azure — run: az login"; exit 1; }
	@az account set --subscription "$(SUBSCRIPTION)" >/dev/null 2>&1 || { echo "✗ Cannot select subscription $(SUBSCRIPTION)"; exit 1; }
	@echo "✓ az logged in; subscription = $(SUBSCRIPTION)"
	@test -f infra/.env.deploy \
		&& echo "✓ infra/.env.deploy present (committed non-secret env source of truth)" \
		|| echo "✗ infra/.env.deploy missing — it is committed; restore with: git checkout -- infra/.env.deploy"
	@sub=$$(az account show --query id -o tsv); miss=""; \
	for s in $(KV_SECRETS); do \
		if az rest --method GET -o none 2>/dev/null \
			--url "https://management.azure.com/subscriptions/$$sub/resourceGroups/$(RG)/providers/Microsoft.KeyVault/vaults/$(KV)/secrets/$$s?api-version=2023-07-01"; then \
			echo "✓ KV secret $$s"; \
		else \
			echo "✗ KV secret $$s not found (or no access to verify)"; miss="$$miss $$s"; \
		fi; \
	done; \
	if [ -n "$$miss" ]; then \
		echo ""; \
		echo "⚠ Missing/unverifiable KV secret(s):$$miss"; \
		echo "  entra-client-secret is required for OBO group resolution. If genuinely missing,"; \
		echo "  create it via the ARM control-plane (works with Contributor; data-plane may be blocked):"; \
		echo "    az rest --method PUT --headers \"Content-Type=application/json\" \\"; \
		echo "      --url \"https://management.azure.com/subscriptions/$$sub/resourceGroups/$(RG)/providers/Microsoft.KeyVault/vaults/$(KV)/secrets/entra-client-secret?api-version=2023-07-01\" \\"; \
		echo "      --body '{\"properties\":{\"value\":\"<entra-app-client-secret>\"}}'"; \
	fi

deploy: preflight ## Build the image in ACR and deploy to ACA (one step)
	./infra/deploy.sh

redeploy: preflight ## Re-deploy an already-built tag without rebuilding (make redeploy TAG=deepagents-abc123)
	@test -n "$(TAG)" || { echo "ERROR: pass TAG=<image-tag> (see: az acr repository show-tags ...)"; exit 1; }
	IMAGE_TAG=$(TAG) SKIP_BUILD=1 ./infra/deploy.sh

test: ## Run the deterministic offline test suites
	@for t in $(TESTS); do echo "===== $$t ====="; $(TEST_ENV) $(PY) $$t || exit 1; done

health: ## Curl the deployed /ok health endpoint
	@curl -sS -m 20 -o /dev/null -w "GET /ok -> HTTP %{http_code}\n" "https://$$($(MAKE) -s url)/ok"

url: ## Print the deployed app FQDN
	@az containerapp show -g $(RG) -n $(APP) --query "properties.configuration.ingress.fqdn" -o tsv

logs: ## Tail the running container's console logs
	@az containerapp logs show -g $(RG) -n $(APP) --follow --tail 100
