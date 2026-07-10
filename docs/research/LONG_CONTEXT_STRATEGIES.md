# Handling Long-Context Conversations — Strategy Options

**Repo:** `langraph-agent-orchestration` · **Target branch:** `feat/trust`
**Status:** Research / decision doc · **Last updated:** 2026-07-07

> **Purpose.** `feat/trust` exists to keep long conversations from degrading (latency,
> cost, and answer-quality decay as history grows). This doc surveys the full option
> space, judges each against a common set of axes, and recommends a layered approach.
> It is written as a menu of options with pros/cons so we can decide deliberately.

---

## 1. Current baseline (what `feat/trust` already ships)

Long-context handling is **not greenfield** — the branch already implements a compaction
system. Any new work is *marginal on top of this*, not a from-scratch build.

| Component | File | What it does |
|---|---|---|
| `TunedSummarizationMiddleware` | `src/v1/core/middlewares/summarization.py` | Token-based compaction: compact when a request would reach ~60k tokens, keep the most recent ~15k verbatim, feed up to 16k of evicted history to a **cheap summary model** (`gpt-5-mini`), and **offload the full evicted history** to `/conversation_history/{thread_id}.md` on the shared backend. |
| Tool-arg truncation pre-pass | (same, `truncate_args_settings`) | Once the chat crosses ~half the trigger, clips oversized `write_file`/`edit_file` **args** in older messages — often defers a full compaction. |
| `PromptCacheKeyMiddleware` | `src/v1/core/middlewares/prompt_cache.py` | Sets a per-conversation `prompt_cache_key` to lift Azure prompt-cache hit rate. |
| `TokenUsageMiddleware` | `src/v1/core/middlewares/observability.py` | Logs per-turn input/output/total tokens so prefill growth is measurable. |

**Config knobs** (`src/v1/core/config.py`, all env-overridable):

| Env var | Default | Meaning |
|---|---|---|
| `SUMMARIZATION_TRIGGER_TOKENS` | `60000` | Compact once a request would reach this many tokens |
| `SUMMARIZATION_KEEP_TOKENS` | `15000` | Tokens of most-recent turns kept verbatim after compaction |
| `SUMMARIZATION_TRIM_TOKENS` | `16000` | Max tokens of evicted history fed into the summary call |

**Why the tuning was needed:** the deepagents default `SummarizationMiddleware` uses a
*model-aware fraction* trigger — for gpt-5.1 (`max_input_tokens=272000`) that resolves to
`("fraction", 0.85)`, i.e. it lets history grow to **~231k tokens before compacting**. Every
turn re-sends that entire prefill through the reasoning model — the dominant cause of
long-conversation latency *and* quality decay.

**What the underlying SDK (`deepagents.SummarizationMiddleware`) already supports natively:**
- `trigger` as `("tokens", N)` / `("messages", N)` / `("fraction", F)`, a single AND-clause
  `{tokens, messages}`, or a **list of clauses (OR semantics)**.
- `keep` as `("tokens", N)` / `("messages", N)` / `("fraction", F)`.
- `truncate_args_settings` cheap pre-pass.
- Full evicted-history offload to a backend markdown file.

This means several options below are **config-only** rather than new code.

---

## 2. How to judge the options

Every approach trades against five axes. Most "obvious" fixes win one and quietly lose another.

| Axis | What it means | Why it bites us specifically |
|---|---|---|
| **Latency** | Full history re-sent as prefill every turn | Worst with the **gpt-5.1 reasoning model** — big prefill + hidden thinking = slow time-to-first-token |
| **Cost** | Input tokens × number of turns | Grows ~quadratically over a long chat |
| **Quality decay** | Attention dilution / "lost in the middle" / stale instructions | The reason `feat/trust` exists — 231k prefills degraded answers |
| **Hard ceiling** | 272k window → API 400, conversation dies | Rare but catastrophic; a truncation floor prevents it |
| **Information loss** | Can the user still reference old context? | Determines whether an option is "safe" or "lossy" |

Plus two cross-cutting concerns: **determinism/complexity** (does it add non-reproducible moving
parts?) and **where it lives** (backend infra vs frontend UX vs product decision).

---

## 3. Options by family

### Family A — Compaction / Summarization (evolve what we have)

#### A1. Tune the thresholds
Lower `SUMMARIZATION_TRIGGER_TOKENS` / `SUMMARIZATION_KEEP_TOKENS`. Pure config, no code.
- **Pros:** Zero code/risk; already wired; env-only; instantly reversible; full history stays retrievable via the offload file.
- **Cons:** A dial, not a new capability. Too aggressive → summary churn (compacts nearly every turn, each costs a summary LLM call) and lossier summaries; too loose → the latency we're fighting. Summarization itself adds a per-compaction LLM call.
- **Fit:** Native. **Cheapest first move.**

#### A2. Hybrid trigger (tokens *OR* messages)
Add a message-count ceiling alongside the token trigger so a chat with many *small* turns still compacts.
- **Pros:** Catches the "50 tiny turns" case a pure token trigger misses; still native; belt-and-suspenders.
- **Cons:** Two knobs to reason about; marginal if turns are already token-heavy (RAG chunks make them big).
- **Fit:** Native — change the `trigger=` argument in `build_summarization_middleware`.

#### A3. Hierarchical / progressive summarization
Summaries-of-summaries: keep a rolling long-term summary that itself gets re-summarized so it never grows unbounded.
- **Pros:** Bounds the *summary* size too (a flat summary slowly grows over a very long chat); better for marathon sessions.
- **Cons:** Custom code on top of the SDK; compounding information loss (summary of a summary…); harder to debug "why did it forget X."
- **Fit:** Custom. Overkill unless we see genuinely marathon (100+ turn) sessions.

#### A4. Structured summary schema instead of prose
Summarize into a typed object — `{user_goal, open_questions, decisions, entities/IDs mentioned, tool_results_cache}` — rather than free text.
- **Pros:** Far less lossy for what matters in our domain (incident numbers, dataset names, RAW/INT/CUR paths); the model can't "forget" a field; more cache-stable.
- **Cons:** Custom summary prompt + schema; domain-specific (needs tuning); overrides the SDK's summary node.
- **Fit:** Custom, but high-value for the ServiceNow/STTM domain where losing an incident ID is a real failure.

### Family B — Truncation / Sliding window

#### B1. Keep last N messages, drop the rest (no summary)
- **Pros:** Dead simple, deterministic, zero LLM cost, lowest latency, hard bound on context — **the only family that guarantees we never hit the 272k ceiling**. `keep=("messages", N)` is native.
- **Cons:** **Lossy and abrupt** — the model literally cannot answer "what was that incident number 20 turns ago." **Gotcha:** truncation must not split a `tool_call`/`tool_result` pair or drop the system message, or Azure OpenAI 400s. (The SDK's message-count keep respects boundaries; a hand-rolled slice would not.)
- **Fit:** Native as a *keep* policy. Best used as a **floor under summarization**, not instead of it.

#### B2. Keep last N tokens (token sliding window)
Same as B1 but token-budgeted — what `keep=("tokens", 15000)` already does *after* a summary. Could also run standalone.
- **Pros:** Adapts to turn size; predictable prefill.
- **Cons:** Same information loss as B1 if used without a summary.
- **Fit:** Native.

> **Key point:** sliding window *alone* throws away context; sliding window *+ summary*
> (= what we have) keeps the gist. Pure sliding window only makes sense if the domain is
> "recent-turns-only" — ours isn't (follow-ups reference old IDs).

### Family C — Selective retention (keep some message types, drop others)

#### C1. Drop tool outputs, keep human inputs + final answers
- **Pros:** Kills the biggest context hog (AI-search chunks, ServiceNow payloads are 10–50× the size of human turns) while preserving the conversational thread cheaply and deterministically.
- **Cons:** **Two real gotchas.** (1) OpenAI requires every `tool_call` on an assistant message to have a matching `tool` result — you can't delete results and keep the calls, or you get a 400; drop the *pair* or rewrite the assistant message. (2) You lose the actual retrieved facts, so "summarize that doc again" breaks. Good for continuity, bad for grounded recall.
- **Fit:** Custom middleware. Useful as a *pre-pass* to shrink history before summarization decides.

#### C2. Cap the size of *incoming* tool responses
Mirror of the existing arg-truncation: clip giant **tool responses** (AI-search results) once they're a few turns old, replacing the body with a short abstract + a file handle.
- **Pros:** Attacks the actual root cause (RAG chunks dominate token growth) without touching conversational flow; the model rarely needs the full chunk text after answering from it.
- **Cons:** Custom middleware; risk of clipping something a follow-up needed (mitigate by keeping a re-readable file handle).
- **Fit:** Custom, but a natural extension of `truncate_args_settings`. **High leverage for a RAG app.**

#### C3. Relevance-ranked pruning
Score each old message for relevance to the current turn; keep top-K.
- **Pros:** In principle keeps exactly what matters.
- **Cons:** Needs an embedding/scoring pass every turn (latency + cost + non-determinism); basically Family E with extra steps; breaks prompt caching.
- **Fit:** Custom, high complexity. **Not recommended.**

### Family D — Session boundary / handoff

#### D1. Soft nudge — "This chat is getting long, start a fresh one?"
Backend emits a signal at N turns/tokens; frontend shows a suggestion chip.
- **Pros:** Matches ChatGPT/Claude.ai behavior; hard cap on cost/latency without silently dropping anything; user stays in control; clean-slate quality.
- **Cons:** UX friction; requires **frontend (`agent-web-ui`) + backend coordination**; doesn't by itself carry context forward — still needs a summary to seed the new chat (complement to Family A, not a replacement).
- **Fit:** Backend signal + FE work. Product decision.

#### D2. Hard cap — force a new session at the limit
- **Pros:** Absolute guarantee on context size; simplest cost model.
- **Cons:** Most disruptive; users hate being kicked mid-task; still needs carry-over.
- **Fit:** Same as D1 but blunter. Only for a strict cost SLA.

#### D3. Silent auto-fork with a seeded summary
At the threshold, transparently start a new `thread_id` pre-loaded with the compacted summary — the user never sees a boundary.
- **Pros:** Fresh-thread cost/latency with *zero* UX friction; effectively "compaction as a new checkpoint lineage."
- **Cons:** Under the hood this *is* summarization (Family A governs carry-over quality); adds thread-lifecycle bookkeeping and "which thread am I on" complexity for history/audit.
- **Fit:** Backend + checkpointer plumbing. Elegant but not obviously better than in-place compaction.

#### D4. Persist old context to cross-session memory / skill
Write a per-user/per-thread memory doc (the `skills/` dir is already read-mounted markdown) that future sessions can read.
- **Pros:** Enables genuine **long-term / cross-session memory** (personalization), not just single-chat survival; leverages the offload file we already write.
- **Cons:** A *bigger feature than long-context handling* — retrieval, staleness, privacy/PII, per-user scoping. Scope creep if the goal is just "don't degrade at turn 30."
- **Fit:** Custom + product. Powerful, but a separate initiative.

### Family E — Retrieval over conversation history (RAG-on-the-chat)

#### E1. Vector-store all messages, retrieve only relevant ones per turn
- **Pros:** In theory unlimited history with small prefill; recall of specific old facts on demand.
- **Cons:** Heavy — an index write every turn, an embedding/query every turn (latency, cost, infra); retrieval can miss the needed message; **fights prompt-caching** (prefix changes every turn → cache misses); non-deterministic; a second retrieval system alongside AI Search.
- **Fit:** Custom, high complexity. **Not recommended as a primary strategy** for a chat app.

#### E2. On-demand read of the offload file (lightweight E1)
We **already** offload evicted history to `/conversation_history/{thread_id}.md`. Let the model `read_file` it when a follow-up references something old.
- **Pros:** Nearly free — the file already exists; no new infra; the model pulls old context only when it needs it; keeps hot context small.
- **Cons:** Relies on the model deciding to read it (prompt-tunable); the file itself grows unbounded over a marathon chat.
- **Fit:** Native-ish — mostly a **prompt change** telling the orchestrator the file exists. **Cheap, underused lever we already paid for.**

### Family F — Architectural context isolation

#### F1. Subagent delegation / context quarantine
Route token-heavy work (retrieval, ServiceNow) into **subagents** whose long tool traffic never lands in the main thread — already done for ServiceNow + AI search.
- **Pros:** Structurally the most effective — the orchestrator thread stays lean because the mess lives in ephemeral subagent contexts; compounds with everything else.
- **Cons:** Architectural; only helps the *tool-traffic* portion, not human/AI conversational turns; more subagents = more orchestration latency.
- **Fit:** Native pattern in deepagents; already on this path. **Lean into it** (keep AI-search bulk text inside a subagent).

#### F2. Offload big payloads to files, pass handles
Instead of inlining a large tool result into the message stream, write it to the backend and pass a short reference the agent can re-read.
- **Pros:** Same win as C2; keeps the message stream small by construction.
- **Cons:** Custom; indirection the model must understand.
- **Fit:** Custom; complements C2/E2.

### Family G — Cost/latency mitigations (don't shrink context, soften the pain)

- **G1. Prompt caching** — `PromptCacheKeyMiddleware` (already on). Cuts *cost & latency* of re-sending the stable prefix, but does **nothing** for quality decay or the hard ceiling. Note: aggressive per-turn pruning (C3/E1) *breaks* cache hits — real tension.
- **G2. `reasoning_effort` + cheap summary model** — both already wired (`AI_LLM_REASONING_EFFORT`, `gpt-5-mini` summary). Latency levers, not context levers.

These are complements, not alternatives — keep them.

---

## 4. Comparison matrix

Legend: ✅ good · ✅✅ excellent · ⚠️ partial/depends · ❌ poor · — n/a

| Option | Latency | Cost | Quality | Prevents 272k ceiling | Info loss | Effort | Native? |
|---|---|---|---|---|---|---|---|
| A1 Tune thresholds | ✅ | ✅ | ⚠️ | ⚠️ | Low (offloaded) | **Config** | ✅ |
| A2 Hybrid trigger | ✅ | ✅ | ⚠️ | ⚠️ | Low | Config | ✅ |
| A3 Hierarchical summary | ✅ | ✅ | ⚠️ | ✅ | Med | High | ❌ |
| A4 Structured summary | ✅ | ✅ | ✅ | ⚠️ | **Lowest** | Med | ❌ |
| B1/B2 Sliding window | ✅✅ | ✅✅ | ❌ | ✅✅ | **High** | Low | ✅ |
| C1 Drop tool outputs | ✅ | ✅ | ⚠️ | ⚠️ | Med (facts) | Med | ❌ |
| C2 Cap tool responses | ✅ | ✅ | ✅ | ⚠️ | Low | Med | ~ |
| C3 Relevance pruning | ⚠️ | ⚠️ | ⚠️ | ⚠️ | Med | High | ❌ |
| D1/D2 Session nudge/cap | ✅✅ | ✅✅ | ✅ | ✅✅ | Med (needs carry-over) | Med (FE+BE) | ❌ |
| D3 Silent auto-fork | ✅✅ | ✅✅ | ✅ | ✅✅ | Low | High | ❌ |
| D4 Cross-session memory | ✅ | ✅ | ✅ | ✅ | **None** | High | ❌ |
| E1 Vector RAG on chat | ⚠️ | ⚠️ | ⚠️ | ✅ | Low | High | ❌ |
| E2 On-demand file read | ✅ | ✅ | ✅ | ⚠️ | **None** | **Low (prompt)** | ✅ |
| F1 Subagent isolation | ✅✅ | ✅ | ✅ | ✅ | None | Arch | ✅ |
| F2 Offload payloads → handles | ✅✅ | ✅ | ✅ | ✅ | Low | Med | ❌ |
| G1/G2 Cache / effort | ✅ | ✅ | — | ❌ | None | Done | ✅ |

---

## 5. Recommendation: layered defense, not one silver bullet

No single option wins all five axes — the right answer is a **stack**, cheapest-first, where each
layer catches what the one above missed. In order of ROI:

1. **Keep summarization as the backbone (Family A)** — the only lossless-*enough* general solution.
   **Tune it (A1) + add a message-count OR-trigger (A2)** first; both are config / one-line.
2. **Add a hard sliding-window floor (B1, `keep=("messages", N)`)** *under* the summarizer — the
   guarantee we never hit 272k, at ~zero cost.
3. **Cap aged tool responses (C2)** — highest-leverage custom piece for a RAG app; the search
   chunks are the real token hog, and it extends the arg-truncation pass we already run.
4. **Turn on the offload file for recall (E2)** — a prompt change telling the orchestrator it can
   `read_file` the history it already writes. Recovers the info-loss downside of layers 2–3 for
   near-free.
5. **Then, if cost/UX still demands it, add a soft session nudge (D1)** as the product-level
   backstop — but only *after* the infra layers, and only if data shows sessions still run away.

**De-prioritize:** pure sliding window with no summary (too lossy for our ID-heavy domain),
relevance-ranked pruning (C3) and vector-RAG-on-chat (E1) (heavy, non-deterministic, break
caching), and cross-session memory (D4) — a worthy but *separate* product initiative, not a fix
for turn-30 degradation.

**Measure it.** `TokenUsageMiddleware` is already in place. A small long-conversation eval
(needle-recall over a ~40-turn chat + follow-up accuracy) is what tells us whether a threshold is
too aggressive. Build this before/alongside tuning so changes are A/B-comparable.

---

## 6. Open decisions

- **Cost/latency SLA:** is there a hard per-conversation token/latency budget? (Decides whether D1/D2 session caps are needed at all.)
- **Recall expectation:** must the assistant reliably recall an ID/path from 20+ turns ago? (If yes → E2 + A4 matter a lot; if no → sliding window is cheaper.)
- **Marathon sessions:** do real users run 100+ turn chats, or is 20–40 the ceiling? (Decides A3 hierarchical vs flat.)
- **Frontend appetite:** is the `agent-web-ui` team available for session-boundary UX (D1)? (Backend-only options avoid this dependency.)
- **Cross-session memory:** is persistent per-user memory (D4) a roadmap item, or explicitly out of scope for this branch?
