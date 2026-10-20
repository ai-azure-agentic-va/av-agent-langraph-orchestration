---
name: adf-pipeline-context
description: >-
  How the adf-agent answers what a NAMED pipeline is for, who owns it, what
  feeds or consumes it ("tell me about pl_x"), including when no factory has
  it: the ADF facts first, then one knowledge-base search, both reported.
---

# Pipeline context — ADF first, then the documentation, and report BOTH

When the user asks what a NAMED pipeline is for, who owns it, what feeds it or
consumes it, or just "tell me about / give me information about pl_x", the ADF
tools answer only half of it: the factory holds structure and at most a
one-line description, while purpose, owner, lineage and caveats are written
down in the knowledge base. Call get_pipeline_structure FIRST (it returns the
stored description and the activity tree), then run ONE search on the pipeline
name exactly as the tool returned it, or exactly as the user wrote it when the
factory returned nothing.

A MISSING PIPELINE IS A REASON TO SEARCH, NOT A REASON TO SKIP. Never make the
documentation lookup conditional on the pipeline existing in ADF: a pipeline
can be documented before it is built, after it is retired, under a former name,
or while it lives in a factory this deployment has no alias for, and in every
one of those cases the knowledge base is the only place that can answer the
user. Search, then report the two findings SEPARATELY — the ADF fact first
('pl_x does not exist in factory Y'), the documented context after it,
attributed to the documentation, carrying its [n] and clearly marked as
documentation about a pipeline this deployment cannot see. Say plainly that
the two disagree, and name the explanations that fit rather than picking one.
What you must never do is let the document imply the pipeline is live,
runnable or deployed here — a document is evidence about a NAME, never
evidence of a pipeline in the factory. 'Not in ADF' and 'not documented' are
two separate findings and the user needs whichever ones are true: if the
search comes back with nothing relevant either, say the pipeline is neither in
the factory nor documented — never fill the gap from the name or memory.

Do NOT search for live-data questions: pipeline or factory inventory,
incident-to-run correlation, source-system discovery, runs, failures, run
trees, recovery, ETA, runtime, trigger state, or any mutation. The ADF tools
answer those completely and a second call there is pure latency.
