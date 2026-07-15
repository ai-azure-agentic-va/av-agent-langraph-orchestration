# ADF Pipeline Hierarchy — Demo Tree and Fix

**Date:** 2026-07-15
**Code:** `src/v1/core/tools/adf/tools.py`, `src/v1/core/prompts/adf.py`
**Tests:** `src/v1/test/v1/utils/test_adf_tools.py` (15/15 pass)
**Factory:** `adf-nfcu-wiki` (alias `nfcu-wiki`, resource group `rg-nfcu-adf-wiki`)

---

## 1. Summary

The ask (from the ADF review call) was:

> Given a pipeline run ID, discover its **child and parent** run IDs, establish the
> **entire hierarchy of that pipeline family**, then look at their run IDs and see
> what state they are in — did they fail as well? There is a **parent ID attribute**
> which is very important.

The agent could already walk a hierarchy **downward** from a root run. It could not
walk **upward**, which is the direction triage actually needs: a "show me failed
pipelines" query returns a *child*, not a root. The parent run ID was being fetched
from Azure on every call and then discarded before the model ever saw it.

Two things were done:

1. **Built a 5-level, 15-pipeline demo tree in `adf-nfcu-wiki`** — nothing in the
   factory was deeper than 2 levels, so the hierarchy logic had never been exercised.
2. **Fixed the agent** to climb from any run to its family root, walk the whole family
   back down, and report accurate failed/succeeded counts.

A pre-existing depth-cap bug was found and fixed in the process (§4.5). It was
silently truncating trees deeper than ~2 levels and reporting wrong counts.

---

## 2. What was created in ADF

15 pipelines, all named `pl_L<level>_*`, all tagged with the annotation
`hierarchy-demo`. **No trigger is attached** — the tree only runs when fired manually,
so it cannot pollute the factory or run up cost.

Every failure is injected with an ADF **Fail activity** (deterministic, no external
dependency, no credentials to expire). Every other step is a **Wait** activity, so a
full run costs nothing and finishes in ~45 seconds.

### Structure

| Level | Pipeline | Invokes | Designed outcome |
|---|---|---|---|
| L1 | `pl_L1_DailyMaster` | L2_Ingest, L2_Transform, L2_Publish | **Failed** (propagated) |
| L2 | `pl_L2_Ingest` | L3_IngestCore, L3_IngestReference | **Failed** (propagated) |
| L2 | `pl_L2_Transform` | L3_BuildDataMart | Succeeded |
| L2 | `pl_L2_Publish` | L3_NotifyDownstream | **Succeeded** — catches its child's failure |
| L3 | `pl_L3_IngestCore` | L4_LoadAccounts, L4_LoadCustomers | **Failed** (propagated) |
| L3 | `pl_L3_IngestReference` | L4_LoadCurrencyRates | Succeeded |
| L3 | `pl_L3_BuildDataMart` | L4_AggregateDaily | Succeeded |
| L3 | `pl_L3_NotifyDownstream` | — (Fail activity, code 5002) | **Failed** — caught, does not propagate |
| L4 | `pl_L4_LoadAccounts` | L5_CopyAccountsFile, L5_ValidateAccounts | **Failed** (propagated) |
| L4 | `pl_L4_LoadCustomers` | L5_CopyCustomersFile | Succeeded |
| L4 | `pl_L4_LoadCurrencyRates` | — | Succeeded |
| L4 | `pl_L4_AggregateDaily` | — | Succeeded |
| L5 | `pl_L5_CopyAccountsFile` | — (Fail activity, code 5001) | **Failed — ROOT CAUSE** |
| L5 | `pl_L5_ValidateAccounts` | — | Succeeded |
| L5 | `pl_L5_CopyCustomersFile` | — | Succeeded |

### The reference run (verified against `az`, 2026-07-15)

Root run ID: **`8c62d2b0-80a1-11f1-9fba-8e76dfbcc945`**

```
L1 pl_L1_DailyMaster        Failed     8c62d2b0-80a1-11f1-9fba-8e76dfbcc945
L2   pl_L2_Ingest           Failed     4002aaa2-6b39-49a3-9f34-6ad76d103d6a
L3     pl_L3_IngestCore     Failed     23192208-c0f5-40f9-82f6-238e57f3db1a
L4       pl_L4_LoadAccounts Failed     e01c90b2-60ac-4548-a984-154a1e298b25
L5         pl_L5_CopyAccountsFile   Failed     6001d4d3-f91f-4e2f-85df-b6b3b3f3ab94  <- ROOT CAUSE
L5         pl_L5_ValidateAccounts   Succeeded  e9030ab2-96eb-409f-a470-bc0f9c5e0271
L4       pl_L4_LoadCustomers        Succeeded  39f00f65-46a7-41f2-85f0-3b3b497ef8be
L5         pl_L5_CopyCustomersFile  Succeeded  828bd26b-70ce-422d-a828-a94953234a12
L3     pl_L3_IngestReference        Succeeded  a7ea43f5-47ad-4be2-94b8-961b6fed7211
L4       pl_L4_LoadCurrencyRates    Succeeded  234cdf58-b49c-45cf-8d81-fa7e6f039de8
L2   pl_L2_Publish                  Succeeded  c745ffa3-a42f-4016-ae28-e0758038d1c3
L3     pl_L3_NotifyDownstream       Failed     e1bcb825-004c-47cb-b120-e3b7cddbfc5a  <- caught by parent
L2   pl_L2_Transform                Succeeded  9ce94224-cde4-47d2-adf0-3706b5822d80
L3     pl_L3_BuildDataMart          Succeeded  8fcb9ce1-6cee-4287-8988-f76775fa668d
L4       pl_L4_AggregateDaily       Succeeded  76b39aee-593c-4ffe-b716-1ac721dab736

15 pipelines | 6 Failed | 9 Succeeded | 2 independent causes | 1 fatal
```

### Why this shape

- **5 levels, root cause at the bottom.** Forces a 4-hop climb. Nothing shallower
  proves the recursion works.
- **9 survivors, one of them adjacent to the failure.** `pl_L5_ValidateAccounts` sits
  beside the failing copy and succeeds. This disproves "a failure fails the entire
  hierarchy" on screen.
- **A caught failure.** `pl_L3_NotifyDownstream` fails, but `pl_L2_Publish` succeeds
  because `MarkNotifyOptional` depends on it with `['Succeeded','Failed']`. So the tree
  has 6 failures but only **2 independent causes**, and only **1** that sank the root.
  This is the same wiring already present in the real `pl_orchestrator`.
- **15 runs** fits inside the 25-run budget; 5 levels sits inside the depth cap.

### Re-running / removing

```bash
# fire a fresh run (~45s)
az datafactory pipeline create-run \
  --resource-group rg-nfcu-adf-wiki --factory-name adf-nfcu-wiki \
  --name pl_L1_DailyMaster

# list just the demo pipelines
az datafactory pipeline list \
  --resource-group rg-nfcu-adf-wiki --factory-name adf-nfcu-wiki \
  --query "[?starts_with(name,'pl_L')].name" -o tsv

# remove them all when finished (delete children last)
```

---

## 3. What was broken

| # | Problem | Where |
|---|---|---|
| 1 | **Parent run ID discarded.** `invoked_by.pipeline_run_id` was fetched on every run object and never rendered — only `.name` and `.invoked_by_type` were printed. The one attribute needed to go upward was invisible to the model. | `tools.py:334`, `tools.py:400` |
| 2 | **No upward traversal.** `get_pipeline_run_tree` only walked down, so it required the root run ID — the one thing triage never starts with. | `get_pipeline_run_tree` |
| 3 | **Counts impossible.** Succeeded child runs were skipped, not expanded, so the family's true failed/succeeded split could not be computed. | `_walk_run_tree` |
| 4 | **Depth cap off by 2×.** (§4.5) | `_walk_run_tree` |
| 5 | **Propagation echo.** ADF re-wraps a child's error into the parent's message *and* the parent's ExecutePipeline activity error, so a 5-level failure printed the same blob 5 times, burying the root cause. | `_walk_run_tree` |

---

## 4. The fix

### 4.1 Surface the parent ID (root-cause fix, one helper)

A single `_invoked_text(run)` helper renders what started a run and appends
`parentRunId=…` when a pipeline did. Both callers that were dropping it now use it, so
the fix lands once rather than at each site:

```
triggeredBy : 9adbf9e9-… (PipelineActivity) parentRunId=e01c90b2-60ac-4548-a984-154a1e298b25
```

`_is_child_run(run)` is the shared predicate: `invoked_by_type == "PipelineActivity"`
and a non-empty `pipeline_run_id`.

### 4.2 Climb to the root

```python
async def _climb_to_root(client, rg, factory_name, run_id):
    run = await client.pipeline_runs.get(rg, factory_name, run_id)
    visited = {run_id}
    while _is_child_run(run):
        parent_id = run.invoked_by.pipeline_run_id
        if parent_id in visited:   # defensive: ADF should never cycle
            break
        visited.add(parent_id)
        run = await client.pipeline_runs.get(rg, factory_name, parent_id)
    return run
```

Terminates when `invoked_by_type` is a trigger (`ScheduleTrigger`, `Manual`, …) rather
than `PipelineActivity`.

**No new tool was added.** `get_pipeline_run_tree` climbs first, then walks down from
the real root — so it now answers from any node, and existing prompt routing is
unchanged. When the caller passed a child, the output says so:

```
run 6001d4d3-… is a CHILD; its family root is pl_L1_DailyMaster
(runId=8c62d2b0-…), started by Manual (Manual)
```

### 4.3 Expand everything, count the family

Succeeded branches are now walked, because "6 failed / 9 succeeded" is only true if the
9 were actually visited. A `Counter` accumulates statuses across the walk:

```
family: 15 pipeline run(s) — 6 Failed, 9 Succeeded
```

If the run budget truncates the walk, the summary says so rather than reporting a
confident wrong number:

```
(partial — run budget reached, counts are lower bounds)
```

Activity-level detail is still failures-only, so a wide ForEach cannot drown the answer.

### 4.4 Suppress the propagation echo

A relay hop is an ExecutePipeline activity that failed *and* has a child run — its error
is just the child's error re-wrapped. Errors now print only where they **originated**;
relay hops are labelled instead:

```
• Exec_L2_Ingest [ExecutePipeline] → Failed (failed because its child run below failed)
```

Output dropped from ~6,000 to **3,217 characters** on the demo tree with no information
lost — each real error (5001, 5002) appears exactly once.

### 4.5 The depth-cap bug (pre-existing)

`_TREE_MAX_DEPTH = 5` did not mean 5 levels. `_walk_run_tree` recursed with `depth + 2`
for indentation while `depth` also gated the cap, so it stopped at **~2.5 pipeline
levels**. Against the demo tree this truncated at **8 of 15 runs and reported
"4 Failed, 4 Succeeded"** — silently wrong, and invisible until a tree deeper than 2
levels existed.

Depth now counts pipeline levels, indentation is derived separately, and the cap is **8**
(clears the deepest real hierarchy with headroom). The run budget, not depth, is what
bounds a wide ForEach. A test pins this so it cannot regress.

### 4.6 Also ported from the POC

`langgraph-rag-agent/src/utils/adf_tools.py` (the working hierarchy demo) was the
reference. Ported: the climb, the family walk, status counts, requested-vs-root
metadata, `parentRunId`, the cycle guard, and run **`parameters`** (which the
traceability doc had marked out of scope — the POC had it).

**Deliberately skipped:** the POC's `ThreadPoolExecutor` parallel child-fetching. The
live call is 8.5s end-to-end sequentially. Add it when a wide family measurably drags.

### 4.7 Prompt changes

`ADF_SUBAGENT_PROMPT` now tells the model how to read a family:

- Several failed runs in one family are usually **one incident echoing upward** — name
  the deepest failed activity as the root cause; do not list ancestors as separate
  problems.
- Failure does **not** spread to siblings or children — report the counts as given.
- A failed child whose parent **succeeded** was caught — call it out separately from
  the root cause.

---

## 5. Four use cases

### UC-1 — Triage from a failed child (the core ask)

**Situation.** `list_pipeline_runs(status='Failed')` returns `pl_L5_CopyAccountsFile`
(`6001d4d3-…`), five levels down. The responder has a symptom and no context.

**Before.** "That run failed with error 5001." No mention that the daily master died.

**After.** The tool climbs 4 hops to `pl_L1_DailyMaster`, returns all 15 runs, and names
the origin. The responder learns the blast radius from the leaf.

### UC-2 — Family health: "did the others fail as well?"

**Situation.** An orchestrator failed. Which of its family actually broke?

**Before.** Impossible — succeeded branches were skipped, so no count existed.

**After.** `family: 15 pipeline run(s) — 6 Failed, 9 Succeeded`. The Transform branch is
visibly untouched, so recovery can be scoped to the Ingest branch instead of rerunning
everything.

### UC-3 — A caught failure must not be misreported

**Situation.** `pl_L3_NotifyDownstream` (`e1bcb825-…`) failed, but its parent
`pl_L2_Publish` **succeeded**.

**Risk.** An agent assuming "a child's failure fails the parent" reports Publish as
broken, or blames the notification for the master's failure. Both are wrong.

**After.** The tree shows a Failed run nested under a Succeeded parent; the prompt
instructs the model to report it as a separate, non-fatal issue. The demo tree exists
specifically to exercise this — the same pattern is already live in `pl_orchestrator`.

### UC-4 — Incident dedup: N failed rows, one cause

**Situation.** A flat failure query over the real factory returns **67 failed runs in
six weeks** — but only **9 real failures**, all from one expired PAT. On the demo tree:
6 failed rows, 2 causes, 1 fatal.

**After.** Relay hops are labelled and each error prints once at its origin, so the
model can collapse the spine into one incident instead of reporting five.

**Caveat:** the tool returns a *tree*, not "2 incidents" — the collapse is still done by
the model via the prompt. See §7.

---

## 6. Four questions to validate it works

Run these against `nfcu-wiki`. Each targets a specific failure mode.

### Q1 — "Why did run `6001d4d3-f91f-4e2f-85df-b6b3b3f3ab94` fail?"

*Tests: the upward climb (UC-1).*

**Expect:** identifies it as a child of `pl_L1_DailyMaster` (`8c62d2b0-…`); names the
root cause as the Fail activity `CopyFailed_SourceMissing`, error 5001, source file
`accounts_20260715.csv` not found.

**Wrong:** answers only about `pl_L5_CopyAccountsFile` without mentioning the master —
the climb didn't happen.

### Q2 — "Show me the whole pipeline family for run `e9030ab2-96eb-409f-a470-bc0f9c5e0271`."

*Tests: the climb from a **succeeded** node.*

This run succeeded. The tool must still climb from it and reveal that its family failed —
proving the climb keys off `invoked_by`, not off status.

**Expect:** the full 15-run family, 6 failed / 9 succeeded, with the root cause in a
sibling branch.

**Wrong:** "that run succeeded, nothing to report."

### Q3 — "How many pipelines failed in the `pl_L1_DailyMaster` run, and what actually caused it?"

*Tests: accurate counts + dedup (UC-2, UC-4).*

**Expect:** 6 of 15 failed, 9 succeeded; **one** root cause (5001 at L5); the four
ancestors failed by propagation, not independently.

**Wrong:** reporting 6 separate problems, or "the whole hierarchy failed" (9 succeeded),
or a count of 8 runs / 4 failed — that last one means the depth cap regressed (§4.5).

### Q4 — "Did `pl_L2_Publish` succeed? Its child failed."

*Tests: the caught-failure trap (UC-3).*

**Expect:** yes, Publish succeeded; `pl_L3_NotifyDownstream` failed with 5002
(endpoint unreachable) but was caught by a downstream activity running on
failure/completion, so it did not propagate. It is a **separate, non-fatal** issue and
is **not** why the master failed.

**Wrong:** claiming Publish failed, or attributing the master's failure to the
notification.

---

## 7. Not done / open questions

1. **Incident grouping lives in the prompt, not the tool.** The tool returns a tree and
   marks relay hops; it does not return "2 incidents". Should incident-level grouping be
   a tool-layer contract? → PM.
2. **`find_pipeline_callers` (traceability #5) still unbuilt.** That is the *static*
   "which pipelines invoke X" question — distinct from the *runtime* parent lookup fixed
   here. Nothing in the review call asked for it.
3. **Wide families are untested.** 25-run budget; a ForEach with hundreds of children
   will truncate and report lower bounds. The demo tree is deep, not wide.
4. **Real hierarchies remain shallow.** `pl_orchestrator` is 2 levels; only the synthetic
   tree is 5. If production orchestrators go deeper than 8, revisit `_TREE_MAX_DEPTH`.

### Related bug worth noting

While verifying, `pipeline-run query-by-factory` returned 100 runs **oldest-first**, so
the demo runs were absent from results entirely. That is the ordering bug
`tools.py:301-303` documents as already fixed in this repo (`RunQueryOrderBy(RunStart,
DESC)`) — but the factory now has enough history to hit it via raw CLI/SDK calls.
Anything querying runs outside these tools needs the same ordering.
