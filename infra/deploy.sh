#!/usr/bin/env bash
# =============================================================================
# Build + deploy fin-deepagents-dev to Azure Container Apps.
#
# Config model (the robust part):
#   * NON-SECRET app config lives in ONE file: infra/.env.deploy (committed —
#     the single source of truth). This script parses it and injects every var
#     into Bicep as the single `appEnv` array param. Edit that file to change
#     runtime config — no Bicep edits needed.
#   * SECRETS are never in that file. The API keys come from Key Vault refs and
#     the backing-service URIs are owned by main.bicep (see the DENY list below).
#
# Builds the image in the shared ACR with `az acr build` (no local Docker),
# then deploys the Bicep into the existing resource group / managed environment.
#
# Prerequisites:
#   az login
#   az account set --subscription 22222222-2222-2222-2222-222222222222
#
# Usage:
#   ./infra/deploy.sh                               # build a fresh tag and deploy
#   IMAGE_TAG=deepagents-abc123 SKIP_BUILD=1 ./infra/deploy.sh   # deploy an existing tag
#   ENV_FILE=/path/to/.env.deploy ./infra/deploy.sh # use a different env file
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (override via env vars)
# ---------------------------------------------------------------------------
SUBSCRIPTION="${SUBSCRIPTION:-22222222-2222-2222-2222-222222222222}"
RESOURCE_GROUP="${RESOURCE_GROUP:-fin-chat-agent-dev-rg}"
ACR_NAME="${ACR_NAME:-findev0000000000acr}"
IMAGE_REPO="${IMAGE_REPO:-langraph-agent-orchestration}"
SKIP_BUILD="${SKIP_BUILD:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source-of-truth for the deployed container's NON-SECRET env vars. This file is
# COMMITTED to the repo (infra/.env.deploy) — the single place to edit runtime
# config. Override with ENV_FILE=/path ./infra/deploy.sh to point at another env.
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env.deploy}"

# Default tag: deepagents-<git-sha>-<timestamp>
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo nogit)"
STAMP="$(date -u +%Y%m%d%H%M%S)"
IMAGE_TAG="${IMAGE_TAG:-deepagents-${GIT_SHA}-${STAMP}}"

# Env-var names that main.bicep owns (Key Vault refs + backing services +
# resource-derived + the persistence-coupled var). These MUST NOT come from the
# env file, or the container would get a duplicate env name (ACA rejects that).
# The parser below strips them and warns if any were present in the file.
DENY="AZURE_OPENAI_API_KEY AZURE_AI_SEARCH_API_KEY LANGSMITH_API_KEY ENTRA_CLIENT_SECRET \
DATABASE_URI REDIS_URI POSTGRESS_DATABASE_URL \
AZURE_KEY_VAULT_URI AZURE_CLIENT_ID PERSISTENCE_BACKEND"

echo "▶ Subscription : ${SUBSCRIPTION}"
echo "▶ Resource grp : ${RESOURCE_GROUP}"
echo "▶ ACR          : ${ACR_NAME}"
echo "▶ Image        : ${ACR_NAME}.azurecr.io/${IMAGE_REPO}:${IMAGE_TAG}"
echo "▶ Env file     : ${ENV_FILE}"
echo ""

# ---------------------------------------------------------------------------
# Step 0 — Build the appEnv JSON array from the env file
# ---------------------------------------------------------------------------
# infra/.env.deploy is committed, so it should always be present. (A custom
# ENV_FILE override could still point at a path that does not exist.)
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: env file not found: ${ENV_FILE}" >&2
  echo "       infra/.env.deploy is the committed source of truth — restore it" >&2
  echo "       (git checkout -- infra/.env.deploy) or pass ENV_FILE=/path." >&2
  exit 1
fi

# Parse the env file into a compact JSON array of {name,value}. Python reads the
# file directly (values never round-trip through the shell), so JSON-valued vars
# (TENANT_GROUP_*_MAPPING) and comma lists (CORS_ORIGINS, *_SELECT_FIELDS) survive
# verbatim. Splits on the first '=' only; skips comments/blanks; honors `export `;
# strips one surrounding quote pair; drops DENY keys (warns); dedupes (last wins).
build_appenv_json() {
  DENY="${DENY}" python3 - "${ENV_FILE}" <<'PY'
import json, os, re, sys

path = sys.argv[1]
deny = set(os.environ.get("DENY", "").split())
seen, out, stripped = {}, [], []

with open(path, encoding="utf-8") as fh:
    for raw in fh:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if "=" not in s:
            print(f"WARN: skipping line without '=': {raw.rstrip()!r}", file=sys.stderr)
            continue
        name, value = s.split("=", 1)          # only the first '=' splits
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            print(f"WARN: skipping invalid env name {name!r}", file=sys.stderr)
            continue
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]                          # strip ONE surrounding quote pair
        if name in deny:
            stripped.append(name)
            continue
        if name in seen:
            out[seen[name]]["value"] = v         # last value wins
        else:
            seen[name] = len(out)
            out.append({"name": name, "value": v})

if stripped:
    print("WARN: ignored Bicep-owned/secret keys present in env file: "
          + ", ".join(sorted(set(stripped))), file=sys.stderr)
if not out:
    print("ERROR: no usable env vars parsed from the env file.", file=sys.stderr)
    sys.exit(1)

json.dump(out, sys.stdout, ensure_ascii=False)
PY
}

APPENV_JSON="$(mktemp -t appenv.XXXXXX)"
trap 'rm -f "${APPENV_JSON}"' EXIT
build_appenv_json > "${APPENV_JSON}"
echo "▶ Parsed $(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "${APPENV_JSON}") env vars from ${ENV_FILE##*/} into appEnv."
echo ""

az account set --subscription "${SUBSCRIPTION}"

# ---------------------------------------------------------------------------
# Step 1 — Build & push the image (ACR Tasks; uses repo-root Dockerfile)
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD}" != "1" ]]; then
  echo "▶ Building image in ACR (this uses the Dockerfile at repo root)…"
  az acr build \
    --registry "${ACR_NAME}" \
    --image "${IMAGE_REPO}:${IMAGE_TAG}" \
    "${REPO_ROOT}"
else
  echo "▶ SKIP_BUILD=1 — deploying existing tag ${IMAGE_TAG}"
fi

# ---------------------------------------------------------------------------
# Step 2 — Deploy the Bicep
# ---------------------------------------------------------------------------
# Password for the provisioned Postgres container (deployPostgres=true).
# MUST be STABLE across runs: the postgres image only sets the password on first
# init, so rotating it strands the already-initialized container. We persist a
# generated password to infra/.pg_password (gitignored) and reuse it every run.
PG_PW_FILE="${SCRIPT_DIR}/.pg_password"
if [[ -n "${POSTGRES_PASSWORD:-}" ]]; then
  :  # caller supplied one explicitly
elif [[ -f "${PG_PW_FILE}" ]]; then
  POSTGRES_PASSWORD="$(cat "${PG_PW_FILE}")"
else
  POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  (umask 077; printf '%s' "${POSTGRES_PASSWORD}" > "${PG_PW_FILE}")
  echo "  Generated a stable Postgres password -> infra/.pg_password (gitignored)."
fi

echo "▶ Deploying Bicep…"
# appEnv is supplied ONLY via @file (a bare JSON array), so there is exactly one
# source for it. It mixes fine with the .bicepparam file + the inline overrides.
az deployment group create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "fin-deepagents-dev-${STAMP}" \
  --template-file "${SCRIPT_DIR}/main.bicep" \
  --parameters "${SCRIPT_DIR}/main.bicepparam" \
  --parameters imageTag="${IMAGE_TAG}" \
  --parameters postgresPassword="${POSTGRES_PASSWORD}" \
  --parameters appRevisionSuffix="r${STAMP}" \
  --parameters appEnv=@"${APPENV_JSON}" \
  --query "properties.outputs" \
  --output json

echo ""
echo "✅ Done. App URL:"
az containerapp show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "fin-deepagents-dev" \
  --query "properties.configuration.ingress.fqdn" -o tsv \
  | sed 's#^#   https://#'
