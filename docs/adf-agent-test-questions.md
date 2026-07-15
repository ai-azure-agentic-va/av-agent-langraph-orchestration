# Azure Data Factory Agent — Test Questions

Test date: 2026-07-15 · Server: `langgraph dev` (local) · Model: gpt-5.1
Factories configured: `nfcu-wiki` = adf-nfcu-wiki (default), `nfcu-data` = nfcu-data-factory, `shared-drive` = sharedDrive-to-ADF

Instructions: ask each question in Studio exactly as written, then paste the agent's reply
under **Answer**. Leave **Verdict** blank — it gets filled in during evaluation
(pass / partial / fail + notes).

> **Tooling note (2026-07-15):** after evaluating Q12/Q13, the run tools were upgraded:
> runs are now sorted newest-first (previously the API's oldest-first order was shown as
> "recent"), and the tools now expose the triggering trigger/parent, activity run IDs,
> and a trigger-name filter. Q14+ exercise these new capabilities. Q12/Q13 can optionally
> be re-asked to confirm the newest-first fix.

---

## A. Factory inventory

### Q1. Which Azure Data Factory environments can you query, and which one is the default?

**Answer:**
I can query these Azure Data Factory environments, and this is the default:
Queryable factories:
nfcu-data-factory (display name: nfcu-data)
adf-nfcu-wiki (display name: nfcu-wiki)
sharedDrive-to-ADF (display name: shared-drive)
Default environment:
adf-nfcu-wiki is the default Azure Data Factory I use if you don’t specify a factory.
If you name a specific factory, I’ll target that one; otherwise, I’ll assume adf-nfcu-wiki.
Want to explore further?
Do you want me to list pipelines or runs for a specific factory like adf-nfcu-wiki?
Are you trying to find which factory a particular pipeline belongs to?
Do you want help diagnosing a failed run in one of these environments?

**Verdict:** ✅ PASS — all 3 factories and the default (adf-nfcu-wiki) match `ADF_FACTORY_MAPPING` exactly (verified against Azure).

### Q2. List all the pipelines in the default data factory.

**Answer:**
ask
ID
description	In the default Azure Data Factory environment (adf-nfcu-wiki), list all configured pipelines and return just their names in a simple bullet list.
subagent_type	
ai
In the default data factory (adf-nfcu-wiki), these pipelines are configured:
wiki-backup
pl_wiki_to_blob_sync
pl_fetch_wiki_pages
pl_process_guardrails
pl_sync_to_blob
pl_orchestrator
pl_test_failures
Want to explore further?
Do you want a description of what each of these pipelines does?
Would you like to see recent runs or failures for one of these pipelines?
Are you interested in the activity/child-pipeline structure of a specific pipeline like pl_orchestrator?

**Verdict:** ✅ PASS — all 7 pipeline names verified against the live factory; none missing, none invented.

### Q3. List all the pipelines in the nfcu-data factory.

**Answer:**

task
ID
description	In the Azure Data Factory environment `nfcu-data-factory`, list all configured pipelines and return just their names in a simple bullet list.
subagent_type	adf-agent

In the nfcu-data-factory environment, the configured pipeline(s) are:
PL_Copy_Large_File
Want to explore further?
Do you want details on what PL_Copy_Large_File does step by step?
Would you like to see recent runs or failures for PL_Copy_Large_File?
Are you interested in adding or locating other pipelines in nfcu-data-factory?


**Verdict:** ✅ PASS — PL_Copy_Large_File is indeed the only pipeline in nfcu-data-factory.

### Q4. List all the pipelines in the shared-drive factory.

**Answer:**

```
ai
In the sharedDrive-to-ADF factory (display name: shared-drive), these pipelines are configured:
pl_SharedDriveSync
pl_SyncSingleDrive_Child
pl_SharedDriveSync_Parent
Want to explore further?
Do you want a description of what each shared-drive pipeline does?
Would you like to see recent runs or failures for one of these pipelines?
Are you interested in the parent/child relationship between pl_SharedDriveSync_Parent and pl_SyncSingleDrive_Child?
```

**Verdict:** ✅ PASS — all 3 pipeline names verified.

### Q5. How many pipelines exist in each data factory you have access to?

**Answer:**

Here’s the count of pipelines in each Azure Data Factory environment I can access:
adf-nfcu-wiki (default factory, display name: nfcu-wiki)
Pipelines: 7
Names:
wiki-backup
pl_wiki_to_blob_sync
pl_fetch_wiki_pages
pl_process_guardrails
pl_sync_to_blob
pl_orchestrator
pl_test_failures
nfcu-data-factory (display name: nfcu-data)
Pipelines: 1
Names:
PL_Copy_Large_File
sharedDrive-to-ADF (display name: shared-drive)
Pipelines: 3
Names:
pl_SharedDriveSync
pl_SyncSingleDrive_Child
pl_SharedDriveSync_Parent
Summary of counts:
adf-nfcu-wiki: 7 pipelines
nfcu-data-factory: 1 pipeline
sharedDrive-to-ADF: 3 pipelines
Want to explore further?
Do you want a brief functional description of each pipeline in a specific factory?
Would you like to see recent run history for pipelines in one of these factories?
Are you interested in identifying which pipelines are orchestrators vs simple copy/sync pipelines?

**Verdict:** ✅ PASS — counts 7 / 1 / 3 and every name verified against the live factories.

---

## B. Pipeline structure and hierarchy

### Q6. What does the pipeline `pl_orchestrator` do? Show me its structure.

**Answer:**

Here’s what the pl_orchestrator pipeline in adf-nfcu-wiki does and how it is structured.
Overall Purpose
pl_orchestrator is the main controller for wiki page processing. It:
Fetches wiki pages from the source system.
Builds a list of pages to process.
Loops through each page to:
Run guardrail / business-rule processing.
Sync the processed content to Blob storage.
Marks/logs that the orchestration has completed.
It effectively orchestrates the end-to-end workflow from retrieval to processing to storage.
Structure: Activities and Child Pipelines
1. Step1_FetchWikiPages
Type: Execute Pipeline
Child pipeline: pl_fetch_wiki_pages
Role: Calls the child pipeline that connects to the wiki source and retrieves the pages (and their metadata/content) to be processed.
2. SetPagesForProcessing
Type: Set Variable
Role: Takes the output from pl_fetch_wiki_pages and sets/normalizes it into a variable (list of pages) that will drive the downstream ForEach loop.
3. ProcessEachPage (ForEach loop)
Type: ForEach
Role: Iterates over the collection of pages defined in SetPagesForProcessing. For each page, it runs two child activities:
Inside ProcessEachPage (per page):
3a. Step2_ProcessGuardrails
Type: Execute Pipeline
Child pipeline: pl_process_guardrails
Role: Applies guardrails / business logic to the page (validation, transformation, policy checks, etc.) so the content is clean and compliant before storage.
3b. Step3_SyncToBlob
Type: Execute Pipeline
Child pipeline: pl_sync_to_blob
Role: Writes/synchronizes the processed wiki page into Blob storage (e.g., as files or structured objects in a container).
4. LogOrchestrationComplete
Type: Set Variable
Role: Marks/logs that the orchestration run has finished (for example, setting a status or timestamp variable for monitoring or downstream use).
Structural View (Parent → Child Pipelines)
Parent pipeline: pl_orchestrator
Step1_FetchWikiPages → child: pl_fetch_wiki_pages
SetPagesForProcessing
ProcessEachPage (ForEach over pages)
Step2_ProcessGuardrails → child: pl_process_guardrails
Step3_SyncToBlob → child: pl_sync_to_blob
LogOrchestrationComplete
Want to explore further?
Do you want the structure and purpose of one of the child pipelines, like pl_fetch_wiki_pages or pl_process_guardrails?
Would you like to see recent runs for pl_orchestrator, including any failures and their root-cause activities?
Are you interested in how the page list variable is shaped (schema/fields) between pl_fetch_wiki_pages and the ProcessEachPage loop?

**Verdict:** ✅ PASS — activity names, types, order, ForEach nesting and all 3 child-pipeline references match the real definition exactly. Purpose narrative is reasonable inference from names.

### Q7. Which child pipelines does `pl_orchestrator` invoke?

**Answer:**

pl_orchestrator invokes three child pipelines via Execute Pipeline activities:
pl_fetch_wiki_pages
Invoked by activity: Step1_FetchWikiPages
Role: Fetches wiki pages from the source system.
pl_process_guardrails
Invoked (inside the ForEach) by activity: Step2_ProcessGuardrails
Role: Applies guardrails / business rules to each page.
pl_sync_to_blob
Invoked (inside the ForEach) by activity: Step3_SyncToBlob
Role: Syncs the processed page content to Blob storage.
Want to explore further?
Do you want a detailed breakdown of what pl_fetch_wiki_pages does?
Would you like the activity structure of pl_process_guardrails or pl_sync_to_blob?
Are you interested in how pl_orchestrator passes parameters to these child pipelines?

**Verdict:** ✅ PASS — exactly 3 children with correct invoking activities (Step1/Step2/Step3), verified.

### Q8. Show me the activity tree for the pipeline `pl_fetch_wiki_pages`.

**Answer:**
Here’s the activity tree for the pl_fetch_wiki_pages pipeline in adf-nfcu-wiki:
FetchWikiTree
Type: Web activity (WebActivity)
Role: Calls an external HTTP endpoint to fetch the wiki tree (the list/structure of pages to work with).
Execution: This is the first activity in the pipeline.
SetPagesList
Type: Set Variable (SetVariable)
Role: Takes the output from FetchWikiTree and stores/derives the list of pages into a pipeline variable for downstream use (e.g., by pl_orchestrator).
Execution: Runs after FetchWikiTree completes successfully.
Dependencies / Execution Links
Order:
FetchWikiTree → SetPagesList
Dependency type:
SetPagesList depends on the success of FetchWikiTree.
There are no failure branches, conditionals, loops (ForEach), or Execute Pipeline activities in this pipeline. It’s a simple two-step chain.
Want to explore further?
Do you want to see how the output of pl_fetch_wiki_pages is passed into pl_orchestrator?
Would you like the activity tree or logic for pl_process_guardrails or pl_sync_to_blob?
Are you interested in recent run history or failures for pl_fetch_wiki_pages?


**Verdict:** ✅ PASS — FetchWikiTree [WebActivity] → SetPagesList [SetVariable], no loops/branches/children: matches the real definition.

### Q9. Is `pl_SharedDriveSync_Parent` in the shared-drive factory a hierarchical pipeline? Which pipelines does it call?

**Answer:**
Yes, pl_SharedDriveSync_Parent in the sharedDrive-to-ADF factory is a hierarchical pipeline. It uses an Execute Pipeline activity inside a ForEach loop, making it an orchestrator over a child pipeline.
Here’s how it’s structured and which pipelines it calls:
Structure of pl_SharedDriveSync_Parent
Lookup_SyncConfig
Type: Lookup
Role: Reads the sync configuration — for example, which shared drives to process and any related settings/metadata.
ForEach_Drive
Type: ForEach
Role: Iterates over each drive/config entry returned by Lookup_SyncConfig.
Inside ForEach_Drive:
Execute_ChildSync
Type: ExecutePipeline
Child pipeline: pl_SyncSingleDrive_Child
Role: Runs the child pipeline that performs the sync for a single shared drive, handling the per-drive processing logic that the parent orchestrates.
Child Pipelines Invoked
Activity: Execute_ChildSync
Invoked child pipeline: pl_SyncSingleDrive_Child
Purpose: Executes the sync process for one drive/config item as part of the parent’s loop, making pl_SharedDriveSync_Parent an orchestrator over multiple per-drive sync runs.
So, pl_SharedDriveSync_Parent is hierarchical and it calls pl_SyncSingleDrive_Child as its child pipeline.
Want to explore further?
Do you want the activity tree for pl_SyncSingleDrive_Child to see what it does per drive?
Would you like a step-by-step description of how Lookup_SyncConfig feeds into the ForEach_Drive loop?
Are you interested in recent runs or failure diagnostics for pl_SharedDriveSync_Parent or pl_SyncSingleDrive_Child?

**Verdict:** ✅ PASS — Lookup_SyncConfig → ForEach_Drive → Execute_ChildSync → pl_SyncSingleDrive_Child: matches the real definition exactly.

### Q10. Explain the structure of the pipeline `pl_wiki_to_blob_sync` — what activities does it run and in what order?

**Answer:**

Here’s the structure of the pl_wiki_to_blob_sync pipeline in adf-nfcu-wiki, in execution order, with activity types and roles.
Top-Level Structure
ProcessEachWikiPage
Type: ForEach (loop)
Role: Iterates over a collection of wiki pages. All inner activities run once per page, implementing per‑page synchronization to Blob.
Everything below runs inside ProcessEachWikiPage for each individual page.
Activities Inside ProcessEachWikiPage (per page, in order)
LogCurrentPage
Type: SetVariable
Role: Stores/logs the identifier or name of the current wiki page being processed. Helps with tracking and debugging which page the loop is currently handling.
PIIDetectionGuardrail
Type: SetVariable
Role: Sets a variable or flag related to PII detection for the current page (e.g., enabling PII checks, setting a guardrail status). This is used to govern or record PII-specific guardrail behavior.
ContentSafetyGuardrail
Type: SetVariable
Role: Configures variables that represent content‑safety rules or state (e.g., whether content safety checks are active, or a mode/status for them).
SimulateGuardrailProcessing
Type: Wait
Role: Introduces a deliberate delay to simulate guardrail processing time or throttle throughput. This can be used for testing or to mimic external/computational guardrail work.
CheckIfPageExists
Type: Lookup
Role: Queries a data source (such as metadata or an index) to determine if the current wiki page already exists or to retrieve its existing state. This informs whether the downstream blob operation is a create, update, or potentially a no‑op.
SyncPageToBlob
Type: Copy
Role: Executes the core data‑movement step: copying the wiki page’s content from the source system into Blob storage. This is the actual synchronization of page data to Blob.
MarkPageSyncComplete
Type: SetVariable
Role: Marks at the variable level that the page sync has completed (e.g., status flag, timestamp). Useful for logging, metrics, or any conditional logic in related pipelines.
Control Flow Characteristics
Looping:
A single ForEach loop (ProcessEachWikiPage) drives the per‑page workflow.
Branching / Hierarchy:
Inside this pipeline there are no child pipelines (ExecutePipeline) and no explicit branching activities like If.
The per‑page sequence is linear: Log → Guardrail flags → Wait → Lookup → Copy → Mark complete.
Want to explore further?
Do you want to see how pl_wiki_to_blob_sync relates to pl_orchestrator in the overall wiki processing flow?
Would you like details on the source and sink (datasets/linked services) used by SyncPageToBlob?
Are you interested in how the list of pages driving ProcessEachWikiPage is constructed upstream (e.g., via pl_fetch_wiki_pages)?

**Verdict:** ✅ PASS — all 7 inner activities, their types and order inside the ForEach match the real definition exactly.

### Q11. Compare the structure of `pl_sync_to_blob` and `pl_wiki_to_blob_sync` — how do they differ?

**Answer:**

Here’s how pl_sync_to_blob and pl_wiki_to_blob_sync differ structurally in adf-nfcu-wiki.
Structure of pl_sync_to_blob
Execution order and roles:
GenerateBlobFileName
Type: SetVariable
Role: Builds the target blob path/name for the current wiki page (e.g., using page ID/title) so later activities know exactly where to write in Blob.
CheckGuardrailsStatus
Type: IfCondition
Role: Applies guardrail logic for this single page.
Inside CheckGuardrailsStatus:
SyncPageToBlob (Copy)
Type: Copy
Role: Copies the page content to the blob location defined by GenerateBlobFileName.
MarkSyncSuccess (SetVariable)
Type: SetVariable
Role: Marks a “success” status/flag when the guardrails allow the sync.
MarkSyncSkipped (SetVariable)
Type: SetVariable
Role: Marks the sync as “skipped” when guardrails are not met (e.g., page excluded, conditions not satisfied).
CheckSimulateFailure
Type: IfCondition
Role: Optional failure simulation.
Inside CheckSimulateFailure:
SimulatedSyncFailure (Fail)
Type: Fail
Role: Intentionally fails the pipeline under certain conditions (testing/chaos/alert validation).
Character: A single‑item sync pipeline with explicit guardrail branching and a test‑failure hook.
Structure of pl_wiki_to_blob_sync
You already saw this structure; summarizing in execution order:
ProcessEachWikiPage
Type: ForEach
Role: Loops over a collection of wiki pages; all inner activities run once per page.
Inside ProcessEachWikiPage (per page):
LogCurrentPage
Type: SetVariable
Role: Records which page is being processed for logging/debugging.
PIIDetectionGuardrail
Type: SetVariable
Role: Sets PII‑related guardrail flags or mode for the current page.
ContentSafetyGuardrail
Type: SetVariable
Role: Sets content‑safety guardrail state or mode for the current page.
SimulateGuardrailProcessing
Type: Wait
Role: Simulates guardrail processing time or throttling via a controlled delay.
CheckIfPageExists
Type: Lookup
Role: Looks up page metadata/state (e.g., does it already exist; what is its current status) to inform sync behavior.
SyncPageToBlob
Type: Copy
Role: Copies the page content from the source system into Blob storage.
MarkPageSyncComplete
Type: SetVariable
Role: Marks the page as synced (status/timestamp) for tracking or downstream logic.
Character: A loop‑based batch pipeline that both orchestrates and executes per‑page sync logic.
Key Differences
1. Looping vs Single‑Item
pl_wiki_to_blob_sync
Uses a ForEach (ProcessEachWikiPage) to process multiple pages in one run.
Each iteration runs a full sequence: log page → set guardrail flags → wait → lookup → copy → mark complete.
Acts as a batch orchestrator + executor.
pl_sync_to_blob
Has no ForEach; operates on one page/item per pipeline run.
Assumes the context (page, guardrail inputs) is already scoped to that single item.
Acts as a single‑item worker pipeline.
2. Guardrail Handling
pl_wiki_to_blob_sync
Guardrails are expressed via variables and behavior within the loop:
PIIDetectionGuardrail and ContentSafetyGuardrail set guardrail flags per page.
SimulateGuardrailProcessing (Wait) can represent throttling or simulated guardrail work.
CheckIfPageExists (Lookup) informs sync decisions based on existing state.
Guardrails are more implicit, tied to the sequence of Wait/Lookup/Copy inside the loop.
pl_sync_to_blob
Guardrails are explicit in CheckGuardrailsStatus (IfCondition):
Only when conditions are met does it run SyncPageToBlob and MarkSyncSuccess.
Otherwise, it sets MarkSyncSkipped.
It also has CheckSimulateFailure + SimulatedSyncFailure for deliberate test failures.
This pipeline centralizes guardrail logic in branching activities.
3. Responsibility Split
pl_wiki_to_blob_sync
Responsible for:
Enumerating pages (via its ForEach input).
Applying guardrail flags, delays, and lookups per page.
Doing the copy and marking completion per page.
Combines orchestration and execution for many pages.
pl_sync_to_blob
Responsible for:
Preparing the blob name for a single page.
Applying guardrail branching and marking success/skipped.
Optionally simulating failures.
Focused on the sync logic for one item with stronger, explicit control‑flow semantics.
Summary
pl_wiki_to_blob_sync
Multi‑page, ForEach‑driven orchestration.
Guardrails via per‑page variables, waits, and lookups.
Performs copy and status marking inside the loop.
pl_sync_to_blob
Single‑page, linear pipeline with IfConditions.
Explicit guardrail branching (sync vs skip) and optional simulated failure.
Acts as a targeted worker for one page’s sync operation.
Want to explore further?
Do you want a diagram-style parent→child/loop view of how all wiki-related pipelines (pl_orchestrator, pl_fetch_wiki_pages, pl_wiki_to_blob_sync, pl_sync_to_blob) fit together?
Would you like to know what parameters/context are typically passed into pl_sync_to_blob from its caller?
Are you interested in how failures in pl_sync_to_blob or pl_wiki_to_blob_sync would appear in run history and how to troubleshoot them?

**Verdict:** ✅ PASS — both structures verified, including pl_sync_to_blob's If branches (SyncPageToBlob + MarkSyncSuccess vs MarkSyncSkipped) and the CheckSimulateFailure → SimulatedSyncFailure [Fail] hook. The comparison (loop-batch vs single-item worker) is sound.

---

## C. Run history and status

### Q12. Show me the recent runs of the data factory pipeline `pl_orchestrator`.

**Answer:**

Here are the 10 most recent runs of the pl_orchestrator pipeline in the default factory (adf-nfcu-wiki):
Run ID: 0845fb35-588e-495f-8bad-bf86c5a4e812
Status: Failed
Start: 2026-07-08 02:00:00.168975+00:00
End: 2026-07-08 02:00:00.956756+00:00
Error (root cause):
FetchWikiTree (WebActivity in pl_fetch_wiki_pages): Access Denied: The Personal Access Token used has expired
SyncPageToBlob (Copy in pl_sync_to_blob): 401 Unauthorized due to expired Personal Access Token.
Run ID: d7553b9e-3a51-4e56-a8c8-3dd727316133
Status: Failed
Start: 2026-07-08 04:00:01.111890+00:00
End: 2026-07-08 04:00:02.001752+00:00
Error: Same pattern — FetchWikiTree PAT expired; SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: a80ca47d-5f96-4e26-9e3f-7959cb32fe01
Status: Failed
Start: 2026-07-08 06:00:01.692150+00:00
End: 2026-07-08 06:00:02.637276+00:00
Error: FetchWikiTree access denied (PAT expired); SyncPageToBlob 401 Unauthorized (PAT expired).
Run ID: cf4b34ad-9822-4e39-a283-0695b38c63ce
Status: Failed
Start: 2026-07-08 08:00:01.202171+00:00
End: 2026-07-08 08:00:02.194986+00:00
Error: FetchWikiTree PAT expired; SyncPageToBlob 401 Unauthorized / access denied (expired PAT).
Run ID: ea631a5f-f30e-43e2-b16e-e4b372d274cb
Status: Failed
Start: 2026-07-08 10:00:01.278403+00:00
End: 2026-07-08 10:00:02.210374+00:00
Error: FetchWikiTree PAT expired; SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: e4b8ea40-6146-423b-bcc2-9102e4506362
Status: Failed
Start: 2026-07-08 12:00:01.700053+00:00
End: 2026-07-08 12:00:02.577510+00:00
Error: FetchWikiTree access denied (PAT expired); SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: 2d62f627-a345-4c06-a055-8b4c58b7ef97
Status: Failed
Start: 2026-07-08 14:00:00.151974+00:00
End: 2026-07-08 14:00:01.017809+00:00
Error: FetchWikiTree access denied (PAT expired); SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: 76f33c92-7f3c-45cd-b8bb-59dda8afa59c
Status: Failed
Start: 2026-07-08 16:00:01.606720+00:00
End: 2026-07-08 16:00:02.584656+00:00
Error: FetchWikiTree PAT expired; SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: 19472d8d-25c2-42c6-a34c-342c1e4a8fe0
Status: Failed
Start: 2026-07-08 18:00:00.151196+00:00
End: 2026-07-08 18:00:00.950205+00:00
Error: FetchWikiTree PAT expired; SyncPageToBlob 401 Unauthorized (expired PAT).
Run ID: 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f
Status: Failed
Start: 2026-07-08 20:00:00.245199+00:00
End: 2026-07-08 20:00:01.022110+00:00
Error: FetchWikiTree access denied (Personal Access Token expired); SyncPageToBlob 401 Unauthorized (expired PAT).
Pattern: All recent pl_orchestrator runs are failing due to an expired Personal Access Token (PAT) used by:
FetchWikiTree (WebActivity in pl_fetch_wiki_pages)
SyncPageToBlob (Copy activity in pl_sync_to_blob)
Want to explore further?
Do you want a focused root-cause breakdown of one specific failed run (activity tree with error at each level)?
Would you like guidance on which linked service/credentials in ADF need to be updated to fix the expired PAT issue?
Are you interested in seeing only successful historical runs of pl_orchestrator before the PAT expired?

**Verdict:** ⚠️ PARTIAL — every run ID, start timestamp, status and the expired-PAT root cause is real (all 10 verified against Azure). Two problems: (1) these are the **oldest** runs in the 7-day window (2026-07-08), not the most recent — the trigger fires every 2 hours through today and the true newest run started 2026-07-15 00:00; a tool bug (no sort order → API's oldest-first paging), **fixed 2026-07-15**. (2) The **end times are fabricated**: the model derived them as start + duration÷1000 (e.g. run 8387dc62 really ended 20:12:57 after ~13 min, not 20:00:01) — the tool showed only start + duration in ms and the model mis-converted. The tool's run-details view now returns the real end time; Q14 got it right.

### Q13. List all data factory pipeline runs in the last 7 days in the default factory.

**Answer:**

Here are recent pipeline runs from the last 7 days in the default factory adf-nfcu-wiki (showing the latest runs the tooling returned). All times are UTC.
Pipelines and runs
Pipeline: pl_orchestrator
Multiple runs (e.g., 0845fb35-588e-495f-8bad-bf86c5a4e812, d7553b9e-3a51-4e56-a8c8-3dd727316133, a80ca47d-5f96-4e26-9e3f-7959cb32fe01, cf4b34ad-9822-4e39-a283-0695b38c63ce)
Statuses: All shown runs are Failed
Pattern:
Root cause in each case is an expired Personal Access Token (PAT) used for Azure DevOps wiki REST calls:
FetchWikiTree Web activity fails with “Access Denied: The Personal Access Token used has expired” (error 2108).
Child pl_sync_to_blob runs fail with 401 Unauthorized on wiki REST calls.
Pipeline: pl_wiki_to_blob_sync
Runs such as ce06866d-8e58-473d-bb33-1ccba0aec277, 36c56cf2-a31e-4470-86df-94b83c0d8a26, de665004-6a4d-447c-9c34-72c8db265504, 04023ea8-2846-4426-a5ef-89e0e8af8157
Statuses: Failed
Pattern:
ForEach loop calling SyncPageToBlob Copy activity.
Copy activity repeatedly fails with error 2200, 401 Unauthorized: “Access Denied: The Personal Access Token used has expired” when calling Azure DevOps wiki endpoints.
Pipeline: pl_fetch_wiki_pages
Runs like d30fdce7-1ea4-444e-863e-8bf2967f2b2e, 3dbfe214-9967-42d5-b896-78c0ab063946, 973f6e4f-23b3-4e68-8ef8-9eb245a16f43, 6fa8d0ca-21d2-42c6-8b0a-5c264f91d771
Statuses: Failed
Root cause:
FetchWikiTree Web activity fails with error 2108: “Access Denied: The Personal Access Token used has expired”.
Pipeline: pl_sync_to_blob
Many runs (e.g., 8613726e-9227-484e-b298-5de01b538548, f22cbcb5-d89b-4548-aed9-b227a67a8759, 09d8c3a2-eae2-4cf3-a87f-3f7602425272, 0f7ebc1f-a971-42b6-b31a-e414b84bfcba, d2959c2c-4406-4a26-818d-1fd52263ec7d, b100d332-9863-4375-97c4-0353a470872b, 6b149bec-e1e0-4e13-b817-3b5c3975a089, c528d273-7326-48d9-bae0-b6b4f14db634, 3459f496-7ddd-4771-9dd4-392406210193, 84add39d-88d8-4d4b-b452-41e1d896f669, b51f48d2-063d-42b2-b3e7-901d43e49b80, 5180e412-28c5-41f2-af76-c796add6b984, 47ed54fe-f608-4a52-97ca-910992a46500, adf96943-0667-499c-9b9a-c1f773b9977c)
Statuses: Failed
Root cause:
SyncPageToBlob Copy activity fails with error 2200, 401 Unauthorized: “Access Denied: The Personal Access Token used has expired”.
Failures occur under CheckGuardrailsStatus because the inner Copy fails.
Pipeline: pl_process_guardrails
Runs like 66cf0d73-2891-4861-b50f-e4849853c755, 7e95f5a4-ba51-4dc4-8e85-ec154261fa09, c775c0d5-6766-4cf0-b900-a8f356e4a61c, 9773dcea-42c7-47f1-b063-9cc339ceeccf, 0e190766-cbb5-4cbe-81cb-0abf6f58a60f, f4c3ed18-8de7-46c5-88c3-5a6723f966ed, 5562faf0-1191-49af-a5e8-a80ddf534ffc, 30ada24f-ee0f-4794-b0b6-6c165f62c0c5, aac93559-c95e-4288-aa16-3aa00baaf9b9, 7fc40cda-3049-48a3-a2a2-b90ee02e6ccc, 72ef2335-99cb-49b0-8792-407f1ae32d3a, 17fe681d-2c9d-4d7a-b5ec-a13a032148cf, 08729abe-9c8f-43ab-a938-e8639b3df341
Statuses: Succeeded
Note: Guardrail processing itself is not failing; issues are confined to wiki access and blob sync that depend on the PAT.
Overall observation
Across the last 7 days in adf-nfcu-wiki, the majority of recent runs for the wiki-related pipelines are failing due to a single underlying issue:
The Personal Access Token used for Azure DevOps wiki REST calls has expired, causing:
Web activities (like FetchWikiTree in pl_fetch_wiki_pages) to fail with “Access Denied: The Personal Access Token used has expired”.
Copy activities (like SyncPageToBlob in pl_sync_to_blob and inside pl_wiki_to_blob_sync) to fail with 401 Unauthorized / error 2200.
To restore successful runs, the authentication used by these activities (likely a linked service or more direct auth configuration) will need the PAT updated or replaced with a more durable auth mechanism.
Want to explore further?
Do you want a focused view of just successful runs (e.g., before the PAT expired) for a specific pipeline?
Would you like a step-by-step root-cause trace for one failed run, from pl_orchestrator down through child pipelines?
Are you interested in which linked service or activity configuration is using the expired PAT so you know exactly where to update it?

**Verdict:** ⚠️ PARTIAL — everything stated is accurate: all 8 spot-checked run IDs exist with the claimed pipeline and status, per-pipeline statuses correct (pl_process_guardrails succeeds, the rest fail), and the expired-PAT diagnosis is right. BUT coverage was silently incomplete: the window actually contains **1,056 runs** (96 orchestrator / 96 fetch / 96 wiki-sync / 384 sync-to-blob failed; 384 guardrails succeeded) and the tool saw only the oldest 100-run page. Same tool bug as Q12, fixed 2026-07-15 (newest-first + "more exist" marker in output).

### Q14. Give me the full details of pipeline run `8387dc62-f0d5-4e4a-952b-b8d9079c9b8f` — its status, what triggered it, how long it took, and what each activity did.

*Scenario: query by run ID.*
*Ground truth: pl_orchestrator, Failed, started 2026-07-08 20:00:00 UTC, triggered by `tr_orchestrator_every_2_hours` (ScheduleTrigger); Step1_FetchWikiPages and ProcessEachPage/Step3_SyncToBlob failed (expired Personal Access Token), Step2_ProcessGuardrails iterations succeeded.*

**Answer:**

Here are the full details for pipeline run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f in the default factory adf-nfcu-wiki.
Pipeline run summary
Factory: adf-nfcu-wiki (alias nfcu-wiki)
Pipeline name: pl_orchestrator
Run ID: 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f
Status: Failed
Triggered by: tr_orchestrator_every_2_hours (ScheduleTrigger)
Start time: 2026-07-08 20:00:00.245199+00:00
End time: 2026-07-08 20:12:57.157168+00:00
Duration: 776911 ms (≈ 12 minutes 57 seconds)
Pipeline-level error message (ADF):
Operation on target Step1_FetchWikiPages failed: Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired. ...
Root cause: Access Denied / expired Personal Access Token (PAT) when calling the Azure DevOps wiki.
Activity-level breakdown
The tool output did not include per-activity timestamps, so only status and error/output are available here.
1. Step1_FetchWikiPages
Type: ExecutePipeline
Status: Failed
Activity Run ID: 54985c3d-d36a-45f9-81e2-dbc515d63b6f
Error:
BadRequest: Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired. ...
This is the call into child pipeline pl_fetch_wiki_pages; the child’s Web activity against the wiki fails due to expired PAT.
2. SetPagesForProcessing
Type: SetVariable
Status: Succeeded
Activity Run ID: ee2671bc-7725-4077-b40e-c0cd5d2ff155
Output:
json

{
  "name": "WikiPages",
  "value": [
    "/ServiceNow Knowledge Base",
    "/Qwen3 model",
    "/Getting Started",
    "/API Documentation"
  ]
}

Role: Stores the list of wiki pages that ProcessEachPage will iterate over.
3. ProcessEachPage
Type: ForEach
Status: Failed
Activity Run ID: 99af7834-a07a-465a-8c13-49beb6824e2c
Error:
ActionFailed: Activity failed because an inner activity failed; Inner activity name: Step3_SyncToBlob, Error: Operation on target SyncPageToBlob failed: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError,'Type=Microsoft.DataTransfer.Common.Shared.HybridDeliveryException,Message=Rest call failed with client error, status code 401 Unauthorized, please check your activity settings. Request URL: https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages? . Response: {..."message":"Access De..."}
Role: Loops through each page in WikiPages, orchestrating guardrail processing and sync to Blob. It fails because multiple inner Step3_SyncToBlob executions fail.
Inside ProcessEachPage, the run shows alternating success of guardrail processing and failure of the sync-to-Blob child pipeline:
4. Step2_ProcessGuardrails
Type: ExecutePipeline
Status: Succeeded
Activity Run ID: 618fc8d2-a67f-4ee4-a93c-32c36aeb0bf0
Output:
json

{
  "pipelineName": "pl_process_guardrails",
  "pipelineRunId": "759a288e-f8cc-408c-850d-31e1809f4a87",
  "pipelineReturnValue": {}
}

Role: Runs pl_process_guardrails for one page; this guardrail processing succeeds.
5. Step3_SyncToBlob
Type: ExecutePipeline
Status: Failed
Activity Run ID: 350c32c6-fd88-4076-877c-9f91efed40ef
Error:
BadRequest: Operation on target SyncPageToBlob failed: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError... status code 401 Unauthorized... Response: {..."message":"Access De..."}
Role: Calls child pipeline pl_sync_to_blob to sync the page to Blob; fails due to 401 Unauthorized from the DevOps wiki REST endpoint.
6. Step2_ProcessGuardrails
Type: ExecutePipeline
Status: Succeeded
Activity Run ID: 6149c8d1-917d-41a7-a7a7-63f5a8f3f1e4
Output:
json

{
  "pipelineName": "pl_process_guardrails",
  "pipelineRunId": "d2e762b5-4b35-4984-8348-bd94fa411749",
  "pipelineReturnValue": {}
}

7. Step3_SyncToBlob
Type: ExecutePipeline
Status: Failed
Activity Run ID: c27b7e90-edb1-46b6-926b-f23c4e1940f5
Error: Same pattern—SyncPageToBlob fails with 401 Unauthorized / Access Denied.
8. Step2_ProcessGuardrails
Type: ExecutePipeline
Status: Succeeded
Activity Run ID: b8b30a03-01eb-4828-ae44-8634070d0bd0
Output:
json

{
  "pipelineName": "pl_process_guardrails",
  "pipelineRunId": "93dfff2a-b354-46aa-89eb-ee4c2ce7e614",
  "pipelineReturnValue": {}
}

9. Step3_SyncToBlob
Type: ExecutePipeline
Status: Failed
Activity Run ID: c8233c59-e75b-409c-964f-6917369aa0b0
Error: Same RestCallFailedWithClientError, status 401 Unauthorized.
10. Step2_ProcessGuardrails
Type: ExecutePipeline
Status: Succeeded
Activity Run ID: ea03f14a-8376-468c-ae99-b0ef8ea0d84b
Output:
json

{
  "pipelineName": "pl_process_guardrails",
  "pipelineRunId": "14538f4a-ae6c-45c7-80de-07ba40d82c1a",
  "pipelineReturnValue": {}
}

11. Step3_SyncToBlob
Type: ExecutePipeline
Status: Failed
Activity Run ID: ef93761a-4d0f-4229-b530-ab80614e7af9
Error:
Same 401 Unauthorized / Access Denied error when calling:
https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages?
Run-level root cause
For run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f:
The pipeline was triggered by a scheduled trigger (tr_orchestrator_every_2_hours).
It ran for ~13 minutes and failed.
Two main failure points:
Step1_FetchWikiPages → child pipeline pl_fetch_wiki_pages fails because the FetchWikiTree Web activity hits “Access Denied: The Personal Access Token used has expired”.
ProcessEachPage → multiple Step3_SyncToBlob child runs of pl_sync_to_blob fail with HTTP 401 Unauthorized / Access Denied on the DevOps wiki REST endpoint due to the same expired PAT.
All Step2_ProcessGuardrails executions succeed; the issue is strictly with accessing the wiki via an expired Personal Access Token.
Want to explore further?
Do you want the detailed activity tree for one of the failing child runs of pl_sync_to_blob in this pipeline run?
Would you like help identifying which linked service or credentials need updating to fix the expired PAT issue?
Are you interested in comparing this failed run with the last successful pl_orchestrator run to see what changed?
**Verdict:** ✅ PASS — status, trigger, start/end, duration (776,911 ms ≈ 12m57s) all exactly right, and every one of the 11 activity run IDs matches Azure. The 4 cited pl_process_guardrails child run IDs are real children of this run (verified via invoked-by parent run ID). Root-cause narrative correct.

### Q15. Which trigger started the most recent run of `pl_orchestrator`, and is it a schedule trigger or a manual run?

*Scenario: triggered-by of a run.*
*Ground truth: `tr_orchestrator_every_2_hours`, a ScheduleTrigger (fires every 2 hours; newest run should be from today). Verified working after the tool upgrade.*

**Answer:**

he most recent run of pl_orchestrator in adf-nfcu-wiki was started by:
Trigger name: tr_orchestrator_every_2_hours
Trigger type: ScheduleTrigger (a scheduled trigger, not a manual run)
So the latest execution was automatically started on a schedule rather than manually.
Want to explore further?
Do you want the full details (status, duration, activity breakdown) of that most recent run 30aea282-fb18-400d-b77f-c547b98aa35b?
Would you like to see the schedule configuration pattern implied by tr_orchestrator_every_2_hours?
Are you interested in whether any recent pl_orchestrator runs were triggered manually instead of by the schedule?

**Verdict:** ✅ PASS — trigger name/type correct, and the newest-run reference (30aea282, 2026-07-15 00:00) matches Azure exactly. Confirms the newest-first fix works.

### Q16. List the pipeline runs started by the trigger `tr_orchestrator_every_2_hours` in the last 2 days. What is their success rate?

*Scenario: filter runs by trigger name.*
*Ground truth: ~12 pl_orchestrator runs/day, every 2 hours, all Failed (expired Personal Access Token) — success rate 0%.*

**Answer:**

Here are the pl_orchestrator pipeline runs in adf-nfcu-wiki started by the trigger tr_orchestrator_every_2_hours in the last 2 days, and their success rate.
Runs (last 2 days, trigger = tr_orchestrator_every_2_hours)
Each line: runId | status | start time (UTC)
30aea282-fb18-400d-b77f-c547b98aa35b | Failed | 2026-07-15 00:00:00.700257+00:00
778490df-8227-41ac-bcc8-a83bb4f5b5d5 | Failed | 2026-07-14 22:00:00.902851+00:00
42071a8e-0d5e-42f1-9721-e4bbc7ce9e95 | Failed | 2026-07-14 20:00:00.595411+00:00
42899e8c-16d9-4ff7-bff5-694468f7e5ac | Failed | 2026-07-14 18:00:01.405543+00:00
93a127ce-9fea-409e-be3f-86f6028988ce | Failed | 2026-07-14 16:00:01.848515+00:00
9cb650f1-a783-4605-9c56-9c3b28087611 | Failed | 2026-07-14 14:00:00.515239+00:00
25ddc4ee-609d-4953-a476-309a854bd4f0 | Failed | 2026-07-14 12:00:01.481507+00:00
f8d5a8a6-f31f-4e5d-8e14-daf43d667af2 | Failed | 2026-07-14 10:00:00.938039+00:00
62762307-6b67-4bd0-9c9e-feac622039ed | Failed | 2026-07-14 08:00:00.822515+00:00
96d076e0-a282-414e-b154-efb53ba957da | Failed | 2026-07-14 06:00:00.462041+00:00
a5aef0c2-e357-490b-9d40-f44658533a92 | Failed | 2026-07-14 04:00:00.215388+00:00
6494b259-4181-4c7b-b60b-7e107dd0a3ea | Failed | 2026-07-14 02:00:01.123469+00:00
336bcbe8-d945-466a-b09a-ed164f38714a | Failed | 2026-07-14 00:00:00.626357+00:00
ba0c0cb7-655f-4ece-a701-1e5069de0e22 | Failed | 2026-07-13 22:00:01.483825+00:00
920d0595-bc34-4426-a298-b7ad6245dd90 | Failed | 2026-07-13 20:00:01.574852+00:00
a2587b61-4eff-4db2-a152-5b4b69a467ec | Failed | 2026-07-13 18:00:00.995161+00:00
99a9932e-b1e8-4d09-b103-0df79f47a3a5 | Failed | 2026-07-13 16:00:00.607394+00:00
5be10e7a-895d-467c-9753-5a183f52cefe | Failed | 2026-07-13 14:00:01.364585+00:00
b5efd99e-c90a-440b-a7f9-03aaba529b1b | Failed | 2026-07-13 12:00:00.923296+00:00
47353281-1b56-4bd3-94ed-84775f119e5e | Failed | 2026-07-13 10:00:00.676272+00:00
2f527458-f5de-499c-8dee-583043b71b4f | Failed | 2026-07-13 08:00:01.679946+00:00
d49b7d3e-da9a-46eb-9475-5e40cb13486c | Failed | 2026-07-13 06:00:01.819964+00:00
abb8ba8e-9bb4-426a-87dd-dbe27f5ae671 | Failed | 2026-07-13 04:00:00.463953+00:00
2713633a-6445-4cb0-8478-448c5a586942 | Failed | 2026-07-13 02:00:00.524350+00:00
Success rate
Total runs (last 2 days, trigger tr_orchestrator_every_2_hours): 24
Succeeded: 0
Failed: 24
Success rate: 0 of 24 (0.0%) for pl_orchestrator runs started by tr_orchestrator_every_2_hours in the last 2 days.
Want to explore further?
Do you want a root-cause summary for why these scheduled runs are consistently failing?
Would you like to see the most recent successful pl_orchestrator run to compare configuration and behavior?
Are you interested in a breakdown of which child pipelines/activities are failing within these scheduled runs?

**Verdict:** ✅ PASS — all spot-checked run IDs verified real with correct timestamps (exact 2-hour cadence), all Failed, success rate 0% correct. 24 runs is the correct count for its exact 48-hour window.

### Q17. In pipeline run `8387dc62-f0d5-4e4a-952b-b8d9079c9b8f`, what happened to the activity `Step1_FetchWikiPages` — did it succeed, and if not, why?

*Scenario: query a specific activity by name inside a run.*
*Ground truth: Failed — its child pipeline's FetchWikiTree web activity got "Access Denied: The Personal Access Token used has expired".*

**Answer:**

In pipeline run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f (pipeline pl_orchestrator), the activity Step1_FetchWikiPages did not succeed — it failed.
Here’s exactly what happened:
Activity name: Step1_FetchWikiPages
Type: ExecutePipeline (it calls child pipeline pl_fetch_wiki_pages)
Status in this run: Failed
Reason it failed (error details):
The inner activity FetchWikiTree (a Web activity in pl_fetch_wiki_pages) attempted to call the Azure DevOps wiki API.
That Web activity failed with an Access Denied error because the Personal Access Token (PAT) used had expired.
The ADF error message is of the form:
Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired. ...
Since the child pipeline failed at FetchWikiTree, the parent ExecutePipeline activity Step1_FetchWikiPages was marked Failed for this run.
So, Step1_FetchWikiPages did not succeed; it failed because the underlying wiki call in FetchWikiTree used an expired DevOps PAT, leading to an Access Denied/401-style failure.
Want to explore further?
Do you want the full detail of the pl_fetch_wiki_pages run that was invoked by Step1_FetchWikiPages in this pipeline run?
Would you like help identifying which linked service or configuration holds the expired PAT so it can be updated?
Are you interested in how this failure in Step1_FetchWikiPages cascades through the rest of pl_orchestrator (e.g., impact on downstream activities)?

**Verdict:** ✅ PASS — matches ground truth (Failed; expired Personal Access Token in the child's FetchWikiTree web call).

### Q18. In pipeline run `8387dc62-f0d5-4e4a-952b-b8d9079c9b8f`, find the activity run with ID `54985c3d-d36a-45f9-81e2-dbc515d63b6f`. Which activity is it, what type is it, and what was its outcome?

*Scenario: query by activity run ID.*
*Ground truth: Step1_FetchWikiPages, ExecutePipeline, Failed (expired Personal Access Token in the child pipeline's FetchWikiTree call).*

**Answer:**

In pipeline run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f:
Activity run ID: 54985c3d-d36a-45f9-81e2-dbc515d63b6f
Activity name: Step1_FetchWikiPages
Activity type: ExecutePipeline (it invokes the child pipeline pl_fetch_wiki_pages)
Outcome / status: Failed
Reason for failure:

The child pipeline’s Web activity FetchWikiTree failed when calling the Azure DevOps wiki API with an error like:

Access Denied: The Personal Access Token used has expired
Because FetchWikiTree failed due to an expired Personal Access Token (PAT), the ExecutePipeline activity Step1_FetchWikiPages was marked as Failed in this run.
Want to explore further?
Do you want the full breakdown of the pl_fetch_wiki_pages run that Step1_FetchWikiPages invoked?
Would you like to see all failed activities in this pipeline run and their error messages?
Are you interested in identifying where the expired PAT is configured so it can be updated?

**Verdict:** ✅ PASS — activity run ID correctly resolved to Step1_FetchWikiPages / ExecutePipeline / Failed with the right cause.

---

## D. Failure diagnosis

### Q19. Show me the recent runs of `pl_test_failures` and diagnose why the latest one failed.

*Scenario: run history + root cause for a pipeline built to fail.*

**Answer:**

There are no recent runs of the pipeline pl_test_failures in the default factory adf-nfcu-wiki within the last 7 days, so:
I can’t list recent runs (there are none to show).
There is no latest run to inspect, and therefore no activity tree or root-cause error to diagnose for a failure.
The tooling did not return any pl_test_failures runs in that window.
Want to explore further?
Do you want me to search for pl_test_failures runs over a longer time range (e.g., last 30 or 90 days)?
Would you like to confirm the pipeline name or factory in case pl_test_failures exists in another environment?
Are you interested in diagnosing failures of a different pipeline that you know has recent runs (e.g., pl_orchestrator or pl_sync_to_blob)?
**Verdict:** ✅ PASS — honest and correct: pl_test_failures genuinely has **zero runs in the last 60 days** (verified). The pipeline exists but has never been triggered, so "no runs to diagnose" is the right answer — no hallucinated failures. (Q21 was rewritten accordingly, since its premise assumed pl_test_failures had failed runs.)

### Q20. Walk me through the full parent-to-child run tree of pipeline run `8387dc62-f0d5-4e4a-952b-b8d9079c9b8f` and identify the root cause of the failure.

*Scenario: run tree by run ID.*
*Ground truth: pl_orchestrator → Step1_FetchWikiPages → pl_fetch_wiki_pages (FetchWikiTree fails: expired Personal Access Token) and ProcessEachPage → Step3_SyncToBlob → pl_sync_to_blob (SyncPageToBlob copy fails: 401 from the same expired token).*

**Answer:**

Here’s the full parent→child run tree for pipeline run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f in adf-nfcu-wiki, and the root cause of its failure.
1. Parent pipeline run
Pipeline: pl_orchestrator

Run ID: 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f

Status: Failed
Top-level failure message:

Operation on target Step1_FetchWikiPages failed: Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired.
Key failing activities in pl_orchestrator:
Step1_FetchWikiPages (ExecutePipeline)
Status: Failed
Error:
Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired.
ProcessEachPage (ForEach)
Status: Failed
Error:
ActionFailed: Activity failed because an inner activity failed; Inner activity name: Step3_SyncToBlob, Error:
Operation on target SyncPageToBlob failed: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError, status code 401 Unauthorized …
message: Access Denied: The Personal Access Token used has expired.
Four instances of Step3_SyncToBlob (ExecutePipeline) inside the ForEach
Status: Failed (each)
Error pattern (all four):
Operation on target SyncPageToBlob failed: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError, status code 401 Unauthorized …
message: Access Denied: The Personal Access Token used has expired.
So the parent pipeline fails because both the initial fetch and multiple downstream sync calls are broken.
2. Child pipeline run: pl_fetch_wiki_pages
Invoked by: Step1_FetchWikiPages

Pipeline: pl_fetch_wiki_pages

Run ID: 812a2c3b-ab69-453e-a25f-0b40ce535df4

Status: Failed
Run-level message:

Operation on target FetchWikiTree failed: Access Denied: The Personal Access Token used has expired.
Key failure:
Activity: FetchWikiTree
Type: WebActivity
Status: Failed (multiple logged attempts, all failing)
Error:
Code 2108, message:
Access Denied: The Personal Access Token used has expired.
This is one direct root-cause point: the Web call to Azure DevOps wiki cannot authenticate because the PAT is expired.
3. Child/grandchild pipeline runs: pl_sync_to_blob
pl_orchestrator calls pl_sync_to_blob multiple times via Step3_SyncToBlob within the ForEach; all these runs fail similarly.
Example run 1
Pipeline: pl_sync_to_blob

Run ID: d9e4106d-4636-4a00-a4f7-cb9e56ac7f70

Invoked by: Step3_SyncToBlob

Status: Failed
Run-level message:

Operation on target SyncPageToBlob failed: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError, status code 401 Unauthorized …

message: Access Denied: The Personal Access Token used has expired.
Key activities:
CheckGuardrailsStatus (IfCondition)
Status: Failed
Error:
ActionFailed: Activity failed because an inner activity failed
(secondary failure caused by inner Copy failing).
SyncPageToBlob (Copy) — multiple executions
Status: Failed
Error (each):
2200: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError … status code 401 Unauthorized …
Request URL: https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages?
Response … "message":"Access Denied: The Personal Access Token used has expired."
Other pl_sync_to_blob runs
Additional runs (e.g., f352b55c-4ff1-4fb9-8a2f-828f3f32d761, 3f0cfdfe-a1ad-483d-af45-ca535f043295, 261031dd-aa61-45c2-96c3-f9021c7030b4) show the same pattern:
Run-level failures centered on SyncPageToBlob (Copy)
HTTP 401 Unauthorized from the Azure DevOps wiki endpoint
Response message: “Access Denied: The Personal Access Token used has expired.”
CheckGuardrailsStatus fails only because the inner Copy fails.
So all sync runs are failing at their Copy source.
4. Root-cause identification
Walking the tree from parent to deepest activities:
Parent pl_orchestrator fails at:
Step1_FetchWikiPages → child pl_fetch_wiki_pages → FetchWikiTree (WebActivity)
ProcessEachPage → Step3_SyncToBlob → child pl_sync_to_blob → SyncPageToBlob (Copy)
Child pipelines pl_fetch_wiki_pages and pl_sync_to_blob both fail at REST calls to the Azure DevOps wiki.
The deepest/root-cause activities are:
FetchWikiTree (WebActivity) in pl_fetch_wiki_pages
SyncPageToBlob (Copy) in each pl_sync_to_blob run
These activities directly call the Azure DevOps wiki endpoint and receive:
HTTP 401 Unauthorized
Error message: “Access Denied: The Personal Access Token used has expired.”
Every higher-level failure (ExecutePipeline, ForEach, IfCondition) is a propagation of these authentication errors.
Root-cause summary
The overall failure of pipeline run 8387dc62-f0d5-4e4a-952b-b8d9079c9b8f is caused by an expired Personal Access Token (PAT) used to authenticate Azure DevOps wiki REST calls. Specifically:
FetchWikiTree (WebActivity) in pl_fetch_wiki_pages cannot fetch the wiki tree due to the expired PAT.
SyncPageToBlob (Copy) in pl_sync_to_blob cannot read wiki pages due to the same expired PAT.
Until the PAT (or equivalent auth configuration) is updated in the linked service or activity settings for these DevOps calls, pl_orchestrator and its children will continue to fail.
Want to explore further?
Do you want a targeted checklist of exactly where to update credentials (linked services/headers) to fix this PAT issue?
Would you like a simplified diagram of this parent→child pipeline structure for documentation?
Are you interested in seeing the last successful run of pl_orchestrator to compare behavior before the PAT expired?
**Verdict:** ✅ PASS — the entire tree is real: child run 812a2c3b (pl_fetch_wiki_pages, Failed) and all 4 cited pl_sync_to_blob child runs (d9e4106d, f352b55c, 3f0cfdfe, 261031dd — all Failed) verified as children of 8387dc62 via their invoked-by parent run ID. Root-cause chain (FetchWikiTree web call + SyncPageToBlob copy source, both expired Personal Access Token) is correct.

### Q21. Which activity failed in the most recent failed run of `pl_wiki_to_blob_sync`, and what was the exact error message?

*Scenario: failed activity + exact error text, latest run resolved dynamically (no hard-coded ID).*
*Ground truth: SyncPageToBlob (Copy, inside the ProcessEachWikiPage ForEach) — error 2200, 401 Unauthorized, "Access Denied: The Personal Access Token used has expired". (Rewritten from pl_test_failures, which has zero runs in 60 days — see Q19.)*

**Answer:**

In the most recent failed run of pl_wiki_to_blob_sync in adf-nfcu-wiki:
Pipeline: pl_wiki_to_blob_sync
Run ID: 55f2f2e4-4381-448f-a8fc-035c19a2b208
Status: Failed
Failing activity
Activity name: SyncPageToBlob
Activity type: Copy
Context: Runs inside the ProcessEachWikiPage ForEach; that ForEach fails because SyncPageToBlob fails.
Exact error message from SyncPageToBlob

2200: Failure happened on 'Source' side. ErrorCode=RestCallFailedWithClientError,'Type=Microsoft.DataTransfer.Common.Shared.HybridDeliveryException,Message=Rest call failed with client error, status code 401 Unauthorized, please check your activity settings. Request URL:

https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages?

Response:

{"$id":"1","customProperties":{"Descriptor":null,"IdentityDisplayName":null,"Token":null,"RequestedPermissions":0,"NamespaceId":"00000000-0000-0000-0000-000000000000"},"innerException":null,"message":"Access Denied: The Personal Access Token used has ex…[truncated]
So the most recent failed pl_wiki_to_blob_sync run is failing because SyncPageToBlob cannot read from the Azure DevOps wiki: the REST call gets 401 Unauthorized with an “Access Denied: The Personal Access Token used has expired” message.
Want to explore further?
Do you want me to show the full activity list for that pl_wiki_to_blob_sync run, not just the failing activity?
Would you like a checklist of what to update in ADF (linked services/auth settings) to fix the expired PAT?
Are you interested in comparing this failed run with the last successful run of pl_wiki_to_blob_sync to see what changed?

**Verdict:** ✅ PASS — run 55f2f2e4 verified as the true newest pl_wiki_to_blob_sync run (2026-07-15 00:00, Failed); failing activity, error code 2200, 401 and the expired-token message all match ground truth. Dynamic "most recent" resolution worked correctly post-fix.

### Q22. In the last 3 days, has the activity `SyncPageToBlob` failed in any pipeline runs? Give a couple of example run IDs and the common error.

*Scenario: activity-name failure pattern across runs.*
*Ground truth: yes — it fails in every pl_sync_to_blob and pl_wiki_to_blob_sync run (~40+/day combined), all with 401 / expired Personal Access Token on the wiki REST source.*

**Answer:**
Yes. In the last 3 days in adf-nfcu-wiki, the activity SyncPageToBlob (Copy) has failed in multiple pipeline runs.
Example failed pipeline runs (with SyncPageToBlob failed)
Here are a few example run IDs where SyncPageToBlob failed:
64bd6f8f-e1dc-4e41-b295-f7e725bcef4d
37438ee8-6f13-4b35-9987-4bc7327181fb
5116f464-140d-4d55-aa56-187a71b7e2e5
3dab7c58-3e7a-4fa7-aac3-05395fc92783
9538e007-a560-4273-93c0-8ce8e4dfbe2c
In each of these runs, at least one SyncPageToBlob Copy activity is in a Failed state.
Common error for SyncPageToBlob failures
Across these runs, the failing SyncPageToBlob activities all show the same error pattern:
ADF error code: 2200
Side: “Failure happened on 'Source' side”
Error details:
ErrorCode=RestCallFailedWithClientError
Type=Microsoft.DataTransfer.Common.Shared.HybridDeliveryException
Message (consistent wording):
“Rest call failed with client error, status code 401 Unauthorized, please check your activity settings.”
Request URL similar to:
https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages?...
Response JSON includes a message of the form:
“Access Denied: The Personal Access Token used has expired” (truncated in logs but the wording is consistent).
Summary:

In the last 3 days, SyncPageToBlob has repeatedly failed due to 401 Unauthorized responses from the Azure DevOps wiki endpoint, with the response explicitly indicating that the Personal Access Token (PAT) used has expired.
Want to explore further?
Do you want the full activity details for one specific failed run (e.g., 64bd6f8f-e1dc-4e41-b295-f7e725bcef4d)?
Would you like a concrete list of where the PAT is likely configured in ADF (linked services or activity headers) to help you update it?
Are you interested in seeing if any SyncPageToBlob runs have succeeded recently, to compare their configuration to the failing ones?

**Verdict:** ✅ PASS — all 5 example run IDs verified real: pl_sync_to_blob, Failed, ~2 hours old at time of asking; common error matches ground truth. (Minor scope note: examples were all pl_sync_to_blob runs — SyncPageToBlob also fails inside pl_wiki_to_blob_sync runs, which the answer didn't sample, but the question only asked for "a couple of examples".)

### Q23. For pipeline run `778490df-8227-41ac-bcc8-a83bb4f5b5d5`, list the child pipeline runs it spawned, with each child's run ID and status.

*Scenario: child run IDs of a parent run (run tree) — deliberately a DIFFERENT run than Q14/Q17/Q18/Q20 (this is the pl_orchestrator run of 2026-07-14 22:00, verified real).*
*Ground truth: same pattern expected — one pl_fetch_wiki_pages child (Failed) plus per-page pairs of pl_process_guardrails (Succeeded) and pl_sync_to_blob (Failed) spawned by the ForEach.*

**Answer:**

For pipeline run 778490df-8227-41ac-bcc8-a83bb4f5b5d5 of pl_orchestrator in adf-nfcu-wiki, these are the child pipeline runs spawned via ExecutePipeline activities that are visible in the run tree:
Child pipeline: pl_fetch_wiki_pages
Run ID: 5aee1c02-0a7e-4894-854e-457231704922
Status: Failed
Child pipeline: pl_sync_to_blob
Run ID: 8785b0a5-01f7-40eb-886d-ae5d661a32e6
Status: Failed
Child pipeline: pl_sync_to_blob
Run ID: d9f08781-9b6c-446a-85a4-4d8b69c5002a
Status: Failed
Child pipeline: pl_sync_to_blob
Run ID: 99c6c2b1-1903-4463-8a22-bc817439d4ec
Status: Failed
Child pipeline: pl_sync_to_blob
Run ID: 9538e007-a560-4273-93c0-8ce8e4dfbe2c
Status: Failed
The run tree also indicates there are “4 succeeded child pipeline run(s) not expanded,” but their names and run IDs are not included in the tool output, so I can’t list those specifically.
Want to explore further?
Do you want the detailed activity breakdown and error messages for one of these child runs (e.g., 9538e007-a560-4273-93c0-8ce8e4dfbe2c)?
Would you like a root-cause summary across all failing child runs for 778490df-8227-41ac-bcc8-a83bb4f5b5d5?
Are you interested in comparing this pl_orchestrator run with another run to see differences in child pipeline outcomes?

**Verdict:** ✅ PASS — all 5 listed child runs verified as genuine children of 778490df (invoked-by parent run ID matches), with correct pipelines and statuses. The "4 succeeded child runs not expanded" caveat is accurate and honest — the run-tree tool deliberately doesn't expand succeeded children (those are the pl_process_guardrails runs).

### Q24. Summarize the health of all data factory pipelines over the last 7 days — which succeeded, which failed, and what the failures have in common.

*Scenario: fleet health summary.*
*Ground truth: only pl_process_guardrails succeeds; pl_orchestrator, pl_fetch_wiki_pages, pl_wiki_to_blob_sync, pl_sync_to_blob all fail on the same expired Personal Access Token.*

**Answer:**


Here’s a summary of data factory pipeline health in the default factory adf-nfcu-wiki over the last 7 days: which pipelines are succeeding, which are failing, and what the failures have in common.
Pipelines and their outcomes (last 7 days)
Consistently successful
pl_process_guardrails
Multiple runs in the last 7 days.
All observed runs are Succeeded.
Guardrail processing itself is healthy; no direct errors.
Consistently failing in the inspected window
pl_orchestrator
Many runs (driven by tr_orchestrator_every_2_hours).
All recent runs examined are Failed.
Failures are due to child pipelines (pl_fetch_wiki_pages and pl_sync_to_blob) failing.
pl_wiki_to_blob_sync
Many runs (driven by tr_wiki_sync_every_2_hours).
All recent runs examined are Failed.
Failures align with issues in pl_fetch_wiki_pages and pl_sync_to_blob.
pl_fetch_wiki_pages
Many runs in the last 7 days.
All examined runs are Failed.
Fails at its Web activity calling the Azure DevOps wiki.
pl_sync_to_blob
Many runs in the last 7 days.
All examined runs are Failed.
Fails at its Copy activity reading from the Azure DevOps wiki.
Not run in the last 7 days
wiki-backup
No runs recorded in the last 7 days.
Common root cause of failures
Across pl_orchestrator, pl_wiki_to_blob_sync, pl_fetch_wiki_pages, and pl_sync_to_blob, the failures share the same underlying issue:
1. Expired Azure DevOps Personal Access Token (PAT)
The key failing activities are:
FetchWikiTree (WebActivity) in pl_fetch_wiki_pages
Error examples:
“Access Denied: The Personal Access Token used has expired.”
Error code 2108 from ADF.
This Web activity calls the Azure DevOps wiki REST API to fetch the wiki page tree; the call fails because the PAT is expired.
SyncPageToBlob (Copy) in pl_sync_to_blob (and invoked from pl_wiki_to_blob_sync and pl_orchestrator)
Error pattern:
ADF error code 2200, “Failure happened on 'Source' side.”
ErrorCode=RestCallFailedWithClientError
Message like:
“Rest call failed with client error, status code 401 Unauthorized, please check your activity settings.”
Request URL: https://dev.azure.com/anasadu/NFCU/_apis/wiki/wikis/NFCU.wiki/pages?
Response JSON: "message":"Access Denied: The Personal Access Token used has expired" (truncated but consistent).
These errors occur repeatedly across many runs, so the PAT used for Azure DevOps wiki access is expired and causing all REST-based source operations to fail.
2. How this propagates across pipelines
pl_fetch_wiki_pages fails at FetchWikiTree, so it cannot provide a valid list of pages.
pl_sync_to_blob fails at SyncPageToBlob, so it cannot sync page content to Blob.
pl_orchestrator fails because its ExecutePipeline activities:
Step1_FetchWikiPages (child pl_fetch_wiki_pages) fail due to the expired PAT.
Step3_SyncToBlob (child pl_sync_to_blob) fail due to the same 401 Unauthorized / expired PAT.
pl_wiki_to_blob_sync fails because its ForEach-based sync relies on the same SyncPageToBlob Copy activity and upstream page retrieval.
3. Guardrails pipeline is not the cause
pl_process_guardrails runs successfully and does not show the PAT-related errors.
Where you see an IfCondition like CheckGuardrailsStatus marked ActionFailed, it is failing only because its inner SyncPageToBlob Copy failed (due to the PAT), not because guardrail logic is broken.
Overall health summary (last 7 days)
Healthy / succeeding:
pl_process_guardrails (guardrail processing pipeline)
Unhealthy / failing due to a shared cause:
pl_orchestrator
pl_wiki_to_blob_sync
pl_fetch_wiki_pages
pl_sync_to_blob
Common failure theme:
All failing pipelines ultimately break on Azure DevOps wiki REST calls using an expired Personal Access Token, producing:
401 Unauthorized
“Access Denied: The Personal Access Token used has expired.”
Until the PAT (or equivalent auth configuration) is updated in the linked service / activity settings for those DevOps calls, these pipelines will continue to fail.
Want to explore further?
Do you want a concrete list of the specific activities and linked services where the PAT needs to be updated?
Would you like a timeline view (by trigger) of when these failures started, to correlate with PAT expiry?
Are you interested in recommendations for making wiki access more resilient (e.g., rotating PATs, using service principals, improved monitoring)?
**Verdict:** ✅ PASS — matches ground truth on every count: only pl_process_guardrails succeeds (384/384 in the window); the 4 wiki pipelines all fail on the shared expired-token cause; wiki-backup correctly reported as having no runs (0 in the window); and the newly-cited trigger tr_wiki_sync_every_2_hours verified real. The propagation explanation (which activity fails in which pipeline) is accurate.

---

## E. Date-range queries (added 2026-07-15 with the start_date/end_date tool upgrade)

### Q25. List all the pipelines that had failed runs between July 1 and July 12, 2026 in the adf-nfcu-wiki factory — which pipelines failed and how often?

*Scenario: explicit date-range window (not "last N days").*
*Ground truth (UTC, Jul 1 00:00 – Jul 13 00:00): 1,008 failed runs across exactly 4 pipelines — pl_sync_to_blob 576, pl_orchestrator 144, pl_fetch_wiki_pages 144, pl_wiki_to_blob_sync 144. (For Jul 10–12 only: 252 failed — 144/36/36/36.)*

**Answer:**

Between July 1 and July 12, 2026, in the adf-nfcu-wiki factory, the following pipelines had failed runs:
pl_sync_to_blob, pl_fetch_wiki_pages, pl_wiki_to_blob_sync, pl_orchestrator.
However, the run data available through the tool is truncated to the newest 40 failed runs, and it explicitly indicates that additional failed runs exist in that date range. Because of this truncation, I cannot reliably compute exact failure counts per pipeline for the full July 1–July 12 window.
From the visible subset (mostly around July 12): pl_sync_to_blob – 26, pl_fetch_wiki_pages – 6, pl_wiki_to_blob_sync – 4, pl_orchestrator – 4 — lower bounds, not authoritative counts.

**Verdict:** ✅ PASS — the pipeline set is exactly right (all 4, none missed, none invented), and the truncation honesty is exemplary: it labeled visible counts as lower bounds instead of presenting them as totals. The inability to give exact counts was a tool limitation, not a model failure — fixed the same day (see Q26).

### Q26. Between July 1 and July 12, 2026, how many pipeline runs failed in the default factory? Give exact counts per pipeline.

*Scenario: exact aggregate counts over a date window — requires the 2026-07-15 tool upgrade (full pagination with totals-by-pipeline summary) AND the prompt fix that stops the ServiceNow "no aggregate metrics" decline rule from bleeding into Data Factory questions.*
*Ground truth: pl_sync_to_blob 576, pl_fetch_wiki_pages 144, pl_orchestrator 144, pl_wiki_to_blob_sync 144 (1,008 total).*

**Answer (verified live 2026-07-15):**

```
Here are the failed pipeline run counts in the default factory between 2026-07-01 and 2026-07-12:
- pl_sync_to_blob: 576
- pl_fetch_wiki_pages: 144
- pl_orchestrator: 144
- pl_wiki_to_blob_sync: 144
```

**Verdict:** ✅ PASS — exact match with ground truth on every count. (Note: the first attempt was DECLINED by the orchestrator applying the ServiceNow aggregate-metrics rule to ADF; fixed by clarifying the ADF routing block that run counts/success rates are in-scope diagnostics.)

---

## F. Tricky edge cases (be prepared — added 2026-07-15)

Each entry: the prompt to ask, why it's tricky, and what a GOOD answer looks like.
Grade honesty over completeness: the failure mode to catch is confident invention.

### EC1. Nonexistent but plausible pipeline
**Prompt:** "Show me the recent runs of `pl_ingest_customer_data`."
**Why tricky:** the name looks real; a weak agent invents runs for it.
**Expected:** reports no such pipeline / no runs found; ideally lists the actual pipelines or offers to. No fabricated run history.

**Answer / Verdict:**

### EC2. Fake run ID (valid GUID format)
**Prompt:** "Give me the details of pipeline run `11111111-2222-3333-4444-555555555555`."
**Why tricky:** well-formed GUID; the ADF API returns an error the agent must surface honestly.
**Expected:** states the run wasn't found in the factory; suggests list_pipeline_runs. No invented details.

**Answer / Verdict:**

### EC3. Activity run ID passed as a pipeline run ID
**Prompt:** "Show me the run tree for run `54985c3d-d36a-45f9-81e2-dbc515d63b6f`." (this is an ACTIVITY run ID from run 8387dc62)
**Why tricky:** ID-type confusion — it's a real GUID from this factory, but the wrong kind.
**Expected:** not-found handling, ideally explaining pipeline run ID vs activity run ID and asking which pipeline run it came from.

**Answer / Verdict:**

### EC4. Valid run ID, wrong factory
**Prompt:** "In the shared-drive factory, get the details of run `8387dc62-f0d5-4e4a-952b-b8d9079c9b8f`."
**Why tricky:** the run exists — but in adf-nfcu-wiki. Run IDs are per-factory.
**Expected:** reports it's not found in shared-drive; ideally offers to check the other factories (it IS in the default one).

**Answer / Verdict:**

### EC5. Case-mismatched pipeline name
**Prompt:** "List the runs of `PL_ORCHESTRATOR` this week."
**Why tricky:** the ADF run filter is exact-match; uppercase returns zero runs even though the pipeline exists as pl_orchestrator.
**Expected:** recovers — matches the intended pipeline (e.g. via list_pipelines) rather than claiming it has no runs or doesn't exist.

**Answer / Verdict:**

### EC6. Pipeline exists but has never run
**Prompt:** "Diagnose the latest failure of `pl_test_failures`."
**Why tricky:** the pipeline is real and failure-themed by name; zero runs exist (verified — none in 60+ days).
**Expected:** honest "no runs to diagnose" (already proven in Q19). No hallucinated failure.

**Answer / Verdict:**

### EC7. Future date range
**Prompt:** "List the failed runs between August 1 and August 5, 2026."
**Why tricky:** the window is in the future; zero results guaranteed.
**Expected:** empty result reported plainly; ideally notes the dates are in the future rather than implying the pipelines were healthy.

**Answer / Verdict:**

### EC8. Inverted date range
**Prompt:** "Show failed runs between July 12 and July 10, 2026."
**Why tricky:** start after end silently yields nothing at the API level.
**Expected:** notices the inversion — swaps and answers, or asks — instead of reporting "no failures" (which would be dangerously wrong: there were 252).

**Answer / Verdict:**

### EC9. Timezone trap
**Prompt:** "What failed on July 14th, Eastern Time?"
**Why tricky:** all run timestamps are UTC; the EDT day spans Jul 14 04:00 UTC – Jul 15 04:00 UTC.
**Expected:** handles or at least flags the UTC/EDT boundary rather than silently mixing timezones.

**Answer / Verdict:**

### EC10. Ambiguous activity name (exists in two pipelines)
**Prompt:** "Why is the `SyncPageToBlob` activity failing?"
**Why tricky:** SyncPageToBlob exists in BOTH pl_sync_to_blob and pl_wiki_to_blob_sync.
**Expected:** covers both homes of the activity (root cause is the same expired token) or asks which pipeline — doesn't silently assume one.

**Answer / Verdict:**

### EC11. Write-action request (tools are read-only)
**Prompt:** "Rerun the latest failed run of `pl_orchestrator`." (also try: "cancel the in-progress run", "fix the expired token")
**Why tricky:** the agent has read-only tools; the failure mode is pretending it did it.
**Expected:** clearly states it can only inspect, not rerun/cancel/modify; points to ADF Studio / the pipeline owner. No fake success confirmation.

**Answer / Verdict:**

### EC12. False-premise question
**Prompt:** "Why did `pl_orchestrator` succeed this morning?"
**Why tricky:** it didn't — every run for days has failed. Agreeable models accept the premise and invent reasons for success.
**Expected:** corrects the premise with evidence (latest runs Failed, expired token) instead of explaining a success that never happened.

**Answer / Verdict:**

### EC13. Cross-factory comparison
**Prompt:** "Which of our data factories had the most pipeline failures in the last 7 days?"
**Why tricky:** needs one query per factory, sequentially (the one-capability-at-a-time rule), and two factories may have zero recent runs — which must be reported as zero, not skipped.
**Expected:** checks all three factories and gives a per-factory comparison; adf-nfcu-wiki wins by a wide margin.

**Answer / Verdict:**

### EC14. Two-system handoff (ServiceNow + Data Factory)
**Prompt:** "Is there an open incident about the wiki sync failures, and can you diagnose the underlying pipeline run?"
**Why tricky:** requires sequential delegation to two subagents (never in parallel), then a coherent merged answer.
**Expected:** ServiceNow lookup first (mock mode may return nothing relevant — fine if honest), then the ADF diagnosis; no parallel-call errors, no blended hallucination.

**Answer / Verdict:**

---

## Evaluation summary — COMPLETE (24/24 evaluated)

| Section | Pass | Partial | Fail |
|---|---|---|---|
| A. Factory inventory (Q1–Q5) | 5 | 0 | 0 |
| B. Structure & hierarchy (Q6–Q11) | 6 | 0 | 0 |
| C. Run history & status (Q12–Q18) | 5 (Q14–Q18) | 2 (Q12, Q13 — stale-page tool bug + fabricated end times; both tool issues fixed 2026-07-15) | 0 |
| D. Failure diagnosis (Q19–Q24) | 6 | 0 | 0 |
| **Total** | **22** | **2** | **0** |

Every factual claim in the pass answers was verified directly against Azure (pipeline
definitions, run records, child-run parentage via invoked-by run IDs, activity run IDs,
triggers). Both partials were caused by tool defects, not model reasoning, and both were
fixed the same day — Q14–Q24, answered after the fixes, were fully accurate including
dynamic "most recent" resolution.

Tool/prompt gaps found by this test round (all fixed 2026-07-15):
1. Run listings showed the API's oldest-first page as "recent runs" → now sorted RunStart DESC.
2. Run listings/details lacked trigger info and activity run IDs → now included; `trigger_name` filter added.
3. The trigger filter initially used operand `TriggerName`, which this API rejects → corrected to `TriggeredByName`.
4. Model-side (not tool): Q12 fabricated end times from start + duration÷1000 — mitigated by the details view returning real end times.
5. No explicit date-range support (only "last N days") → `start_date`/`end_date` (YYYY-MM-DD, UTC, inclusive) added (Q25).
6. Counts capped at the first API page (~100) and display capped at 40 → run listing now paginates the whole window (up to 2,000 runs), reporting exact totals by pipeline and status plus the newest 40 rows (Q26).
7. Prompt: the ServiceNow "no aggregate metrics" DECLINE rule bled into Data Factory count questions → ADF routing block now states run counts/success rates are in-scope diagnostics (Q26).
