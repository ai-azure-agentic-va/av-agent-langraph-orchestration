---
name: adf-incident-correlation
description: >-
  How the adf-agent runs and reports correlate_incident_with_pipeline_runs for
  a ServiceNow incident: the ticket's own words and timestamp, the 30-minute
  window, named pipelines no factory has, and time as a lead, never proof.
---

# Incident correlation — the ticket's own words, and time as a LEAD only

When the task carries a ServiceNow incident, call
correlate_incident_with_pipeline_runs with the ticket text passed through
UNCHANGED and opened_at exactly as the ticket states it. Do NOT summarize the
text first: pipeline names and run GUIDs are read out of it and a paraphrase
deletes them. Do NOT supply an opened_at the ticket did not give you — every
run is ranked against that moment, so if the task text carries no creation
timestamp, say so and ask for it instead of estimating one. The window is 30
minutes before to 30 minutes after that timestamp: leave minutes_before /
minutes_after at their defaults unless the USER asked for a different window,
and never widen it on your own.

Report the tool's answer in its parts: the pipelines the ticket named that ARE
in the factory, any it named that are NOT (say that plainly — it is a finding,
not an error, and no runs exist to report for them), and the failed runs with
the gap to the ticket on each. CLOSENESS IN TIME IS A LEAD, NOT PROOF: say a
run failed N minutes before the ticket was raised; never say it caused the
incident unless the ticket itself names that run or pipeline. When the tool
reports a factory-wide sweep, say the ticket named no pipeline this deployment
has and that the runs listed are only what else failed nearby. When it finds
nothing, say no run failed in the window searched (state the window the tool
reports) rather than that the ticket is wrong, and offer to search a wider
one. Then, if the user asked WHY it failed, follow the correlation with
get_pipeline_run_tree on the closest run's ID — copied character-for-character
from the tool output, never reconstructed from memory.
