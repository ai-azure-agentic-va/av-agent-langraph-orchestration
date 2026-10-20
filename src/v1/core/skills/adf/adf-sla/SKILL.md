---
name: adf-sla
description: >-
  How the adf-agent answers an SLA question about a pipeline (is the SLA
  realistic, met, breached or at risk): where the threshold comes from and
  what a met/breached conclusion rests on.
---

# SLA questions

YOU own the threshold. Use the SLA the request states; if it states none,
search the knowledge base ONCE and pass what the document STATES as
sla_minutes (hours -> minutes). If nothing comes back, or no threshold is
documented for this pipeline, run analyze_pipeline_runtime WITHOUT sla_minutes
and say plainly that no SLA is documented — never infer one from the average,
a similar pipeline, or a rounded runtime. Base a met/breached conclusion on the
exceedance count/rate as well as the average. The same one search covers a
runbook, operational standard, or escalation step the request needs.
