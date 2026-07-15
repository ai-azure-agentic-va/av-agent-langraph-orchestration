# Orchestration Deep-Dive: From Question to Answer

How a user question travels through authentication, the parent orchestrator,
the skills system, and the three capabilities — the knowledge base
(`ai_search_tool`), the ServiceNow subagent, and the ADF subagent — down to the
Azure Data Factory API and back. Written against the code as of 2026-07.

---

## 1. The cast

| Actor | Kind | Where defined | What it owns |
|---|---|---|---|
| **Parent orchestrator** | The deep agent itself (a LangGraph ReAct loop) | `src/v1/core/agent.py` → `create_deep_agent(...)` | Routing, final answer composition, skills, citations |
| **Knowledge base** | A plain **tool** on the parent (not a subagent) | `src/v1/core/tools/ai_search/ai_search.py` | Azure AI Search retrieval, `[n]`-cited grounding text |
| **ServiceNow agent** | **Subagent** (`servicenow-ticket-agent`) | `src/v1/core/subagents/servicenow/subagent.py` | Incident/ticket lookup tools |
| **ADF agent** | **Subagent** (`adf-agent`) | `src/v1/core/subagents/adf/subagent.py` | 6 Data Factory tools + `get_current_datetime` |

Key asymmetry: the KB is a tool the parent calls **inline**; ServiceNow and ADF
are **subagents** — separate agent loops the parent reaches only through one
shared delegation tool named `task`.

---

## 2. Process startup (before any question exists)

`src/v1/api/main.py` — the FastAPI `lifespan` hook runs once at boot and calls
`build_agent()` to warm the process-wide singleton. Everything below happens
**once per process**, not per request:

1. **Checkpointer** — `get_checkpointer(...)`: Postgres (or in-memory when
   `PERSISTENCE_BACKEND=memory`). This is what gives a `thread_id` its
   conversation memory.
2. **Model** — `get_azure_chat_model()`: one shared `AzureChatOpenAI` client
   (managed-identity token providers or API key).
3. **Backend** — `build_backend()` returns a `CompositeBackend`:
   - default route → `StateBackend` (in-memory, per-thread files channel);
   - `"/skills/"` route → `FilesystemBackend(root_dir=src/v1/core/skills,
     virtual_mode=True)`. This is the **mount** that makes the on-disk skills
     library readable by an otherwise in-memory agent, with `..`/`~` traversal
     blocked.
4. **Subagent registration** — conditional:
   ```python
   adf_enabled = bool(settings.adf_factory_mapping)
   subagents = [SERVICENOW_SUBAGENT] + ([ADF_SUBAGENT] if adf_enabled else [])
   system_prompt = SYSTEM_PROMPT + ("\n\n" + ADF_ROUTING_BLOCK if adf_enabled else "")
   ```
   No factories configured → no ADF subagent, no ADF prompt text. The model is
   never told about a capability it doesn't have.
5. **`create_deep_agent(...)`** assembles the graph: it binds the parent's
   tools (`ai_search_tool` + built-in file tools), compiles each subagent spec
   into its **own runnable agent graph**, and builds the middleware stack —
   deepagents' own (`SummarizationMiddleware`, `SkillsMiddleware`,
   `SubAgentMiddleware`, filesystem middleware) plus ours
   (`SafetyGateMiddleware`, `SubagentAccessMiddleware`,
   `CitationFilterMiddleware`). A harness profile strips middleware we don't
   want (`TodoListMiddleware`, `PatchToolCallsMiddleware`,
   `AnthropicPromptCachingMiddleware`).
6. `agent.with_config({"recursion_limit": settings.agent_max_steps})` — the
   hard step ceiling for the parent loop.

---

## 3. Authentication — who is asking?

`src/v1/utils/auth.py` implements **custom LangGraph auth**:

1. Every request carries a JWT (Entra ID). Auth validates the signature
   (JWKS), issuer, audience, expiry.
2. The caller's **Entra groups** are resolved — from token claims and/or via
   Microsoft Graph **on-behalf-of** flow (`v1/utils/graph_groups.py`, which
   caches per-oid with TTL + LRU). Both object-ids *and* display names are kept.
3. Auth stamps the principal as `langgraph_auth_user` (with its `groups`) into
   the run's **config**. From that point on, any tool or middleware inside the
   run can ask "who is calling?" via `groups_from_config()`
   (`v1/utils/group_routing.py`) — it reads
   `config.configurable.langgraph_auth_user.groups`, returning `()` when
   unauthenticated (best-effort, never raises).

Groups drive two per-request behaviors:
- **index routing** — `ai_search_tool` picks the caller's search index;
- **subagent gating** — `SubagentAccessMiddleware` (section 6).

---

## 4. What is a skill, and why do we load one?

**What.** A skill is a *directory with a `SKILL.md` file* (Anthropic's Agent
Skills pattern): YAML frontmatter (`name`, `description`) followed by markdown
instructions. Ours live in `src/v1/core/skills/`:

- **`sttm`** — how to answer source-to-target-mapping / data-lineage questions
  (layer conventions, side-by-side mapping tables, verbatim column names).
- **`message-formatting`** — how the orchestrator must present grounded
  results (document lists, ServiceNow detail cards reproduced verbatim, URL
  rules, `[n]` citation mechanics).

**Why.** Two reasons:

1. **Prompt economy (progressive disclosure).** The full instructions are
   thousands of tokens each. Loading them into the system prompt *always*
   would bloat every model call. Instead the model sees only each skill's
   name + description + path, and reads the full `SKILL.md` **only when the
   question matches** — paying the token cost only on the turns that need it.
2. **Consistency.** Formatting/lineage rules live in one versioned file
   instead of being scattered through the system prompt, so they can evolve
   without touching agent code.

A skill **never grants ability** — it shapes *how* the model uses what tools
already returned. The orchestrator prompt is explicit: skills "never replace
calling `ai_search_tool` for the underlying data."

**Where configured (three touchpoints in the parent):**

| Touchpoint | Code | Role |
|---|---|---|
| Source list | `create_deep_agent(skills=SKILLS_SOURCES)` where `SKILLS_SOURCES = ["/skills/"]` | Tells `SkillsMiddleware` where to enumerate skills |
| Backend mount | `CompositeBackend(routes={"/skills/": FilesystemBackend(...)})` | Makes the virtual path actually resolve to `src/v1/core/skills/` on disk |
| Prompt contract | `prompts/orchestrator.py` | Tells the model *when* to read which skill (STTM questions → `sttm`; rendering results → `message-formatting`) |

Subagents get **no skills** — `SERVICENOW_SUBAGENT` / `ADF_SUBAGENT` specs
define only `name`, `description`, `system_prompt`, `tools`. Skills are a
parent-only concern here.

**How loading works mechanically** (deepagents `SkillsMiddleware`):

1. `before_agent` — at the start of a run, list each source directory through
   the backend, parse every `SKILL.md`'s frontmatter into metadata
   (name/description/path; 10 MB size guard, spec-conformance checks).
2. `wrap_model_call` — append a **"## Skills System"** section to the system
   prompt: the skills list (name, description, `/skills/<name>/SKILL.md`
   path) plus instructions for progressive disclosure ("read the file with
   `read_file(..., limit=1000)` when a skill applies").
3. At answer time, if the question matches a skill, the **model itself**
   calls `read_file` on `/skills/sttm/SKILL.md` (served by the mounted
   `FilesystemBackend`), then follows those instructions when composing.

---

## 5. A question arrives — the granular timeline

Say an internal user asks: *"Why did pl_orchestrator fail last night?"*

**Step 0 — HTTP + auth.** The request hits the LangGraph API server. Custom
auth validates the JWT, resolves groups, stamps `langgraph_auth_user`. The
request names a `thread_id`; the checkpointer loads that thread's prior
messages so the conversation continues where it left off.

**Step 1 — graph resolution.** LangGraph calls the graph factory
(`build_agent`) — which returns the cached singleton (~0 ms after warm-up).

**Step 2 — the parent's model call is assembled.** Middleware wraps the
request in layers; the ones that matter to routing:

- `SummarizationMiddleware` — if the thread has grown too large, older
  history is compacted so the context window never overflows.
- `SafetyGateMiddleware` — safety gate ahead of the model call.
- **`SubagentAccessMiddleware`** — reads `groups_from_config()`, intersects
  with `SERVICENOW_DISABLED_GROUPS` / `ADF_DISABLED_GROUPS`; for a disabled
  subagent it appends that subagent's ACCESS RESTRICTION note; only if
  **every** registered subagent is disabled does it strip the `task` tool
  entirely (defense-in-depth: stray calls are also hard-blocked at execution).
- `SkillsMiddleware` — appends the Skills System section (section 4).
- `SubAgentMiddleware` — injected the `task` tool whose description
  enumerates the registered subagents:
  `- servicenow-ticket-agent: ...` / `- adf-agent: ...`.

**Step 3 — what the model actually sees.** One system prompt =
orchestrator prompt (+ ADF routing block, since ADF is configured)
(+ Skills System section) (+ any restriction notes), and a tool list:
`ai_search_tool`, file tools (`read_file`, ...), and `task`.

**Step 4 — the routing decision.** The prompt's rules:
- KB/documentation question → call `ai_search_tool` directly.
- Incident/ticket question → `task(subagent_type="servicenow-ticket-agent", description=...)`.
- Pipeline/run/Data-Factory question → `task(subagent_type="adf-agent", description=...)`.
- **ONE capability at a time — never in parallel.** Mixed questions
  ("the incident about the pipeline failure") are handled in two *sequential*
  parent steps: one delegation, wait, then the next.

Our question is about a pipeline run → the model emits
`task(description="Diagnose why pl_orchestrator failed last night...", subagent_type="adf-agent")`.

**Step 5 — the `task` tool hands off (deepagents `subagents.py`).**

1. Validates `subagent_type` exists (else returns "the only allowed types are …" as text).
2. `SubagentAccessMiddleware.wrap_tool_call` re-checks the caller's groups —
   a disabled subagent returns an error `ToolMessage` here even if the model
   ignored the prompt note.
3. Builds the subagent's **starting state**: parent state minus excluded/private
   keys, with `messages = [HumanMessage(description)]`. The subagent does
   **not** see the parent's conversation — only the task description the
   parent wrote. This is the isolated context window.
4. Invokes the subagent's own compiled graph.

**Step 6 — inside the ADF agent (its own ReAct loop).** The subagent has its
own system prompt (`prompts/adf.py`) and only its 7 tools. For our question it
typically loops:

| Iteration | Tool call | Why |
|---|---|---|
| 1 | `get_current_datetime()` | resolve "last night" into a window |
| 2 | `list_pipeline_runs(pipeline_name="pl_orchestrator", status="Failed", last_n_days=1)` | find the failed run's `runId` |
| 3 | `get_pipeline_run_tree(run_id="...")` | walk parent → child → grandchild runs to the root-cause activity |
| 4 | *(no tool)* | compose the answer: root-cause activity + error first, then the failure path |

Inside those tools: the factory alias resolves via `ADF_FACTORY_MAPPING`
(default from `ADF_DEFAULT_FACTORY`); one cached
`DataFactoryManagementClient` per subscription authenticates with the shared
async `DefaultAzureCredential` (managed identity in Azure, `az login`
locally); the tree walk is bounded (depth ≤ 5, ≤ 25 runs), expands only
failed branches, and strips/truncates HTML error blobs to 600 chars. Any
Azure error comes back as `[adf-agent] ERROR ...` **text**, so the subagent's
model can read it and adapt instead of crashing the run.

**Step 7 — the handoff back.** The subagent's loop ends when its model
answers without tool calls. The `task` tool takes the **last non-empty AI
message text** and returns it to the parent as a single `ToolMessage` (plus a
merged state update). All the subagent's intermediate tool chatter stays in
its own context and is discarded — the parent sees only the finished summary.

**Step 8 — the parent composes the final answer.** The parent's loop
continues with that ToolMessage in context. If rendering rules apply (lists,
incidents, citations), it first `read_file`s the `message-formatting` skill
and follows it. It answers grounded in what the subagent reported — verbatim
run IDs, statuses, error messages.

**Step 9 — after the answer.** `CitationFilterMiddleware` emits a
`sources_final` event containing only the sources actually cited inline. The
checkpointer persists the turn under the `thread_id`. The
`recursion_limit` (= `AGENT_MAX_STEPS`) has been guarding the whole time — a
runaway tool loop terminates instead of spinning forever.

---

## 6. "The looping" — how the three agents interleave

There is no free-form chatter between agents. It is **strictly nested,
synchronous loops** with the parent always in control:

```
USER QUESTION
   │
   ▼
┌─ PARENT LOOP (orchestrator) ── recursion_limit = AGENT_MAX_STEPS ─────────┐
│                                                                           │
│  model → decides ONE capability for this step                            │
│    │                                                                      │
│    ├── ai_search_tool ───────────► Azure AI Search ──► grounded text ──┐  │
│    │                                                                   │  │
│    ├── task(servicenow-ticket-agent, desc)                             │  │
│    │      └─► SERVICENOW SUBAGENT LOOP (own context, own tools)        │  │
│    │            model ⇄ servicenow tools … final text ─► ToolMessage ──┤  │
│    │                                                                   │  │
│    ├── task(adf-agent, desc)                                           │  │
│    │      └─► ADF SUBAGENT LOOP (own context, own tools)               │  │
│    │            model ⇄ list_pipeline_runs / run_tree …                │  │
│    │            final text ─► ToolMessage ─────────────────────────────┤  │
│    │                                                                   │  │
│    └── read_file(/skills/…/SKILL.md)  (presentation rules only)        │  │
│                                                                        │  │
│  model sees the result ◄───────────────────────────────────────────────┘  │
│  … repeat (sequentially) until it answers with no tool call               │
└────────────────────────────────────────────────────────────────────────────┘
   │
   ▼
FINAL ANSWER  → citation filtering → checkpoint saved
```

Properties worth stating explicitly:

- **A subagent call is one parent step.** While the ADF loop runs, the parent
  is suspended inside its `task` tool call.
- **Isolation.** Each delegation starts the subagent from a fresh
  `HumanMessage(description)`. Subagents are ephemeral: no memory across
  delegations; continuity lives in the parent's thread (checkpointer).
- **Sequencing is prompt-enforced**: one capability per step, never parallel,
  mixed questions handled in sequential steps.
- **The parent never touches ADF/ServiceNow APIs itself** — the tools exist
  only inside the subagents, so the ONLY path is `task` delegation, which is
  exactly the choke point the access middleware gates per caller group.

---

## 7. Appendix — what "the orchestrator rules" actually say

Step 3 of the timeline says the model reads "the orchestrator prompt". That is
`SYSTEM_PROMPT` in `src/v1/core/prompts/orchestrator.py` (~200 lines) — the
parent agent's employee handbook. Its seven rule groups:

1. **Identity & capabilities** — "you are the FIN orchestration agent"; what
   the KB tool and the ServiceNow subagent each cover (the ADF block appends
   the third capability when configured).
2. **Routing** — docs/policy/lineage → `ai_search_tool` (always a fresh
   search, even for follow-ups); tickets → `servicenow-ticket-agent`;
   pipelines → `adf-agent`. Golden rule: **one capability at a time, never in
   parallel**; mixed questions run as sequential steps in a prescribed order.
3. **Delegation coaching** — write complete task descriptions (pass incident
   numbers/criteria through); ask for every match, not a count; if "open
   incidents" is empty, check resolved/closed before reporting none.
4. **Tool discipline** — no todo/shell tools; `read_file` only for skill
   `SKILL.md` files under `/skills/`.
5. **Grounding** — every fact must come from tool output; no model knowledge,
   no gap-filling; "nothing found" is a valid answer; out-of-scope questions
   get a brief clean refusal with no tool call and no "look elsewhere" tips.
6. **Formatting** — markdown, **never tables**; ticket fields and URLs
   verbatim; `[n]` citations reused exactly as returned; every answer ends
   with the exact three-bullet "Want to explore further?" section (except
   refusals). Detailed presentation rules live in the `message-formatting`
   skill.
7. **Scope guardrails** — investigation, not reporting: a request needs an
   operational *subject* (incident, data source, cause, engineer, group);
   subject-less dumps and aggregate metrics are declined toward ServiceNow's
   own reporting tools.

The full system prompt on any model call is therefore:
`SYSTEM_PROMPT` + `ADF_ROUTING_BLOCK` (if ADF configured) + the auto-generated
"## Skills System" section + any per-caller ACCESS RESTRICTION notes.

---

## 8. File map (where to look)

| Concern | File |
|---|---|
| App entry / warm-up / shutdown | `src/v1/api/main.py` |
| Agent assembly, middleware order, conditional ADF | `src/v1/core/agent.py` |
| Auth (JWT, groups, `langgraph_auth_user`) | `src/v1/utils/auth.py`, `src/v1/utils/graph_groups.py` |
| Group extraction inside a run | `src/v1/utils/group_routing.py` |
| Skills library + mount | `src/v1/core/skills/__init__.py`, `src/v1/core/skills/*/SKILL.md` |
| Orchestrator prompt + ADF routing block | `src/v1/core/prompts/orchestrator.py` |
| Per-group subagent gate | `src/v1/core/middlewares/subagent_access.py` |
| ADF subagent spec / prompt / tools | `src/v1/core/subagents/adf/subagent.py`, `src/v1/core/prompts/adf.py`, `src/v1/core/tools/adf/tools.py` |
| ServiceNow counterparts | `src/v1/core/subagents/servicenow/`, `src/v1/core/prompts/servicenow.py`, `src/v1/core/tools/servicenow/tools.py` |
| Config knobs (`ADF_FACTORY_MAPPING`, disabled groups, …) | `src/v1/core/config.py` |
| Offline tests for the gate and ADF tools | `src/v1/test/v1/utils/test_subagent_access.py`, `test_adf_tools.py` |
