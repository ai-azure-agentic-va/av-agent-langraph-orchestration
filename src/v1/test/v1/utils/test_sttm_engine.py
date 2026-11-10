"""Tests for the STTM session engine (``v1.core.tools.sttm``), with no model.

The engine keeps an STTM authoring session: the analyst's inputs, the file
details, the derived and audit fields, the three tables and the review gates.
The tests drive it directly:

- reading a pasted specification: header roles, types, lengths and required
  flags, HTML and MDX tables, chat prefixes, and the tables and rows it does
  not read (values tables, tables that lost their line breaks);
- missing inputs: nothing is generated before the instructions and the
  specification are in, and what is still open is listed; a message with
  several sources' specifications gives each session the table its passage
  chooses;
- passages: an input but the specification is recorded only with words copied
  from the analyst's message, outside the specification's tables, instructions
  only from a part of it the analyst names as instructions, and the views
  quote them;
- nothing invented: a key, a file detail, a derived field or a value the
  analyst did not give is refused, and a pasted specification (even a replaced
  or inherited one) is not the analyst's word;
- the review modes and their gates, proposals, one-value corrections, links,
  and validation that names the failing attributes;
- rule files: a value can cite only a file that is present, and a closed
  vocabulary only the words in it;
- approval, new versions and export.

Runs standalone (``python test_sttm_engine.py``) or under pytest.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import v1.core.tools.sttm.rules as rules_mod
from v1.core.tools.sttm.export import export_session
from v1.core.tools.sttm.render import inputs_view, summary_view
from v1.core.tools.sttm.rules import available_rule_files, rule_files_summary
from v1.core.tools.sttm.session import (
    CHECKS,
    MAX_PASSAGE_CHARS,
    Change,
    Session,
    SessionError,
    Step,
    Tables,
    advance,
    approve,
    find_passage,
    gaps,
    input_blockers,
    new_session_id,
    new_version,
    record_input,
    set_audit_fields,
    set_derived_fields,
    set_details,
    set_review_mode,
    start_session,
    update_cells,
    validate,
)
from v1.core.tools.sttm.spec import parse_specification, without_attribute_tables

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

_INSTRUCTIONS = "Source-system instructions: CoreBank, daily accounts feed."
# Lines 2-6 of the first analyst message; CUSTOMER_ID is on line 4.
_SPEC = (
    "| Field Name | Data Type | Length | Required | Description | Key |\n"
    "|---|---|---|---|---|---|\n"
    "| CUSTOMER_ID | VARCHAR | 20 | Y | Customer identifier | PK |\n"
    "| ACCOUNT_NO | VARCHAR | 16 | Y | Account number | |\n"
    "| OPEN_DATE | DATE | | N | Date opened | |"
)
_PII_RULES = (
    "Classification levels: Public, Internal, Confidential.\n"
    "PII/PCI flags: PII, PCI, NONE.\n"
    "Handling: Mask, Tokenize."
)
_TYPE_RULES = "Specification types map to STRING, DATE and TIMESTAMP."
_MAPPING_RULES = "Audit fields: LOAD_TS."


def _msg(message_id: str, text: str) -> dict[str, Any]:
    return {"id": message_id, "text": text}


def _document(name: str, text: str) -> str:
    """An attached file's text, as a message holds it."""

    return f'<document name="{name}" pages="1">\n{text}\n</document>'


def _skill(name: str, text: str) -> str:
    """A skill's block, as the frontend adds it to a message."""

    return (
        f'<user_skill name="{name}">\nFor this message, use my skill "{name}". These are my own '
        "instructions for this kind of task; follow them when you work on my request and compose "
        f"your answer.\n\n{text}\n</user_skill>"
    )


def _inputs(*kinds: str, passage: str = _INSTRUCTIONS) -> list[dict[str, str]]:
    """record_input's inputs: each kind, all but the specification with ``passage``."""

    return [
        {"kind": kind} if kind == "specification" else {"kind": kind, "passage": passage}
        for kind in kinds
    ]


def _new() -> Session:
    session, _ = start_session(
        {},
        source_system="CoreBank",
        source="daily accounts feed",
        message=_msg("m0", "Start an STTM session."),
        now=NOW,
    )
    return session


def _with_spec() -> Session:
    """A session with message m1 recorded as the instructions and the specification."""

    session = _new()
    record_input(
        session,
        _inputs("source_system_instructions", "specification"),
        _msg("m1", f"{_INSTRUCTIONS}\n{_SPEC}"),
        replace=False,
        now=NOW,
    )
    return session


def _refused(action: Callable[[], Any]) -> str:
    try:
        action()
    except SessionError as error:
        return str(error)
    raise AssertionError("the engine accepted it")


def _cells(session: Session, *updates: tuple, message: dict, source: str = "analyst", **kw: Any):
    """update_cells with (table, attribute, column, value) tuples."""

    return update_cells(
        session,
        [
            {"table": table, "attribute": attribute, "column": column, "value": value}
            for table, attribute, column, value in updates
        ],
        message,
        source=source,
        rule_file=kw.get("rule_file"),
        now=NOW,
    )


@contextmanager
def _rule_files(files: dict[str, str]) -> Iterator[Path]:
    """Run with exactly ``files`` in the STTM rule-file directory."""

    saved = rules_mod.AUTHORING_DIR
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for name, text in files.items():
            (directory / name).write_text(text, encoding="utf-8")
        rules_mod.AUTHORING_DIR = directory
        try:
            yield directory
        finally:
            rules_mod.AUTHORING_DIR = saved


def _label(step: Step) -> str:
    if step.kind == "gate":
        return f"gate:{step.gate}"
    if step.kind == "generated":
        return f"generated:{step.stage}"
    return step.kind


def _walk(session: Session, prefix: str = "walk") -> list[str]:
    """Advance, a new analyst message each time, accepting every review, until it stops."""

    stops = []
    for n in range(10):
        step = advance(session, _msg(f"{prefix}-{n}", "Looks good."), confirm_review=True)
        stops.append(_label(step))
        if step.kind in ("ready", "blocked"):
            return stops
    raise AssertionError(f"the workflow did not end: {stops}")


def _results(session: Session) -> dict[str, Any]:
    return {result.check: result for result in validate(session)}


# -- reading the specification ---------------------------------------------------


def test_a_pasted_table_is_read_by_its_header_roles() -> None:
    parsed = parse_specification(f"{_INSTRUCTIONS}\n{_SPEC}")

    assert parsed.tables == [
        "Table at line 2: 3 attribute(s); columns name = 'Field Name', data_type = "
        "'Data Type', length = 'Length', required = 'Required', description = "
        "'Description', key = 'Key'"
    ]
    assert parsed.table_lines == {2, 3, 4, 5, 6}
    assert parsed.notes == [] and parsed.skipped_lines == []
    first, _, last = parsed.attributes
    assert (
        first.name,
        first.line,
        first.data_type,
        first.length,
        first.required,
        first.description,
        first.key,
    ) == ("CUSTOMER_ID", 4, "VARCHAR", "20", "Y", "Customer identifier", "PK")
    assert (last.name, last.length, last.required, last.key) == ("OPEN_DATE", None, "N", None)


def test_a_nullable_column_is_read_as_the_inverse_required_flag() -> None:
    parsed = parse_specification(
        "| Column | Type | Nullable |\n|---|---|---|\n| A | INT | Yes |\n| B | INT | No |\n"
        "| C | INT | nullable |\n| D | INT | not null |"
    )

    assert [attribute.required for attribute in parsed.attributes] == ["N", "Y", "N", "Y"]


def test_a_sized_type_is_split_only_when_there_is_no_length_column() -> None:
    split = parse_specification(
        "| Name | Type |\n|---|---|\n| AMOUNT | DECIMAL(18, 2) |\n| CODE | CHAR(3) |\n"
        "| NOTE | TEXT |"
    )
    kept = parse_specification("| Name | Type | Length |\n|---|---|---|\n| CODE | CHAR(3) | 3 |")

    assert [(a.data_type, a.length) for a in split.attributes] == [
        ("DECIMAL", "18,2"),
        ("CHAR", "3"),
        ("TEXT", None),
    ]
    assert [(a.data_type, a.length) for a in kept.attributes] == [("CHAR(3)", "3")]


def test_tab_separated_cells_are_read_too() -> None:
    parsed = parse_specification(
        "Attribute\tType\tDescription\nID\tINT\tThe id\nNAME\tVARCHAR(50)\tThe name"
    )

    assert [(a.name, a.data_type, a.length, a.description) for a in parsed.attributes] == [
        ("ID", "INT", None, "The id"),
        ("NAME", "VARCHAR", "50", "The name"),
    ]


def test_tables_that_list_no_attributes_are_skipped_and_said_so() -> None:
    parsed = parse_specification(
        "\n".join(
            [
                "Notes from the kickoff:",
                "| Step | Owner |",
                "|---|---|",
                "| Load | Data team |",
                "",
                "| Field | Logic | Join Key |",
                "|---|---|---|",
                "| AGE_DAYS | today minus DOB | ID |",
                "",
                "| Field | Owner |",
                "|---|---|",
                "| ID | Data team |",
            ]
        )
    )

    assert parsed.attributes == [] and parsed.tables == []
    assert parsed.notes == [
        "Skipped the table at line 2: no column in its header (Step, Owner) names the attributes.",
        "Skipped the table at line 6: its columns (Field, Logic, Join Key) describe derived "
        "fields, not specification attributes.",
        "Skipped the table at line 10: besides the names it has no type, length, required or "
        "description column (Field, Owner).",
    ]


def test_rows_with_no_name_and_extra_cells_are_reported_not_guessed() -> None:
    parsed = parse_specification(
        "| Name | Type |\n|---|---|\n| ID | INT |\n|  | INT |\n| CODE | CHAR | extra |"
    )

    assert [a.name for a in parsed.attributes] == ["ID", "CODE"]
    assert parsed.tables == [
        "Table at line 1: 2 attribute(s); columns name = 'Name', data_type = 'Type'"
    ]
    assert parsed.notes == [
        "Line 4 has no attribute name and was not read.",
        "Line 5 has 3 cells but the header has 2; the extra cells were not read.",
    ]
    assert parsed.skipped_lines == [4]


def test_only_attribute_tables_are_taken_out_of_a_message() -> None:
    other = "| Step | Owner |\n|---|---|\n| Load | Data team |"

    assert without_attribute_tables(f"{_INSTRUCTIONS}\n{_SPEC}\nThanks!") == (
        f"{_INSTRUCTIONS}\nThanks!"
    )
    assert without_attribute_tables(other) == other


# A documentation page's table as an MDX component: cells over several lines,
# with markup and entities in them.
_MDX = """# widget Object

<Table align={["left","left","left"]}>
  <thead>
    <tr>
      <th>
        Property
      </th>
      <th>Data Type (length)</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>
        <span style={{ color: '#AA4A44' }}>`legacyCode`</span>
      </td>
      <td>deprecated</td>
      <td>deprecated</td>
    </tr>
    <tr>
      <td>`parts`</td>
      <td>array</td>
      <td>Its parts &amp; their <b>order</b></td>
    </tr>
    <tr>
      <td>`stateCode`</td>
      <td>string (2)</td>
      <td>Two-letter state code</td>
    </tr>
  </tbody>
</Table>

Map widget to the curated layer."""


def test_an_html_or_mdx_table_is_read_by_its_cells() -> None:
    parsed = parse_specification(_MDX)
    one_line = "<table><tr><th>Field</th><th>Type</th></tr><tr><td>ID</td><td>INT</td></tr></table>"

    assert parsed.tables == [
        (
            "Table at line 3: 3 attribute(s); columns name = 'Property', data_type = "
            "'Data Type (length)', description = 'Description'"
        )
    ]
    assert [(a.name, a.line, a.data_type, a.length, a.description) for a in parsed.attributes] == [
        ("legacyCode", 14, "deprecated", None, "deprecated"),
        ("parts", 21, "array", None, "Its parts & their order"),
        ("stateCode", 26, "string", "2", "Two-letter state code"),
    ]
    assert parsed.table_lines == set(range(3, 33))
    assert without_attribute_tables(_MDX) == "# widget Object\n\n\nMap widget to the curated layer."
    assert [(a.name, a.data_type) for a in parse_specification(one_line).attributes] == [
        ("ID", "INT")
    ]


def test_chat_prefixes_copied_with_a_message_are_not_read_as_cells() -> None:
    # Copied from a chat window: the sender's initials, and "You:" before each message.
    copied = parse_specification(
        "XY\nYou:Start an STTM session for source system DEMO, source orders.\n"
        "You:| Attribute | Data Type | Description |\n|---|---|---|\n"
        "| ORDER_ID | VARCHAR(36) | Order id |\n| AMOUNT | DECIMAL(12,2) | Amount |"
    )
    inline = parse_specification(
        "XY You:| Attribute | Data Type | Description |\n|---|---|---|\n"
        "| ORDER_ID | VARCHAR(36) | Order id |"
    )

    assert copied.tables == [
        (
            "Table at line 3: 2 attribute(s); columns name = 'Attribute', data_type = "
            "'Data Type', description = 'Description'"
        )
    ]
    assert [(a.name, a.data_type, a.length) for a in copied.attributes] == [
        ("ORDER_ID", "VARCHAR", "36"),
        ("AMOUNT", "DECIMAL", "12,2"),
    ]
    assert [(a.name, a.line) for a in inline.attributes] == [("ORDER_ID", 3)]


def test_a_property_name_column_names_the_attributes() -> None:
    parsed = parse_specification(
        "Property name\tFormat\tNotes\norderId\tstring\tthe id\ntotal\tnumber\tthe amount"
    )

    assert parsed.tables == [
        (
            "Table at line 1: 2 attribute(s); columns name = 'Property name', data_type = "
            "'Format', description = 'Notes'"
        )
    ]


def test_a_table_of_values_for_attributes_already_listed_is_not_more_specification() -> None:
    values = (
        "Handling per attribute:\n\n| Attribute | Handling | Description |\n|---|---|---|\n"
        "| ORDER_ID | keep | the key |\n| AMOUNT | round | to cents |"
    )
    text = (
        "| Attribute | Data Type | Description |\n|---|---|---|\n"
        f"| ORDER_ID | VARCHAR(36) | Order id |\n| AMOUNT | DECIMAL(12,2) | Amount |\n\n{values}"
    )

    parsed = parse_specification(text)

    assert [a.name for a in parsed.attributes] == ["ORDER_ID", "AMOUNT"]
    assert parsed.notes == [
        (
            "Skipped the table at line 8: every attribute in it is in an earlier table, so it "
            "gives values for them, not the specification."
        )
    ]
    # They are the analyst's values, not the specification's.
    assert without_attribute_tables(text) == f"\n{values}"


_FLATTENED = "| Attribute | Type | Description | |---|---|---| | A | VARCHAR | x | | B | INT | y |"


def test_a_table_that_lost_its_line_breaks_is_reported_not_read() -> None:
    note = (
        "Line {} looks like a table that lost its columns or line breaks when it was copied, so "
        "it was not read."
    )
    flattened = [
        _FLATTENED,
        "Here it is: | Attribute | Type | Description | |---|---|---| | A | VARCHAR | x |",
        (
            "XY You:Attribute Name Data Type Length Nullable Primary Key Description ORDER_ID "
            "VARCHAR 36 N Y Order id AMOUNT DECIMAL 12 N N Amount"
        ),
    ]
    # Prose that names the columns, a header with no rows yet, a whole table.
    read = [
        (
            "Name, type, length and description for each field are below.\n"
            "Field name data type and length come from the vendor."
        ),
        "| Attribute | Type | Description |",
        _SPEC,
    ]

    for text in flattened:
        parsed = parse_specification(text)
        assert (parsed.attributes, parsed.flattened, parsed.notes) == ([], [1], [note.format(1)])
    later = parse_specification(f"Here is the spec:\n{_FLATTENED}")
    assert (later.flattened, later.notes) == ([2], [note.format(2)])
    assert [parse_specification(text).flattened for text in read] == [[], [], []]


def test_a_flattened_specification_is_refused_with_how_to_paste_it_again() -> None:
    session = _new()
    unread = "spec.pdf has no text to read (a scanned document?)."
    message = {"id": "m1", "text": f"{_INSTRUCTIONS}\n{_FLATTENED}", "notes": [unread]}
    reason = (
        "The specification in the analyst's message lost its columns or line breaks when it was "
        "copied (line 2). Ask the analyst to paste it again with each row on its own line and "
        "its cells separated by tabs or pipes: copied straight from the spreadsheet or document, "
        f"or as a Markdown table. {unread}"
    )

    error = _refused(
        lambda: record_input(session, _inputs("specification"), message, replace=False, now=NOW)
    )

    assert error == f"Nothing was recorded. {reason}"
    assert session.inputs == [] and session.attributes == [] and "m1" not in session.messages

    # The instructions in the same message are recorded all the same.
    recorded = record_input(
        session,
        _inputs("source_system_instructions", "specification"),
        message,
        replace=False,
        now=NOW,
    )

    assert (recorded.recorded, recorded.refused, recorded.reasons) == (
        ["Source-system instructions"],
        ["Source specification"],
        [reason],
    )
    assert [r.kind for r in session.inputs] == ["source_system_instructions"]
    assert session.attributes == []


# -- sessions and inputs ---------------------------------------------------------


def test_session_ids_are_dated_unique_and_free_of_look_alike_letters() -> None:
    ids: list[str] = []
    for _ in range(200):
        ids.append(new_session_id(ids, NOW))

    assert len(set(ids)) == 200
    assert all(re.fullmatch(r"20261103-[A-HJKMNP-Z]{4}", session_id) for session_id in ids), ids


def test_a_session_needs_a_source_system_and_a_source() -> None:
    assert _refused(
        lambda: start_session(
            {}, source_system="CoreBank", source=" ", message=_msg("m0", "Start."), now=NOW
        )
    ) == ("Name both the source system and the source.")


def test_nothing_is_generated_before_the_instructions_and_the_specification() -> None:
    session = _new()

    step = advance(session, _msg("m1", "Go ahead."), confirm_review=True)

    assert step.kind == "blocked"
    assert step.problems == [
        "Source-system instructions: not given yet.",
        "Source specification: not given yet.",
    ]
    assert not any(session.generated.values()) and session.status == "draft"

    record_input(session, _inputs("specification"), _msg("m2", _SPEC), replace=False, now=NOW)
    assert input_blockers(session) == ["Source-system instructions: not given yet."]


def test_a_message_with_no_text_is_not_recorded() -> None:
    session = _new()
    message = _msg("m1", "  \n")

    error = _refused(
        lambda: record_input(session, _inputs("specification"), message, replace=False, now=NOW)
    )

    assert error == (
        "The analyst's message has no text to record. Ask the analyst to paste the content "
        "as text or attach it as a PDF with selectable text; a specification as a table with "
        "a header row, pipe- or tab-separated."
    )
    assert session.inputs == []


def test_a_specification_with_no_attribute_table_is_not_recorded() -> None:
    session = _new()
    message = _msg("m1", "The specification is on the file share.")

    error = _refused(
        lambda: record_input(session, _inputs("specification"), message, replace=False, now=NOW)
    )

    assert error.startswith(
        "Nothing was recorded. No attribute table was found in the analyst's message."
    )
    assert session.inputs == [] and session.attributes == []


def test_an_unknown_input_kind_is_refused() -> None:
    session = _new()

    error = _refused(
        lambda: record_input(session, _inputs("spec"), _msg("m1", _SPEC), replace=False, now=NOW)
    )

    assert error == (
        "Unknown input kind(s) ['spec']; use: source_system_instructions, source_instructions, "
        "specification, derived_fields, asl_schema."
    )


def test_duplicate_attribute_names_block_generation_until_the_analyst_decides() -> None:
    session = _new()
    duplicated = f"{_INSTRUCTIONS}\n{_SPEC}\n| CUSTOMER_ID | INT | | Y | Customer number | |"
    record_input(
        session,
        _inputs("source_system_instructions", "specification"),
        _msg("m1", duplicated),
        replace=False,
        now=NOW,
    )

    step = advance(session, _msg("m2", "Go ahead."), confirm_review=False)

    assert step.kind == "blocked"
    assert step.problems == [
        "Duplicate attribute name CUSTOMER_ID (specification lines 4, 7); the analyst has to "
        "say which one is right."
    ]
    assert not any(session.generated.values())


def test_a_specification_and_an_asl_schema_in_one_message_are_refused() -> None:
    session = _new()
    text = f"{_SPEC}\n\n| Column | Type |\n|---|---|\n| CUST_ID | STRING |"

    error = _refused(
        lambda: record_input(
            session,
            _inputs("specification", "asl_schema", passage="Column Type CUST_ID STRING"),
            _msg("m1", text),
            replace=False,
            now=NOW,
        )
    )

    assert error.startswith(
        "Nothing was recorded. The message holds more than one attribute table and is being "
        "recorded as both the specification and the ASL schema"
    )
    assert session.inputs == [] and session.attributes == []

    # Instructions sent with them are recorded; neither table is.
    recorded = record_input(
        session,
        _inputs("source_system_instructions")
        + _inputs("specification", "asl_schema", passage="Column Type CUST_ID STRING"),
        _msg("m2", f"{_INSTRUCTIONS}\n{text}"),
        replace=False,
        now=NOW,
    )

    assert (recorded.recorded, recorded.refused) == (
        ["Source-system instructions"],
        ["Source specification", "ASL schema export"],
    )
    assert [r.kind for r in session.inputs] == ["source_system_instructions"]
    assert session.attributes == []


def _two_specifications(customer_key: str = "CUSTOMER_ID") -> dict[str, Any]:
    """A message with two sources' specifications, each under its own heading."""

    head = "| Attribute Name | Data Type | Length |\n|---|---|---|\n"
    return _msg(
        "m1",
        f"{_INSTRUCTIONS}\n\nAccounts specification:\n\n{head}"
        "| ACCOUNT_ID | VARCHAR | 20 |\n| CUSTOMER_ID | VARCHAR | 20 |\n\n"
        f"Customers specification:\n\n{head}"
        f"| {customer_key} | VARCHAR | 20 |\n| FULL_NAME | VARCHAR | 100 |",
    )


def _two_sessions(message: dict[str, Any]) -> tuple[Session, Session]:
    """The accounts and the customers sessions, both started from ``message``."""

    accounts, _ = start_session(
        {}, source_system="CoreBank", source="accounts", message=message, now=NOW
    )
    customers, _ = start_session(
        {accounts.id: accounts},
        source_system="CoreBank",
        source="customers",
        message=message,
        now=NOW,
    )
    return accounts, customers


def _specification(
    session: Session, message: dict, other: Session, passage: str = "", replace: bool = False
):
    """record_input of the specification, with the conversation's ``other`` session."""

    return record_input(
        session,
        [{"kind": "specification", "passage": passage}],
        message,
        replace=replace,
        now=NOW,
        others=[other],
    )


def test_each_sources_specification_is_read_from_its_own_table_of_a_message() -> None:
    message = _two_specifications()
    accounts, customers = _two_sessions(message)

    # Read together, the tables share CUSTOMER_ID.
    error = _refused(lambda: _specification(accounts, message, customers))
    assert error == (
        "Nothing was recorded. The attribute tables in the analyst's message share attribute "
        "names (CUSTOMER_ID). If they are the specifications of different sources, record each "
        "session's specification with the passage of its own table: the heading above it or a "
        "row of it. If they are one source's, ask the analyst to send its specification again "
        "with each attribute once."
    )

    # The heading above a table, or a row of it, chooses that table alone.
    by_heading = _specification(accounts, message, customers, "Accounts specification")
    by_row = _specification(customers, message, accounts, "FULL_NAME VARCHAR 100")

    assert [a.name for a in accounts.attributes] == ["ACCOUNT_ID", "CUSTOMER_ID"]
    assert [a.name for a in customers.attributes] == ["CUSTOMER_ID", "FULL_NAME"]
    assert (by_heading.passages, by_row.passages, by_row.notes) == (
        {"Source specification": "Accounts specification"},
        {"Source specification": "FULL_NAME | VARCHAR | 100"},
        [],
    )
    # The views quote the passage that chose it.
    assert '  - Passage: "Accounts specification"' in inputs_view(accounts).splitlines()
    assert (
        '  - Source specification: "Accounts specification"' in summary_view(accounts).splitlines()
    )

    # Words both tables have, or neither, choose none; nothing changes.
    for passage, problem in [
        (
            "Attribute Name Data Type",
            (
                "is in the heading or rows of more than one attribute table: give words that "
                "only its own table's heading or rows have"
            ),
        ),
        (
            _INSTRUCTIONS,
            (
                "is not the heading above an attribute table of the analyst's message or a row "
                "of it, copied exactly (2 words or more)"
            ),
        ),
    ]:
        error = _refused(
            lambda passage=passage: _specification(
                customers, message, accounts, passage, replace=True
            )
        )
        assert error == (
            f"Nothing was recorded. The passage for the Source specification {problem}. Leave "
            "it out when the message holds only this source's tables."
        )
    assert [a.name for a in customers.attributes] == ["CUSTOMER_ID", "FULL_NAME"]


def test_sessions_recording_one_message_each_choose_their_own_table() -> None:
    message = _two_specifications(customer_key="CUST_NO")
    accounts, customers = _two_sessions(message)

    # With no name in common, the first session to record the message reads every table ...
    _specification(accounts, message, customers)
    assert [a.name for a in accounts.attributes] == [
        "ACCOUNT_ID",
        "CUSTOMER_ID",
        "CUST_NO",
        "FULL_NAME",
    ]

    # ... so the next one has to choose its table, and is told the first one has to too.
    again = (
        f"Record the specification of session {accounts.id} again with replace=True and the "
        "passage of its own table, as it read every table of the message."
    )
    error = _refused(lambda: _specification(customers, message, accounts))
    assert error == (
        "Nothing was recorded. The analyst's message holds more than one attribute table, and "
        f"session {accounts.id} (CoreBank / accounts) recorded its specification from it too: "
        "give each session's specification the passage of its own table (the heading above it "
        f"or a row of it), so each reads that table alone. {again}"
    )
    recorded = _specification(customers, message, accounts, "Customers specification")
    assert [a.name for a in customers.attributes] == ["CUST_NO", "FULL_NAME"]
    assert recorded.notes == [again]

    # Recorded again from the same message, the first keeps only its own table; the
    # message is still its specification, not a replaced one.
    redone = _specification(accounts, message, customers, "Accounts specification", replace=True)
    assert (redone.recorded, redone.notes) == (["Source specification"], [])
    assert [a.name for a in accounts.attributes] == ["ACCOUNT_ID", "CUSTOMER_ID"]
    assert [(r.kind, r.message_id, r.passage) for r in accounts.inputs] == [
        ("specification", "m1", "Accounts specification")
    ]
    assert accounts.replaced_specifications == []


def test_the_gaps_list_what_the_analyst_has_not_given() -> None:
    session = _with_spec()

    assert gaps(session) == [
        "Join keys: not given.",
        "CMDB application number: not given.",
        "File name pattern: not given.",
        "Delimiter: not given.",
        "Header rows: not given.",
        "Trailer rows: not given.",
        "Frequency: not given.",
        "Derived fields: none declared, and the analyst has not said whether there are any.",
    ]

    untyped = _new()
    record_input(
        untyped,
        _inputs("specification"),
        _msg(
            "m1",
            "| Field Name | Data Type |\n|---|---|\n| CUSTOMER_ID | VARCHAR |\n| OPEN_DATE | |",
        ),
        replace=False,
        now=NOW,
    )
    assert "No data type in the specification for 1 attribute(s): OPEN_DATE." in gaps(untyped)

    record_input(
        session,
        _inputs("asl_schema", passage="ASL: CUST_ID STRING"),
        _msg("m2", "ASL: CUST_ID STRING"),
        replace=False,
        now=NOW,
    )
    assert gaps(session)[-1] == (
        "An ASL schema was recorded. Reconciling the Curated-to-ASL STTM against it is not "
        "supported yet; its names count only where they are set as values."
    )

    set_details(
        session,
        {"file_pattern": "accounts_*.txt"},
        _msg("m3", "What is the file called?"),
        proposed=True,
        now=NOW,
    )
    assert "File name pattern: accounts_*.txt is proposed, not confirmed." in gaps(session)


def test_a_new_specification_message_adds_its_attributes() -> None:
    session = _with_spec()
    more = "| Field Name | Data Type |\n|---|---|\n| BALANCE | DECIMAL(18,2) |"

    result = record_input(
        session, _inputs("specification"), _msg("m2", more), replace=False, now=NOW
    )

    assert result.recorded == ["Source specification"] and result.attributes_added == 1
    assert [a.name for a in session.attributes] == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
        "OPEN_DATE",
        "BALANCE",
    ]
    assert (session.attributes[-1].data_type, session.attributes[-1].length) == ("DECIMAL", "18,2")

    again = record_input(
        session, _inputs("specification"), _msg("m2", more), replace=False, now=NOW
    )
    assert again.already == ["Source specification"] and again.recorded == []
    assert len(session.attributes) == 4


def test_replacing_the_specification_drops_the_old_attributes_and_their_values() -> None:
    session = _with_spec()
    _cells(
        session,
        ("dd", "CUSTOMER_ID", "business_description", "The customer"),
        ("dd", "ACCOUNT_NO", "business_description", "The account"),
        message=_msg("m2", "Describe them."),
        source="draft",
    )
    new_spec = (
        "| Field Name | Data Type | Key |\n|---|---|---|\n| ACCOUNT_NO | VARCHAR | PK |\n"
        "| BALANCE | DECIMAL(18,2) | |"
    )

    record_input(session, _inputs("specification"), _msg("m3", new_spec), replace=True, now=NOW)

    assert [a.name for a in session.attributes] == ["ACCOUNT_NO", "BALANCE"]
    assert (session.details["primary_key"].value, session.details["primary_key"].source) == (
        "ACCOUNT_NO",
        "specification",
    )
    assert [(r.kind, r.message_id) for r in session.inputs] == [
        ("source_system_instructions", "m1"),
        ("specification", "m3"),
    ]
    assert session.replaced_specifications == ["m1"]
    assert sorted(session.cells["data_dictionary"]) == ["accountno"]


def test_a_replaced_specification_is_still_not_the_analysts_word() -> None:
    # m1 held the instructions and the first specification. Once m3 replaces the
    # specification, m1's tables still do not count: ACCOUNT_NO is only in them.
    session = _with_spec()
    new_spec = "| Field Name | Data Type | Key |\n|---|---|---|\n| ACCOUNT_NO | VARCHAR | PK |"
    record_input(session, _inputs("specification"), _msg("m3", new_spec), replace=True, now=NOW)

    outcome = set_details(
        session, {"join_keys": ["ACCOUNT_NO"]}, _msg("m4", "Go on."), proposed=False, now=NOW
    )

    assert outcome.changes == []
    assert outcome.refused == [
        "Join keys: 'ACCOUNT_NO' is not in the analyst's messages to this session. Ask the "
        "analyst, or propose it (source='draft') for the analyst to confirm at the input review."
    ]

    # A message that held only the specification: the file it came in is left
    # out, its notes too, and the words the analyst typed with it still count.
    attached = _document("accounts.pdf", f"Confidential feed.\n{_SPEC}")
    for text, said in [
        (f"Confidential feed.\n{_SPEC}", True),
        (f"Here is the specification.\n\n{attached}", False),
    ]:
        session = _new()
        record_input(
            session,
            _inputs("source_system_instructions"),
            _msg("m1", _INSTRUCTIONS),
            replace=False,
            now=NOW,
        )
        record_input(session, _inputs("specification"), _msg("m2", text), replace=False, now=NOW)
        record_input(session, _inputs("specification"), _msg("m3", _SPEC), replace=True, now=NOW)
        outcome = _cells(
            session,
            ("dd", "CUSTOMER_ID", "classification", "Confidential"),
            message=_msg("m4", "Go on."),
        )
        assert (len(outcome.changes), len(outcome.refused)) == ((1, 0) if said else (0, 1)), text


def test_a_pasted_specification_is_not_the_analysts_word() -> None:
    session = _new()
    record_input(
        session,
        _inputs("source_system_instructions"),
        _msg("m1", _INSTRUCTIONS),
        replace=False,
        now=NOW,
    )
    spec = (
        "| Field Name | Data Type | Description |\n|---|---|---|\n"
        "| CUSTOMER_ID | VARCHAR | Confidential PII, carry to curated |"
    )
    record_input(session, _inputs("specification"), _msg("m2", spec), replace=False, now=NOW)

    outcome = _cells(
        session,
        ("dd", "CUSTOMER_ID", "classification", "Confidential"),
        ("dd", "CUSTOMER_ID", "pii_pci", "PII"),
        ("dd", "CUSTOMER_ID", "carry_to_curated", "Y"),
        message=_msg("m3", "Go on."),
    )

    assert outcome.changes == []
    assert outcome.refused == [
        "Data Dictionary, CUSTOMER_ID, classification: 'Confidential' is not in the analyst's "
        "instructions or feedback to this session (a pasted specification does not count). "
        "Use their words for it exactly as written, or ask the analyst.",
        "Data Dictionary, CUSTOMER_ID, pii_pci: 'PII' is not in the analyst's instructions or "
        "feedback to this session (a pasted specification does not count). Use their words for "
        "it exactly as written, or ask the analyst.",
        "Data Dictionary, CUSTOMER_ID, carry_to_curated: the analyst has not said anything "
        "about carry to curated in their instructions or feedback to this session (a pasted "
        "specification does not count). Ask the analyst.",
    ]


def test_instructions_carry_over_to_a_new_session_for_the_same_source_system() -> None:
    first = _with_spec()
    sessions = {first.id: first}

    second, inherited = start_session(
        sessions,
        source_system="corebank",
        source="monthly balances",
        message=_msg("m5", "Now the monthly balances file."),
        now=NOW,
    )

    assert inherited == [f"Source-system instructions: carried over from session {first.id}"]
    assert [(r.kind, r.message_id, r.inherited_from) for r in second.inputs] == [
        ("source_system_instructions", "m1", first.id)
    ]
    # The message held the first session's specification too: only the
    # instructions come over.
    assert second.messages["m1"].strip() == _INSTRUCTIONS
    assert second.attributes == []

    other, inherited = start_session(
        sessions, source_system="Cards", source="daily", message=_msg("m6", "Start."), now=NOW
    )
    assert inherited == [] and other.inputs == []


def test_an_inherited_specification_table_is_not_the_analysts_word() -> None:
    first = _with_spec()
    second, _ = start_session(
        {first.id: first},
        source_system="CoreBank",
        source="monthly balances",
        message=_msg("m5", "Now the monthly balances file."),
        now=NOW,
    )
    record_input(second, _inputs("specification"), _msg("m6", _SPEC), replace=False, now=NOW)

    outcome = set_details(
        second, {"primary_key": "ACCOUNT_NO"}, _msg("m7", "Go on."), proposed=False, now=NOW
    )

    assert outcome.changes == [] and outcome.refused[0].startswith(
        "Primary key: 'ACCOUNT_NO' is not in the analyst's messages to this session."
    )


# -- passages --------------------------------------------------------------------


def test_a_passage_is_found_only_as_words_copied_from_the_message() -> None:
    text = "Source system: CoreBank. Source: daily accounts feed."

    # Case, punctuation and spacing aside; the message's own text comes back.
    assert find_passage("source system corebank source", text) == "Source system: CoreBank. Source"
    assert find_passage("SOURCE  system - corebank; source daily", text) == (
        "Source system: CoreBank. Source: daily"
    )
    assert find_passage("customer id is the key", "So CUSTOMER_ID is the key.") == (
        "CUSTOMER_ID is the key"
    )
    # Too short to tell, a word left out, a word cut short, a restatement.
    for passage in [
        "",
        "Source system CoreBank",
        "Source system CoreBank daily accounts",
        "system CoreBan Source daily",
        "The source system is CoreBank",
    ]:
        assert find_passage(passage, text) is None, passage


def test_an_input_is_recorded_only_if_its_passage_is_found() -> None:
    session = _new()
    instructions = {"kind": "source_system_instructions", "passage": _INSTRUCTIONS}
    not_said = {"kind": "source_instructions", "passage": "The file arrives daily at six."}
    reason = (
        "The passage for Source instructions is missing, under 4 words, or not in the analyst's "
        "message. Give each input but a specification with words of the message that hold it, "
        "copied exactly. If the message does not hold that input, do not record it: ask the "
        "analyst for it."
    )

    error = _refused(
        lambda: record_input(session, [not_said], _msg("m1", _INSTRUCTIONS), replace=False, now=NOW)
    )

    assert error == f"Nothing was recorded. {reason}"
    assert session.inputs == [] and "m1" not in session.messages

    # One input refused does not hold back the others.
    recorded = record_input(
        session, [instructions, not_said], _msg("m1", _INSTRUCTIONS), replace=False, now=NOW
    )

    passage = "Source-system instructions: CoreBank, daily accounts feed"
    assert (recorded.recorded, recorded.refused, recorded.reasons) == (
        ["Source-system instructions"],
        ["Source instructions"],
        [reason],
    )
    assert recorded.passages == {"Source-system instructions": passage}
    assert [(r.kind, r.passage) for r in session.inputs] == [
        ("source_system_instructions", passage)
    ]
    assert "m1" in session.messages

    # Sent again, the recorded one is already in and the other is still refused.
    again = record_input(
        session, [instructions, not_said], _msg("m1", _INSTRUCTIONS), replace=False, now=NOW
    )

    assert (again.recorded, again.already, again.refused) == (
        [],
        ["Source-system instructions"],
        ["Source instructions"],
    )
    assert len(session.inputs) == 1


def test_the_request_cannot_pass_for_instructions() -> None:
    session = _new()
    request = (
        "Start an STTM session for the CoreBank accounts in the fast review mode. Build the Data "
        "Dictionary and the STTMs, then give me both workbooks."
    )
    # The instructions share the request's line, not its sentences.
    message = _msg("m1", f"{request} {_INSTRUCTIONS}\nDerived fields: none.")
    refusal = (
        "The passage for {} is in the analyst's request (a sentence that asks for a session, its "
        "review mode or the workbooks), not in instructions on how the data is to be handled. If "
        "the message has no such instructions, do not record the input: ask the analyst for them."
    )

    for passage in [request, "for the CoreBank accounts", "Build the Data Dictionary and"]:
        error = _refused(
            lambda passage=passage: record_input(
                session,
                [{"kind": "source_system_instructions", "passage": passage}],
                message,
                replace=False,
                now=NOW,
            )
        )
        assert error == "Nothing was recorded. " + refusal.format("Source-system instructions")
    assert session.inputs == []

    recorded = record_input(
        session,
        [
            {"kind": "source_system_instructions", "passage": _INSTRUCTIONS},
            {"kind": "source_instructions", "passage": "in the fast review mode"},
            {"kind": "derived_fields", "passage": "Derived fields: none"},
        ],
        message,
        replace=False,
        now=NOW,
    )

    assert (recorded.recorded, recorded.refused) == (
        ["Source-system instructions"],
        ["Source instructions", "Derived-field definitions"],
    )
    # A "none" is not an input either, and the refusal says what it is.
    assert recorded.reasons[1:] == [
        (
            "When the analyst says there are no derived fields, that is "
            "sttm_update(no_derived_fields=True), not an input."
        ),
        refusal.format("Source instructions"),
    ]


def test_a_specification_row_cannot_pass_for_the_instructions() -> None:
    session = _new()
    row = "CUSTOMER_ID VARCHAR 20 Y Customer identifier"
    message = _msg("m1", f"Here is the specification.\n{_SPEC}")
    refusal = (
        "The passage for Source-system instructions is missing, under 4 words, or not in the "
        "analyst's message (for instructions and derived fields, outside the specification's "
        "tables)."
    )
    instructions = {"kind": "source_system_instructions", "passage": row}

    recorded = record_input(
        session, [instructions, {"kind": "specification"}], message, replace=False, now=NOW
    )

    assert (recorded.recorded, recorded.refused) == (
        ["Source specification"],
        ["Source-system instructions"],
    )
    assert recorded.reasons[0].startswith(refusal)
    # No passage at all is refused the same way.
    again = record_input(
        session,
        [{"kind": "source_system_instructions"}, {"kind": "specification"}],
        message,
        replace=False,
        now=NOW,
    )
    assert (again.recorded, again.already, again.refused) == (
        [],
        ["Source specification"],
        ["Source-system instructions"],
    )
    assert again.reasons[0].startswith(refusal)

    # Once recorded as the specification, the message keeps its tables out of a later passage.
    error = _refused(lambda: record_input(session, [instructions], message, replace=False, now=NOW))
    assert error.startswith(f"Nothing was recorded. {refusal}")
    assert [r.kind for r in session.inputs] == ["specification"]


def test_instructions_count_only_where_the_analyst_names_them() -> None:
    feed = "CoreBank sends a daily full extract."
    named = [
        f"Source-system instructions: {feed}",
        f"Source-system instructions:\n\n{feed}\nDates are all UTC.",
        f"## Source system instructions\n{feed}",
        # A heading alone in its paragraph, as a document's heading pastes; and
        # one over a heading of its own.
        f"Source System Instructions\n\n{feed}",
        f"Source-system instructions\n\nNotes from the data owner:\n{feed}",
        _skill("Fast STTM", f"Source-system instructions for CoreBank:\n{feed}"),
        _skill("CoreBank source system", feed),
        "Here is the file.\n\n" + _document("corebank-source-system.pdf", feed),
        "The source-system instructions are attached.\n\n" + _document("corebank.pdf", feed),
        # A "no" that is part of them.
        f"Source-system instructions: No nulls in key fields. {feed}",
    ]
    not_named = [
        # Values for the tables under a heading of their own.
        f"Start an STTM session for the CoreBank accounts.\n\nFile details:\n{feed}",
        f"Source-system instructions\n\nStart with the accounts.\nKeys:\n{feed}",
        # A skill's own opening line calls it instructions; the skill does not.
        _skill("Fast STTM", feed),
        "Here is the file.\n\n" + _document("corebank.pdf", feed),
        # Words that say there are none.
        f"No source-system instructions yet. {feed}",
        f"Source-system instructions: none.\n{feed}",
        f"Source-system instructions: N/A\n{feed}",
        f"I don't have the instructions. {feed}",
        "The instructions are not attached.\n\n" + _document("corebank.pdf", feed),
    ]
    instructions = [{"kind": "source_system_instructions", "passage": feed}]
    reason = (
        "Nothing in the analyst's message names the passage for Source-system instructions as "
        'instructions: a heading or a line above it ("Source-system instructions: ..."), or, for '
        "an attached document or a skill, its name or the analyst's typed text. Values under "
        "another heading are values to record with sttm_update, not instructions. If no part of "
        "the message is named so, do not record the input: ask the analyst to send the "
        'instructions with a line naming them ("Source-system instructions:").'
    )

    for text in named:
        recorded = record_input(_new(), instructions, _msg("m1", text), replace=False, now=NOW)
        assert recorded.passages == {"Source-system instructions": feed[:-1]}, text
    for text in not_named:
        session = _new()
        error = _refused(
            lambda text=text, session=session: record_input(
                session, instructions, _msg("m1", text), replace=False, now=NOW
            )
        )
        assert error == f"Nothing was recorded. {reason}", text
        assert session.inputs == []


def test_an_asl_schema_passage_may_come_from_its_table() -> None:
    session = _with_spec()
    schema = "| Column | Type | Description |\n|---|---|---|\n| CUST_ID | STRING | Customer id |"

    recorded = record_input(
        session,
        _inputs("asl_schema", passage="CUST_ID STRING Customer id"),
        _msg("m2", schema),
        replace=False,
        now=NOW,
    )

    assert recorded.passages == {"ASL schema export": "CUST_ID | STRING | Customer id"}


_RULES = "Source instructions: " + " ".join(f"rule {n} applies to the feed" for n in range(60))


def test_a_long_passage_is_kept_up_to_its_cap() -> None:
    session = _with_spec()

    recorded = record_input(
        session,
        _inputs("source_instructions", passage=_RULES),
        _msg("m2", _RULES),
        replace=False,
        now=NOW,
    )

    assert session.inputs[-1].passage == _RULES[:MAX_PASSAGE_CHARS] + "…"
    assert recorded.passages == {"Source instructions": session.inputs[-1].passage}


def test_the_inputs_and_the_summary_quote_each_passage() -> None:
    first = _with_spec()
    record_input(
        first,
        _inputs("source_instructions", passage=_RULES),
        _msg("m2", _RULES),
        replace=False,
        now=NOW,
    )
    second, _ = start_session(
        {first.id: first},
        source_system="CoreBank",
        source="monthly balances",
        message=_msg("m5", "Now the monthly balances file."),
        now=NOW,
    )
    instructions = '"Source-system instructions: CoreBank, daily accounts feed"'

    def quoted(view: str) -> list[str]:
        return [line for line in view.splitlines() if line.startswith("  - ") and '"' in line]

    assert quoted(inputs_view(first)) == [
        f"  - Passage: {instructions}",
        f'  - Passage: "{_RULES[:MAX_PASSAGE_CHARS]}…"',
    ]
    assert quoted(summary_view(first)) == [
        f"  - Source-system instructions: {instructions}",
        f'  - Source instructions: "{_RULES[:160].rstrip()}…"',
    ]
    assert quoted(summary_view(second)) == [
        f"  - Source-system instructions: {instructions}, carried over from session {first.id}"
    ]
    # The attribute count after the quotes is still the session's.
    assert (
        "- Attributes: 3 from the specification, 0 derived, 0 audit; 3 of 3 carried to Curated "
        "(or not decided yet)"
    ) in summary_view(first).splitlines()

    # A session recorded before passages were kept.
    first.inputs[0].passage = None
    assert "  - Passage: no passage recorded" in inputs_view(first).splitlines()
    assert "  - Source-system instructions: no passage recorded" in summary_view(first).splitlines()


# -- nothing invented ------------------------------------------------------------


def test_a_primary_key_comes_from_the_specification_or_the_analyst() -> None:
    session = _with_spec()
    assert session.details["primary_key"].value == "CUSTOMER_ID"

    # The specification's own key, sent again where the analyst's words don't say it, is left
    # as the specification gave it: nothing to refuse, nothing to ask the analyst.
    for key in (["CUSTOMER_ID"], "customer_id"):
        outcome = set_details(
            session, {"primary_key": key}, _msg("m2", "Let's go on."), proposed=False, now=NOW
        )
        assert (outcome.changes, outcome.refused) == ([], []), key
    assert session.details["primary_key"].source == "specification"

    outcome = set_details(
        session,
        {"primary_key": ["ACCOUNT_NO"]},
        _msg("m2", "Let's go on."),
        proposed=False,
        now=NOW,
    )
    assert outcome.changes == []
    assert outcome.refused == [
        "Primary key: 'ACCOUNT_NO' is not in the analyst's messages to this session. Ask the "
        "analyst, or propose it (source='draft') for the analyst to confirm at the input review."
    ]

    outcome = set_details(
        session,
        {"primary_key": "ACCOUNT_NO"},
        _msg("m3", "The key is ACCOUNT_NO, not CUSTOMER_ID."),
        proposed=False,
        now=NOW,
    )
    assert outcome.refused == []
    assert [(c.attribute, c.old, c.new) for c in outcome.changes] == [
        ("Primary key", "CUSTOMER_ID (specification)", "ACCOUNT_NO (analyst)")
    ]

    outcome = set_details(
        session, {"primary_key": "MADE_UP"}, _msg("m4", "Use MADE_UP."), proposed=False, now=NOW
    )
    assert outcome.refused == [
        "Primary key: MADE_UP is not a specification attribute or derived field."
    ]


def test_cells_are_never_added_for_an_attribute_the_session_does_not_have() -> None:
    session = _with_spec()

    outcome = _cells(
        session,
        ("data_dictionary", "MADE_UP", "pii_pci", "PII"),
        message=_msg("m2", "MADE_UP is PII."),
    )

    assert outcome.changes == []
    assert outcome.refused == [
        "Data Dictionary, MADE_UP, pii_pci: 'MADE_UP' is not a row of the Data Dictionary: it "
        "is not in the specification, the derived fields or the audit fields, and attributes "
        "are never added here."
    ]
    assert session.cells.get("data_dictionary", {}) == {}


def test_values_for_attributes_wait_for_the_specification() -> None:
    session = _new()
    message = _msg("m1", f"{_INSTRUCTIONS} The key is CUSTOMER_ID; CUSTOMER_ID is PII.")
    record_input(session, _inputs("source_system_instructions"), message, replace=False, now=NOW)
    later = (
        "No specification is recorded yet, so the tables have no rows: set it again once one is."
    )

    outcome = set_details(session, {"primary_key": "CUSTOMER_ID"}, message, proposed=False, now=NOW)
    assert outcome.refused == [f"Primary key: CUSTOMER_ID is not an attribute yet. {later}"]

    outcome = _cells(session, ("data_dictionary", "CUSTOMER_ID", "pii_pci", "PII"), message=message)
    assert outcome.refused == [
        (
            "Data Dictionary, CUSTOMER_ID, pii_pci: 'CUSTOMER_ID' is not a row of the Data "
            f"Dictionary yet. {later}"
        )
    ]
    assert session.cells.get("data_dictionary", {}) == {}


def test_derived_fields_and_their_logic_are_copied_from_the_analyst() -> None:
    session = _with_spec()
    message = _msg(
        "m2", "Add a derived field ACCOUNT_AGE_DAYS: today minus OPEN_DATE, joined on CUSTOMER_ID."
    )

    outcome = set_derived_fields(
        session,
        [
            {
                "name": "ACCOUNT_AGE_DAYS",
                "logic": "today minus OPEN_DATE",
                "join_key": "CUSTOMER_ID",
            },
            {"name": "RISK_SCORE", "logic": "a score"},
            {"name": "CUSTOMER_ID"},
        ],
        [],
        None,
        message,
        now=NOW,
    )

    assert [(c.attribute, c.column, c.new) for c in outcome.changes] == [
        ("ACCOUNT_AGE_DAYS", "declared", "ACCOUNT_AGE_DAYS"),
        ("ACCOUNT_AGE_DAYS", "logic", "today minus OPEN_DATE"),
        ("ACCOUNT_AGE_DAYS", "join_key", "CUSTOMER_ID"),
    ]
    assert outcome.refused == [
        "Derived field 'RISK_SCORE' is not in the analyst's messages to this session. Derived "
        "fields come from the analyst only: use the analyst's name for it, or ask.",
        "CUSTOMER_ID is a specification attribute or an audit field, not a derived field.",
    ]
    tables = Tables(session)
    row = tables.row("data_dictionary", "ACCOUNT_AGE_DAYS")
    assert row is not None and row.origin == "derived"
    assert tables.resolve("data_dictionary", row, "derivation_logic").value == (
        "today minus OPEN_DATE"
    )
    assert tables.resolve("raw_to_curated", row, "transformation_rule").value == (
        "today minus OPEN_DATE"
    )

    outcome = set_derived_fields(
        session,
        [{"name": "ACCOUNT_AGE_DAYS", "logic": "DATEDIFF(day, OPEN_DATE, GETDATE())"}],
        [],
        True,
        _msg("m3", "Write it as SQL. There are no other derived fields."),
        now=NOW,
    )
    assert outcome.changes == []
    assert outcome.refused == [
        "Derived field ACCOUNT_AGE_DAYS: the logic 'DATEDIFF(day, OPEN_DATE, GETDATE())' is not "
        "in the analyst's messages; copy the analyst's definition, do not write one.",
        "The session has derived fields; remove them before recording that there are none.",
    ]


def test_a_no_or_a_number_counts_only_for_what_it_answers() -> None:
    session = _with_spec()
    message = _msg("m2", "No, the CMDB number is APP-1234. There is one header row.")

    outcome = set_derived_fields(session, [], [], True, message, now=NOW)
    assert outcome.refused == [
        "The analyst has not said that there are no derived fields; ask them."
    ]

    outcome = set_details(
        session,
        {"cmdb_number": "APP-1234", "header_rows": 1, "trailer_rows": 0, "join_keys": []},
        message,
        proposed=False,
        now=NOW,
    )
    assert [c.attribute for c in outcome.changes] == ["CMDB application number", "Header rows"]
    # In the order the details are listed, not the order they were given in.
    assert outcome.refused == [
        "Join keys: an empty list records that there are none, which the analyst has not said.",
        "Trailer rows: '0' is not in the analyst's messages to this session. Ask the analyst, "
        "or propose it (source='draft') for the analyst to confirm at the input review.",
    ]

    message = _msg("m3", "There are no derived fields and no join keys. No trailer.")
    assert set_derived_fields(session, [], [], True, message, now=NOW).refused == []
    outcome = set_details(
        session, {"trailer_rows": 0, "join_keys": []}, message, proposed=False, now=NOW
    )
    assert outcome.refused == []
    assert [c.new for c in outcome.changes] == ["none (analyst)", "0 (analyst)"]
    assert session.no_derived_fields is not None and session.no_derived_fields.ref == "m3"


def test_a_delimiter_needs_the_analysts_word_not_a_symbol_in_a_table() -> None:
    session = _with_spec()

    outcome = set_details(
        session, {"delimiter": "|"}, _msg("m2", "Go on."), proposed=False, now=NOW
    )
    assert outcome.refused == [
        "Delimiter: 'pipe' is not in the analyst's messages to this session. Ask the analyst, "
        "or propose it (source='draft') for the analyst to confirm at the input review."
    ]

    outcome = set_details(
        session, {"delimiter": "|"}, _msg("m3", "It is pipe delimited."), proposed=False, now=NOW
    )
    assert [c.new for c in outcome.changes] == ["pipe (analyst)"]


def test_a_y_or_n_value_needs_the_analyst_to_speak_about_it() -> None:
    session = _with_spec()

    outcome = _cells(
        session,
        ("dd", "*", "carry_to_curated", "Y"),
        message=_msg("m2", "Looks fine."),
    )
    assert outcome.changes == []
    assert outcome.refused == [
        "Data Dictionary, *, carry_to_curated: the analyst has not said anything about carry to "
        "curated in their instructions or feedback to this session (a pasted specification "
        "does not count). Ask the analyst."
    ]

    outcome = _cells(
        session,
        ("dd", "*", "carry_to_curated", "yes"),
        message=_msg("m3", "Carry every attribute forward."),
    )
    assert outcome.refused == []
    assert [(c.attribute, c.old, c.new) for c in outcome.changes] == [
        ("* (table default, now used by 3 row(s))", None, "Y")
    ]


def test_a_governed_value_needs_the_analysts_words() -> None:
    session = _with_spec()

    outcome = _cells(
        session,
        ("data_dictionary", "CUSTOMER_ID", "classification", "Confidential"),
        message=_msg("m2", "Looks fine."),
    )

    assert outcome.refused == [
        "Data Dictionary, CUSTOMER_ID, classification: 'Confidential' is not in the analyst's "
        "instructions or feedback to this session (a pasted specification does not count). "
        "Use their words for it exactly as written, or ask the analyst."
    ]


# -- review modes, gates and proposals ------------------------------------------


def test_each_review_mode_stops_at_its_own_gates() -> None:
    walks = {}
    for mode, words in (
        (None, None),
        ("extensive", "Use extensive review."),
        ("moderate", "Moderate mode, please."),
        ("fast", "Use fast mode."),
    ):
        session = _with_spec()
        if mode:
            set_review_mode(session, mode, _msg("m2", words))
        with _rule_files({}):
            walks[mode] = _walk(session)
        assert session.ready_on == f"walk-{len(walks[mode]) - 1}"
        expected_skipped = {
            None: [],
            "extensive": [],
            "moderate": ["input_review"],
            "fast": ["input_review", "dd_review", "sttm_review"],
        }[mode]
        assert [g for g, gate in session.gates.items() if gate.status == "skipped"] == (
            expected_skipped
        )

    full = [
        "gate:input_review",
        "generated:data_dictionary",
        "gate:dd_review",
        "generated:sttm",
        "gate:sttm_review",
        "ready",
    ]
    assert walks[None] == full and walks["extensive"] == full
    assert walks["moderate"] == full[1:]
    assert walks["fast"] == ["generated:data_dictionary", "generated:sttm", "ready"]


def test_a_mode_that_skips_reviews_needs_the_analyst_to_name_it() -> None:
    session = _with_spec()

    assert _refused(lambda: set_review_mode(session, "fast", _msg("m2", "I need this fast."))) == (
        "The fast review mode skips reviews, so the analyst has to ask for it by name; ask "
        "which mode they want."
    )
    assert session.review_mode is None

    change = set_review_mode(session, "fast", _msg("m3", "Use fast mode."))
    assert change is not None and (change.old, change.new) == ("default", "fast")
    assert session.review_mode == "fast"
    assert set_review_mode(session, "fast", _msg("m3", "Use fast mode.")) is None

    answered = _with_spec()
    assert set_review_mode(answered, "moderate", _msg("m2", "Moderate")) is not None

    assert _refused(lambda: set_review_mode(session, "quick", _msg("m4", "quick mode"))) == (
        "Unknown review mode 'quick'; use fast, moderate, extensive or default."
    )
    # Extensive skips nothing, so it needs no particular wording.
    assert set_review_mode(session, "extensive", _msg("m5", "Slow down.")) is not None


def test_a_review_is_accepted_only_in_a_later_analyst_message() -> None:
    session = _with_spec()

    step = advance(session, _msg("m2", "Go on."), confirm_review=False)
    assert (step.kind, step.gate, step.reminder) == ("gate", "input_review", None)

    step = advance(session, _msg("m2", "Go on."), confirm_review=True)
    assert step.reminder == (
        "It opened in this same analyst message; the analyst has to answer it first."
    )

    step = advance(session, _msg("m3", "Hmm, wait."), confirm_review=False)
    assert step.reminder == (
        "Pass confirm_review=True only when the analyst's message accepts this review."
    )
    assert session.gates["input_review"].status == "open"

    step = advance(session, _msg("m4", "Fine."), confirm_review=True)
    assert _label(step) == "generated:data_dictionary"
    gate = session.gates["input_review"]
    assert (gate.status, gate.opened_on, gate.closed_on) == ("confirmed", "m2", "m4")


def test_proposals_are_confirmed_at_the_input_review_and_only_there() -> None:
    session = _with_spec()

    outcome = set_details(
        session,
        {"file_pattern": "accounts_*.txt", "frequency": "daily", "cmdb_number": "APP-1"},
        _msg("m2", "What would you suggest?"),
        proposed=True,
        now=NOW,
    )
    assert [c.new for c in outcome.changes] == ["accounts_*.txt (proposed)", "daily (proposed)"]
    assert outcome.refused == [
        "CMDB application number: cannot be proposed; only the analyst can give it."
    ]
    assert session.details["file_pattern"].note == "proposed by the agent"

    advance(session, _msg("m3", "Let me look."), confirm_review=False)
    advance(session, _msg("m4", "Yes, those are right."), confirm_review=True)
    detail = session.details["file_pattern"]
    assert (detail.value, detail.source, detail.ref, detail.note) == (
        "accounts_*.txt",
        "analyst",
        "m4",
        "confirmed at the input review",
    )

    outcome = set_details(
        session, {"delimiter": "pipe"}, _msg("m5", "And the delimiter?"), proposed=True, now=NOW
    )
    assert outcome.changes == []
    assert outcome.refused == [
        "Nothing was proposed: proposals are confirmed at the input review, which is over. "
        "Ask the analyst for these values."
    ]


def test_a_mode_without_the_input_review_takes_no_proposals() -> None:
    session = _with_spec()
    set_review_mode(session, "moderate", _msg("m2", "Use moderate review."))

    outcome = set_details(
        session, {"frequency": "daily"}, _msg("m3", "What next?"), proposed=True, now=NOW
    )
    assert outcome.refused == [
        "Nothing was proposed: proposals are confirmed at the input review, which this review "
        "mode skips. Ask the analyst for these values."
    ]

    outcome = set_details(
        session,
        {"file_pattern": "accounts_*.txt"},
        _msg("m4", "What next?"),
        proposed=False,
        now=NOW,
    )
    assert outcome.refused == [
        "File name pattern: 'accounts_*.txt' is not in the analyst's messages to this session. "
        "Ask the analyst."
    ]


# -- corrections, links and validation -------------------------------------------


def test_a_correction_changes_only_the_value_it_names() -> None:
    session = _with_spec()
    _cells(
        session,
        ("dd", "CUSTOMER_ID", "classification", "Confidential"),
        ("dd", "ACCOUNT_NO", "classification", "Confidential"),
        ("dd", "*", "carry_to_curated", "Y"),
        message=_msg("m2", "CUSTOMER_ID and ACCOUNT_NO are Confidential. Carry all of them."),
    )
    before = session.model_copy(deep=True)

    outcome = _cells(
        session,
        ("dd", "ACCOUNT_NO", "classification", "Internal"),
        message=_msg("m3", "Correction: ACCOUNT_NO is Internal."),
    )

    assert [(c.attribute, c.column, c.old, c.new) for c in outcome.changes] == [
        ("ACCOUNT_NO", "classification", "Confidential", "Internal")
    ]
    changed = session.cells["data_dictionary"]["accountno"]["classification"]
    assert (changed.value, changed.ref) == ("Internal", "m3")
    session.cells["data_dictionary"]["accountno"]["classification"] = before.cells[
        "data_dictionary"
    ]["accountno"]["classification"]
    assert session.cells == before.cells


def test_linked_columns_follow_the_data_dictionary_and_the_file_details() -> None:
    session = _with_spec()
    _cells(
        session,
        ("dd", "CUSTOMER_ID", "pii_pci", "PII"),
        message=_msg("m2", "CUSTOMER_ID is PII."),
    )
    set_details(
        session,
        {"file_pattern": "accounts_*.txt"},
        _msg("m3", "Files are named accounts_*.txt."),
        proposed=False,
        now=NOW,
    )

    tables = Tables(session)
    r2c = tables.row("raw_to_curated", "CUSTOMER_ID")
    c2a = tables.row("curated_to_asl", "CUSTOMER_ID")
    resolved = tables.resolve("raw_to_curated", r2c, "pii_pci")
    assert (resolved.value, resolved.source, resolved.note) == (
        "PII",
        "analyst",
        "from Data Dictionary PII/PCI",
    )
    assert tables.resolve("curated_to_asl", c2a, "sensitivity").value == "PII"
    raw_file = tables.resolve("raw_to_curated", r2c, "raw_file")
    assert (raw_file.value, raw_file.note) == ("accounts_*.txt", "from File name pattern")

    _cells(
        session,
        ("c2a", "CUSTOMER_ID", "sensitivity", "PCI"),
        message=_msg("m4", "Mark the ASL sensitivity of CUSTOMER_ID as PCI."),
    )
    with _rule_files({}):
        assert _results(session)["pii_consistent"].failing == [
            "CUSTOMER_ID: DD PII, R2C PII, C2A PCI"
        ]


def test_validation_names_every_attribute_that_fails() -> None:
    with _rule_files({}):
        results = _results(_with_spec())

    assert list(results) == list(CHECKS)
    names = ("CUSTOMER_ID", "ACCOUNT_NO", "OPEN_DATE")
    assert results["carry_forward_in_r2c"].failing == [
        f"{name}: carry_to_curated not set in the Data Dictionary" for name in names
    ]
    assert results["pii_classified"].failing == [
        f"{name}: pii_pci, classification" for name in names
    ]
    assert results["types_follow_mapping"].failing == [
        f"{name}: {column} not set"
        for column in ("asl_type", "curated_type", "intg_type")
        for name in names
    ]
    assert results["types_follow_mapping"].note == (
        "data-type-mapping.md is not present, so types can only come from the analyst."
    )
    assert results["file_details_complete"].failing == [
        "CMDB application number: not given",
        "File name pattern: not given",
        "Delimiter: not given",
        "Header rows: not given",
        "Trailer rows: not given",
        "Frequency: not given",
    ]
    assert results["required_values"].failing == [
        f"Curated-to-ASL STTM {name}: asl_table, asl_column, logic" for name in names
    ]
    assert [check for check, result in results.items() if not result.passed] == [
        "carry_forward_in_r2c",
        "pii_classified",
        "types_follow_mapping",
        "file_details_complete",
        "required_values",
    ]


def test_an_attribute_not_carried_forward_has_no_sttm_rows() -> None:
    session = _with_spec()
    _cells(
        session,
        ("dd", "*", "carry_to_curated", "Y"),
        ("dd", "OPEN_DATE", "carry_to_curated", "N"),
        message=_msg("m2", "Carry everything except OPEN_DATE."),
    )

    outcome = _cells(
        session,
        ("r2c", "OPEN_DATE", "raw_path", "/raw/corebank"),
        message=_msg("m3", "The raw path is /raw/corebank."),
    )

    assert outcome.refused == [
        "Raw-to-Curated STTM, OPEN_DATE, raw_path: 'OPEN_DATE' is not a row of the "
        "Raw-to-Curated STTM: its carry_to_curated is N."
    ]
    assert [row.name for row in Tables(session).rows("curated_to_asl")] == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
    ]


def _complete_session() -> Session:
    """A default-mode session with every value set, walked to ready. Needs the rule files."""

    session = _with_spec()
    message = _msg(
        "m2",
        "CMDB is APP-1234. Files are named accounts_*.txt, pipe delimited, with 1 header row "
        "and no trailer. They arrive daily. No join keys. There are no derived fields. Carry "
        "every attribute forward.",
    )
    outcome = set_details(
        session,
        {
            "cmdb_number": "APP-1234",
            "file_pattern": "accounts_*.txt",
            "delimiter": "pipe",
            "header_rows": 1,
            "trailer_rows": 0,
            "frequency": "daily",
            "join_keys": [],
        },
        message,
        proposed=False,
        now=NOW,
    )
    assert outcome.refused == [], outcome.refused
    assert set_derived_fields(session, [], [], True, message, now=NOW).refused == []
    set_audit_fields(session, ["LOAD_TS"], "sttm-mapping.md", message)
    assert _cells(session, ("dd", "*", "carry_to_curated", "Y"), message=message).refused == []

    names = ("CUSTOMER_ID", "ACCOUNT_NO", "OPEN_DATE", "LOAD_TS")
    mappings = _msg(
        "m3",
        "Raw path /raw/corebank/accounts. INTG path /intg/corebank, table INTG_ACCOUNTS. "
        "Curated path /curated/corebank, table CUR_ACCOUNTS. ASL table ASL_ACCOUNTS. Every "
        f"layer keeps the column names: {', '.join(names)}. Logic: pass through. "
        "CUSTOMER_ID is PII and Confidential.",
    )
    outcome = _cells(
        session,
        ("r2c", "*", "raw_path", "/raw/corebank/accounts"),
        ("r2c", "*", "intg_path", "/intg/corebank"),
        ("r2c", "*", "intg_table", "INTG_ACCOUNTS"),
        ("r2c", "*", "curated_path", "/curated/corebank"),
        ("r2c", "*", "curated_table", "CUR_ACCOUNTS"),
        ("r2c", "*", "transformation_rule", "pass through"),
        ("c2a", "*", "asl_table", "ASL_ACCOUNTS"),
        ("c2a", "*", "logic", "pass through"),
        ("dd", "CUSTOMER_ID", "pii_pci", "PII"),
        ("dd", "CUSTOMER_ID", "classification", "Confidential"),
        *[
            (table, name, column, name)
            for name in names
            for table, column in (
                ("r2c", "intg_column"),
                ("r2c", "curated_column"),
                ("c2a", "asl_column"),
            )
        ],
        message=mappings,
    )
    assert outcome.refused == [], outcome.refused
    outcome = _cells(
        session,
        ("dd", "*", "classification", "Internal"),
        ("dd", "*", "pii_pci", "NONE"),
        message=mappings,
        source="rule",
        rule_file="pii-pci-handling.md",
    )
    assert outcome.refused == [], outcome.refused
    types = [("dd", "LOAD_TS", "data_type", "TIMESTAMP")]
    for table, column in (("r2c", "intg_type"), ("r2c", "curated_type"), ("c2a", "asl_type")):
        types += [
            (table, "*", column, "STRING"),
            (table, "OPEN_DATE", column, "DATE"),
            (table, "LOAD_TS", column, "TIMESTAMP"),
        ]
    outcome = _cells(
        session, *types, message=mappings, source="rule", rule_file="data-type-mapping.md"
    )
    assert outcome.refused == [], outcome.refused
    assert _walk(session)[-1] == "ready"
    return session


def test_a_complete_session_passes_every_check_and_is_approved_without_a_reason() -> None:
    rules = {
        "pii-pci-handling.md": _PII_RULES,
        "data-type-mapping.md": _TYPE_RULES,
        "sttm-mapping.md": _MAPPING_RULES,
    }
    with _rule_files(rules):
        session = _complete_session()
        assert [r.check for r in session.validation or [] if not r.passed] == []

        results = approve(
            session,
            _msg("m9", "Approved."),
            override_reason=None,
            by="analyst@example.com",
            now=NOW,
        )

    assert all(result.passed for result in results)
    approval = session.approval
    assert approval is not None
    assert (approval.by, approval.message_id, approval.message) == (
        "analyst@example.com",
        "m9",
        "Approved.",
    )
    assert approval.failed_checks == [] and approval.override_reason is None
    assert session.status == "approved" and session.locked
    assert _refused(
        lambda: set_details(
            session, {"frequency": "weekly"}, _msg("m10", "Weekly."), proposed=False, now=NOW
        )
    ) == (
        f"Session {session.id} (version 1) is approved and read-only. To change it, start a new "
        f"version: sttm_start_session(new_version_of='{session.id}')."
    )


def _ready_fast_session() -> Session:
    session = _with_spec()
    set_review_mode(session, "fast", _msg("m2", "Use fast mode."))
    assert _walk(session) == ["generated:data_dictionary", "generated:sttm", "ready"]
    return session


def test_approval_needs_a_later_message_and_the_analysts_own_reason() -> None:
    with _rule_files({}):
        assert _refused(
            lambda: approve(
                _with_spec(), _msg("m2", "Approve."), override_reason=None, by=None, now=NOW
            )
        ) == (
            "The session is not ready for approval: call sttm_advance until it reports the final "
            "summary, show it to the analyst, and approve when the analyst says so."
        )

        session = _ready_fast_session()
        assert _refused(
            lambda: approve(
                session, _msg(session.ready_on or "", "Ok"), override_reason=None, by=None, now=NOW
            )
        ) == (
            "The final summary was produced in this same analyst message; the analyst has to see "
            "it and approve in a later message."
        )
        message = _msg("m8", "Approve it anyway, the gaps get fixed later.")
        assert _refused(
            lambda: approve(session, message, override_reason=None, by=None, now=NOW)
        ) == (
            "Validation failed (carry_forward_in_r2c, pii_classified, types_follow_mapping, "
            "file_details_complete, required_values). Approving anyway needs the analyst's "
            "reason, in their own words (override_reason)."
        )
        assert _refused(
            lambda: approve(
                session, message, override_reason="we accept the risk", by=None, now=NOW
            )
        ) == (
            "The override reason has to be the analyst's own words from the approving message; "
            "quote it, do not write one."
        )
        assert not session.locked

        approve(session, message, override_reason="the gaps get fixed later", by=None, now=NOW)

    approval = session.approval
    assert approval is not None and approval.override_reason == "the gaps get fixed later"
    assert approval.failed_checks == [
        "carry_forward_in_r2c",
        "pii_classified",
        "types_follow_mapping",
        "file_details_complete",
        "required_values",
    ]


def test_an_approved_session_changes_only_through_a_new_version() -> None:
    with _rule_files({}):
        assert _refused(lambda: new_version(_with_spec(), [], now=NOW)).endswith(
            "is not approved, so it can still be changed as it is; a new version is only for "
            "an approved session."
        )
        approved = _ready_fast_session()
        approve(
            approved,
            _msg("m8", "Approve, fix later."),
            override_reason="fix later",
            by=None,
            now=NOW,
        )

    later = NOW + timedelta(days=1)
    version = new_version(approved, [approved.id], now=later)

    assert (version.version, version.supersedes, version.status, version.revision) == (
        2,
        approved.id,
        "in_progress",
        0,
    )
    assert version.id != approved.id and version.id.startswith("20261104-")
    assert version.approval is None and version.ready_on is None and version.validation is None
    assert version.attributes == approved.attributes and version.cells == approved.cells
    outcome = set_details(
        version, {"frequency": "weekly"}, _msg("m9", "It is weekly now."), proposed=False, now=later
    )
    assert [c.new for c in outcome.changes] == ["weekly (analyst)"]
    assert approved.locked and "frequency" not in approved.details


# -- rule files ------------------------------------------------------------------


def test_rule_values_cite_a_present_file_and_its_own_words() -> None:
    rules = {"pii-pci-handling.md": _PII_RULES, "sttm-mapping.md": _MAPPING_RULES}
    with _rule_files(rules):
        session = _with_spec()
        message = _msg("m2", "Use the rule files.")

        assert _refused(
            lambda: _cells(
                session,
                ("dd", "CUSTOMER_ID", "data_type", "STRING"),
                message=message,
                source="rule",
                rule_file="data-type-mapping.md",
            )
        ) == (
            "data-type-mapping.md is not present (present: pii-pci-handling.md, "
            "sttm-mapping.md), so no value can come from it. Leave the value unresolved, or ask "
            "the analyst."
        )
        assert (
            _refused(
                lambda: _cells(
                    session,
                    ("dd", "CUSTOMER_ID", "classification", "Internal"),
                    message=message,
                    source="rule",
                )
            )
            == "A rule-sourced value has to name its rule file (rule_file)."
        )

        outcome = _cells(
            session,
            ("dd", "CUSTOMER_ID", "classification", "Restricted"),
            ("dd", "CUSTOMER_ID", "pii_pci", "PII"),
            ("r2c", "CUSTOMER_ID", "raw_file", "accounts.txt"),
            ("dd", "ACCOUNT_NO", "classification", "Internal"),
            message=message,
            source="rule",
            rule_file="pii-pci-handling.md",
        )
        assert outcome.refused == [
            "Data Dictionary, CUSTOMER_ID, classification: 'Restricted' is not in "
            "pii-pci-handling.md.",
            "Raw-to-Curated STTM, CUSTOMER_ID, raw_file: a linked column takes values from "
            "analyst, not rule.",
        ]
        assert [(c.attribute, c.column, c.new) for c in outcome.changes] == [
            ("CUSTOMER_ID", "pii_pci", "PII"),
            ("ACCOUNT_NO", "classification", "Internal"),
        ]
        value = session.cells["data_dictionary"]["accountno"]["classification"]
        assert (value.source, value.ref) == ("rule", "pii-pci-handling.md")

        outcome = _cells(
            session,
            ("dd", "OPEN_DATE", "pii_pci", "NONE"),
            message=message,
            source="rule",
            rule_file="sttm-mapping.md",
        )
        assert outcome.refused == [
            "Data Dictionary, OPEN_DATE, pii_pci: decided by pii-pci-handling.md or SKILL.md, "
            "not sttm-mapping.md."
        ]


def test_cell_updates_are_checked_one_by_one() -> None:
    session = _with_spec()
    message = _msg("m2", "Describe them.")

    outcome = _cells(
        session,
        ("dd", "CUSTOMER_ID", "primary_key", "Y"),
        ("dd", "CUSTOMER_ID", "colour", "blue"),
        ("r2c", "*", "Carry to Curated", "Y"),
        ("gold", "CUSTOMER_ID", "colour", "blue"),
        ("dd", "CUSTOMER_ID", "classification", "Internal"),
        ("dd", "CUSTOMER_ID", "carry_to_curated", "maybe"),
        ("dd", "ACCOUNT_NO", "business_description", None),
        ("dd", "CUSTOMER_ID", "business_description", "The customer who owns the account"),
        message=message,
        source="draft",
    )

    assert outcome.refused == [
        "Data Dictionary, CUSTOMER_ID, primary_key: worked out from the specification and the "
        "derived fields; change those instead.",
        "Data Dictionary, CUSTOMER_ID, colour: unknown column; the columns are attribute_name, "
        "origin, primary_key, data_type, length, required, technical_description, "
        "business_description, classification, pii_pci, handling_method, carry_to_curated, "
        "derivation_logic, join_key. The column is yours to choose: send the value again "
        "under the one of these it is for, if any.",
        (
            "Raw-to-Curated STTM, *, Carry to Curated: unknown column; it is a column of the "
            "Data Dictionary; the columns are attribute_name, raw_path, raw_file, intg_path, "
            "intg_table, intg_column, intg_type, curated_path, curated_table, curated_column, "
            "curated_type, transformation_rule, logic_type, pii_pci. The column is yours to "
            "choose: send the value again under the one of these it is for, if any."
        ),
        "Unknown table 'gold'; use: data_dictionary, raw_to_curated, curated_to_asl.",
        "Data Dictionary, CUSTOMER_ID, classification: a governed column takes values from "
        "analyst or rule, not draft.",
        "Data Dictionary, CUSTOMER_ID, carry_to_curated: a governed column takes values from "
        "analyst or rule, not draft.",
        "Data Dictionary, ACCOUNT_NO, business_description: has no value of its own to clear.",
    ]
    assert [(c.attribute, c.column, c.new) for c in outcome.changes] == [
        ("CUSTOMER_ID", "business_description", "The customer who owns the account")
    ]
    assert session.cells["data_dictionary"]["customerid"]["business_description"].source == (
        "draft"
    )

    outcome = _cells(
        session,
        ("dd", "CUSTOMER_ID", "carry_to_curated", "maybe"),
        message=_msg("m3", "Carry it if you can."),
    )
    assert outcome.refused == [
        "Data Dictionary, CUSTOMER_ID, carry_to_curated: 'maybe' is not Y or N."
    ]

    # The mark for a value not decided yet is no value, though "review" and "?" are said.
    outcome = _cells(
        session,
        ("c2a", "*", "sensitivity", "REVIEW"),
        ("dd", "CUSTOMER_ID", "business_description", "?"),
        message=_msg("m4", "Use the fast review mode. What is still open?"),
    )
    assert outcome.refused == [
        (
            "Curated-to-ASL STTM, *, sensitivity: 'REVIEW' marks a value not decided yet; leave "
            "it unset."
        ),
        (
            "Data Dictionary, CUSTOMER_ID, business_description: '?' marks a value not decided "
            "yet; leave it unset."
        ),
    ]

    assert _refused(
        lambda: _cells(session, *[("dd", "*", "length", "1")] * 201, message=message)
    ) == ("At most 200 updates per call; send the rest in batches.")
    assert _refused(
        lambda: _cells(session, ("dd", "*", "length", "1"), message=message, source="agent")
    ) == ("source must be 'analyst', 'rule' or 'draft'.")


def test_only_the_rule_files_present_are_offered() -> None:
    with _rule_files({"README.md": "x", "pii-pci-handling.md": "x", "team-notes.md": "x"}):
        present = list(available_rule_files())
        summary = rule_files_summary().splitlines()

    assert present == ["pii-pci-handling.md", "team-notes.md"]
    assert len(summary) == 10
    assert summary[0].startswith("- SKILL.md: NOT present (")
    assert summary[3].startswith(
        "- pii-pci-handling.md: present at /skills/sttm/authoring/pii-pci-handling.md ("
    )
    assert summary[-1] == (
        "- team-notes.md: present at /skills/sttm/authoring/team-notes.md (not one of the "
        "expected files)"
    )


def test_audit_fields_come_from_a_present_rule_file_all_or_nothing() -> None:
    with _rule_files({"sttm-mapping.md": "Audit fields: LOAD_TS, BATCH_ID, CUSTOMER_ID."}):
        session = _with_spec()
        message = _msg("m2", "Add the audit fields.")

        assert _refused(lambda: set_audit_fields(session, ["LOAD_TS"], None, message)) == (
            "Audit fields come from a rule file: name it (rule_file)."
        )
        assert (
            _refused(
                lambda: set_audit_fields(session, ["LOAD_TS"], "data-type-mapping.md", message)
            )
            == "Audit fields come from a rule file, and data-type-mapping.md is not present."
        )
        assert _refused(
            lambda: set_audit_fields(
                session, ["LOAD_TS", "SOURCE_FILE", "LOAD_TS"], "sttm-mapping.md", message
            )
        ) == (
            "No audit fields were changed. SOURCE_FILE is not in sttm-mapping.md. LOAD_TS is "
            "listed more than once."
        )
        assert _refused(
            lambda: set_audit_fields(session, ["CUSTOMER_ID"], "sttm-mapping.md", message)
        ) == (
            "No audit fields were changed. CUSTOMER_ID is already a specification attribute or "
            "derived field."
        )
        assert session.audit_fields == []

        outcome = set_audit_fields(session, ["LOAD_TS", "BATCH_ID"], "sttm-mapping.md", message)
        assert outcome.changes == [
            Change(
                table="audit_fields",
                attribute="(all)",
                column="names",
                old=None,
                new="LOAD_TS, BATCH_ID",
            )
        ]
        rows = [(row.name, row.origin) for row in Tables(session).rows("data_dictionary")]
        assert rows[-2:] == [("LOAD_TS", "audit"), ("BATCH_ID", "audit")]
        assert set_details(
            session,
            {"primary_key": "LOAD_TS"},
            _msg("m3", "Key on LOAD_TS."),
            proposed=False,
            now=NOW,
        ).refused == ["Primary key: LOAD_TS is not a specification attribute or derived field."]

        _cells(
            session,
            ("dd", "LOAD_TS", "business_description", "When the row was loaded"),
            message=message,
            source="draft",
        )
        assert "loadts" in session.cells["data_dictionary"]
        outcome = set_audit_fields(session, [], None, _msg("m4", "Drop the audit fields."))
        assert (outcome.changes[0].old, outcome.changes[0].new) == ("LOAD_TS, BATCH_ID", None)
        assert session.audit_fields == []
        assert "loadts" not in session.cells.get("data_dictionary", {})


# -- export ----------------------------------------------------------------------


def test_the_json_export_holds_the_whole_session_and_its_summary() -> None:
    with _rule_files({}):
        session = _with_spec()
        files = export_session(session, "json")

    assert [file.path for file in files] == [
        f"/sttm/exports/{session.id}-v1.json",
        f"/sttm/exports/{session.id}-v1-summary.md",
    ]
    document = json.loads(files[0].content)
    assert (document["format"], document["format_version"]) == ("sttm-session", 1)
    assert document["session"]["id"] == session.id and document["approval"] is None
    assert len(document["validation"]) == len(CHECKS)
    first = document["tables"]["data_dictionary"]["rows"][0]
    assert first["values"]["attribute_name"] == "CUSTOMER_ID"
    assert (first["values"]["data_type"], first["sources"]["data_type"]) == (
        "VARCHAR",
        "specification",
    )
    assert first["sources"]["pii_pci"] == "unresolved"
    assert files[1].content.startswith(
        f"### STTM session {session.id}: CoreBank / daily accounts feed"
    )


def test_the_export_writes_json_only() -> None:
    """The workbooks are built for download by sttm_workbook (test_sttm_workbook.py),
    not exported to files: an export in any other format is refused."""

    session = _with_spec()

    def export(format: str) -> Callable[[], Any]:
        return lambda: export_session(session, format)

    for format in ("xlsx", "csv"):
        assert _refused(export(format)) == f"Unknown export format '{format}'; use: json."


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
