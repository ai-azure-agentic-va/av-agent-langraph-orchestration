---
name: message-formatting
description: >-
  Shapes HOW the orchestrator presents grounded results in its final answer —
  read it before composing any reply that renders retrieved data. Covers
  document/inventory lists, URL and hyperlink rules, ServiceNow incident list
  rows and full detail cards (reproduced verbatim), ASCII diagrams, [n] citation
  mechanics, and synthesis/non-repetition. Use whenever an answer lists
  documents or workbooks, shows one or more ServiceNow incidents, contains URLs
  or a diagram, or cites sources. It governs presentation only; every value must
  still be grounded in what the tools returned.
metadata:
  domain: presentation
  applies-to: orchestrator final answers
---

# Message Formatting

This skill governs **how** you present results in your final answer. It never
changes **what** you may say: every fact must still come from `ai_search_tool`
or the `servicenow-ticket-agent`, and this skill never substitutes for calling
them. It only shapes the presentation of results they already returned.

## When to use this skill

Consult it before composing an answer that renders retrieved data — in
particular when the reply:

- lists documents, workbooks, or an inventory ("what is available / which
  documents / list the workbooks");
- shows one or more ServiceNow incidents (a list, a single incident, or a full
  detail card);
- contains any URL or hyperlink;
- includes a diagram;
- cites knowledge-base sources with `[n]` markers.

It shapes presentation only. Still ground every value in tool output, and never
add data the tools did not return.

## Core rules (always in effect)

- Use markdown (bold, bullet points, headers) wherever it improves readability.
- **NEVER render tables.** Do not use a markdown table for any data. Present
  every item as a bulleted entry with its attributes as sub-bullets or inline
  `label: value` pairs. Put long free-text fields (descriptions, notes,
  resolutions) in a list, never in a table column.

## Citations — reusing `[n]` markers

The `ai_search_tool` grounding text prefixes each source with a `[n]` marker.

- When a statement draws on a source, reuse that same `[n]` marker inline right
  after the statement (e.g. "Members can reset their PIN online [1].").
- Use only the markers present in the grounding text, keep each identical to its
  source's number, and never invent or renumber them.
- The numbers are stable across EVERY search in this turn: the same document
  keeps the same number if it resurfaces in a later search, and a new document
  gets the next unused number. Cite the exact number shown next to the passage
  you used, even after several searches — never restart at `[1]`.
- If a search returns "No results found in the knowledge base for this query.",
  it surfaced nothing relevant: do not emit any `[n]` marker for that search and
  do not imply a source backs the answer.

## Synthesis & non-repetition

- When the grounding text spans multiple source files, synthesize ONE answer
  that draws on all relevant sources rather than asking the user which document
  to use.
- Do not restate or recap information you already presented in the same answer.
  End with caveats or notes if needed, not a summary of what you just said.

## Document & inventory lists

For inventory questions ("what is available / which documents / list the
workbooks"), render the answer as a bulleted list with **one bullet per distinct
document**:

- Lead each bullet with the document/workbook name in **bold**, then give its
  domain, coverage, and any notes inline.
- List EVERY document the grounding text names — never merge several documents
  into a single bullet, and do not stop after the first few.
- Do not use a table.

## Links & URLs

- Render every URL as a markdown hyperlink in the form `[TEXT](URL)`, e.g.
  `[descriptive](https://example.com)`. If the source gives no link text, use
  the single most relevant word as the text.
- **EXCEPTION — ServiceNow incidents:** the link text is ALWAYS the incident
  number itself, e.g. `[INC3235130](<ticket_url>)`, and the number appears
  EXACTLY ONCE per row. NEVER render the number as plain/bold text followed by a
  separate link like "(link)", "(ticket)", or a second copy of the number.

## Diagrams

If there are any diagrams, use ASCII diagram output instead of markdown.

## ServiceNow incidents — reproduce verbatim

When the subagent returns incidents, PRESERVE exactly what it hands back,
verbatim — including every `ticket_url` link.

### List result (even a single match)

It returns ONE line per incident in EXACTLY this shape:

```
[<number>](<ticket_url>) — <short description> — **State:** <state> — **Priority:** P<n> - <label> — **Assigned to:** <name> (<code>)
```

Reproduce that line CHARACTER-FOR-CHARACTER:

- the incident number is the link text and appears once;
- the bold `**State:**` / `**Priority:**` / `**Assigned to:**` labels stay bold;
- the row stays ONE line — do NOT expand it into sub-bullets or a full card.

### Single incident or a full-details request

It returns the complete card. Render that card's fields verbatim:

- do NOT drop fields the subagent returned;
- but NEVER add fields the subagent omitted — no placeholder rows like
  `Resolved at: Not set` / `Not available` / `N/A` for data it did not return.
  It omits empty fields on purpose (an open incident has no resolved/closed
  timestamps).

### Timestamps

Timestamps come back in UTC with an explicit `UTC` suffix (e.g.
`2026-05-10 17:00:00 UTC`). Keep that `UTC` marker intact — never strip it or
drop the time component.

### Narrow questions

For a narrow question (an engineer lookup, a single resolution note, one
attribute) present only what the subagent returned for that question — not the
full card.

## Ending every answer

Every answer still ends with the mandatory `## Want to explore further?`
follow-up block exactly as the system prompt specifies (a level-2 heading and
exactly three specific question bullets), on every answer EXCEPT out-of-scope
refusals. This skill does not change that rule — it remains in force here.
