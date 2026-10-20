from __future__ import annotations

from v1.core.middlewares.adf_result_keep import AdfResultKeepMiddleware
from v1.core.middlewares.source_accumulator import SourceAccumulatorMiddleware
from v1.core.middlewares.user_context import SUBAGENT_GUIDANCE, UserContextMiddleware
from v1.core.prompts import ADF_SUBAGENT_DESCRIPTION, ADF_SUBAGENT_PROMPT
from v1.core.tools import ADF_TOOLS, ai_search_tool

# ``ai_search_tool`` joins the ADF roster so the subagent can resolve WRITTEN
# standards it was not given — above all an SLA threshold, which ADF itself does
# not store. It is the one non-ADF tool here; the prompt bounds it tightly to
# documented thresholds/runbooks and forbids it as a source of pipeline facts.
#
# `all_sources` is copied INTO the subagent (so numbering continues correctly and
# a `[n]` minted here looks plausible), but the subagent's ToolMessages never
# enter the parent thread, so the parent's own accumulator cannot see the
# artifact. The marker survives CitationGuardMiddleware — parent and subagent
# share one per-run registry, keyed on a `run_id` that propagates unchanged into
# the subgraph — so the orchestrator's SubagentSourcesMiddleware puts the
# documents on the `task` result and in `all_sources`, where the UI finds them.
#
# The subagent also runs its OWN SourceAccumulatorMiddleware, which writes its
# documents into its own `all_sources`; deepagents copies that back to the parent
# on return (every state key except messages/todos/structured_response), where
# `merge_sources` unions it into the conversation's running set. That keeps them
# persisted even if the run's registry were evicted.
ADF_SUBAGENT = {
    "name": "adf-agent",
    "description": ADF_SUBAGENT_DESCRIPTION,
    "system_prompt": ADF_SUBAGENT_PROMPT,
    "tools": [*ADF_TOOLS, ai_search_tool],
    # ADF records no per-user owner, so the signed-in-user block is here to make a
    # "my pipelines" ask say it cannot filter by person rather than pass off every
    # run as the user's. The keep reminder rides on the ADF tools' results only
    # (never ai_search_tool's): the lines graded runs found missing were dropped
    # here, before the orchestrator ever saw the answer.
    "middleware": [
        SourceAccumulatorMiddleware(),
        UserContextMiddleware(guidance=SUBAGENT_GUIDANCE),
        AdfResultKeepMiddleware(tool.name for tool in ADF_TOOLS),
    ],
}
