---
name: sttm
description: >-
  Produce Source-to-Target Mapping (STTM) / data-lineage answers across the
  data platform's Landing -> RAW -> INT -> CUR -> ASL layers. Use when a
  question asks how a field, attribute, or table is mapped, transformed, or
  derived between layers; for end-to-end column lineage; for surrogate /
  business key derivation; or for which transformation logic applies. Emits
  side-by-side field-mapping tables or per-layer lineage tables with verbatim
  column names, transformation expressions, Logic Type, PII/PCI flags, and
  [n] citation markers.
metadata:
  domain: data-platform
  layers: Landing,RAW,INT,CUR,ASL
---

# Source-to-Target Mapping (STTM)

STTM describes **data lineage** across the data platform: how a value moves and
changes as it flows through the layers, and what transformation produces each
target attribute.

## When to use this skill

Reach for it when the question is about *how data maps or moves between layers*,
for example:

- "How does `<field>` map from RAW to INT?" / "What's the transformation for
  `<column>`?"
- "Show the end-to-end lineage of `<attribute>`." / "Where does `<ASL column>`
  come from?"
- "How is the `<entity>` key derived in ASL?" (business key -> synthetic key)
- "Is `<field>` PII/PCI?" / "What's the Logic Type for `<target column>`?"

If the question is not about field/attribute lineage between layers, this skill
does not apply.

## The layers

```
Landing -> RAW -> INT -> CUR -> ASL
```

- **RAW->INT** and **INT->CUR** tabs hold field-level mappings: transformation
  logic, presence flags, and PII/PCI flags.
- The **CUR->ASL** workbook has one row per target attribute, carrying the
  source CUR column, target ASL table/column, data type, nullability, PK/FK
  indicators, and a **Logic Type** (Pass Through, Derived, ETL Generated,
  Lookup).

## Choosing the table shape

Pick the shape from what the question asks for.

### Single-hop field mapping (one tab, or one source/target pair)

**One row per field**, source and target side by side. Columns:

| Source Table.Column | Target Table.Column | Data Type | Transformation Logic | Logic Type |

Never split one field into separate source and target rows.

### End-to-end lineage of one attribute

**One row per layer**, in flow order (Landing -> RAW -> INT -> CUR -> ASL).
Columns:

| Layer | Table/Column | Data Type | Transformation Logic |

### ASL surrogate key

The ASL key is usually **not** raw in ASL: CUR derives a business key, then
hashes it into a synthetic ASL key. When the grounding documents that
derivation **for this entity**, surface it in the ASL hop instead of a bare
"Pass Through". Quote, verbatim:

- the CUR business-key expression,
- the hash / ASL-key expression,
- the ASL key column, and
- its ASL table,

using the Logic Type the workbook assigns (Derived / ETL Generated /
Synthetic Key).

### Landing & RAW (raw ingest, no transformation)

For these layers Transformation Logic is **"N/A (raw ingest)"** — *not*
"not in retrieved sources". (Reserve "not in retrieved sources" for an
INT/CUR/ASL layer where a transformation is expected but its row was not
retrieved.) Still fill Table/Column from the source extract/file whenever the
grounding names it (e.g. the File Details extract path).

## Fidelity & citations

- Pull **exact** column names and transformation expressions verbatim — do not
  paraphrase them into vague prose.
- Tie every row to its evidence with the source's `[n]` citation marker.
- Carry PII/PCI flags through when the grounding records them.
