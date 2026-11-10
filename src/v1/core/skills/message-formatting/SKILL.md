---
name: message-formatting
description: >-
  Shapes HOW the orchestrator presents grounded results in its final answer —
  read it before composing any reply that renders retrieved data. Covers
  answer-first readability (the status or outcome on the first line, in bold,
  then scannable detail), document/inventory lists, URL and hyperlink rules,
  ServiceNow incident list rows and full detail cards (reproduced verbatim),
  relayed Data Factory answers, relayed Databricks job run answers (ending with
  their "Run and task details" block), data-lake file arrival results, ASCII diagrams, [n] citation mechanics,
  synthesis/non-repetition, and the closing follow-up block. Use whenever an answer reports a status or outcome
  (is it running, did it succeed or fail, was it recovered, is the SLA met, are
  the triggers in the expected state), lists documents or workbooks, shows one
  or more ServiceNow incidents, contains URLs or a diagram, or cites sources. It
  governs presentation only; every value must still be grounded in what the
  tools returned.
metadata:
  domain: presentation
  applies-to: orchestrator final answers
---

# Message Formatting

This skill governs **how** you present results in your final answer. It never
changes **what** you may say: every fact must still come from what your tools
and subagents returned, and this skill never substitutes for calling them. It
only shapes the presentation of results they already returned.

## When to use this skill

Consult it before composing an answer that renders retrieved data — in
particular when the reply:

- reports a status or an outcome — whether something is running, succeeded,
  failed, was recovered, meets its SLA, is enabled, or whether a change was
  made (see "Answer first" below);
- lists documents, workbooks, or an inventory ("what is available / which
  documents / list the workbooks");
- shows one or more ServiceNow incidents (a list, a single incident, or a full
  detail card);
- reports a data-lake file arrival check;
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
  incidents" below), in an ordered list (1., 2., 3.) when there are several. The
  one exception is a similar-incidents answer (see "Similar incidents" below).
- **EXCEPTION — an STTM session answer keeps its tables.** Its attribute,
  mapping, review and validation tables are what the analyst is reviewing:
  relay each one as handed over, every row and column, never redrawn as
  bullets.

## Answer first — the outcome before the detail

A reader should get the answer from the first line without reading the rest.
An answer that leaves its result in the middle of a paragraph, or on the last
line of a block, makes them read everything to find it.

- **Open with the answer itself**, on one short line. When the question asks for
  a status or an outcome, put it in **bold** at the start of that line:
  "**Yes** — …", "**Not recovered** — …", "**Failed** — …", "**3 of 5 PASS** — …".
- **Use the status word the tool or source gave** (Succeeded, Failed,
  InProgress, Recovered, Not Recovered, PASS / FAIL, Resolved, Closed), never a
  softer synonym, and never a status the tools did not return. The line
  restates the result; it adds no fact the detail below does not hold.
- **Then the detail, scannable:** one fact per bullet or `label: value` line,
  paragraphs of two or three sentences at most, and a blank line between
  blocks.
- **No preamble** before the answer line ("Here is what I found…", "Based on
  the results…") and no recap after the detail.
- **When there is no single outcome** — an inventory, a search, a how-to — the
  first line is the direct answer or the count, never a restatement of the
  question.

Where the first line is already fixed, it wins over this section:

- a ServiceNow result opens with the block the subagent handed back (its count
  line, card or heading); add nothing above it (see "ServiceNow incidents"
  below);
- a Data Factory answer is relayed whole, and its first line already states the
  outcome (see "Data Factory answers" below);
- a Databricks job run answer is relayed whole, and its first line
  already states the outcome (see "Databricks job run answers" below);
- a refusal stays the brief refusal the system prompt prescribes.

## Data Factory answers — relayed whole

When your reply carries a Data Factory answer, the subagent has already
written it outcome first: a bold first line with the result in the tool's own
words (Recovered / Not Recovered, the run's status and runtime, the SLA
assessment, a PASS / FAIL count, a trigger's new state), then the tool's detail
block. Carry it into your reply as written — its first line first, then every
row, note, count and link, in order — and add only the follow-up block after
it.

- Do not put a headline, title or summary of your own above it, and do not move
  its outcome line down or merge it into a sentence.
- Do not turn its block into a table or into prose. Status words, run IDs,
  timestamps, parameters and error text stay verbatim.
- A refused change keeps its outcome-only line ("tr_A was not enabled."): never
  add a reason to it.
- **Factory:** the subagent names a factory by its Azure resource name, the name
  the ADF UI shows. Repeat that name verbatim and never swap in a shorter or
  tidier one: the deployment's own factory aliases are config shorthand, and a
  Portal search for one finds no factory.
- **Time:** ADF times arrive labelled '... UTC'. Keep the label. Same rule as for
  ServiceNow times (see "Timestamps" below); the ADF Portal shows local time, so
  an unlabelled run looks a day off. A time the USER gives ('4 am PDT') is not
  UTC: convert it (11:00 UTC) or quote it as written, never just label it UTC.
- **Links:** keep every run and Monitor link as "Links & URLs" says, and when
  you keep an `[open Monitor]` link, keep the subagent's note about widening the
  time range with it.

## Databricks job run answers — relayed whole

When your reply carries a Databricks job run answer, the subagent has already
written it in order: a bold outcome line naming the run, its workspace
and the result ("**Run 987654321098765 in dbw-ops failed.**"), then the cause
and a suggested fix, then the "Run and task details" block. Carry it into your
reply as written — bold outcome line first, then the explanation, then the
"Run and task details" block verbatim (every line, value and link, in the same
order) — and add only the follow-up block after it.

- Do not put a headline, title or summary of your own above it, and do not move
  its outcome line down or merge it into a sentence.
- Never turn the block into a table or into prose, and never drop its Workspace
  line. Run IDs, job IDs, links, state messages and error text stay verbatim.

## Data Lake answers — file arrival checks

When your reply carries a data-lake file arrival result:

- **First line:** the status in bold, then the file and where and when it
  landed: "**Processed successfully** — `DAILY_PAYMENTS_20261012.csv` arrived
  2026-10-12 06:25 EDT in `lnd/loansys/daily_payments/archive`". Use the tool's
  status: Processed successfully (archive), Processing failed (error), Still
  being processed (inbound), Not found for this date, Incorrect path, or
  Partial result.
- **Then one fact per line:** SLA given (or "none given"), SLA status (`On time`
  or `Late by N min`, when an SLA was given), created and lastModified (both,
  labeled), size, and the folders checked.
- Copy the tool's `SLA given`, `SLA status`, and `arrived` values whole (ET
  equivalent, "read as ET" note, CT or PT arrival time).
- Keep paths, file names, byte counts and timestamps verbatim. No tables.
- **Incorrect path:** say the path is incorrect, quote it, and ask the user to
  check it. Never call it a missing file and never suggest another path.

## Citations — reusing `[n]` markers

Knowledge-base search results prefix each source with a `[n]` marker.

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

- **A knowledge-base source's own location is NEVER printed as visible text.**
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
  ServiceNow records, NOT knowledge-base sources, and the rules above do not
  apply to them. The link text is ALWAYS the record's own number, e.g.
  `[INC0000001](<ticket_url>)` or `[KB0010001](<article_url>)`, and that number
  appears EXACTLY ONCE per entry. NEVER render the number as plain/bold text
  followed by a separate link like "(link)", "(ticket)", "(article)", a second
  copy of the number, or the bare URL on its own line.
- **EXCEPTION — ADF Studio run links:** Data Factory answers link run ids
  themselves, as `[open run](<https://adf.azure.com/...>)`, plus at most one
  `[open Monitor](<...>)` line per answer. These are tool output, not retrieved
  sources, so the "rare occasion" wording above does NOT apply and is not a
  reason to prune them: a listing of forty runs carries forty links by design,
  and one per run is the ONLY way a reader can open a run id (ADF's Monitor tab
  cannot search by run id and opens on the last 24 hours). Reproduce every link
  verbatim — same label words, same angle brackets, same full URL — attached to
  the FIRST `runId=` on its row, never to a `parentRunId` that happens to sit
  closer. They never become `[n]` markers and never enter Referenced Sources.
  Run ids that arrive with no link stay bare; do not construct one for them.
- **EXCEPTION — download links to files built for the user:** an STTM session
  answer can carry links to the workbooks it built, such as
  `[Data Dictionary workbook](/api/...)`, with the time they work until.
  Reproduce each link verbatim — same label words, same path — in the body of
  your answer, with that time, and never in the follow-up block. They are not
  sources: they never become `[n]` markers. Never write, shorten or complete
  such a link yourself; a file with no link has none.
- **EXCEPTION — Databricks run details:** the `Run URL:` line of a Databricks
  job run answer's "Run and task details" block stays exactly as given, as the
  bare URL; never turn it into a `[text](url)` link, and don't add a second link
  for the run elsewhere.

## Diagrams

If there are any diagrams, use ASCII diagram output instead of markdown.

## ServiceNow incidents — reproduce verbatim

When the subagent returns incidents, PRESERVE exactly what it hands back,
verbatim — including every `ticket_url` link. Never show a raw sys_id.

### List result (even a single match)

It returns ONE finished block, `rendered_answer`: a count line, a blank line, then ONE
fully-rendered line per incident, already numbered. The backend built all of it, not
the subagent, so it is already correct — reproduce it CHARACTER-FOR-CHARACTER:

- the count line ("Found 28 incidents; showing 1-10.", then "showing 11-20.", then
  "showing 21-28." as paging continues) goes FIRST, unchanged. Your
  own sentence naming the subject and any follow-up suggestions go AFTER the block,
  never inside it, and never replace the count line with a
  number-free "here are the open incidents" / "more are available";
- never re-style, re-order, or re-label its parts, and never expand it into
  sub-bullets or a full card — it stays ONE line;
- the raw URL is NEVER shown as visible text; it lives only inside the markdown
  link behind the incident number;
- the rows arrive as an ordered list (1., 2., 3., …) in result order; a later page keeps
  the numbers its count line gives ("showing 11-16" → 11. to 16.). KEEP every number —
  never drop, restart at 1 or re-style them; a single match is unnumbered. Only when you
  write a list yourself from merged or filtered rows do you number it 1., 2., 3., …, and
  such a list is still opened by a count line;
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

### Similar incidents ("find incidents similar to INC…", "how was this fixed")

The subagent hands back ONE finished block, built by the backend:
`## Incident summary` (the incident's one row, sometimes one Summary line), then
`## Similar closed incidents` with its count line ("Found 3 similar closed
incidents from the same team (…).", "Found 3 similar closed incidents for <app>
from the same team (…). Checked incidents 1-23 of 137, newest first." or "No
similar closed incidents found …") and up to 10 numbered matches, each with indented
Summary / Cause / Resolution notes lines, sometimes ending with an offer to look for
more similar incidents.
Reproduce it CHARACTER-FOR-CHARACTER — headings, count line, numbering and
indented lines included. Add NO sentence of your own above, inside or in place of
it; never re-level the headings, never expand the incident into a card, and never
replace an empty result with your own one-liner. The same question must come out
the same way every time.

### Timestamps

Timestamps come back as `2026-05-10 17:00:00 UTC`. Reproduce each one in exactly
that form, ` UTC` right after the time, with no bold, code span or other
formatting around it: the UI finds that exact form and shows it in the viewer's
own local zone. Never drop the ` UTC`, never add another zone marker (`GMT`, `Z`,
`+00:00`, "local time"), and never convert a time yourself. This applies to
incident and change-request times alike.

### Narrow questions about ONE incident

For a narrow question about a single ServiceNow incident (an engineer lookup, a
single resolution note, one attribute) present only what the subagent returned
for that question — not the full card. A one-attribute answer arrives as ONE
line, e.g. `**Resolved by:** <name> — [INC…](<ticket_url>)`: reproduce it
exactly, linked number included, and add nothing (no other owner fields, no
card). This trims FIELDS out of one record; it NEVER trims ROWS out of a result
set, and it applies to ServiceNow incidents only. It is not a rule about ADF,
pipeline, or discovery answers: those keep every row the subagent listed,
however narrowly the question was phrased.

For "who opened/reported/raised this incident?", use the ticket's `caller` /
Reported by value. Caller is distinct from Assigned to, Resolved by, and
Engineer; never substitute one of those fields when caller is available.

## ServiceNow change requests — reproduce verbatim

The subagent hands back ONE finished block, built by the backend in the client's
change template: a `### Recent <subject> Changes` heading (`### Open <subject>
Changes` when open ones were asked for), the count line ("Found 8 closed change
requests related to '<subject>', most recent first by Actual End Date."), then the
numbered changes, each opening `**Change Number:** [CHG…](<url>) — <title>` with
indented Assigned To / Assignment Group / Actual End Date / Closure Code / Closure
Notes lines (an open change shows State and its planned dates instead). A
CHG-number lookup is one such block with more lines and no heading. Reproduce it
CHARACTER-FOR-CHARACTER: heading, count line, numbering and indented lines
included. Add NO sentence of your own above or in place of it, never drop or
re-level the heading, and keep every `Not available` line: here those are the
template's fixed fields, not placeholders. Never offer a search by closure code,
in a follow-up bullet or anywhere else.

## ServiceNow KB articles — fixed shell, formattable body

For a KB-article question the subagent hands back an already-rendered block: a
count line, then one block per article opening `**[KB0010001](<article_url>)** —
Title`, a meta line, and the article body.

Keep the SHELL exactly as handed over:

- the count line first, unchanged (it also reports how many expired articles
  were withheld);
- the article number stays the markdown link opening each block. Do NOT turn it
  into plain or bold text, and do NOT move the URL out to its own line, a
  "(link)" / "(article)" suffix, or a trailing source list. It is the article's
  ONLY hyperlink: never add a second "Article Link:" row repeating its URL, and
  never print a raw article URL as visible text;
- the meta line under the header (`Category: … | Author: … | Published: …`) is
  kept as one line, exactly as given. Never drop it, never split it onto several
  lines, and never add a field it omitted;
- each body's closing `Source: Knowledge Article KB…` line stays exactly as
  given;
- keep the `---` separators between articles, and never quote an article the
  subagent did not return (expired ones are withheld on purpose).

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

Reproduce every body IN FULL: it is the procedure the user asked for, so never
summarize it, cut it short, or replace the steps with "see the article".
Restyling is PRESENTATION ONLY. Never reword a sentence, never add, merge, drop,
reorder or summarize a step or command, and never introduce a fact the article
did not state. Commands especially are copied character-for-character — a
retyped command is a broken command.

## Ending every answer

End EVERY answer with a follow-up section as its final block, in EXACTLY this
format: the level-2 heading `## Want to explore further?` on its own line, then
exactly three "- " bullets, each a short specific request PHRASED IN THE USER'S
OWN VOICE (the next thing the user would ask you), with nothing after the third
bullet.

- It goes on every answer (knowledge-base, ServiceNow, Data Factory, data-lake
  and "no results" alike) EXCEPT a refusal (out-of-scope or confidentiality: it
  ends right after its brief sentence) and a short answer about the signed-in
  user themselves.
- Make the three specific to this answer's topic, never generic placeholders.
- Never invent an identifier: name only incident numbers, pipelines, paths and
  other ids the user or a tool gave in this conversation, and while asking the
  user for one, name none, not even as an example.
- CRITICAL: each bullet is sent back to you VERBATIM as the user's next message
  when it is clicked, so write it the way the USER would type a request to you —
  an imperative or a first-person question such as "Show me…", "List the…", "How
  do I…", "What are…", or "Compare…". NEVER address the bullet to the reader or
  ask about the reader's wishes: do NOT begin it with "Do you want", "Do you want
  me to", "Would you like", "Should I", "Can I", or any other second-person
  phrasing. A bullet like "Do you want to see …?" is read as a question about the
  assistant's own preferences and gets wrongly refused, so it is forbidden —
  phrase it as "Show me …" instead.
