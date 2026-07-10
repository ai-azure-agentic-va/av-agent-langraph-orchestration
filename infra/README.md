# fin-deepagents-dev — Infrastructure as Code

Bicep IaC that deploys this backend as a **new** Azure Container App,
`fin-deepagents-dev`, into the **existing** shared dev platform.

**Status: deployed and healthy.**
`https://fin-deepagents-dev.example-0000000.eastus2.azurecontainerapps.io`
(`/ok` → 200; SDK endpoints enforce JWT → 401 without a token.)

## Server model

The repo `Dockerfile` is `FROM langchain/langgraph-api:3.11` — the **LangGraph
Platform server** (verified by running the image). It:

- listens on **port 8000**, health route **`/ok`** (not `/livez`/`/readyz`);
- **hard-requires `DATABASE_URI` (Postgres) + `REDIS_URI` at boot** — crash-loops
  without both, independent of the app's `PERSISTENCE_BACKEND`;
- needs `LANGSMITH_API_KEY` for the self-hosted-lite license check;
- reads its graph/auth/http config from env vars baked by the Dockerfile
  (`LANGSERVE_GRAPHS`, `LANGGRAPH_AUTH`, `LANGGRAPH_HTTP`).

The `Dockerfile` is generated with `langgraph dockerfile` so it bakes the full
`langgraph.json` (graphs **+ auth + http app**). If you change `langgraph.json`,
regenerate it: `langgraph dockerfile Dockerfile`. Without the baked
`LANGGRAPH_AUTH`/`LANGGRAPH_HTTP`, the server would run `auth=noop` (SDK
endpoints open) and not mount the custom routes (`/starter_prompts`,
`/feedback`). The Bicep params `langgraphAuthPath`/`langgraphHttpAppPath` exist
only as an override for an older image and default to empty (use the image).

> ⚠️ `langgraph.json` must **not** set `http.enable_custom_route_auth: true`:
> this langgraph-api version crashes at startup (`ValueError: Cannot apply
> middleware: route _IncludedRouter`) because `main.py` mounts routes via
> `app.include_router(...)`. Auth is still enforced on the custom routes by the
> global `LANGGRAPH_AUTH` middleware (verified: `/starter_prompts` → 401).

## What it provisions

Three new container apps; everything else is **reused** (referenced, never created):

| New resource | Purpose |
| --- | --- |
| `fin-deepagents-dev` | the agent (port 8000) |
| `fin-deepagents-dev-redis` | internal Redis (`redis:7-alpine`, TCP 6379) → `REDIS_URI`. Toggle `deployRedis=false`. |
| `fin-deepagents-dev-postgres` | internal Postgres (`postgres:16-alpine`, TCP 5432) → `DATABASE_URI`. Toggle `deployPostgres=false`. |

| Shared resource (existing, referenced) | Name |
| --- | --- |
| Resource group | `fin-chat-agent-dev-rg` |
| ACA managed environment | `fin-chat-env-dev` (East US 2) |
| Container registry | `findev0000000000acr` |
| User-assigned identity | `fin-chat-agent-mi-dev` |
| Key Vault | `fin-chat-kv-dev-xxxxxx` |

The identity already holds **AcrPull**, **Key Vault Secrets User**, **Search
Index Data Reader** (`aisearchfin`), **Cognitive Services OpenAI User**
(`fin-openai-dev`) — no new role assignments needed.

## Configuration model

All **non-secret** app config lives in **one** file: `infra/.env.deploy`, which
is **committed to the repo** — the single source of truth. `deploy.sh` parses it
and injects every entry into the container as the Bicep `appEnv` array param, so
changing runtime config is a one-file edit, no Bicep changes (and a fresh clone
deploys with no setup step). There is no `.example` template — edit and commit
this file directly. Secrets never go here; they stay in Key Vault.

```
edit infra/.env.deploy   ──parsed by──▶  deploy.sh  ──appEnv=@file──▶  main.bicep  ──▶  container env
```

`main.bicep` only owns the deployment shape (sizes, probes, ports, backing
services) plus **10 env vars** that can't come from the file — they're either
derived from Azure resources, coupled to a toggle, or secrets. `deploy.sh`
**strips these from the env file** (warning if present) so the container never
gets a duplicate env name (ACA rejects duplicates):

| Bicep-owned env var | Source |
| --- | --- |
| `AZURE_OPENAI_API_KEY` / `AZURE_AI_SEARCH_API_KEY` / `LANGSMITH_API_KEY` / `ENTRA_CLIENT_SECRET` | Key Vault references |
| `DATABASE_URI` / `REDIS_URI` / `POSTGRESS_DATABASE_URL` | backing-service URIs |
| `AZURE_KEY_VAULT_URI` | the Key Vault resource |
| `AZURE_CLIENT_ID` | the user-assigned identity |
| `PERSISTENCE_BACKEND` | `main.bicepparam` (coupled to wiring `POSTGRESS_DATABASE_URL`) |

> **Adding a new env var:** add it to `infra/.env.deploy` and commit it.
> Nothing in Bicep needs to change. Only edit Bicep if the new var is a secret
> (add a Key Vault reference) or must be derived from a resource (add it to
> `bicepPlainEnv` **and** the `DENY` list in `deploy.sh`).

> ℹ Multi-environment later is just another file: `ENV_FILE=infra/.env.deploy.stage ./infra/deploy.sh`.

### Why a dedicated Postgres (not the shared managed server)

The shared managed Postgres (`agent-server-postgres-dsn`) **rejects** the
platform server's boot migration with `permission denied for schema public` —
that DSN's user doesn't own the DB (Postgres 15+ locks down `public`), and the
DB is shared with the chat-agent. The public server is also network-private and
Entra-auth-disabled, so its grants can't be fixed from outside the VNet. So we
give the platform server its **own** in-env Postgres where it owns its schema.
Storage is **ephemeral** (a replica restart resets the DB; the server re-migrates
on boot) — fine for dev. For durable storage, attach an Azure Files volume or
point `deployPostgres=false` at a dedicated managed DB whose user can `CREATE`.

## Secrets

Sensitive values are **not** plaintext env vars:

| Env var | Source |
| --- | --- |
| `AZURE_OPENAI_API_KEY` | Key Vault ref → `azure-openai-api-key` |
| `AZURE_AI_SEARCH_API_KEY` | Key Vault ref → `azure-ai-search-api-key` |
| `LANGSMITH_API_KEY` | Key Vault ref → `langsmith-api-key` |
| `ENTRA_CLIENT_SECRET` | Key Vault ref → `entra-client-secret` (activates OBO group resolution) |
| `DATABASE_URI` | container-app secret `database-uri` (in-env Postgres DSN; encrypted at rest). `deployPostgres=false` → Key Vault ref `agent-server-postgres-dsn` |
| `REDIS_URI` | in-env `redis://fin-deepagents-dev-redis:6379` (or KV ref `agent-server-redis-url` when `deployRedis=false`) |

Key Vault refs resolve at runtime via the user-assigned identity. The app also
gets `AZURE_KEY_VAULT_URI` + `AZURE_CLIENT_ID` so its own `resolve_env_secret()`
path (e.g. ServiceNow live-mode creds) can read KV directly.

Secrets are **never** in `infra/.env.deploy` — `deploy.sh` strips the secret keys
even if someone adds them. The full non-secret runtime env set is in the
committed [`.env.deploy`](./.env.deploy).

### Postgres password

The provisioned Postgres password is **stable** across deploys: `deploy.sh`
generates it once into `infra/.pg_password` (gitignored) and reuses it. Don't
rotate it casually — the `postgres` image only applies the password on first
init, so a changed password strands the already-initialized container.

### `.dockerignore`

`az acr build` uploads the whole context and ignores `.gitignore`, and the
Dockerfile does `ADD . …`. The repo-root [`.dockerignore`](../.dockerignore)
keeps the local `.env` (real keys) and cruft out of the image.

## Deploy

```bash
az login
az account set --subscription 22222222-2222-2222-2222-222222222222

# Build the image in ACR + deploy everything
./infra/deploy.sh

# …or deploy an already-built tag without rebuilding:
IMAGE_TAG=<tag> SKIP_BUILD=1 ./infra/deploy.sh
```

Each run sets a fresh `appRevisionSuffix` so the agent rolls a new revision and
picks up current secrets (ACA does not roll on secret-value-only changes).

## Verify after deploy

Run the smoke-test script — it checks auth enforcement and Postgres
end-to-end (it mints a JWT via `az` for the API audience, or set `ACCESS_TOKEN`):

```bash
./infra/test-deployment.sh            # all suites (health + auth + postgres)
./infra/test-deployment.sh auth       # only auth
./infra/test-deployment.sh postgres   # only postgres
```

It asserts: public `/ok` `/info` `/metrics` → 200; protected endpoints reject
missing/garbage tokens (401) and accept a valid token (200); `/metrics` shows a
healthy PG pool (`pg_pool_requests_errors == 0`); and a full thread CRUD cycle
(create → read-back → search → delete → 404) proves Postgres read/write
persistence. Exit code is non-zero if anything fails.

Manual spot-checks / logs:

```bash
BASE=https://fin-deepagents-dev.example-0000000.eastus2.azurecontainerapps.io
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/ok"                 # 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/threads"    # 401 (auth enforced)
az containerapp logs show -g fin-chat-agent-dev-rg -n fin-deepagents-dev --type console --follow
```

Also confirm `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (`text-embedding-3-large` by
default) actually exists on the target Azure OpenAI resource, or KB search fails
at query-embedding time.

## Files

| File | Purpose |
| --- | --- |
| `main.bicep` | agent + internal Redis + internal Postgres + KV-ref secrets + existing-resource refs; takes non-secret config via the `appEnv` array param |
| `main.bicepparam` | deploy-shape knobs only (image, ports, probes, backing-service toggles, `persistenceBackend`) — **no app config** |
| `deploy.sh` | parses `.env.deploy` → `appEnv`, `az acr build`, then `az deployment group create` (handles stable PG password + revision roll) |
| `.env.deploy` | **(committed)** non-secret runtime config — the single source of truth (`deploy.sh` injects it as `appEnv`) |
| `../.dockerignore` | keeps `.env*` secrets out of the image |
