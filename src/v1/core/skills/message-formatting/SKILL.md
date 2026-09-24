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
- The numbers are stable across the WHOLE conversation, not just this turn:
  referenced sources are append-only, so the same document keeps the same number
  whenever it resurfaces (this search, a later search, or a later turn), and a
  genuinely new document gets the next unused number. Cite the exact number shown
  next to the passage you used, even after several searches or turns — NEVER
  restart at `[1]` on a later turn.
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

- **An `ai_search_tool` source's own location is NEVER printed as visible text.**
  Do not put a retrieved document's URL, SharePoint path, or `BREADCRUMB:`
  hierarchy path (which ends in the source's file name, e.g. `... > Jul 24.md`) in
  the answer — not as a raw URL, not as a `[text](url)` hyperlink, not on a
  "Location:" / "URL:" line, and not as a printed breadcrumb. A source is pointed
  at ONLY by its `[n]` citation marker: the UI turns that marker into the clickable
  link and lists the source in the "Referenced Sources" panel. Citing `[n]` is the
  complete way to reference a source; adding its URL or path just duplicates that
  panel and clutters the answer. (Read the `BREADCRUMB:` line only to pass it back
  as a `section` request — it is an internal navigation path, never answer content.)
- Never paste a bare URL as visible text anywhere in an answer. On the rare
  occasion a link genuinely belongs in the body — e.g. a URL written inside a
  source's own content that the user explicitly asked you to surface — render it
  as a `[TEXT](URL)` hyperlink with descriptive text, never the raw URL as the text.
- **EXCEPTION — ServiceNow incidents and ServiceNow KB articles:** these are
  ServiceNow records, NOT `ai_search_tool` sources, and the rules above do not
  apply to them. The link text is ALWAYS the record's own number, e.g.
  `[INC0000001](<ticket_url>)` or `[KB0010001](<article_url>)`, and that number
  appears EXACTLY ONCE per entry. NEVER render the number as plain/bold text
  followed by a separate link like "(link)", "(ticket)", "(article)", a second
  copy of the number, or the bare URL on its own line.
- **EXCEPTION — ADF Studio run links:** `adf-agent` output links run ids itself,
  as `[open run](<https://adf.azure.com/...>)`, plus at most one
  `[open Monitor](<...>)` line per answer. These are tool output, not retrieved
  sources, so the "rare occasion" wording above does NOT apply and is not a
  reason to prune them: a listing of forty runs carries forty links by design,
  and one per run is the ONLY way a reader can open a run id (ADF's Monitor tab
  cannot search by run id and opens on the last 24 hours). Reproduce every link
  verbatim — same label words, same angle brackets, same full URL — attached to
  the FIRST `runId=` on its row, never to a `parentRunId` that happens to sit
  closer. They never become `[n]` markers and never enter Referenced Sources.
  Run ids that arrive with no link stay bare; do not construct one for them.

## Diagrams

If there are any diagrams, use ASCII diagram output instead of markdown.

## ServiceNow incidents — reproduce verbatim

When the subagent returns incidents, PRESERVE exactly what it hands back,
verbatim — including every `ticket_url` link.

### List result (even a single match)

It returns a count line followed by ONE fully-rendered line per incident. The
backend built both, not the subagent, so they are already correct — reproduce them
CHARACTER-FOR-CHARACTER:

- the count line ("Found 28 incidents; showing 1-10.", then "showing 11-20.", then
  "showing 21-28." as paging continues) goes FIRST, unchanged. Your
  own sentence naming the subject may follow it, but never replace it with a
  number-free "here are the open incidents" / "more are available";
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

### Narrow questions about ONE incident

For a narrow question about a single ServiceNow incident (an engineer lookup, a
single resolution note, one attribute) present only what the subagent returned
for that question — not the full card. This trims FIELDS out of one record; it
NEVER trims ROWS out of a result set, and it applies to ServiceNow incidents
only. It is not a rule about ADF, pipeline, or discovery answers: those keep
every row the subagent listed, however narrowly the question was phrased.

For "who opened/reported/raised this incident?", use the ticket's `caller` /
Reported by value. Caller is distinct from Assigned to, Resolved by, and
Engineer; never substitute one of those fields when caller is available.

## ServiceNow KB articles — fixed shell, formattable body

For a KB-article question the subagent hands back an already-rendered block: a
count line, then one block per article opening `**[KB0010001](<article_url>)** —
Title`, a meta line, and the article body.

Keep the SHELL exactly as handed over:

- the count line first, unchanged (it also reports how many expired articles
  were withheld);
- the article number stays the markdown link opening each block. Do NOT turn it
  into plain or bold text, and do NOT move the URL out to its own line, a
  "(link)" / "(article)" suffix, or a trailing source list;
- the meta line under the header (`Category: … | Author: … | Published: …`) is
  kept as one line, exactly as given. Never drop it, never split it onto several
  lines, and never add a field it omitted;
- keep the `---` separators between articles, and never quote an article the
  subagent did not return.

Then RESTYLE the body — EVERY article's body, whether the answer holds one or
ten. It arrives as flat unstyled paragraphs, a wall of text nobody wants to
read, so give it the structure the source lost:

- **bold** the key term, product name, or the exact error string;
- promote a bare label line ("Cause", "Workaround / Fix", "Resolution",
  "Symptoms") to a bold sub-heading;
- turn "1)" / "2)" / "a)" step sequences into a numbered or bulleted list, one
  step per item;
- put every shell command, path, filename, hostname and error string in a
  `code span`; use a fenced block for a multi-line command sequence;
- keep a blank line between blocks so the answer breathes.

Restyling is PRESENTATION ONLY. Never reword a sentence, never add, merge, drop,
reorder or summarize a step or command, and never introduce a fact the article
did not state. Commands especially are copied character-for-character — a
retyped command is a broken command.

## Ending every answer

Every answer still ends with the mandatory `## Want to explore further?`
follow-up block exactly as the system prompt specifies (a level-2 heading and
exactly three specific question bullets), on every answer EXCEPT
out-of-scope refusals, which the system prompt exempts. This skill does not
change that rule — it remains in force here.
