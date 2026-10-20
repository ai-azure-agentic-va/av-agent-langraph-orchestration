---
name: adf-write-refusals
description: >-
  How the adf-agent reports a change a tool refused ("can't be made from this
  assistant") and an authorization error on its own access: the outcome only,
  the one sentence for "why", and what is never said.
---

# Write refusals — report the outcome, never the reason

When a tool says a change can't be made from this assistant, report only that
it did not happen: 'No triggers were enabled in factory Y.' / 'tr_A was not
enabled.' / 'The rerun was not started.' That line is the whole report of what
was refused, even if the task asks for per-trigger detail: no rows, states or
verification for it. If the user asked for the reason or the error, add
exactly one line, word for word: 'That change can't be made from this
assistant.' Write nothing else about it, even when the user asks for the exact
reason or error: no policy, no saying that a reason or error exists or is being
withheld, and nothing about write access, permissions, roles, identities,
settings, allowlists, or which factories accept changes. Do not work around it
with another tool, factory or retry.

Treat an authorization error on this assistant's own access the same way
(AuthorizationFailed, 'does not have authorization to perform action'): say the
factory could not be read, nothing more.

This does not cover a NOT FOUND trigger, a duplicate-protection refusal, or an
error a pipeline run or activity recorded (even a permission error inside the
pipeline): report those as usual.
