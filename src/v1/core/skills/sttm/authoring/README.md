# STTM authoring rule files

The sttm-agent builds Data Dictionaries and STTMs against the organization's own
STTM standards. Those standards are not part of this repository: drop each file
into this directory as it becomes available. No code change is needed.

| File | What it decides |
| --- | --- |
| `SKILL.md` | Main instruction set: ground rules, field rules, section definitions, validation checks, output format and the workflow phases |
| `field-columns.md` | The field record columns: order, derivation rule and allowed values |
| `data-type-mapping.md` | Data types from Raw to INTG to Curated to ASL, including audit field types |
| `pii-pci-handling.md` | PII/PCI classification levels and how each is handled |
| `ASL_Abbreviations.md` | Abbreviations that turn logical names into ASL physical names |
| `example-output.md` | Reference output: representative fields and naming conventions |
| `sttm-mapping.md` | Raw-to-Curated and Curated-to-ASL column rules, exclusions, ETL/audit fields |
| `review-gates.md` | The review gates and the correction workflow |
| `virtual_source_input.md` | Turning text or non-standard input into the standard input template |

How they are used (`src/v1/core/tools/sttm/rules.py`):

- The sttm-agent's prompt lists which files are present, read when the backend
  starts, and the agent reads a present file with `read_file` at
  `/skills/sttm/authoring/<file>`.
- A table value can be recorded as coming from a rule (`source="rule"`) only
  when it names a present file. A value from a closed list (classification,
  PII/PCI, handling) has to appear in that file, and every audit field has to be
  listed in the file named.
- While a file is missing, the columns it would decide stay unresolved unless
  the analyst gives the value. Nothing is filled in from general knowledge.

This directory sits one level below the `sttm` skill, so the skills loader never
offers these files to the orchestrator, not even one named `SKILL.md`.

## Workbook templates

The workbook templates are not part of this repository either: place them as
`templates/data-dictionary.xlsx` (the Data Dictionary) and `templates/sttm.xlsx`
(the STTM). The sttm-agent's `sttm_workbook` tool fills a copy of each with the
session (`src/v1/core/tools/sttm/workbook.py`):

- Each mapping sheet (Data Dictionary, Raw-to-Curated, Curated-to-ASL) gets one
  row per attribute under the template's header row, keeping its cell styles,
  widths, freeze, filter and dropdowns. The Data Dictionary sheet's dropdowns
  (Business_Key_Identifier, Primary_Foreign_Composite_Key, Mandatory_Optional,
  Is_Null_Scenario, Classification_Level, PII_PCI, PII_PCI_Handling_Method,
  Critical_Data_Element, Fair_Lending_Risk_Category) list ranges of its Values
  sheet. A value that matches a choice is written as that choice ("Masked" as
  "5. Masked"), and a dropdown follows its sheet when a layout step renames it.
- The form sheets are filled in: File Details (labels in column C, answers in
  D), the STTM's Document Info (data source, source system) and each workbook's
  change log, in the row under the headers that start at "Version". The other
  sheets are kept as they are, and an About sheet says where the values come
  from and what the cell fills mean.
- A column or form answer the session has no value for stays blank
  (Critical_Data_Element, Fair_Lending_Risk_Category, the team contacts):
  nothing is filled in from general knowledge.

Which sheet and header holds which session column is set in `_TABLE_SHEETS`, the
`_*_FIELDS` lists, `_FORMS` and `_CHANGE_LOG` in `workbook.py`; a template whose
sheet or header names change needs them updated. A template that is missing,
unreadable or without its mapping sheet gives a workbook laid out from the
session's own columns instead, and its About sheet says why.

Notes for a template:

- Excel stores a dropdown that lists another sheet's cells in an extension that
  openpyxl drops when it reads the file. Give such dropdowns as plain list
  validations, which Excel 2010 and later shows the same way.
- The Data Dictionary sheet's own highlights of blank cells are not carried into
  built files, as that sheet is rewritten. File Details keeps its own.
- Built files keep what the template carries, so a template should have no links,
  e-mail addresses, authors or document labels.

The analyst can reshape either workbook (sheets, columns, sorting, highlighting
and more) with layout steps, which never change a value. The files are kept 24
hours for the analyst who built them (`src/v1/core/tools/sttm/files.py`) and
downloaded through `GET /sttm/files/<id>` (`src/v1/api/routes/sttm_files.py`).
