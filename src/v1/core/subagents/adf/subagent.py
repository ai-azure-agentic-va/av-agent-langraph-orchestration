from __future__ import annotations

from v1.core.middlewares.source_accumulator import SourceAccumulatorMiddleware
from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
from v1.core.prompts import ADF_SUBAGENT_DESCRIPTION, ADF_SUBAGENT_PROMPT
from v1.core.tools import ADF_TOOLS, ai_search_tool

# ``ai_search_tool`` joins the ADF roster so the subagent can resolve WRITTEN
# standards it was not given — above all an SLA threshold, which ADF itself does
# not store. It is the one non-ADF tool here; the prompt bounds it tightly to
# documented thresholds/runbooks and forbids it as a source of pipeline facts.
#
# The subagent runs its OWN SourceAccumulatorMiddleware so those searches reach
# "Referenced Sources" like the orchestrator's do. Without it the citation path
# is one-way and silently broken: `all_sources` is copied INTO the subagent (so
# numbering continues correctly and a `[n]` minted here looks plausible), but the
# subagent's ToolMessages never enter the parent thread, so the parent's own
# accumulator cannot see the artifact. The marker would then survive
# CitationGuardMiddleware — parent and subagent share one per-run registry, keyed
# on a `run_id` that propagates unchanged into the subgraph — and render as a
# dead bracket pointing at no panel entry.
#
# Running the accumulator HERE closes the loop: it writes this subagent's
# documents into its own `all_sources`, which deepagents copies back to the
# parent on return (every state key except messages/todos/structured_response),
# where `merge_sources` unions it into the conversation's running set.
ADF_SUBAGENT = {
    "name": "adf-agent",
    "description": ADF_SUBAGENT_DESCRIPTION,
    "system_prompt": ADF_SUBAGENT_PROMPT,
    "tools": [*ADF_TOOLS, ai_search_tool],
    # ADF records no per-user owner, so the signed-in-user block is here to make a
    # "my pipelines" ask say it cannot filter by person rather than pass off every
    # run as the user's.
    "middleware": [
        SourceAccumulatorMiddleware(),
        UserContextMiddleware(guidance=SUBAGENT_GUIDANCE),
    ],
}
