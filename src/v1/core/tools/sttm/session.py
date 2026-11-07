"""One STTM session: what the analyst supplied, what was decided, and the three tables.

A session turns one source's specification into the Data Dictionary, the
Raw-to-Curated STTM and the Curated-to-ASL STTM. It is kept in the
``sttm_sessions`` state channel as a plain dict (a dumped :class:`Session`),
so it lives in the conversation's checkpoint.

Only what someone decided is stored:

* the analyst messages the session was built from, verbatim;
* the attributes read from the specification (:mod:`.spec`), the derived
  fields the analyst declared and the audit fields a rule file names;
* the file details (keys, layout, CMDB number);
* explicit cell values, each with its source: ``analyst`` (found in the
  analyst's own words), ``rule`` (a rule file present in this deployment) or
  ``draft`` (the agent's wording, descriptive columns only).

The rows of the three tables, the values one table takes from another and the
validation results are worked out from that each time (:class:`Tables`), so a
correction changes one cell and whatever follows it, and no table can fall out
of step with another.

Nothing here supplies a value of its own: a column with no specification
value, no explicit value and no link stays unresolved, and validation names it.

The tables are a normalized representation of what the requirements ask each
document to hold; :mod:`.workbook` maps them onto the organization's workbook
templates.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from v1.core.tools.sttm.rules import available_rule_files, rule_file_text
from v1.core.tools.sttm.spec import (
    duplicate_names,
    normalize_name,
    parse_specification,
    without_attribute_tables,
)

# --- Inputs, gates and file details ------------------------------------------

INPUT_KINDS: dict[str, str] = {
    "source_system_instructions": "Source-system instructions",
    "source_instructions": "Source instructions",
    "specification": "Source specification",
    "derived_fields": "Derived-field definitions",
    "asl_schema": "ASL schema export",
}
REQUIRED_INPUTS = ("source_system_instructions", "specification")

GATES: dict[str, str] = {
    "input_review": "Input review (gate 1)",
    "dd_review": "Data Dictionary review (gate 2)",
    "sttm_review": "STTM review (gate 3)",
}
# The gates each review mode skips. With no mode chosen, every gate stops.
REVIEW_MODES: dict[str, frozenset[str]] = {
    "default": frozenset(),
    "extensive": frozenset(),
    "moderate": frozenset({"input_review"}),
    "fast": frozenset(GATES),
}
GENERATED: dict[str, str] = {
    "data_dictionary": "Data Dictionary",
    "sttm": "Raw-to-Curated and Curated-to-ASL STTMs",
}
# The workflow, in order: gates stop for the analyst, generated stages for the agent.
STAGES = ("input_review", "data_dictionary", "dd_review", "sttm", "sttm_review")

DETAILS: dict[str, str] = {
    "primary_key": "Primary key",
    "join_keys": "Join keys",
    "cmdb_number": "CMDB application number",
    "file_pattern": "File name pattern",
    "delimiter": "Delimiter",
    "header_rows": "Header rows",
    "trailer_rows": "Trailer rows",
    "frequency": "Frequency",
    "expected_arrival": "Expected arrival",
}
REQUIRED_DETAILS = (
    "primary_key",
    "cmdb_number",
    "file_pattern",
    "delimiter",
    "header_rows",
    "trailer_rows",
    "frequency",
)
# Asked at the input review: the required details plus the join keys, which
# may be "none".
ASKED_DETAILS = (*REQUIRED_DETAILS, "join_keys")
# What the agent may propose for the analyst to confirm at the input review. A
# CMDB number only ever comes from the analyst.
PROPOSABLE_DETAILS = frozenset(DETAILS) - {"cmdb_number"}

# Delimiters by canonical name, with the ways an analyst writes them.
DELIMITERS: dict[str, tuple[str, ...]] = {
    "tab": ("tab", "tsv", "\\t", "\t"),
    "pipe": ("pipe", "|"),
    "comma": ("comma", "csv", ","),
    "semicolon": ("semicolon", ";"),
    "fixed width": ("fixed width", "fixed-width", "fixed length", "positional"),
}
_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_NONE_WORDS = (
    "no",
    "none",
    "nothing",
    "zero",
    "n/a",
    "not applicable",
    "without",
    "aren't",
    "are not",
    "isn't",
    "is not",
)

# --- Columns -----------------------------------------------------------------

TABLES: dict[str, str] = {
    "data_dictionary": "Data Dictionary",
    "raw_to_curated": "Raw-to-Curated STTM",
    "curated_to_asl": "Curated-to-ASL STTM",
}
_TABLE_ALIASES = {"dd": "data_dictionary", "r2c": "raw_to_curated", "c2a": "curated_to_asl"}
ColumnKind = Literal["engine", "spec", "linked", "governed", "descriptive"]
Source = Literal["specification", "analyst", "rule", "draft"]


@dataclass(frozen=True)
class Column:
    """One column of a table, and where its values may come from.

    * ``engine``: worked out from the attributes and derived fields; never set.
    * ``spec``: read from the specification; the analyst may correct it.
    * ``linked``: follows another column or a file detail; the analyst may
      override it.
    * ``governed``: an organizational standard decides it, so the value comes
      from a present rule file or the analyst, never from the agent.
    * ``descriptive``: prose the agent may draft, marked as a draft.
    """

    key: str
    label: str
    kind: ColumnKind
    required: bool = False
    # Rule files that may decide the column; empty means any present one.
    rule_files: tuple[str, ...] = ()
    # A rule-sourced value must appear in its rule file (a closed vocabulary).
    quoted: bool = False
    yes_no: bool = False
    # Y/N columns: the words an analyst uses about the column. Their answer is
    # rarely a bare Y or N ("carry everything forward"), so the evidence for
    # one is that they spoke about it.
    topics: tuple[str, ...] = ()
    # (table, column), or ("details", key), that the column follows.
    link: tuple[str, str] | None = None
    # The derived-field attribute a derived row takes the value from.
    derived_link: str | None = None


_TYPE_RULES = ("data-type-mapping.md", "SKILL.md")
_PII_RULES = ("pii-pci-handling.md", "SKILL.md")
_REQUIRED_TOPICS = ("required", "mandatory", "optional", "nullable", "null")
_CARRY_TOPICS = (
    "carry",
    "carried",
    "carries",
    "carrying",
    "exclude",
    "excluded",
    "excluding",
    "drop",
    "dropped",
)

COLUMNS: dict[str, tuple[Column, ...]] = {
    "data_dictionary": (
        Column("attribute_name", "Attribute", "engine"),
        Column("origin", "Origin", "engine"),
        Column("primary_key", "PK", "engine"),
        Column("data_type", "Data type", "spec", required=True, rule_files=_TYPE_RULES),
        Column("length", "Length", "spec"),
        Column("required", "Required", "spec", yes_no=True, topics=_REQUIRED_TOPICS),
        Column("technical_description", "Technical description", "spec"),
        Column("business_description", "Business description", "descriptive"),
        Column(
            "classification",
            "Classification",
            "governed",
            required=True,
            rule_files=_PII_RULES,
            quoted=True,
        ),
        Column("pii_pci", "PII/PCI", "governed", required=True, rule_files=_PII_RULES, quoted=True),
        Column("handling_method", "Handling", "governed", rule_files=_PII_RULES, quoted=True),
        Column(
            "carry_to_curated",
            "Carry to curated",
            "governed",
            required=True,
            yes_no=True,
            topics=_CARRY_TOPICS,
        ),
        Column("derivation_logic", "Derivation logic", "engine"),
        Column("join_key", "Join key", "engine"),
    ),
    "raw_to_curated": (
        Column("attribute_name", "Attribute", "engine"),
        Column("raw_path", "Raw path", "governed", required=True),
        Column("raw_file", "Raw file", "linked", required=True, link=("details", "file_pattern")),
        Column("intg_path", "INTG path", "governed", required=True),
        Column("intg_table", "INTG table", "governed", required=True),
        Column("intg_column", "INTG column", "governed", required=True),
        Column("intg_type", "INTG type", "governed", required=True, rule_files=_TYPE_RULES),
        Column("curated_path", "Curated path", "governed", required=True),
        Column("curated_table", "Curated table", "governed", required=True),
        Column("curated_column", "Curated column", "governed", required=True),
        Column("curated_type", "Curated type", "governed", required=True, rule_files=_TYPE_RULES),
        Column(
            "transformation_rule",
            "Transformation rule",
            "governed",
            required=True,
            derived_link="logic",
        ),
        Column("pii_pci", "PII/PCI", "linked", link=("data_dictionary", "pii_pci")),
    ),
    "curated_to_asl": (
        Column("attribute_name", "Attribute", "engine"),
        Column(
            "curated_table", "Curated table", "linked", link=("raw_to_curated", "curated_table")
        ),
        Column(
            "curated_column", "Curated column", "linked", link=("raw_to_curated", "curated_column")
        ),
        Column("asl_table", "ASL table", "governed", required=True),
        Column("asl_column", "ASL column", "governed", required=True),
        Column("asl_type", "ASL type", "governed", required=True, rule_files=_TYPE_RULES),
        Column("business_attribute_name", "Business attribute name", "descriptive"),
        Column("constraints", "Constraints", "governed"),
        Column("logic", "Logic", "governed", required=True, derived_link="logic"),
        Column("sensitivity", "Sensitivity", "linked", link=("data_dictionary", "pii_pci")),
        Column("report_usage", "Report usage", "governed"),
    ),
}
COLUMN_INDEX: dict[str, dict[str, Column]] = {
    table: {column.key: column for column in columns} for table, columns in COLUMNS.items()
}
# Spec columns and the specification field each one reads.
_SPEC_FIELDS = {
    "data_type": "data_type",
    "length": "length",
    "required": "required",
    "technical_description": "description",
}
# Shown in place of an unresolved PII/PCI value, so it reads as "needs review".
UNRESOLVED_LABELS = {"pii_pci": "REVIEW", "sensitivity": "REVIEW"}
TYPE_COLUMNS = frozenset(
    {
        ("raw_to_curated", "intg_type"),
        ("raw_to_curated", "curated_type"),
        ("curated_to_asl", "asl_type"),
    }
)
PII_COLUMNS = frozenset({("data_dictionary", "pii_pci"), ("data_dictionary", "classification")})

_YES = frozenset({"y", "yes", "true", "1", "x"})
_NO = frozenset({"n", "no", "false", "0"})


def table_key(name: str) -> str | None:
    """The table a name refers to (its key, its label or dd/r2c/c2a), or None."""

    normalized = normalize_name(name)
    for key, label in TABLES.items():
        if normalized in (normalize_name(key), normalize_name(label)):
            return key
    return _TABLE_ALIASES.get(normalized)


def column_key(table: str, name: str) -> str | None:
    """The column of ``table`` a name refers to (its key or its label), or None."""

    normalized = normalize_name(name)
    for column in COLUMNS[table]:
        if normalized in (normalize_name(column.key), normalize_name(column.label)):
            return column.key
    return None


def allowed_sources(column: Column) -> frozenset[str]:
    """The sources a value of ``column`` may come from."""

    if column.kind == "spec":
        return frozenset({"analyst", "rule"} if column.rule_files else {"analyst"})
    return {
        "engine": frozenset(),
        "linked": frozenset({"analyst"}),
        "governed": frozenset({"analyst", "rule"}),
        "descriptive": frozenset({"analyst", "rule", "draft"}),
    }[column.kind]


def yes_no(value: str) -> str | None:
    """``value`` as Y or N, or None when it is neither."""

    lowered = value.strip().casefold()
    return "Y" if lowered in _YES else "N" if lowered in _NO else None


# --- Stored records ----------------------------------------------------------


class Value(BaseModel):
    """A decided value and where it came from."""

    value: str
    source: Source
    # The message it came from, or the rule file.
    ref: str | None = None
    at: str
    note: str | None = None


class InputRecord(BaseModel):
    kind: str
    message_id: str
    recorded_at: str
    # The session the input was carried over from (instructions only).
    inherited_from: str | None = None
    # Specification only: the tables read, what was skipped, and the lines of
    # attribute rows that could not be read.
    tables: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    skipped_lines: list[int] = Field(default_factory=list)


class Attribute(BaseModel):
    """An attribute as the specification gives it."""

    name: str
    message_id: str
    line: int
    data_type: str | None = None
    length: str | None = None
    required: str | None = None
    description: str | None = None
    key: str | None = None


class DerivedField(BaseModel):
    """An attribute the analyst declared that is not in the source file."""

    name: str
    message_id: str
    logic: str | None = None
    join_key: str | None = None


class AuditField(BaseModel):
    """A standard technical column that a rule file adds."""

    name: str
    rule_file: str


class Gate(BaseModel):
    status: Literal["pending", "open", "confirmed", "skipped"] = "pending"
    # The analyst message the gate opened in, and the one that confirmed it.
    opened_on: str | None = None
    closed_on: str | None = None


class Change(BaseModel):
    table: str
    attribute: str
    column: str
    old: str | None = None
    new: str | None = None


class FeedbackEntry(BaseModel):
    """One change request: the analyst message behind it and every value it changed."""

    at: str
    message_id: str
    summary: str
    changes: list[Change] = Field(default_factory=list)


class CheckResult(BaseModel):
    check: str
    passed: bool
    failing: list[str] = Field(default_factory=list)
    note: str | None = None


class Approval(BaseModel):
    by: str | None = None
    at: str
    message_id: str
    # The analyst's approving message, verbatim.
    message: str
    override_reason: str | None = None
    failed_checks: list[str] = Field(default_factory=list)


def _gates() -> dict[str, Gate]:
    return {gate: Gate() for gate in GATES}


def _generated() -> dict[str, bool]:
    return dict.fromkeys(GENERATED, False)


class Session(BaseModel):
    id: str
    version: int = 1
    # The approved session this version replaces.
    supersedes: str | None = None
    # Raised by every change; the state reducer keeps the higher revision.
    revision: int = 0
    source_system: str
    source: str
    status: Literal["draft", "in_progress", "approved"] = "draft"
    # None: no mode chosen, every gate stops.
    review_mode: Literal["fast", "moderate", "extensive"] | None = None
    created_at: str
    updated_at: str
    # Analyst message id -> its text, for every message the session was built from.
    messages: dict[str, str] = Field(default_factory=dict)
    inputs: list[InputRecord] = Field(default_factory=list)
    # Messages whose specification a later one replaced (record_input with
    # replace): still the source's text, not the analyst's.
    replaced_specifications: list[str] = Field(default_factory=list)
    attributes: list[Attribute] = Field(default_factory=list)
    derived_fields: list[DerivedField] = Field(default_factory=list)
    # Set when the analyst said the source has no derived fields.
    no_derived_fields: Value | None = None
    audit_fields: list[AuditField] = Field(default_factory=list)
    details: dict[str, Value] = Field(default_factory=dict)
    generated: dict[str, bool] = Field(default_factory=_generated)
    gates: dict[str, Gate] = Field(default_factory=_gates)
    # table -> row key (normalized attribute name, or "*" for the table default)
    # -> column -> value
    cells: dict[str, dict[str, dict[str, Value]]] = Field(default_factory=dict)
    feedback: list[FeedbackEntry] = Field(default_factory=list)
    validation: list[CheckResult] | None = None
    # The analyst message the final summary was produced in; approval must come later.
    ready_on: str | None = None
    approval: Approval | None = None

    @property
    def locked(self) -> bool:
        return self.status == "approved"


# Letters for session ids: no I, L or O, which read as digits. An id carries no
# long run of digits, which filters take for a card number and mask.
_ID_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"


def new_session_id(existing: Iterable[str], now: datetime) -> str:
    """The date and four random letters (20261103-KQXT), unique among ``existing``."""

    taken = set(existing)
    while True:
        code = "".join(secrets.choice(_ID_LETTERS) for _ in range(4))
        candidate = f"{now:%Y%m%d}-{code}"
        if candidate not in taken:
            return candidate


def timestamp(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


# --- Evidence ----------------------------------------------------------------


_CLAUSE_BREAK = re.compile(r"[.!?;\n]+")


def _has_word(text: str, words: Iterable[str]) -> bool:
    for word in words:
        word = " ".join(word.casefold().split())
        if word and re.search(rf"(?<![0-9a-z]){re.escape(word)}(?![0-9a-z])", text):
            return True
    return False


class Evidence:
    """Text a value must be found in: the analyst's messages, or a rule file."""

    def __init__(self, text: str) -> None:
        self.raw = " ".join(text.casefold().split())
        self.compact = normalize_name(text)
        # Sentences and lines, for answers that count only next to their topic.
        self.clauses = [" ".join(part.split()) for part in _CLAUSE_BREAK.split(text.casefold())]

    def says(self, value: str) -> bool:
        """Whether ``value`` appears, ignoring case, spacing and punctuation.

        A value of three letters or digits or fewer must appear as a word of its
        own, so "Y" is not found in "yes".
        """

        needle = normalize_name(value)
        if len(needle) > 3:
            return needle in self.compact
        return self.says_word([value])

    def says_word(self, words: Iterable[str]) -> bool:
        """Whether any of ``words`` appears as a word or phrase of its own."""

        return _has_word(self.raw, words)

    def says_about(self, topics: Iterable[str], words: Iterable[str]) -> bool:
        """Whether a sentence or line that mentions one of ``topics`` has one of ``words``.

        A "no" or a number says something only about what it answers: the "no"
        in "no, the CMDB number is different" says nothing about derived fields.
        """

        topics, words = tuple(topics), tuple(words)
        return any(
            any(topic in clause for topic in topics) and _has_word(clause, words)
            for clause in self.clauses
        )

    def says_number(self, number: int, *, about: Iterable[str]) -> bool:
        words = [str(number)]
        if number < len(_NUMBER_WORDS):
            words.append(_NUMBER_WORDS[number])
        if number == 0:
            words += _NONE_WORDS
        if number == 1:
            words.append("single")
        return self.says_about(about, words)


def _input_kinds(session: Session) -> dict[str, set[str]]:
    """Message id -> the kinds of input it was recorded as.

    A replaced specification still counts as one: replacing it does not make
    its text the analyst's.
    """

    kinds: dict[str, set[str]] = {}
    for record in session.inputs:
        kinds.setdefault(record.message_id, set()).add(record.kind)
    for message_id in session.replaced_specifications:
        kinds.setdefault(message_id, set()).add("specification")
    return kinds


def analyst_evidence(session: Session, current: Mapping[str, Any] | None) -> Evidence:
    """The analyst's own words to this session: its messages and the current one.

    The specification does not count. It is the source's description of the
    feed and names every attribute, so it is no evidence that the analyst chose
    a key, a classification or a target. A message recorded only as the
    specification (now or before a replacement) is left out, and so are the
    attribute tables of a message that holds the specification and other inputs.
    """

    kinds = _input_kinds(session)
    texts = []
    for message_id, text in session.messages.items():
        found = kinds.get(message_id, set())
        if found == {"specification"}:
            continue
        texts.append(without_attribute_tables(text) if "specification" in found else text)
    if current is not None and str(current.get("id")) not in session.messages:
        texts.append(str(current.get("text") or ""))
    return Evidence("\n".join(texts))


# --- Rows and resolution -----------------------------------------------------


@dataclass(frozen=True)
class Row:
    key: str
    name: str
    origin: Literal["specification", "derived", "audit"]
    attribute: Attribute | None = None
    derived: DerivedField | None = None
    audit: AuditField | None = None


@dataclass(frozen=True)
class Resolved:
    """A cell as shown: its value and where the value came from."""

    value: str | None
    # specification | analyst | rule | draft | engine | unresolved
    source: str
    ref: str | None = None
    note: str | None = None
    # The value is the table default ("*").
    from_default: bool = False


_ORIGIN_LABELS = {"specification": "Specification", "derived": "Derived", "audit": "Audit"}


def dd_rows(session: Session) -> list[Row]:
    """Specification attributes, then derived fields, then audit fields; one row per name."""

    rows: list[Row] = []
    seen: set[str] = set()

    def add(row: Row) -> None:
        if row.key and row.key not in seen:
            seen.add(row.key)
            rows.append(row)

    for attribute in session.attributes:
        add(Row(normalize_name(attribute.name), attribute.name, "specification", attribute))
    for derived in session.derived_fields:
        add(Row(normalize_name(derived.name), derived.name, "derived", derived=derived))
    for audit in session.audit_fields:
        add(Row(normalize_name(audit.name), audit.name, "audit", audit=audit))
    return rows


def primary_key_names(session: Session) -> list[str]:
    detail = session.details.get("primary_key")
    if detail is None:
        return []
    return [name.strip() for name in detail.value.split(",") if name.strip()]


class Tables:
    """The three tables of a session, worked out from what the session stores."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        """Re-read the rows after the session changed."""

        self.dd = dd_rows(self.session)
        self._pk = {normalize_name(name) for name in primary_key_names(self.session)}
        self._carried: list[Row] | None = None

    def rows(self, table: str) -> list[Row]:
        """The rows of ``table``. Both STTMs hold the attributes carried to Curated.

        An attribute whose carry flag is not set yet counts as carried, so its
        mapping is still asked for.
        """

        if table == "data_dictionary":
            return self.dd
        if self._carried is None:
            self._carried = [
                row
                for row in self.dd
                if self.resolve("data_dictionary", row, "carry_to_curated").value != "N"
            ]
        return self._carried

    def row(self, table: str, name: str) -> Row | None:
        key = normalize_name(name)
        return next((row for row in self.rows(table) if row.key == key), None)

    def resolve(self, table: str, row: Row, column: str) -> Resolved:
        """A cell's value: its own, else the specification's, a link's, or the table default."""

        spec = COLUMN_INDEX[table][column]
        if spec.kind == "engine":
            return Resolved(self._engine(row, column), "engine")
        cells = self.session.cells.get(table, {})
        own = cells.get(row.key, {}).get(column)
        if own is not None:
            return Resolved(own.value, own.source, own.ref, own.note)
        if spec.kind == "spec" and row.attribute is not None:
            value = getattr(row.attribute, _SPEC_FIELDS[column])
            if value is not None:
                return Resolved(value, "specification", row.attribute.message_id)
        if spec.derived_link and row.derived is not None:
            value = getattr(row.derived, spec.derived_link)
            if value:
                return Resolved(
                    value, "analyst", row.derived.message_id, "from the derived-field definition"
                )
        if spec.link is not None:
            linked = self._link(row, spec.link)
            if linked.value is not None:
                return linked
        return self.default(table, column)

    def default(self, table: str, column: str) -> Resolved:
        """The table default of a column ("*"), as a cell."""

        value = self.session.cells.get(table, {}).get("*", {}).get(column)
        if value is None:
            return Resolved(None, "unresolved")
        return Resolved(value.value, value.source, value.ref, "table default", from_default=True)

    def _engine(self, row: Row, column: str) -> str | None:
        if column == "attribute_name":
            return row.name
        if column == "origin":
            return _ORIGIN_LABELS[row.origin]
        if column == "primary_key":
            return "Y" if row.key in self._pk else None
        if column == "derivation_logic":
            return row.derived.logic if row.derived else None
        if column == "join_key":
            return row.derived.join_key if row.derived else None
        raise KeyError(column)

    def _link(self, row: Row, link: tuple[str, str]) -> Resolved:
        table, column = link
        if table == "details":
            detail = self.session.details.get(column)
            if detail is None:
                return Resolved(None, "unresolved")
            return Resolved(detail.value, detail.source, detail.ref, f"from {DETAILS[column]}")
        resolved = self.resolve(table, row, column)
        note = f"from {TABLES[table]} {COLUMN_INDEX[table][column].label}"
        return Resolved(resolved.value, resolved.source, resolved.ref, note)


# --- Sessions and inputs -----------------------------------------------------


class SessionError(ValueError):
    """A request the session cannot carry out; the message says why and what to do."""


def ensure_editable(session: Session) -> None:
    if session.locked:
        raise SessionError(
            f"Session {session.id} (version {session.version}) is approved and read-only. To "
            f"change it, start a new version: sttm_start_session(new_version_of='{session.id}')."
        )


def log_message(session: Session, message: Mapping[str, Any]) -> None:
    """Keep the analyst message a change was made in, verbatim."""

    session.messages.setdefault(str(message["id"]), str(message.get("text") or ""))


def start_session(
    sessions: Mapping[str, Session],
    *,
    source_system: str,
    source: str,
    message: Mapping[str, Any],
    now: datetime,
) -> tuple[Session, list[str]]:
    """A new session, with the latest instructions given for its source system and source.

    Returns the session and one line per input it carried over.
    """

    if not source_system.strip() or not source.strip():
        raise SessionError("Name both the source system and the source.")
    at = timestamp(now)
    session = Session(
        id=new_session_id(sessions, now),
        source_system=source_system.strip(),
        source=source.strip(),
        created_at=at,
        updated_at=at,
    )
    inherited = _inherit_instructions(session, sessions.values())
    log_message(session, message)
    return session, inherited


def _inherit_instructions(session: Session, others: Iterable[Session]) -> list[str]:
    """Copy the latest source-system and source instructions from earlier sessions.

    A message that also held the earlier session's specification comes over
    without its attribute tables: only the instructions carry over, and the old
    specification is no more the analyst's words here than it was there.
    """

    system, source = normalize_name(session.source_system), normalize_name(session.source)
    same_system = [other for other in others if normalize_name(other.source_system) == system]
    inherited = []
    for kind in ("source_system_instructions", "source_instructions"):
        candidates = [
            other
            for other in same_system
            if (kind == "source_system_instructions" or normalize_name(other.source) == source)
            and any(record.kind == kind for record in other.inputs)
        ]
        if not candidates:
            continue
        latest = max(candidates, key=lambda other: (other.updated_at, other.id))
        kinds = _input_kinds(latest)
        for record in latest.inputs:
            if record.kind == kind:
                session.inputs.append(
                    record.model_copy(update={"inherited_from": record.inherited_from or latest.id})
                )
                text = latest.messages.get(record.message_id, "")
                if "specification" in kinds.get(record.message_id, set()):
                    text = without_attribute_tables(text)
                session.messages[record.message_id] = text
        inherited.append(f"{INPUT_KINDS[kind]}: carried over from session {latest.id}")
    return inherited


def new_version(previous: Session, existing: Iterable[str], *, now: datetime) -> Session:
    """An editable copy of an approved session, as its next version."""

    if not previous.locked:
        raise SessionError(
            f"Session {previous.id} is not approved, so it can still be changed as it is; a new "
            "version is only for an approved session."
        )
    at = timestamp(now)
    return previous.model_copy(
        deep=True,
        update={
            "id": new_session_id(existing, now),
            "version": previous.version + 1,
            "supersedes": previous.id,
            "revision": 0,
            "status": "in_progress",
            "created_at": at,
            "updated_at": at,
            "validation": None,
            "ready_on": None,
            "approval": None,
        },
    )


def _asked_for_mode(evidence: Evidence, mode: str) -> bool:
    """Whether the analyst named ``mode`` as a review mode, not just used the word.

    "Use fast mode" and a bare "Fast" answering the question count; "I need
    this fast" does not.
    """

    return evidence.says_about(("mode", "review"), [mode]) or any(
        len(clause.split()) <= 3 and _has_word(clause, [mode]) for clause in evidence.clauses
    )


def set_review_mode(session: Session, mode: str, message: Mapping[str, Any]) -> Change | None:
    """Choose the review mode. A mode that skips a review needs the analyst's word for it."""

    ensure_editable(session)
    chosen = mode.strip().casefold()
    if chosen not in REVIEW_MODES:
        raise SessionError(
            f"Unknown review mode {mode!r}; use fast, moderate, extensive or default."
        )
    if REVIEW_MODES[chosen] and not _asked_for_mode(analyst_evidence(session, message), chosen):
        raise SessionError(
            f"The {chosen} review mode skips reviews, so the analyst has to ask for it by name; "
            "ask which mode they want."
        )
    new = None if chosen == "default" else chosen
    if new == session.review_mode:
        return None
    log_message(session, message)
    old = session.review_mode or "default"
    session.review_mode = new  # type: ignore[assignment]
    return Change(table="session", attribute="review mode", column="value", old=old, new=chosen)


@dataclass
class Recorded:
    """What :func:`record_input` did."""

    recorded: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    attributes_added: int = 0
    tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def record_input(
    session: Session,
    kinds: list[str],
    message: Mapping[str, Any],
    *,
    replace: bool,
    now: datetime,
) -> Recorded:
    """Record the analyst's current message, verbatim, as the given inputs.

    A specification may be split over several messages: each one adds its
    attributes. ``replace`` drops what earlier messages gave for these kinds.
    """

    ensure_editable(session)
    unknown = [kind for kind in kinds if kind not in INPUT_KINDS]
    if unknown or not kinds:
        raise SessionError(
            f"Unknown input kind(s) {unknown or kinds}; use: {', '.join(INPUT_KINDS)}."
        )
    text = str(message.get("text") or "")
    if not text.strip():
        raise SessionError(
            "The analyst's message has no text to record. Ask the analyst to paste the content "
            "as text; a specification as a table with a header row, pipe- or tab-separated."
        )
    message_id = str(message["id"])
    parsed = parse_specification(text) if "specification" in kinds else None
    if parsed is not None and not parsed.attributes:
        raise SessionError(
            "No attribute table was found in the analyst's message, so nothing was recorded. A "
            "specification needs a table with a header row naming the attribute column, pipe- "
            "or tab-separated. " + " ".join(parsed.notes)
        )
    if parsed is not None and "asl_schema" in kinds and len(parsed.tables) > 1:
        raise SessionError(
            "The message holds more than one attribute table and is being recorded as both the "
            "specification and the ASL schema, so the schema's rows would be read as "
            "specification attributes. Nothing was recorded: ask the analyst to send the ASL "
            "schema in a message of its own."
        )
    result = Recorded()
    at = timestamp(now)
    for kind in kinds:
        if any(r.kind == kind and r.message_id == message_id for r in session.inputs):
            result.already.append(INPUT_KINDS[kind])
            continue
        if replace:
            dropped = {r.message_id for r in session.inputs if r.kind == kind}
            session.inputs = [r for r in session.inputs if r.kind != kind]
            if kind == "specification":
                session.attributes = [a for a in session.attributes if a.message_id not in dropped]
                session.replaced_specifications += sorted(
                    dropped - set(session.replaced_specifications)
                )
        record = InputRecord(kind=kind, message_id=message_id, recorded_at=at)
        if kind == "specification" and parsed is not None:
            record.tables, record.notes = parsed.tables, parsed.notes
            record.skipped_lines = parsed.skipped_lines
            session.attributes += [
                Attribute(message_id=message_id, **asdict(attribute))
                for attribute in parsed.attributes
            ]
            result.attributes_added = len(parsed.attributes)
            result.tables, result.notes = parsed.tables, parsed.notes
        session.inputs.append(record)
        result.recorded.append(INPUT_KINDS[kind])
    if "specification" in kinds:
        _key_from_specification(session, now)
    log_message(session, message)
    prune_cells(session)
    return result


def _key_from_specification(session: Session, now: datetime) -> None:
    """Take the primary key from the specification's key column, unless the analyst set one."""

    current = session.details.get("primary_key")
    if current is not None and current.source != "specification":
        return
    keyed = [attribute for attribute in session.attributes if attribute.key == "PK"]
    if not keyed:
        session.details.pop("primary_key", None)
        return
    session.details["primary_key"] = Value(
        value=", ".join(attribute.name for attribute in keyed),
        source="specification",
        ref=keyed[0].message_id,
        at=timestamp(now),
    )


def prune_cells(session: Session) -> None:
    """Drop the values of attributes that are no longer in the session."""

    keys = {row.key for row in dd_rows(session)} | {"*"}
    for table, rows in session.cells.items():
        session.cells[table] = {key: row for key, row in rows.items() if key in keys and row}


# --- File details, derived fields and audit fields ---------------------------


@dataclass
class Outcome:
    """What a change did: the values it changed and the requests it refused."""

    changes: list[Change] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def _not_said(label: str, value: str, *, can_propose: bool) -> str:
    hint = (
        "Ask the analyst, or propose it (source='draft') for the analyst to confirm at the input "
        "review."
        if can_propose
        else "Ask the analyst."
    )
    return f"{label}: {value!r} is not in the analyst's messages to this session. {hint}"


def _proposals_closed(session: Session) -> str | None:
    """Why file details can't be proposed now; None while the input review is still ahead."""

    if "input_review" in REVIEW_MODES[session.review_mode or "default"]:
        return "this review mode skips"
    if session.gates["input_review"].status not in ("pending", "open"):
        return "is over"
    return None


def _names(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(name).strip() for name in raw or [] if str(name).strip()]


def _detail_value(
    key: str, raw: Any, tables: Tables, evidence: Evidence, *, proposed: bool, can_propose: bool
) -> str:
    """``raw`` as stored for detail ``key``; raises SessionError when it can't be accepted."""

    label = DETAILS[key]
    if proposed and key not in PROPOSABLE_DETAILS:
        raise SessionError(f"{label}: cannot be proposed; only the analyst can give it.")
    can_propose = can_propose and key in PROPOSABLE_DETAILS
    if key == "primary_key":
        names = _names(raw)
        if not names:
            raise SessionError(f"{label}: name at least one attribute.")
        found = []
        for name in names:
            row = tables.row("data_dictionary", name)
            if row is None or row.origin == "audit":
                raise SessionError(
                    f"{label}: {name} is not a specification attribute or derived field."
                )
            if not proposed and not evidence.says(name):
                raise SessionError(_not_said(label, name, can_propose=can_propose))
            found.append(row.name)
        return ", ".join(found)
    if key == "join_keys":
        names = _names(raw)
        if not names:
            if not proposed and not evidence.says_about(("join",), _NONE_WORDS):
                raise SessionError(
                    f"{label}: an empty list records that there are none, which the analyst has "
                    "not said."
                )
            return "none"
        for name in names:
            if not proposed and not evidence.says(name):
                raise SessionError(_not_said(label, name, can_propose=can_propose))
        return ", ".join(names)
    if key in ("header_rows", "trailer_rows"):
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise SessionError(f"{label}: {raw!r} is not a whole number.") from None
        if number < 0:
            raise SessionError(f"{label}: {number} is negative.")
        about = ("header",) if key == "header_rows" else ("trailer", "footer")
        if not proposed and not evidence.says_number(number, about=about):
            raise SessionError(_not_said(label, str(number), can_propose=can_propose))
        return str(number)
    value = str(raw).strip()
    if not value:
        raise SessionError(f"{label}: empty value.")
    if key == "delimiter":
        canonical = next(
            (name for name, ways in DELIMITERS.items() if value.casefold() in ways), None
        )
        if canonical is not None:
            # A symbol on its own is no evidence: the pasted tables are full of them.
            words = [way for way in DELIMITERS[canonical] if any(c.isalpha() for c in way)]
            if not proposed and not evidence.says_word(words):
                raise SessionError(_not_said(label, canonical, can_propose=can_propose))
            return canonical
    if not proposed and not evidence.says(value):
        raise SessionError(_not_said(label, value, can_propose=can_propose))
    return value


def _described(value: Value | None) -> str | None:
    if value is None:
        return None
    return f"{value.value} ({'proposed' if value.source == 'draft' else value.source})"


def set_details(
    session: Session,
    values: Mapping[str, Any],
    message: Mapping[str, Any],
    *,
    proposed: bool,
    now: datetime,
) -> Outcome:
    """Set file details. Each is checked on its own; the valid ones are kept.

    Proposals are confirmed at the input review, so they are only taken while
    that review is still ahead.
    """

    ensure_editable(session)
    tables = Tables(session)
    evidence = analyst_evidence(session, message)
    outcome = Outcome()
    at = timestamp(now)
    closed = _proposals_closed(session)
    if proposed and closed:
        outcome.refused.append(
            f"Nothing was proposed: proposals are confirmed at the input review, which {closed}. "
            "Ask the analyst for these values."
        )
        return outcome
    # In the order the details are listed, whatever order they were given in.
    for key, label in DETAILS.items():
        raw = values.get(key)
        if raw is None:
            continue
        try:
            value = _detail_value(
                key, raw, tables, evidence, proposed=proposed, can_propose=closed is None
            )
        except SessionError as error:
            outcome.refused.append(str(error))
            continue
        old = session.details.get(key)
        new = Value(
            value=value,
            source="draft" if proposed else "analyst",
            ref=str(message["id"]),
            at=at,
            note="proposed by the agent" if proposed else None,
        )
        if old is not None and (old.value, old.source) == (new.value, new.source):
            continue
        session.details[key] = new
        outcome.changes.append(
            Change(
                table="file_details",
                attribute=label,
                column="value",
                old=_described(old),
                new=_described(new),
            )
        )
    log_message(session, message)
    return outcome


def _missing_derivation(derived: DerivedField) -> str | None:
    missing = [
        label
        for label, value in (("logic", derived.logic), ("join key", derived.join_key))
        if not value
    ]
    return " or ".join(missing) or None


def set_derived_fields(
    session: Session,
    upserts: list[Mapping[str, Any]],
    removals: list[str],
    no_derived_fields: bool | None,
    message: Mapping[str, Any],
    *,
    now: datetime,
) -> Outcome:
    """Declare, change or remove derived fields, or record that there are none.

    A derived field, its logic and its join key all have to be in the analyst's
    own words: the agent copies them, it never writes them.
    """

    ensure_editable(session)
    evidence = analyst_evidence(session, message)
    outcome = Outcome()
    message_id = str(message["id"])
    taken = {normalize_name(a.name) for a in [*session.attributes, *session.audit_fields]}
    for raw in upserts:
        name = str(raw.get("name") or "").strip()
        key = normalize_name(name)
        if not key:
            outcome.refused.append("A derived field needs a name.")
            continue
        if key in taken:
            outcome.refused.append(
                f"{name} is a specification attribute or an audit field, not a derived field."
            )
            continue
        current = next((d for d in session.derived_fields if normalize_name(d.name) == key), None)
        if current is None:
            if not evidence.says(name):
                outcome.refused.append(
                    f"Derived field {name!r} is not in the analyst's messages to this session. "
                    "Derived fields come from the analyst only: use the analyst's name for it, "
                    "or ask."
                )
                continue
            current = DerivedField(name=name, message_id=message_id)
            session.derived_fields.append(current)
            outcome.changes.append(
                Change(table="derived_fields", attribute=name, column="declared", new=name)
            )
        for attribute in ("logic", "join_key"):
            if raw.get(attribute) is None:
                continue
            value = str(raw[attribute]).strip() or None
            label = attribute.replace("_", " ")
            if value is not None and not evidence.says(value):
                outcome.refused.append(
                    f"Derived field {current.name}: the {label} {value!r} is not in the "
                    "analyst's messages; copy the analyst's definition, do not write one."
                )
                continue
            old = getattr(current, attribute)
            if value != old:
                setattr(current, attribute, value)
                current.message_id = message_id
                outcome.changes.append(
                    Change(
                        table="derived_fields",
                        attribute=current.name,
                        column=attribute,
                        old=old,
                        new=value,
                    )
                )
    for name in removals:
        key = normalize_name(name)
        kept = [d for d in session.derived_fields if normalize_name(d.name) != key]
        if len(kept) == len(session.derived_fields):
            outcome.refused.append(f"{name} is not a derived field of this session.")
            continue
        session.derived_fields = kept
        outcome.changes.append(
            Change(table="derived_fields", attribute=name, column="declared", old=name)
        )
    if no_derived_fields is not None:
        if no_derived_fields and session.derived_fields:
            outcome.refused.append(
                "The session has derived fields; remove them before recording that there are none."
            )
        elif no_derived_fields and not evidence.says_about(("deriv",), _NONE_WORDS):
            outcome.refused.append(
                "The analyst has not said that there are no derived fields; ask them."
            )
        elif no_derived_fields != (session.no_derived_fields is not None):
            session.no_derived_fields = (
                Value(value="none", source="analyst", ref=message_id, at=timestamp(now))
                if no_derived_fields
                else None
            )
            outcome.changes.append(
                Change(
                    table="derived_fields",
                    attribute="(all)",
                    column="none declared",
                    new="yes" if no_derived_fields else "no",
                )
            )
    if session.derived_fields:
        session.no_derived_fields = None
    log_message(session, message)
    prune_cells(session)
    return outcome


def set_audit_fields(
    session: Session, names: list[str], rule_file: str | None, message: Mapping[str, Any]
) -> Outcome:
    """Replace the audit fields with ``names``, each of which must be in ``rule_file``.

    All or nothing: when any name is refused, nothing changes.
    """

    ensure_editable(session)
    names = _names(names)
    if names:
        text = rule_file_text(rule_file) if rule_file else None
        if text is None:
            raise SessionError(
                f"Audit fields come from a rule file, and {rule_file} is not present."
                if rule_file
                else "Audit fields come from a rule file: name it (rule_file)."
            )
        in_file = Evidence(text)
        taken = {normalize_name(a.name) for a in [*session.attributes, *session.derived_fields]}
        problems = [f"{name} is not in {rule_file}." for name in names if not in_file.says(name)]
        problems += [
            f"{name} is already a specification attribute or derived field."
            for name in names
            if normalize_name(name) in taken
        ]
        problems += [
            f"{name} is listed more than once."
            for name in duplicate_names([AuditField(name=name, rule_file="") for name in names])
        ]
        if problems:
            raise SessionError("No audit fields were changed. " + " ".join(problems))
    old = [audit.name for audit in session.audit_fields]
    session.audit_fields = [AuditField(name=name, rule_file=rule_file or "") for name in names]
    log_message(session, message)
    prune_cells(session)
    outcome = Outcome()
    if old != names:
        outcome.changes.append(
            Change(
                table="audit_fields",
                attribute="(all)",
                column="names",
                old=", ".join(old) or None,
                new=", ".join(names) or None,
            )
        )
    return outcome


# --- Cell updates ------------------------------------------------------------

MAX_UPDATES = 200


@dataclass(frozen=True)
class _UpdateContext:
    source: str
    rule_file: str | None
    in_rule: Evidence | None
    evidence: Evidence
    message_id: str
    at: str


def update_cells(
    session: Session,
    updates: list[Mapping[str, Any]],
    message: Mapping[str, Any],
    *,
    source: str,
    rule_file: str | None,
    now: datetime,
) -> Outcome:
    """Set or clear cells. Each update is checked on its own; the valid ones are applied."""

    ensure_editable(session)
    if source not in ("analyst", "rule", "draft"):
        raise SessionError("source must be 'analyst', 'rule' or 'draft'.")
    if len(updates) > MAX_UPDATES:
        raise SessionError(f"At most {MAX_UPDATES} updates per call; send the rest in batches.")
    in_rule = None
    if source == "rule":
        if not rule_file:
            raise SessionError("A rule-sourced value has to name its rule file (rule_file).")
        text = rule_file_text(rule_file)
        if text is None:
            present = ", ".join(available_rule_files()) or "none"
            raise SessionError(
                f"{rule_file} is not present (present: {present}), so no value can come from "
                "it. Leave the value unresolved, or ask the analyst."
            )
        in_rule = Evidence(text)
    context = _UpdateContext(
        source=source,
        rule_file=rule_file if source == "rule" else None,
        in_rule=in_rule,
        evidence=analyst_evidence(session, message),
        message_id=str(message["id"]),
        at=timestamp(now),
    )
    tables = Tables(session)
    outcome = Outcome()
    for update in updates:
        try:
            change = _apply_update(session, tables, update, context)
        except SessionError as error:
            outcome.refused.append(str(error))
            continue
        if change is not None:
            outcome.changes.append(change)
    log_message(session, message)
    return outcome


def _apply_update(
    session: Session, tables: Tables, update: Mapping[str, Any], context: _UpdateContext
) -> Change | None:
    table = table_key(str(update.get("table") or ""))
    if table is None:
        raise SessionError(f"Unknown table {update.get('table')!r}; use: {', '.join(TABLES)}.")
    attribute = str(update.get("attribute") or "").strip()
    where = f"{TABLES[table]}, {attribute or '(no attribute)'}, {update.get('column')}"
    column = column_key(table, str(update.get("column") or ""))
    if column is None:
        raise SessionError(
            f"{where}: unknown column; the columns are "
            + ", ".join(c.key for c in COLUMNS[table])
            + "."
        )
    spec = COLUMN_INDEX[table][column]
    if spec.kind == "engine":
        raise SessionError(
            f"{where}: worked out from the specification and the derived fields; change those "
            "instead."
        )
    row = None
    if attribute != "*":
        row = tables.row(table, attribute)
        if row is None:
            in_dd = tables.row("data_dictionary", attribute) is not None
            raise SessionError(
                f"{where}: {attribute!r} is not a row of the {TABLES[table]}"
                + (
                    ": its carry_to_curated is N."
                    if in_dd
                    else ": it is not in the specification, the derived fields or the audit "
                    "fields, and attributes are never added here."
                )
            )
    key = "*" if row is None else row.key
    before = tables.resolve(table, row, column) if row else tables.default(table, column)
    raw = update.get("value")
    new: Value | None = None
    if raw is None or not str(raw).strip():
        if column not in session.cells.get(table, {}).get(key, {}):
            raise SessionError(f"{where}: has no value of its own to clear.")
    else:
        new = _checked_value(where, spec, str(raw).strip(), context)
    row_cells = session.cells.setdefault(table, {}).setdefault(key, {})
    if new is None:
        row_cells.pop(column, None)
    else:
        row_cells[column] = new
    if not row_cells:
        del session.cells[table][key]
    tables.refresh()
    after = tables.resolve(table, row, column) if row else tables.default(table, column)
    if (before.value, before.source) == (after.value, after.source):
        return None
    name = row.name if row else "*"
    if row is None:
        reach = sum(
            1 for other in tables.rows(table) if tables.resolve(table, other, column).from_default
        )
        name = f"* (table default, now used by {reach} row(s))"
    return Change(table=table, attribute=name, column=column, old=before.value, new=after.value)


def _checked_value(where: str, spec: Column, value: str, context: _UpdateContext) -> Value:
    if context.source not in allowed_sources(spec):
        allowed = " or ".join(sorted(allowed_sources(spec)))
        raise SessionError(
            f"{where}: a {spec.kind} column takes values from {allowed}, not {context.source}."
        )
    if spec.yes_no:
        normalized = yes_no(value)
        if normalized is None:
            raise SessionError(f"{where}: {value!r} is not Y or N.")
        if context.source == "analyst" and not context.evidence.says_word(spec.topics):
            raise SessionError(
                f"{where}: the analyst has not said anything about {spec.label.casefold()} in "
                "their instructions or feedback to this session (a pasted specification does "
                "not count). Ask the analyst."
            )
        value = normalized
    elif context.source == "analyst" and not context.evidence.says(value):
        raise SessionError(
            f"{where}: {value!r} is not in the analyst's instructions or feedback to this "
            "session (a pasted specification does not count). Ask the analyst."
        )
    if context.source == "rule":
        if spec.rule_files and context.rule_file not in spec.rule_files:
            raise SessionError(
                f"{where}: decided by {' or '.join(spec.rule_files)}, not {context.rule_file}."
            )
        if spec.quoted and context.in_rule is not None and not context.in_rule.says(value):
            raise SessionError(f"{where}: {value!r} is not in {context.rule_file}.")
    return Value(
        value=value,
        source=context.source,  # type: ignore[arg-type]
        ref=context.rule_file if context.source == "rule" else context.message_id,
        at=context.at,
    )


# --- Workflow ----------------------------------------------------------------


def input_blockers(session: Session) -> list[str]:
    """What has to be fixed before anything is generated."""

    recorded = {record.kind for record in session.inputs}
    problems = [
        f"{INPUT_KINDS[kind]}: not given yet." for kind in REQUIRED_INPUTS if kind not in recorded
    ]
    for name, group in duplicate_names(session.attributes).items():
        lines = ", ".join(str(attribute.line) for attribute in group)
        problems.append(
            f"Duplicate attribute name {name} (specification lines {lines}); the analyst has to "
            "say which one is right."
        )
    return problems


def gaps(session: Session) -> list[str]:
    """Missing or unconfirmed information to ask the analyst about."""

    found: list[str] = []
    untyped = [a.name for a in session.attributes if not a.data_type]
    if untyped:
        found.append(
            f"No data type in the specification for {len(untyped)} attribute(s): "
            f"{_listed(untyped)}."
        )
    for key in DETAILS:
        detail = session.details.get(key)
        if detail is None and key in ASKED_DETAILS:
            found.append(f"{DETAILS[key]}: not given.")
        elif detail is not None and detail.source == "draft":
            found.append(f"{DETAILS[key]}: {detail.value} is proposed, not confirmed.")
    if not session.derived_fields and session.no_derived_fields is None:
        found.append(
            "Derived fields: none declared, and the analyst has not said whether there are any."
        )
    for derived in session.derived_fields:
        missing = _missing_derivation(derived)
        if missing:
            found.append(f"Derived field {derived.name}: no {missing}.")
    for record in session.inputs:
        found += record.notes
    if any(record.kind == "asl_schema" for record in session.inputs):
        found.append(
            "An ASL schema was recorded. Reconciling the Curated-to-ASL STTM against it is not "
            "supported yet; its names count only where they are set as values."
        )
    return found


@dataclass
class Step:
    """Where :func:`advance` stopped."""

    kind: Literal["blocked", "gate", "generated", "ready"]
    gate: str | None = None
    stage: str | None = None
    problems: list[str] = field(default_factory=list)
    # Why an open gate was not confirmed.
    reminder: str | None = None


def advance(session: Session, message: Mapping[str, Any], *, confirm_review: bool) -> Step:
    """Move the session to its next stop: a gate, a generated stage, or ready for approval.

    A gate opens in one analyst message and can only be confirmed in a later
    one, so a review cannot be opened and accepted in the same turn.
    """

    ensure_editable(session)
    log_message(session, message)
    message_id = str(message["id"])
    blockers = input_blockers(session)
    if blockers:
        return Step("blocked", problems=blockers)
    skipped = REVIEW_MODES[session.review_mode or "default"]
    for stage in STAGES:
        if stage in GENERATED:
            if not session.generated[stage]:
                session.generated[stage] = True
                session.status = "in_progress"
                return Step("generated", stage=stage)
            continue
        gate = session.gates[stage]
        if gate.status in ("confirmed", "skipped"):
            continue
        if stage in skipped:
            gate.status = "skipped"
            continue
        if gate.status == "pending":
            gate.status, gate.opened_on = "open", message_id
            return Step("gate", gate=stage)
        if confirm_review and message_id != gate.opened_on:
            gate.status, gate.closed_on = "confirmed", message_id
            if stage == "input_review":
                _confirm_proposals(session, message_id)
            continue
        reminder = (
            "It opened in this same analyst message; the analyst has to answer it first."
            if message_id == gate.opened_on
            else "Pass confirm_review=True only when the analyst's message accepts this review."
        )
        return Step("gate", gate=stage, reminder=reminder)
    session.validation = validate(session)
    if session.ready_on is None:
        session.ready_on = message_id
    return Step("ready")


def _confirm_proposals(session: Session, message_id: str) -> None:
    """The analyst accepted the input review, and with it the details proposed there."""

    for key, detail in session.details.items():
        if detail.source == "draft":
            session.details[key] = detail.model_copy(
                update={
                    "source": "analyst",
                    "ref": message_id,
                    "note": "confirmed at the input review",
                }
            )


def approve(
    session: Session,
    message: Mapping[str, Any],
    *,
    override_reason: str | None,
    by: str | None,
    now: datetime,
) -> list[CheckResult]:
    """Approve and lock the session; raises SessionError when it cannot be approved."""

    ensure_editable(session)
    message_id = str(message["id"])
    if session.ready_on is None:
        raise SessionError(
            "The session is not ready for approval: call sttm_advance until it reports the "
            "final summary, show it to the analyst, and approve when the analyst says so."
        )
    if session.ready_on == message_id:
        raise SessionError(
            "The final summary was produced in this same analyst message; the analyst has to "
            "see it and approve in a later message."
        )
    results = validate(session)
    failed = [result.check for result in results if not result.passed]
    reason = (override_reason or "").strip() or None
    if failed and reason is None:
        raise SessionError(
            "Validation failed (" + ", ".join(failed) + "). Approving anyway needs the "
            "analyst's reason, in their own words (override_reason)."
        )
    if failed and not Evidence(str(message.get("text") or "")).says(reason or ""):
        raise SessionError(
            "The override reason has to be the analyst's own words from the approving message; "
            "quote it, do not write one."
        )
    log_message(session, message)
    session.validation = results
    session.approval = Approval(
        by=by,
        at=timestamp(now),
        message_id=message_id,
        message=str(message.get("text") or ""),
        override_reason=reason if failed else None,
        failed_checks=failed,
    )
    session.status = "approved"
    return results


# --- Validation --------------------------------------------------------------

CHECKS: dict[str, str] = {
    "spec_in_dd": "Every specification attribute is in the Data Dictionary",
    "carry_forward_in_r2c": "Every carried-forward attribute has a complete Raw-to-Curated row",
    "pii_consistent": "PII/PCI is the same in the Data Dictionary and both STTMs",
    "pii_classified": "Every attribute has a PII/PCI flag and a classification",
    "derived_complete": "Every derived field has logic and a join key",
    "keys_present": "The primary key is set and names Data Dictionary attributes",
    "types_follow_mapping": (
        "Every INTG, Curated and ASL type is set, from the type mapping or by the analyst"
    ),
    "no_unspecified_attributes": (
        "Every attribute comes from the specification, the analyst or a rule file"
    ),
    "file_details_complete": "The file details are complete",
    "required_values": "Every other required column has a value",
}
MAX_FAILING = 50


def _check(check: str, failing: list[str], note: str | None = None) -> CheckResult:
    shown = failing[:MAX_FAILING]
    if len(failing) > MAX_FAILING:
        shown.append(f"... and {len(failing) - MAX_FAILING} more")
    return CheckResult(check=check, passed=not failing, failing=shown, note=note)


def validate(session: Session) -> list[CheckResult]:
    """Run every check in :data:`CHECKS`; each one names the attributes that fail it."""

    tables = Tables(session)
    present = available_rule_files()
    dd = tables.rows("data_dictionary")
    carried = tables.rows("raw_to_curated")
    results = []

    failing = [
        f"{name}: duplicate name (lines {', '.join(str(a.line) for a in group)})"
        for name, group in duplicate_names(session.attributes).items()
    ]
    for record in session.inputs:
        failing += [f"line {line}: attribute row with no name" for line in record.skipped_lines]
    results.append(_check("spec_in_dd", failing))

    failing = []
    for row in dd:
        carry = tables.resolve("data_dictionary", row, "carry_to_curated").value
        if carry is None:
            failing.append(f"{row.name}: carry_to_curated not set")
            continue
        if carry != "Y":
            continue
        missing = [
            column.key
            for column in COLUMNS["raw_to_curated"]
            if column.required
            and ("raw_to_curated", column.key) not in TYPE_COLUMNS
            and tables.resolve("raw_to_curated", row, column.key).value is None
        ]
        if missing:
            failing.append(f"{row.name}: {', '.join(missing)}")
    results.append(_check("carry_forward_in_r2c", failing))

    failing = []
    for row in carried:
        values = {
            label: tables.resolve(table, row, column).value
            for label, table, column in (
                ("DD", "data_dictionary", "pii_pci"),
                ("R2C", "raw_to_curated", "pii_pci"),
                ("C2A", "curated_to_asl", "sensitivity"),
            )
        }
        if len({(value or "").casefold() for value in values.values()}) > 1:
            failing.append(
                f"{row.name}: " + ", ".join(f"{k} {v or 'unset'}" for k, v in values.items())
            )
    results.append(_check("pii_consistent", failing))

    failing = []
    for row in dd:
        missing = [
            column
            for column in ("pii_pci", "classification")
            if tables.resolve("data_dictionary", row, column).value is None
        ]
        if missing:
            failing.append(f"{row.name}: {', '.join(missing)}")
    results.append(_check("pii_classified", failing))

    failing = [
        f"{derived.name}: no {missing}"
        for derived in session.derived_fields
        if (missing := _missing_derivation(derived))
    ]
    results.append(_check("derived_complete", failing))

    failing, note = [], None
    detail = session.details.get("primary_key")
    if detail is None:
        failing.append("no primary key")
    else:
        failing += [
            f"{name}: not in the Data Dictionary"
            for name in primary_key_names(session)
            if tables.row("data_dictionary", name) is None
        ]
        if detail.source == "draft":
            note = f"The primary key ({detail.value}) is the agent's proposal, not confirmed."
    results.append(_check("keys_present", failing, note))

    failing = []
    for table, column in sorted(TYPE_COLUMNS):
        for row in tables.rows(table):
            resolved = tables.resolve(table, row, column)
            if resolved.value is None:
                failing.append(f"{row.name}: {column} not set")
            elif resolved.source == "rule" and resolved.ref not in present:
                failing.append(f"{row.name}: {column} cites {resolved.ref}, which is not present")
    note = (
        None
        if "data-type-mapping.md" in present
        else "data-type-mapping.md is not present, so types can only come from the analyst."
    )
    results.append(_check("types_follow_mapping", failing, note))

    evidence = analyst_evidence(session, None)
    failing = [
        f"{derived.name}: derived field not named in the analyst's messages"
        for derived in session.derived_fields
        if not evidence.says(derived.name)
    ]
    for audit in session.audit_fields:
        text = rule_file_text(audit.rule_file) if audit.rule_file in present else None
        if text is None or not Evidence(text).says(audit.name):
            failing.append(f"{audit.name}: audit field not found in {audit.rule_file}")
    results.append(_check("no_unspecified_attributes", failing))

    failing, proposed = [], []
    for key in REQUIRED_DETAILS:
        value = session.details.get(key)
        if value is None:
            failing.append(f"{DETAILS[key]}: not given")
        elif value.source == "draft":
            proposed.append(DETAILS[key])
    note = f"Proposed, not confirmed: {', '.join(proposed)}." if proposed else None
    results.append(_check("file_details_complete", failing, note))

    failing = []
    for table in ("data_dictionary", "curated_to_asl"):
        for row in tables.rows(table):
            missing = [
                column.key
                for column in COLUMNS[table]
                if column.required
                and (table, column.key) not in TYPE_COLUMNS | PII_COLUMNS
                and column.key != "carry_to_curated"
                and tables.resolve(table, row, column.key).value is None
            ]
            if missing:
                failing.append(f"{TABLES[table]} {row.name}: {', '.join(missing)}")
    results.append(_check("required_values", failing))
    return results


def _listed(names: list[str], limit: int = 20) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


__all__ = [
    "CHECKS",
    "COLUMNS",
    "COLUMN_INDEX",
    "DETAILS",
    "GATES",
    "GENERATED",
    "INPUT_KINDS",
    "REQUIRED_DETAILS",
    "REQUIRED_INPUTS",
    "REVIEW_MODES",
    "TABLES",
    "UNRESOLVED_LABELS",
    "Approval",
    "Attribute",
    "AuditField",
    "Change",
    "CheckResult",
    "Column",
    "DerivedField",
    "Evidence",
    "FeedbackEntry",
    "Gate",
    "InputRecord",
    "Outcome",
    "Recorded",
    "Resolved",
    "Row",
    "Session",
    "SessionError",
    "Step",
    "Tables",
    "Value",
    "advance",
    "allowed_sources",
    "analyst_evidence",
    "approve",
    "column_key",
    "dd_rows",
    "gaps",
    "input_blockers",
    "new_session_id",
    "new_version",
    "primary_key_names",
    "prune_cells",
    "record_input",
    "set_audit_fields",
    "set_derived_fields",
    "set_details",
    "set_review_mode",
    "start_session",
    "table_key",
    "timestamp",
    "update_cells",
    "validate",
    "yes_no",
]
