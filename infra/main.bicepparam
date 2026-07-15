using './main.bicep'

// -----------------------------------------------------------------------------
// example-deepagents-dev — infra/deploy-shape parameters ONLY.
// -----------------------------------------------------------------------------
// App CONFIG (auth, Azure OpenAI/Search endpoints, tenant mappings, LangSmith,
// ServiceNow, log level, …) is NO LONGER here. It lives in infra/.env.deploy and
// is injected by deploy.sh as the `appEnv` array param. Edit that file to change
// runtime config; this file only carries knobs that shape the deployment itself.
//
// Secrets are never here — they come from Key Vault references (see main.bicep).
//
// The repo Dockerfile builds the LangGraph Platform server, which listens on
// 8000, serves health at /ok, and REQUIRES DATABASE_URI + REDIS_URI at boot.
// -----------------------------------------------------------------------------

// Set this to the tag produced by infra/deploy.sh (or your CI build).
// deploy.sh overrides this per run via `--parameters imageTag=...`.
param imageTag = 'REPLACE_WITH_IMAGE_TAG'

// --- App identity ------------------------------------------------------------
param containerAppName = 'example-deepagents-dev'

// --- Runtime shape (matches the langgraph-api image) -------------------------
param targetPort = 8000
param enableHttpProbes = true
param livenessPath = '/ok'
param readinessPath = '/ok'

// --- Container resources -----------------------------------------------------
// Pinned to the LIVE size. The app was bumped to 1.5 CPU / 3Gi out-of-band
// (az containerapp update) on 2026-06-19; main.bicep defaults to 1.0/2Gi, so
// without these the next deploy would SHRINK the app. Keep in sync with live.
param cpu = '1.5'
param memory = '3Gi'

// --- Backing services --------------------------------------------------------
// The platform server cannot boot without Redis + Postgres.
// deployRedis=true provisions a small internal Redis container app (none exists
// in the RG today). Set false to use an existing Redis URL from KV
// (redisUrlSecretName, default 'agent-server-redis-url').
param deployRedis = true
// deployPostgres=true provisions a DEDICATED internal Postgres container app.
// The shared managed Postgres (agent-server-postgres-dsn) rejects the platform
// server's boot migration with "permission denied for schema public" (that DSN
// user doesn't own the DB), so we give the server its own DB. Set false only if
// you point postgresDsnSecretName at a DB where the user can CREATE its schema.
param deployPostgres = true
// Used only when deployPostgres=false:
param postgresDsnSecretName = 'agent-server-postgres-dsn'
// postgresPassword is generated and passed by infra/deploy.sh (do not hardcode here).

// --- App-level checkpointer (separate from the platform server's own storage) -
// 'memory' = the in-graph checkpointer is in-memory; the platform server still
// persists its threads/runs/assistants in DATABASE_URI. Set 'postgres' to also
// run the app's own LangGraph checkpointer on the same Postgres. This is the ONE
// app-config knob kept here, because it's coupled to whether the template wires
// POSTGRESS_DATABASE_URL (and Bicep emits the PERSISTENCE_BACKEND env var itself).
param persistenceBackend = 'postgres'
