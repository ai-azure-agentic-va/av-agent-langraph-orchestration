// =============================================================================
// fin-deepagents-dev — Azure Container App (IaC)
// =============================================================================
// Deploys the langraph-agent-orchestration backend as a NEW container app
// named `fin-deepagents-dev` INTO the existing dev platform.
//
// SERVER: the repo Dockerfile is `FROM langchain/langgraph-api:3.11` — the
// LangGraph Platform server. It listens on port 8000, serves health at /ok, and
// HARD-REQUIRES `DATABASE_URI` (Postgres) + `REDIS_URI` at boot (it crash-loops
// without both, regardless of the app's PERSISTENCE_BACKEND). So this template:
//
//   * Wires DATABASE_URI from the existing Key Vault secret (the shared managed
//     Postgres flexible server) and REDIS_URI to a small Redis.
//   * Provisions a lightweight internal Redis container app in the SAME ACA
//     environment (none exists in the RG today). Toggle with `deployRedis=false`
//     to instead point REDIS_URI at an existing Redis URL stored in Key Vault.
//
// Reuses (does NOT recreate) the shared dev infrastructure:
//   - ACA managed environment : fin-chat-env-dev
//   - Container registry       : findev0000000000acr   (pull via user identity)
//   - User-assigned identity   : fin-chat-agent-mi-dev  (AcrPull, KV Secrets User,
//                                Search Index Data Reader, Cognitive Services OpenAI User)
//   - Key Vault                : fin-chat-kv-dev-xxxxxx
//
// Sensitive secrets are read from Key Vault via Key Vault references on the
// container-app secrets (resolved at runtime by the user-assigned identity);
// they are never set as plaintext.
//
// Scope: resource group (deploy into fin-chat-agent-dev-rg).
// =============================================================================

targetScope = 'resourceGroup'

// ----------------------------------------------------------------------------
// Core / naming
// ----------------------------------------------------------------------------
@description('Azure region. Must match the existing managed environment region.')
param location string = 'eastus2'

@description('Name of the new container app.')
param containerAppName string = 'fin-deepagents-dev'

@description('Name of the container inside the app.')
param containerName string = 'deepagents'

// ----------------------------------------------------------------------------
// Existing shared infrastructure (referenced, never created)
// ----------------------------------------------------------------------------
@description('Existing ACA managed environment to deploy into.')
param managedEnvironmentName string = 'fin-chat-env-dev'

@description('Existing user-assigned managed identity used for ACR pull + Key Vault access.')
param userAssignedIdentityName string = 'fin-chat-agent-mi-dev'

@description('Existing Azure Container Registry name (without .azurecr.io).')
param acrName string = 'findev0000000000acr'

@description('Existing Key Vault name that holds the secrets.')
param keyVaultName string = 'fin-chat-kv-dev-xxxxxx'

// ----------------------------------------------------------------------------
// Image
// ----------------------------------------------------------------------------
@description('Image repository in the ACR.')
param imageRepository string = 'langraph-agent-orchestration'

@description('Image tag to deploy (build & push first, e.g. with infra/deploy.sh).')
param imageTag string

// ----------------------------------------------------------------------------
// Runtime shape (defaults match the langchain/langgraph-api platform server)
// ----------------------------------------------------------------------------
@description('Container listening port. The langchain/langgraph-api server listens on 8000.')
param targetPort int = 8000

@description('Enable HTTP liveness/readiness probes. The platform server health route is /ok (unauthenticated).')
param enableHttpProbes bool = true

@description('HTTP liveness probe path (langgraph-api exposes /ok, not /livez).')
param livenessPath string = '/ok'

@description('HTTP readiness probe path.')
param readinessPath string = '/ok'

@description('vCPU for the app container.')
param cpu string = '1.0'

@description('Memory for the app container.')
param memory string = '2Gi'

@minValue(0)
@description('Minimum replica count.')
param minReplicas int = 1

@minValue(1)
@description('Maximum replica count.')
param maxReplicas int = 10

@description('HTTP concurrency target for autoscaling.')
param concurrentRequests int = 50

@description('Expose the app to the public internet (external ingress).')
param externalIngress bool = true

@allowed([
  'Single'
  'Multiple'
])
@description('Revision mode.')
param activeRevisionsMode string = 'Single'

@description('Revision suffix for the agent app. deploy.sh sets a per-run timestamp so secret-only changes roll a fresh revision. Empty = ACA auto-generates.')
param appRevisionSuffix string = ''

// ----------------------------------------------------------------------------
// Backing services (required by the langgraph-api platform server)
// ----------------------------------------------------------------------------
@description('Provision a small internal Redis container app in this environment (none exists in the RG today). Set false to use redisUrlSecretName from Key Vault instead.')
param deployRedis bool = true

@description('Redis image for the provisioned Redis container app.')
param redisImage string = 'redis:7-alpine'

@description('Provision a dedicated internal Postgres container app for the platform server. RECOMMENDED: the shared managed Postgres (agent-server-postgres-dsn) rejects the platform-server migration with "permission denied for schema public" because that DSN user does not own the DB. Set false to use postgresDsnSecretName from Key Vault (only works if that user can CREATE in its schema).')
param deployPostgres bool = true

@description('Postgres image for the provisioned Postgres container app.')
param postgresImage string = 'postgres:16-alpine'

@description('Database name created in the provisioned Postgres.')
param postgresDb string = 'deepagents'

@description('Superuser name for the provisioned Postgres.')
param postgresUser string = 'postgres'

@secure()
@description('Password for the provisioned Postgres container app. infra/deploy.sh generates and passes this; required (non-empty) when deployPostgres=true.')
param postgresPassword string = ''

@description('Key Vault secret holding the Postgres connection string -> DATABASE_URI. Only used when deployPostgres=false.')
param postgresDsnSecretName string = 'agent-server-postgres-dsn'

@description('Key Vault secret holding the Redis URL -> REDIS_URI. Only used when deployRedis=false.')
param redisUrlSecretName string = 'agent-server-redis-url'

// ----------------------------------------------------------------------------
// App-level checkpointer (separate from the platform server's own storage)
// ----------------------------------------------------------------------------
@allowed([
  'memory'
  'postgres'
])
@description('In-graph checkpointer backend. The platform server always uses DATABASE_URI for its own state; this only controls the app code\'s LangGraph checkpointer. "postgres" additionally wires POSTGRESS_DATABASE_URL from the same Postgres secret.')
param persistenceBackend string = 'memory'

// ----------------------------------------------------------------------------
// Key Vault secret names (existing secrets in the shared vault)
// ----------------------------------------------------------------------------
@description('KV secret holding the Azure OpenAI API key.')
param openAiApiKeySecretName string = 'azure-openai-api-key'

@description('KV secret holding the Azure AI Search API key.')
param searchApiKeySecretName string = 'azure-ai-search-api-key'

@description('KV secret holding the LangSmith API key.')
param langsmithApiKeySecretName string = 'langsmith-api-key'

@description('KV secret holding the Entra app client secret (ENTRA_CLIENT_SECRET). Required to ACTIVATE the on-behalf-of (OBO) group-resolution path; absent it stays dormant.')
param entraClientSecretName string = 'entra-client-secret'

// ----------------------------------------------------------------------------
// Application configuration (non-sensitive env vars)
// ----------------------------------------------------------------------------
// All non-secret app config is driven from infra/.env.deploy: deploy.sh parses
// that file and passes the resulting list here as a single array. Each item is
// { name, value }. This is the single source of truth for app config — do NOT
// re-add per-variable params here (that's the duplication this rewrite removed).
//
// SECRETS ARE NEVER HERE. The 3 API keys come from Key Vault references, and
// DATABASE_URI / REDIS_URI / POSTGRESS_DATABASE_URL / AZURE_KEY_VAULT_URI /
// AZURE_CLIENT_ID / PERSISTENCE_BACKEND are owned by this template (see
// bicepPlainEnv + the secret arrays below). deploy.sh strips those keys from the
// env file so the container never gets a duplicate env-var name (ACA rejects
// duplicates, and Bicep does not catch them at compile time).
@description('Non-secret container env vars, injected verbatim from infra/.env.deploy by deploy.sh. Each item: { name, value }. Left untyped (array) on purpose so it concats cleanly with the heterogeneous secretRef env objects below.')
param appEnv array = []

// --- LangGraph platform config override (normally baked into the image) ------
// The Dockerfile (regenerated with `langgraph dockerfile`) now bakes
// LANGGRAPH_AUTH + LANGGRAPH_HTTP from langgraph.json, so these default to empty
// and we rely on the image. Set a path here only to override the baked value
// (e.g. an older image that lacks it). Paths are inside the image.
@description('Override: in-image path to the langgraph_sdk Auth object. Empty = use the value baked into the image.')
param langgraphAuthPath string = ''

@description('Override: in-image path to the custom FastAPI app mounted by the platform. Empty = use the value baked into the image.')
param langgraphHttpAppPath string = ''

// =============================================================================
// Existing resources
// =============================================================================
resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: managedEnvironmentName
}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: userAssignedIdentityName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// =============================================================================
// Derived values
// =============================================================================
var acrLoginServer = acr.properties.loginServer
var keyVaultUri = keyVault.properties.vaultUri
var image = '${acrLoginServer}/${imageRepository}:${imageTag}'
var usePostgres = persistenceBackend == 'postgres'

var redisAppName = '${containerAppName}-redis'
// In-environment TCP address for the provisioned Redis container app.
var redisUri = 'redis://${redisAppName}:6379'

var pgAppName = '${containerAppName}-postgres'
// In-environment DSN for the provisioned Postgres container app (carries the password).
var dbDsn = 'postgresql://${postgresUser}:${postgresPassword}@${pgAppName}:5432/${postgresDb}?sslmode=disable'
// DATABASE_URI / POSTGRESS_DATABASE_URL both read from this container-app secret name.
var dbSecretRefName = deployPostgres ? 'database-uri' : 'postgres-dsn'

// Key Vault reference secrets (resolved at runtime by the user-assigned identity).
var keySecrets = [
  {
    name: 'azure-openai-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/${openAiApiKeySecretName}'
    identity: uami.id
  }
  {
    name: 'search-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/${searchApiKeySecretName}'
    identity: uami.id
  }
  {
    name: 'langsmith-api-key'
    keyVaultUrl: '${keyVaultUri}secrets/${langsmithApiKeySecretName}'
    identity: uami.id
  }
  {
    name: 'entra-client-secret'
    keyVaultUrl: '${keyVaultUri}secrets/${entraClientSecretName}'
    identity: uami.id
  }
]

// DATABASE_URI source: the provisioned Postgres DSN (held as a plain container-app
// secret, encrypted at rest) when deployPostgres, else a Key Vault reference.
var dbSecret = deployPostgres ? [
  {
    name: 'database-uri'
    value: dbDsn
  }
] : [
  {
    name: 'postgres-dsn'
    keyVaultUrl: '${keyVaultUri}secrets/${postgresDsnSecretName}'
    identity: uami.id
  }
]

// Redis URL comes from Key Vault only when we are NOT provisioning Redis here.
var redisSecret = deployRedis ? [] : [
  {
    name: 'redis-url'
    keyVaultUrl: '${keyVaultUri}secrets/${redisUrlSecretName}'
    identity: uami.id
  }
]

var secrets = concat(keySecrets, dbSecret, redisSecret)

// Bicep-owned plain env vars — the ones that must be computed here rather than
// come from infra/.env.deploy:
//   PERSISTENCE_BACKEND  - coupled to whether POSTGRESS_DATABASE_URL is wired
//   AZURE_KEY_VAULT_URI  - derived from the Key Vault resource
//   AZURE_CLIENT_ID      - derived from the user-assigned identity (so
//                          DefaultAzureCredential picks the right MI at runtime)
// deploy.sh strips these from the env file, so they never collide with appEnv.
// Every OTHER non-secret env var (APP_ENV, AGENT_*, AGENT_AUTH_*, ENTRA_*,
// AZURE_OPENAI_*, AZURE_AI_SEARCH_*, TENANT_GROUP_*_MAPPING, LANGSMITH_*,
// SERVICENOW_*, …) now lives in infra/.env.deploy and arrives via `appEnv`.
var bicepPlainEnv = [
  { name: 'PERSISTENCE_BACKEND', value: persistenceBackend }
  { name: 'AZURE_KEY_VAULT_URI', value: keyVaultUri }
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
]

// Platform-server backing services (required at boot).
var platformEnv = [
  { name: 'DATABASE_URI', secretRef: dbSecretRefName }
]

var redisEnv = deployRedis ? [
  { name: 'REDIS_URI', value: redisUri }
] : [
  { name: 'REDIS_URI', secretRef: 'redis-url' }
]

// Secret-backed application environment variables (secretRef -> Key Vault reference).
var secretEnv = [
  { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
  { name: 'AZURE_AI_SEARCH_API_KEY', secretRef: 'search-api-key' }
  { name: 'LANGSMITH_API_KEY', secretRef: 'langsmith-api-key' }
  { name: 'ENTRA_CLIENT_SECRET', secretRef: 'entra-client-secret' }
]

// In-graph checkpointer DSN (only when the app code's checkpointer uses postgres).
var appPostgresEnv = [
  { name: 'POSTGRESS_DATABASE_URL', secretRef: dbSecretRefName }
]

// Optional override of the LANGGRAPH_AUTH/HTTP config baked into the image.
// Empty params (the default) leave the image's baked ENV in place.
var langgraphConfigEnv = concat(
  empty(langgraphAuthPath) ? [] : [
    { name: 'LANGGRAPH_AUTH', value: '{"path": "${langgraphAuthPath}"}' }
  ],
  empty(langgraphHttpAppPath) ? [] : [
    // No enable_custom_route_auth: true here — it crashes the server (apply_middleware
    // can't wrap main.py's include_router routes). Global LANGGRAPH_AUTH still covers them.
    { name: 'LANGGRAPH_HTTP', value: '{"app": "${langgraphHttpAppPath}"}' }
  ]
)

// appEnv (from infra/.env.deploy) leads; bicepPlainEnv + the secretRef/backing
// arrays follow. ACA requires unique env names — deploy.sh's denylist guarantees
// appEnv never carries a name Bicep also sets, so there are no duplicates.
var containerEnv = usePostgres
  ? concat(appEnv, bicepPlainEnv, secretEnv, platformEnv, redisEnv, langgraphConfigEnv, appPostgresEnv)
  : concat(appEnv, bicepPlainEnv, secretEnv, platformEnv, redisEnv, langgraphConfigEnv)

var httpProbes = [
  {
    type: 'Liveness'
    httpGet: {
      path: livenessPath
      port: targetPort
    }
    periodSeconds: 30
  }
  {
    type: 'Readiness'
    httpGet: {
      path: readinessPath
      port: targetPort
    }
    periodSeconds: 30
  }
]

// =============================================================================
// Redis (internal, in-environment) — only when deployRedis = true
// =============================================================================
resource redisApp 'Microsoft.App/containerApps@2024-03-01' = if (deployRedis) {
  name: redisAppName
  location: location
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 6379
        exposedPort: 6379
      }
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: redisImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// =============================================================================
// Postgres (internal, in-environment) — only when deployPostgres = true
// =============================================================================
// Dedicated DB so the platform server (which runs CREATE migrations on boot)
// owns its schema. Avoids the shared managed-PG "permission denied for schema
// public" failure and any table collisions with the chat-agent. Storage is
// ephemeral: a replica restart resets the DB (acceptable for dev; the server
// re-migrates on start). Add an Azure Files volume for durable storage.
resource postgresApp 'Microsoft.App/containerApps@2024-03-01' = if (deployPostgres) {
  name: pgAppName
  location: location
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 5432
        exposedPort: 5432
      }
      secrets: [
        {
          name: 'postgres-password'
          value: postgresPassword
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'postgres'
          image: postgresImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'POSTGRES_USER', value: postgresUser }
            { name: 'POSTGRES_PASSWORD', secretRef: 'postgres-password' }
            { name: 'POSTGRES_DB', value: postgresDb }
            { name: 'PGDATA', value: '/var/lib/postgresql/data/pgdata' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// =============================================================================
// Container App (the agent)
// =============================================================================
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: activeRevisionsMode
      ingress: {
        external: externalIngress
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: acrLoginServer
          identity: uami.id
        }
      ]
      secrets: secrets
    }
    template: {
      // A unique suffix per deploy forces a fresh revision so changes that don't
      // alter the template (rotated Key Vault / DB secret values) are actually
      // picked up. deploy.sh passes a timestamp; empty = ACA auto-generates.
      revisionSuffix: appRevisionSuffix
      containers: [
        {
          name: containerName
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: containerEnv
          probes: enableHttpProbes ? httpProbes : []
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(concurrentRequests)
              }
            }
          }
        ]
      }
    }
  }
  // No explicit dependsOn: the app reaches Redis/Postgres by static in-env DNS,
  // and ACA restarts the app until the sidecars are ready (the platform server
  // retries its DB/Redis startup pings).
}

// =============================================================================
// Outputs
// =============================================================================
@description('Public FQDN of the deployed container app.')
output fqdn string = containerApp.properties.configuration.ingress.fqdn

@description('Full https URL of the deployed container app.')
output appUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'

@description('Image that was deployed.')
output deployedImage string = image

@description('REDIS_URI the app uses (in-env Redis when provisioned, else from Key Vault).')
output redisUriInUse string = deployRedis ? redisUri : 'secretref:redis-url (${redisUrlSecretName})'

@description('DATABASE_URI source (in-env Postgres container when provisioned, else Key Vault secret).')
output databaseUriSource string = deployPostgres ? 'in-env postgres: ${pgAppName}:5432/${postgresDb}' : 'secretref:postgres-dsn (${postgresDsnSecretName})'
