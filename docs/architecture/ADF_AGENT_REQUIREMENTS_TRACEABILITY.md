# ADF Agent — Requirements Traceability

Each capability from the feature description, mapped to the current code on
`feature/adf-agent`, with open questions for the Product Owner.

Legend: ✅ Met · ⚠️ Partial · 🔵 Met by design parity

Code references:
- Tools: `src/v1/core/tools/adf/tools.py`
- Subagent wiring: `src/v1/core/subagents/adf/subagent.py`
- Prompt: `src/v1/core/prompts/adf.py`

---

## 1. Retrieving pipeline execution status (Succeeded, Failed, Running, Queued, Cancelled)

| Answer (current state) | Questions for Product Owner |
|---|---|
| ✅ **Met.** `list_pipeline_runs(status=…)` filters by status; `get_pipeline_run_details` and `get_pipeline_run_tree` surface `run.status`. Note: Azure's value for "Running" is `InProgress` (not "Running"); the agent prompt already maps user wording to Azure's values. | Should the agent normalize Azure's raw values (`InProgress`, `Canceling`) back to the business vocabulary (`Running`, `Cancelling`) in its answers, or is echoing Azure's exact status acceptable? |

## 2. Fetching pipeline run details by Pipeline Run ID or Pipeline Name

| Answer (current state) | Questions for Product Owner |
|---|---|
| ✅ **Met.** By Run ID: `get_pipeline_run_details(run_id)` / `get_pipeline_run_tree(run_id)`. By Pipeline Name: `list_pipeline_runs(pipeline_name=…)` returns the recent runs, then the agent drills into a specific run. | When a user gives only a pipeline name (many runs), which run should the agent default to — the **most recent**, the **most recent failed**, or should it always ask? What default best serves triage? |

## 3. Identifying the failed activity and its error message

| Answer (current state) | Questions for Product Owner |
|---|---|
| ✅ **Met.** Per-activity name/type/status/error are surfaced flat by `get_pipeline_run_details` and in-tree by `get_pipeline_run_tree`, which leads with the deepest (root-cause) failed activity. Azure error blobs are HTML-stripped and truncated to 600 chars. | Is a **600-char** truncated error message enough for triage, or do incident responders need the **full untruncated** error text (with a link back to the ADF monitoring page for the run)? |

## 4. Parent-child pipeline relationships / dependency chains

| Answer (current state) | Questions for Product Owner |
|---|---|
| ✅ **Met, both directions.** `get_pipeline_structure` shows Execute Pipeline activities and the child pipeline each invokes (static definition). `get_pipeline_run_tree` takes **any** run in a family: it climbs UP via `invoked_by.pipeline_run_id` to the trigger-started root, then walks the whole tree back down (depth 8, 25-run budget), expanding succeeded branches too so the family's "N failed / M succeeded" count is accurate. Verified end-to-end against the 5-level, 15-pipeline `pl_L1_DailyMaster` demo tree in `adf-nfcu-wiki` (6 failed / 9 succeeded). | The run tree caps at **depth 8 / 25 runs**. A wide ForEach (hundreds of children) will hit the 25-run budget and the counts then report as lower bounds — is that acceptable, or must a wide family always render in full? |

## 5. Upstream and downstream pipeline dependencies

| Answer (current state) | Questions for Product Owner |
|---|---|
| ⚠️ **Partial → build in progress.** **Downstream** (what a pipeline invokes) is covered by `get_pipeline_structure`. **Upstream** (which pipelines invoke a given one) has no native ADF API — **PM approved a full-scan approach**: enumerate every pipeline in the factory and parse its Execute Pipeline references to find callers. Tool to be added: `find_pipeline_callers(pipeline_name)`. | (1) Should "upstream" also include **trigger-based** entry points (schedule / event / tumbling-window triggers), or only pipeline-invokes-pipeline links? (2) The full scan is **per-factory** — is cross-factory upstream lookup ever needed? (3) On large factories the scan is many API calls; is a cached/periodic index acceptable, or must it be live every time? |

## 6. Execution history for recent pipeline runs

| Answer (current state) | Questions for Product Owner |
|---|---|
| ✅ **Met.** `list_pipeline_runs(last_n_days=7 default)` with optional pipeline/status filters; output caps at 40 rows with a "narrow your filter" hint. | Is a **7-day** default window and a **40-row** display cap aligned with how far back triage typically looks? Should the agent support paging beyond 40, or is "narrow the filter" sufficient? |

## 7. Pipeline metadata (name, trigger name, integration runtime, execution parameters)

| Answer (current state) | Questions for Product Owner |
|---|---|
| ⚠️ **Partial.** **Pipeline name** ✅. **Trigger name** ✅ (`run.invoked_by`, surfaced by `list_pipeline_runs` / `get_pipeline_run_details` — and when a parent pipeline invoked the run, its `parentRunId` too). **Execution parameters** ✅ (`get_pipeline_run_details`). **Integration runtime** — **not selected**, still out of scope. | Confirm scope: is **integration runtime** genuinely not needed for v1? If a failure is IR-related (e.g. self-hosted IR offline), will responders miss root cause without it? |

## 8. Structured responses consumable by the orchestrator and other agents

| Answer (current state) | Questions for Product Owner |
|---|---|
| 🔵 **Met by design parity** (PM: "do the same as ServiceNow / knowledge-base agent"). At the **subagent → orchestrator boundary all agents return natural language** — there is no JSON schema crossing that boundary for ServiceNow, the knowledge base, or ADF. At the tool layer ADF returns `str` (identical to the knowledge-base `ai_search_tool`); ServiceNow tools return `dict`, but that only affects what each subagent's own model reads, not the orchestrator. No change required. | Is there any **downstream non-LLM consumer** (a dashboard, an automation, a ticketing hook) that will parse ADF output programmatically? If yes, we need a defined **JSON contract** at the tool layer (ServiceNow-style dicts) rather than prose — please confirm. |

---

## Summary

| # | Capability | Status |
|---|-----------|--------|
| 1 | Execution status | ✅ Met |
| 2 | Run details by ID / name | ✅ Met |
| 3 | Failed activity + error | ✅ Met |
| 4 | Parent-child relationships | ✅ Met (up + down, from any run) |
| 5 | Upstream + downstream deps | ⚠️ Downstream met; upstream (static callers) approved, NOT built |
| 6 | Execution history | ✅ Met |
| 7 | Metadata (name/trigger/IR/params) | ⚠️ Name, trigger, parentRunId, params met; IR out of scope |
| 8 | Structured responses | 🔵 Met by parity (NL to orchestrator) |

**Net-new work approved by PM:** (a) `find_pipeline_callers` upstream full-scan tool (#5) — **still not built**; note this is the *static* "which pipelines invoke X" question, distinct from the *runtime* parent lookup in #4, which is now done.

**Open gap (not in the original list, raised in review):** a family's failures are not
**deduplicated into incidents**. A child's failure fails its ancestors, so one root cause
surfaces as N failed runs — the demo tree shows 6 failures from 2 real causes. The tool now
marks relay hops and prints each error only where it originated, but the agent still reasons
about the collapse via the prompt rather than the tool returning "2 incidents". Confirm with PM
whether incident-level grouping belongs in the tool.
