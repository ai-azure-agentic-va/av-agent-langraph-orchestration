# ADF Subagent in Production Orchestration — Design

Date: 2026-07-12
Status: Approved

## Goal

Add an `adf-agent` subagent (ported from the POC at
`avi-agent-langraph-orchestration`) to the production deepagents orchestration,
alongside the existing ServiceNow subagent and the knowledge-base
(`ai_search_tool`) capability. The agent answers Azure Data Factory questions:
listing pipelines, describing pipeline structure/hierarchy, listing runs, and
diagnosing failures — including walking a hierarchical run's full parent→child
run tree to the root cause.

## Scope

- ADF agent only (the POC's Log Analytics agent is NOT ported).
- Multi-factory: the agent picks a factory by friendly name from a configured
  registry; a default factory covers the common case.
- Group-gated like ServiceNow (`ADF_DISABLED_GROUPS`).

## Architecture

New files, following the ServiceNow layout exactly:

- `src/v1/core/tools/adf/tools.py` — six async tools + resource close hook
- `src/v1/core/subagents/adf/subagent.py` — `ADF_SUBAGENT` dict
- `src/v1/core/prompts/adf.py` — `ADF_SUBAGENT_PROMPT`
- `src/v1/core/middlewares/subagent_access.py` — generalized per-subagent
  access gate, replacing `servicenow_access.py`

Touched files:

- `pyproject.toml` — add `azure-mgmt-datafactory`
- `src/v1/core/config.py` — `ADF_FACTORY_MAPPING`, `ADF_DEFAULT_FACTORY`,
  `ADF_DISABLED_GROUPS`
- `src/v1/utils/azure_credentials.py` — async `DefaultAzureCredential`
  singleton (`azure.identity.aio`) + close helper
- `src/v1/core/prompts/orchestrator.py` — base prompt + `ADF_ROUTING_BLOCK`
- `src/v1/core/agent.py` — conditional ADF registration, prompt composition,
  shutdown hook

### Conditional registration

If `ADF_FACTORY_MAPPING` is empty, the ADF subagent is not registered and the
orchestrator prompt contains no ADF text — deployments without ADF behave
exactly as today.

## Config (pydantic Settings)

- `adf_factory_mapping: dict[str, dict[str, str]]`, alias `ADF_FACTORY_MAPPING`.
  JSON mapping friendly alias → factory coordinates:
  `{"finance-dev": {"subscription_id": "…", "resource_group": "…",
  "factory_name": "…"}}`
- `adf_default_factory: str | None`, alias `ADF_DEFAULT_FACTORY`. Alias used
  when the caller does not name a factory. Falls back to the sole mapping entry
  when exactly one factory is configured.
- `adf_disabled_groups: StringList`, alias `ADF_DISABLED_GROUPS`. Same
  semantics as `SERVICENOW_DISABLED_GROUPS`.

## Tools (all async, `azure.mgmt.datafactory.aio`)

Ported from the POC with behavior preserved (run-tree depth 5 / 25-run budget,
failed-branch-only expansion, HTML-stripped error truncation, errors returned
as `[adf-agent]`-prefixed text so the model can react):

1. `list_factories()` — configured factory aliases, marking the default (new)
2. `list_pipelines(factory?)`
3. `list_pipeline_runs(pipeline_name?, last_n_days?, status?, factory?)`
4. `get_pipeline_run_details(run_id, factory?)`
5. `get_pipeline_run_tree(run_id, factory?)`
6. `get_pipeline_structure(pipeline_name, factory?)`

Factory resolution: empty `factory` → default; unknown alias → friendly error
listing valid aliases. Outputs name the factory alias, since results can come
from different factories.

Clients: one cached `aio.DataFactoryManagementClient` per subscription id,
sharing a process-wide `azure.identity.aio.DefaultAzureCredential`.
`close_adf_resources()` closes clients + credential and is wired into
`close_agent_resources()`.

## Subagent

`ADF_SUBAGENT`: name `adf-agent`; description and system prompt extended from
the POC with factory-selection guidance (use the default unless the user names
a factory; `list_factories` when unsure). Tools: the six ADF tools plus
`get_current_datetime` (for time-relative run windows).

## Access gating — `SubagentAccessMiddleware`

The current `ServiceNowAccessMiddleware` drops the whole `task` tool, which
would also kill ADF delegation once a second subagent exists. The generalized
middleware is driven by a gate spec per subagent (name, disabled-groups
setting, restriction note, blocked-call message):

- Caller disabled for SOME gated subagents → keep `task`, append each disabled
  subagent's restriction note to the system message, hard-block `task` calls
  with those `subagent_type`s.
- Caller disabled for ALL registered subagents → drop `task` entirely (today's
  behavior, correctly scoped).
- ServiceNow's tuned restriction-note text is preserved verbatim; the ADF note
  is modeled on it.

## Orchestrator prompt

`ADF_ROUTING_BLOCK` (appended only when ADF is configured) adds: an
`adf-agent` capability bullet (delegate via `task`) for data-pipeline /
pipeline-run questions (names often `pl_*`); routing rules folding ADF into
the existing "ONE capability at a time, never in parallel" discipline; and
data-pipeline topics added to the in-scope list.

## Tests

- `src/v1/test/v1/utils/test_adf_tools.py` — mocked aio client: factory
  resolution (default / named / unknown), run-tree recursion budget, error
  truncation, empty-input help messages.
- `src/v1/test/v1/utils/test_subagent_access.py` — migrates the existing
  ServiceNow gate cases to the new middleware and adds: ADF-only disabled,
  ServiceNow-only disabled (task kept, ADF delegation still works),
  both disabled (task dropped), hard-block per subagent.
- `test_servicenow_access.py` is superseded and removed.
