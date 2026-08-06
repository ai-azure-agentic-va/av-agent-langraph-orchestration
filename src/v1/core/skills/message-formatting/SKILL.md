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
- **NEVER render tables.** Do not use a markdown table for any data. For
  knowledge-base content, present each item as a bulleted entry with its
  attributes as sub-bullets or inline `label: value` pairs. Put long free-text
  fields (descriptions, notes, resolutions) in a list, never in a table column.
- **EXCEPTION — ServiceNow incidents are NOT bulleted entries.** The
  bullets-with-sub-bullets shape above never applies to incident results: each
  incident is ONE line reproduced verbatim from the subagent (see "ServiceNow
  incidents" below), in an ordered list (1., 2., 3.) when there are several.

## Citations — reusing `[n]` markers

The `ai_search_tool` grounding text prefixes each source with a `[n]` marker.

- When a statement draws on a source, reuse that same `[n]` marker inline right
  after the statement (e.g. "Members can reset their PIN online [1].").
- A citation marker is ONLY the number in the `[n]` that PREFIXES a passage.
  A bracketed number inside a passage's title, URL, or body (e.g. a document's own
  "[1242]" cross-reference) is NOT a citation marker — never emit it as one.
- Use only the prefix markers present in the grounding text, keep each identical
  to its source's number, and never invent or renumber them.
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
  number itself, e.g. `[INC0000001](<ticket_url>)`, and the number appears
  EXACTLY ONCE per row. NEVER render the number as plain/bold text followed by a
  separate link like "(link)", "(ticket)", or a second copy of the number.

## Diagrams

If there are any diagrams, use ASCII diagram output instead of markdown.

## ServiceNow incidents — reproduce verbatim

When the subagent returns incidents, PRESERVE exactly what it hands back,
verbatim — including every `ticket_url` link.

### List result (even a single match)

It returns ONE fully-rendered line per incident. The backend built that line, not
the subagent, so it is already correct — reproduce it CHARACTER-FOR-CHARACTER:

- never re-style, re-order, or re-label its parts, and never expand it into
  sub-bullets or a full card — it stays ONE line;
- the raw URL is NEVER shown as visible text; it lives only inside the markdown
  link behind the incident number;
- when there are several incidents, wrap the rows in an ordered markdown list
  (1., 2., 3., …), one row per incident, in result order;
- NEVER add a field the line does not carry — no `Data source / business service:`,
  no `Category:`, no `Assignment group:`, and no placeholder like
  `(not available in this view)`. Empty fields are dropped on purpose. If the user
  asked about a concept the line does not carry (e.g. the data source), it lives in
  the incident's long description — do not fabricate a per-row field for it.

### Single incident — a summary OR a full-details request

Both shapes ("summarize INC…", "what is INC…", "full details for INC…") come back
as a card. Render that card verbatim:

- a SUMMARY is one fully-rendered block the backend built — the incident number
  already a markdown link, the field order and the bold labels already set, empty
  fields already dropped. Reproduce it CHARACTER-FOR-CHARACTER, then keep the one
  short plain-language paragraph the subagent added after it. Never re-order,
  re-label, re-style, split or merge its lines, and never rebuild it from the
  underlying fields;
- the card OPENS with the incident number as a markdown link, `[INC…](<ticket_url>)`,
  exactly as a list row does. Keep it. A summary is the SHORTEST view, not a
  link-free one — never downgrade its number to plain or bold text, and never
  replace the link with "(ServiceNow link)" or the bare URL as visible text;
- do NOT drop fields the subagent returned;
- but NEVER add fields the subagent omitted — no placeholder rows like
  `Resolved at: Not set` / `Not available` / `N/A` for data it did not return.
  It omits empty fields on purpose (an open incident has no resolved/closed
  timestamps).

### Timestamps

Timestamps come back as a bare date+time (e.g. `2026-05-10 17:00:00`) with NO
timezone label. That is intentional — the UI converts each one into the viewer's
own local zone. Reproduce the value exactly: keep the time component, and NEVER
append a zone marker of any kind (`UTC`, `GMT`, `Z`, `+00:00`, "local time").
Labelling an already-converted clock value with a zone makes it wrong.

### Narrow questions

For a narrow question (an engineer lookup, a single resolution note, one
attribute) present only what the subagent returned for that question — not the
full card.

## Ending every answer

Every answer still ends with the mandatory `## Want to explore further?`
follow-up block exactly as the system prompt specifies (a level-2 heading and
exactly three specific question bullets), on every answer EXCEPT the two
refusal kinds the system prompt exempts: out-of-scope refusals and
reporting/aggregate declines. This skill does not change that rule — it
remains in force here.
