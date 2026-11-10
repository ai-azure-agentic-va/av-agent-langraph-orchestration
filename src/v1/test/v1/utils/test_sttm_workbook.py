"""Tests for the STTM workbooks, where they are kept, and their download route.

``v1.core.tools.sttm.workbook`` turns a session into the Data Dictionary and
STTM workbooks:

- from the organization's templates (here, small made-up ones of the same
  shape, and the ones this repository ships when it has them): the session's
  rows under the template's own headers, with its dropdowns, widths, hidden
  columns, freeze, filter and cell styles kept, its forms filled in and its
  other sheets left as they are;
- without a usable template (missing, unreadable, without its sheet or its
  columns): laid out from the session's own columns, saying why;
- the fills of the agent's drafts and of undecided cells, and text that is
  never read back as a formula;
- layout steps: each checked on its own, then applied in order, and one that
  does not fit is refused with its number. No step changes a value.

``v1.core.tools.sttm.files`` keeps each file 24 hours for the caller who built
it, and ``GET /sttm/files/{id}`` serves it to them alone; every other caller
gets the same 404.

Runs standalone (``python test_sttm_workbook.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

from langgraph.store.base import PutOp, SearchOp
from langgraph.store.memory import InMemoryStore
from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from starlette.requests import Request
from test_access_control import _STTM, _TEAM_A, _caller, _policy
from test_sttm_engine import NOW, _cells, _msg, _rule_files, _with_spec

import v1.api.routes.sttm_files as route_mod
import v1.core.tools.sttm.files as files_mod
import v1.core.tools.sttm.workbook as workbook_mod
from v1.api.routes.sttm_files import NOT_FOUND, UNAVAILABLE, create_sttm_files_router
from v1.core.config import Settings
from v1.core.middlewares.access_control import TASK_TOOL_NAME
from v1.core.tools.sttm.files import (
    KEEP,
    NAMESPACE,
    KeptFile,
    download_url,
    get_file,
    is_file_id,
    keep_file,
    new_file_id,
    workbook_file_id,
)
from v1.core.tools.sttm.session import (
    COLUMNS,
    UNRESOLVED_LABELS,
    Approval,
    Session,
    SessionError,
    Tables,
    set_derived_fields,
    set_details,
    timestamp,
)
from v1.core.tools.sttm.workbook import (
    MAX_STEPS,
    MEDIA_TYPE,
    WORKBOOKS,
    Built,
    BuiltWorkbook,
    Op,
    StepError,
    build_workbooks,
    describe_step,
    file_name,
    normalize_steps,
)

# -- helpers ---------------------------------------------------------------------


@contextmanager
def _templates(files: Mapping[str, bytes]) -> Iterator[Path]:
    """Run with exactly ``files`` in the workbook template directory."""

    saved = workbook_mod.TEMPLATES_DIR
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        for name, content in files.items():
            (directory / name).write_bytes(content)
        workbook_mod.TEMPLATES_DIR = directory
        try:
            yield directory
        finally:
            workbook_mod.TEMPLATES_DIR = saved


@contextmanager
def _logged(logger: logging.Logger, level: int = logging.INFO) -> Iterator[list[str]]:
    """``logger``'s messages at ``level`` and above, kept from the console."""

    messages: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    sink = _Sink(level=level)
    saved = (logger.level, logger.propagate)
    logger.addHandler(sink)
    logger.setLevel(level)
    logger.propagate = False
    try:
        yield messages
    finally:
        logger.removeHandler(sink)
        logger.level, logger.propagate = saved


def _session() -> Session:
    """A session with file details, PII decisions, drafts, Curated and ASL inputs and a
    derived field. Its rows: CUSTOMER_ID (the key), ACCOUNT_NO, OPEN_DATE (not carried
    to Curated) and FULL_KEY (derived)."""

    with _rule_files({}):
        session = _with_spec()
        outcomes = [
            set_details(
                session,
                {
                    "cmdb_number": "APP-1234",
                    "file_pattern": "accounts_*.txt",
                    "delimiter": "pipe",
                    "header_rows": 1,
                    "trailer_rows": 0,
                },
                _msg(
                    "m2",
                    "CMDB is APP-1234. Files are named accounts_*.txt, pipe delimited, with 1 "
                    "header row and no trailer.",
                ),
                proposed=False,
                now=NOW,
            ),
            set_details(
                session, {"frequency": "daily"}, _msg("m3", "Proposed."), proposed=True, now=NOW
            ),
            _cells(
                session,
                ("dd", "CUSTOMER_ID", "pii_pci", "Yes"),
                ("dd", "CUSTOMER_ID", "classification", "Confidential"),
                ("dd", "CUSTOMER_ID", "handling_method", "Masked"),
                ("dd", "ACCOUNT_NO", "pii_pci", "No"),
                ("dd", "ACCOUNT_NO", "classification", "Internal"),
                message=_msg(
                    "m4",
                    "CUSTOMER_ID is PII: Yes. Classification Confidential. Handling: Masked. "
                    "ACCOUNT_NO PII: No, classification Internal.",
                ),
            ),
            _cells(
                session,
                ("dd", "ACCOUNT_NO", "business_description", '=HYPERLINK("http://x.test","x")'),
                ("dd", "OPEN_DATE", "business_description", "Opened\x07 on"),
                ("c2a", "CUSTOMER_ID", "business_attribute_name", "Customer Id"),
                message=_msg("m5", "Drafts."),
                source="draft",
            ),
            _cells(
                session,
                ("r2c", "*", "raw_path", "/raw/corebank/accounts"),
                ("r2c", "*", "curated_table", "CUR_ACCOUNTS"),
                ("c2a", "*", "asl_table", "ASL_ACCOUNTS"),
                ("dd", "*", "carry_to_curated", "Y"),
                ("dd", "OPEN_DATE", "carry_to_curated", "N"),
                message=_msg(
                    "m6",
                    "Raw path /raw/corebank/accounts. Curated table CUR_ACCOUNTS. ASL table "
                    "ASL_ACCOUNTS. Carry every attribute forward except OPEN_DATE.",
                ),
            ),
            set_derived_fields(
                session,
                [
                    {
                        "name": "FULL_KEY",
                        "logic": "CUSTOMER_ID || ACCOUNT_NO",
                        "join_key": "CUSTOMER_ID",
                    }
                ],
                [],
                None,
                _msg(
                    "m7",
                    "Add a derived field FULL_KEY: CUSTOMER_ID || ACCOUNT_NO, join key CUSTOMER_ID.",
                ),
                now=NOW,
            ),
        ]
    for outcome in outcomes:
        assert not outcome.refused, outcome.refused
    return session


@cache
def _base_session() -> Session:
    return _session()


def _built(
    steps: Sequence[Mapping[str, Any]] = (),
    *,
    templates: Mapping[str, bytes] | None = None,
    session: Session | None = None,
) -> Built:
    """Both workbooks of ``session`` (by default :func:`_session`'s), from ``templates``
    (by default the made-up ones)."""

    files = _template_files() if templates is None else templates
    with _rule_files({}), _templates(files):
        return build_workbooks(session or _base_session(), steps, now=NOW)


def _book(workbook: BuiltWorkbook) -> Workbook:
    return load_workbook(io.BytesIO(workbook.content))


def _books(steps: Sequence[Mapping[str, Any]] = (), **kw: Any) -> tuple[Workbook, Workbook]:
    built = _built(steps, **kw)
    return _book(built.workbooks[0]), _book(built.workbooks[1])


def _values(ws: Worksheet) -> list[tuple[Any, ...]]:
    return [tuple(cell.value for cell in row) for row in ws.iter_rows()]


def _column(ws: Worksheet, header: str) -> list[Any]:
    """The values under ``header``, without the header."""

    headers = [cell.value for cell in ws[1]]
    number = headers.index(header) + 1
    return [ws.cell(row, number).value for row in range(2, ws.max_row + 1)]


def _fill(cell: Any) -> str | None:
    """A cell's solid fill as RRGGBB, or None."""

    if cell.fill is None or cell.fill.fill_type != "solid":
        return None
    return str(cell.fill.fgColor.rgb)[-6:]


def _text(cell: Any) -> str | None:
    """A cell's text color as RRGGBB, or None for the default."""

    color = cell.font.color
    if color is None or color.type != "rgb":
        return None
    return str(color.rgb)[-6:]


def _validations(ws: Worksheet) -> dict[str, str]:
    """Cell range -> list formula."""

    return {
        str(validation.sqref): validation.formula1
        for validation in ws.data_validations.dataValidation
    }


def _refusal(steps: Sequence[Any], **kw: Any) -> str:
    try:
        _built(steps, **kw)
    except StepError as error:
        return str(error)
    raise AssertionError("the steps were applied")


def _normalize_refusal(steps: Sequence[Any], first: int = 1) -> str:
    try:
        normalize_steps(steps, first=first)
    except StepError as error:
        return str(error)
    raise AssertionError("the steps were accepted")


# -- made-up templates, shaped like the organization's ------------------------------


def _saved(wb: Workbook) -> bytes:
    # Who made a template must not carry into the files built from it.
    wb.properties.creator = "Template Author"
    wb.properties.lastModifiedBy = "Template Editor"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _header(ws: Worksheet, headers: Sequence[str]) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    for number, header in enumerate(headers, start=1):
        cell = ws.cell(1, number, header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
    ws.row_dimensions[1].height = 30


def _list(ws: Worksheet, cells: str, formula: str, **options: Any) -> None:
    validation = DataValidation(type="list", formula1=formula, allow_blank=True, **options)
    validation.add(cells)
    ws.add_data_validation(validation)


_DD_HEADERS = (
    "Sequence_Id",
    "Data_Source_Name",
    "Physical_Attribute_Name",
    "Business_Attribute_Name",
    "Business_Key_Identifier",
    "Mandatory_Optional",
    "Source_Attribute_DataType",
    "Length",
    "Classification_Level",
    "PII_PCI",
    "PII_PCI_Handling_Method",
    "PII_PCI_Handling_Description",
    "Comments",
    "Steward_Notes",
)
_FILE_DETAIL_LABELS = (
    "Data Source Name",
    "Source System Name",
    "CMDB Number",
    "File Name",
    "Number of Attributes",
    "Data Format",
    "Is Header Present",
    "Delimiters",
    "Frequency",
    "Date/Time of File Expectation",
)
_HANDLING = '"1. Removed,2. Hashed,5. Masked"'


@cache
def _dd_template() -> bytes:
    wb = Workbook()
    wb.active.title = "Instructions"
    wb.active["A1"] = "Fill in one row per attribute."
    form = wb.create_sheet("File Details")
    form["C1"] = "File Details"
    form.merge_cells("C1:D1")
    for row, label in enumerate(_FILE_DETAIL_LABELS, start=2):
        form.cell(row, 3, label)
    form["C12"], form["D12"] = "Owner", "keep me"

    ws = wb.create_sheet("Data Dictionary")
    _header(ws, _DD_HEADERS)
    for row in (2, 3):
        for number in range(1, len(_DD_HEADERS) + 1):
            ws.cell(row, number, "EXAMPLE_ROW")
    ws.merge_cells("M3:N3")
    ws["C2"].font = Font(name="Arial", size=9)
    ws.column_dimensions["B"].hidden = True
    ws.column_dimensions["C"].width = 30
    _list(ws, "E2:E1000", '"Yes,No"')
    _list(ws, "F2:F1000", '"Mandatory,Optional"')
    _list(ws, "I2:I1000", '"Secret,Confidential,Sensitive,Open"')
    _list(ws, "J2:J1000", '"Yes,No"')
    _list(
        ws,
        "K2:K1000",
        _HANDLING,
        showErrorMessage=True,
        errorTitle="Handling method",
        error="Pick a method from the list.",
    )
    _list(ws, "N2:N1000", "Values!$A$2:$A$3")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = "A1:N3"

    values = wb.create_sheet("Values")
    values.append(("PII_PCI_Handling_Method_Values", "PII_PCI_Handling_Method_Description"))
    values.append(("1. Removed", "Removed before landing"))
    values.append(("5. Masked", "Shown masked to readers"))
    return _saved(wb)


_R2C_HEADERS = (
    "id",
    "data_source",
    "raw_file_name",
    "raw_column",
    "in_cur(Y/N)",
    "cur_table_name",
    "cur_column",
    "cur_datatype",
    "PII/PCI",
)
_ASL_HEADERS = (
    "ID",
    "Target System",
    "Target Table Name",
    "Target Column",
    "Target Datatype",
    "Source Column",
    "Transformation Logic Detail",
    "Reviewer Comments",
    "Reviewer Comments",
)


@cache
def _sttm_template() -> bytes:
    wb = Workbook()
    info = wb.active
    info.title = "Document Info"
    info["A1"] = "Document Information"
    info["A2"], info["A3"] = "Data Source", "Source System"
    for number, label in enumerate(("Version", "Date", "Approved By", "Comments"), start=1):
        info.cell(5, number, label)
    r2c = wb.create_sheet("Data Mapping (RAW-CUR)")
    _header(r2c, _R2C_HEADERS)
    _list(r2c, "E2:E500", '"Y,N"')
    r2c.freeze_panes = "A2"
    asl = wb.create_sheet("ASL Data Mapping")
    _header(asl, _ASL_HEADERS)
    return _saved(wb)


def _template_files() -> dict[str, bytes]:
    return {"data-dictionary.xlsx": _dd_template(), "sttm.xlsx": _sttm_template()}


def _modified(template: bytes, change: Callable[[Workbook], None]) -> bytes:
    wb = load_workbook(io.BytesIO(template))
    change(wb)
    return _saved(wb)


def _fills(ws: Worksheet, min_row: int = 2) -> dict[str, str]:
    """Coordinate -> fill of every filled cell from ``min_row`` down."""

    return {
        cell.coordinate: fill
        for row in ws.iter_rows(min_row=min_row)
        for cell in row
        if (fill := _fill(cell))
    }


def _core(workbook: BuiltWorkbook) -> str:
    with zipfile.ZipFile(io.BytesIO(workbook.content)) as archive:
        return archive.read("docProps/core.xml").decode("utf-8")


_DATA_SOURCE = "CoreBank-daily accounts feed"
_VALIDATION = (
    "6 of 10 checks pass. Failing: Every carried-forward attribute has a complete "
    "Raw-to-Curated row; Every attribute has a PII/PCI flag and a classification; Every INTG, "
    "Curated and ASL type is set, from the type mapping or by the analyst; Every other "
    "required column has a value"
)


# -- the organization's templates -------------------------------------------------


def test_the_session_fills_the_templates_mapping_sheets() -> None:
    session = _base_session()
    built = _built()
    dd_file, sttm_file = built.workbooks
    assert built.notes == ()
    assert [(book.key, book.label, book.template, book.reason) for book in built.workbooks] == [
        ("data_dictionary", "Data Dictionary workbook", True, None),
        ("sttm", "STTM workbook", True, None),
    ]
    assert dd_file.name == f"sttm-daily-accounts-feed-{session.id}-v1-data-dictionary.xlsx"
    assert sttm_file.name == f"sttm-daily-accounts-feed-{session.id}-v1-sttm.xlsx"
    # The drafts: CUSTOMER_ID's business name and the proposed frequency on File Details.
    assert (dd_file.drafts, dd_file.unresolved) == (2, 5)
    assert (sttm_file.drafts, sttm_file.unresolved) == (0, 15)
    assert [(sheet.title, sheet.kind) for sheet in dd_file.sheets] == [
        ("Instructions", "fixed"),
        ("File Details", "fixed"),
        ("Data Dictionary", "table"),
        ("Values", "fixed"),
        ("About", "fixed"),
    ]
    assert dd_file.sheets[2].columns == _DD_HEADERS
    assert dd_file.sheets[2].hidden == ("Data_Source_Name",)

    dd, sttm = _book(dd_file), _book(sttm_file)
    ws = dd["Data Dictionary"]
    # "Masked" is written as the dropdown's "5. Masked", with the template's description of
    # it; "Internal" is no choice of the Classification dropdown, so it stays as given.
    assert _values(ws) == [
        _DD_HEADERS,
        (1, _DATA_SOURCE, "CUSTOMER_ID", "Customer Id", "Yes", "Mandatory", "VARCHAR", 20)
        + ("Confidential", "Yes", "5. Masked", "Shown masked to readers", None, None),
        (2, _DATA_SOURCE, "ACCOUNT_NO", None, "No", "Mandatory", "VARCHAR", 16)
        + ("Internal", "No", None, None, None, None),
        (3, _DATA_SOURCE, "OPEN_DATE", None, "No", "Optional", "DATE", None)
        + ("?", "REVIEW", None, None, "Not carried to Curated.", None),
        (4, _DATA_SOURCE, "FULL_KEY", None, "No", None, "?", None, "?", "REVIEW", None, None)
        + ("Derived: CUSTOMER_ID || ACCOUNT_NO. Join key: CUSTOMER_ID.", None),
    ]
    assert _fills(ws) == {
        "D2": "FFF2CC",
        "I4": "FFC7CE",
        "J4": "FFC7CE",
        "G5": "FFC7CE",
        "I5": "FFC7CE",
        "J5": "FFC7CE",
    }
    # The STTM sheets hold the attributes carried to Curated: not OPEN_DATE.
    r2c = sttm["Data Mapping (RAW-CUR)"]
    assert _values(r2c) == [
        _R2C_HEADERS,
        (1, _DATA_SOURCE, "accounts_*.txt", "CUSTOMER_ID", "Y", "CUR_ACCOUNTS", "?", "?", "Yes"),
        (2, _DATA_SOURCE, "accounts_*.txt", "ACCOUNT_NO", "Y", "CUR_ACCOUNTS", "?", "?", "No"),
        (3, _DATA_SOURCE, "accounts_*.txt", None, "Y", "CUR_ACCOUNTS", "?", "?", "REVIEW"),
    ]
    assert set(_fills(r2c)) == {"G2", "H2", "G3", "H3", "G4", "H4", "I4"}
    asl = sttm["ASL Data Mapping"]
    assert _values(asl) == [
        _ASL_HEADERS,
        (1, "ASL", "ASL_ACCOUNTS", "?", "?", None, "?", None, None),
        (2, "ASL", "ASL_ACCOUNTS", "?", "?", None, "?", None, None),
        (3, "ASL", "ASL_ACCOUNTS", "?", "?", None, "CUSTOMER_ID || ACCOUNT_NO", None, None),
    ]
    assert set(_fills(asl).values()) == {"FFC7CE"} and len(_fills(asl)) == 8


def test_the_raw_to_curated_sheet_gives_the_source_type_and_the_logic_type() -> None:
    def more_columns(wb: Workbook) -> None:
        ws = wb["Data Mapping (RAW-CUR)"]
        ws.cell(1, ws.max_column + 1, "raw_datatype")
        ws.cell(1, ws.max_column + 1, "cur_logictype")

    session = _session()
    with _rule_files({}):
        outcome = _cells(
            session,
            ("r2c", "CUSTOMER_ID", "logic_type", "Direct move"),
            message=_msg("m8", "CUSTOMER_ID's logic type: Direct move."),
        )
    assert not outcome.refused, outcome.refused
    files = {**_template_files(), "sttm.xlsx": _modified(_sttm_template(), more_columns)}
    r2c = _book(_built(templates=files, session=session).workbooks[1])["Data Mapping (RAW-CUR)"]

    # The source's own type is the specification's (a derived field has none); the
    # logic type is the analyst's.
    assert _column(r2c, "raw_datatype") == ["VARCHAR", "VARCHAR", None]
    assert _column(r2c, "cur_logictype") == ["Direct move", None, None]


def test_the_templates_look_is_kept() -> None:
    dd, sttm = _books()
    ws = dd["Data Dictionary"]
    assert _validations(ws) == {
        "E2:E5": '"Yes,No"',
        "F2:F5": '"Mandatory,Optional"',
        "I2:I5": '"Secret,Confidential,Sensitive,Open"',
        "J2:J5": '"Yes,No"',
        "K2:K5": _HANDLING,
        "N2:N5": "Values!$A$2:$A$3",
    }
    handling = next(v for v in ws.data_validations.dataValidation if str(v.sqref) == "K2:K5")
    assert (handling.showErrorMessage, handling.errorTitle, handling.error) == (
        True,
        "Handling method",
        "Pick a method from the list.",
    )
    assert (ws.freeze_panes, ws.auto_filter.ref) == ("C2", "A1:N5")
    assert not ws.merged_cells.ranges
    assert ws.column_dimensions["B"].hidden and ws.column_dimensions["C"].width == 30
    assert ws.row_dimensions[1].height == 30
    assert all(_fill(cell) == "1F4E78" and cell.font.b for cell in ws[1])
    # Each column's data cells are styled as its first template row.
    assert {(ws.cell(row, 3).font.name, ws.cell(row, 3).font.size) for row in range(2, 6)} == {
        ("Arial", 9)
    }
    assert "EXAMPLE_ROW" not in {value for row in _values(ws) for value in row}

    r2c, asl = sttm["Data Mapping (RAW-CUR)"], sttm["ASL Data Mapping"]
    assert (r2c.freeze_panes, r2c.auto_filter.ref, _validations(r2c)) == (
        "A2",
        None,
        {"E2:E4": '"Y,N"'},
    )
    assert (asl.freeze_panes, asl.auto_filter.ref, _validations(asl)) == (None, None, {})


def test_a_dropdown_that_lists_another_sheets_cells_is_matched_to_them() -> None:
    def handling(formula: str | None) -> Callable[[Workbook], None]:
        def change(wb: Workbook) -> None:
            validations = wb["Data Dictionary"].data_validations
            dropdown = next(v for v in validations.dataValidation if str(v.sqref) == "K2:K1000")
            if formula is None:
                validations.dataValidation.remove(dropdown)
            else:
                dropdown.formula1 = formula

        return change

    def handling_column(formula: str | None) -> tuple[list[Any], dict[str, str]]:
        template = _modified(_dd_template(), handling(formula))
        dd, _ = _books(templates={**_template_files(), "data-dictionary.xlsx": template})
        ws = dd["Data Dictionary"]
        return _column(ws, "PII_PCI_Handling_Method"), _validations(ws)

    # The Values sheet's "1. Removed" and "5. Masked".
    column, validations = handling_column("Values!$A$2:$A$3")
    assert column == ["5. Masked", None, None, None]
    assert validations["K2:K5"] == "Values!$A$2:$A$3"
    # Without a dropdown the value stays as given.
    column, validations = handling_column(None)
    assert column == ["Masked", None, None, None] and "K2:K5" not in validations


def test_a_dropdowns_choices_are_those_it_lists_or_the_cells_it_names() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Data Dictionary"
    ws["Z2"], ws["Z3"] = "Own", "Sheet"
    values = wb.create_sheet("My Values")
    values["A1"] = "Handling"
    values["A2"], values["A3"], values["A5"] = "1. Removed", " ", 5
    values["A6"] = CellRichText(["5. ", TextBlock(InlineFont(b=True), "Masked")])
    cases = {
        '"Yes, No"': ("Yes", "No"),
        # Blank cells are no choice; a number or rich text reads as its text.
        "'My Values'!$A$2:$A$6": ("1. Removed", "5", "5. Masked"),
        "'my values'!A2:A6": ("1. Removed", "5", "5. Masked"),
        "'My Values'!$A:$A": ("Handling", "1. Removed", "5", "5. Masked"),
        "$Z$2:$Z$3": ("Own", "Sheet"),
        # A defined name, a sheet the workbook does not have, a function: not read.
        "Handling": None,
        "Nope!$A$2:$A$3": None,
        "OFFSET('My Values'!$A$2,0,0,2,1)": None,
    }
    for formula, choices in cases.items():
        assert workbook_mod._list_choices(ws, formula) == choices, formula


def test_the_template_forms_are_filled_in_and_other_sheets_kept() -> None:
    dd, sttm = _books()
    assert dd.sheetnames == ["Instructions", "File Details", "Data Dictionary", "Values", "About"]
    assert sttm.sheetnames == [
        "Document Info",
        "Data Mapping (RAW-CUR)",
        "ASL Data Mapping",
        "About",
    ]
    form = dd["File Details"]
    assert [form.cell(row, 4).value for row in range(2, 13)] == [
        _DATA_SOURCE,
        "CoreBank",
        "APP-1234",
        "accounts_*.txt",
        3,
        "Delimited",
        "Y",
        "|",
        "daily",
        None,
        "keep me",
    ]
    assert _fills(form, min_row=1) == {"D10": "FFF2CC"}
    assert [str(cells) for cells in form.merged_cells.ranges] == ["C1:D1"]
    assert _values(dd["Instructions"]) == [("Fill in one row per attribute.",)]
    assert _values(dd["Values"]) == [
        ("PII_PCI_Handling_Method_Values", "PII_PCI_Handling_Method_Description"),
        ("1. Removed", "Removed before landing"),
        ("5. Masked", "Shown masked to readers"),
    ]
    assert _values(sttm["Document Info"]) == [
        ("Document Information", None, None, None),
        ("Data Source", None, None, _DATA_SOURCE),
        ("Source System", None, None, "CoreBank"),
        (None, None, None, None),
        ("Version", "Date", "Approved By", "Comments"),
        ("1.0", "2026-11-03", None, "Draft, not approved"),
    ]
    # Each workbook opens on its first mapping sheet.
    for wb, active in ((dd, "Data Dictionary"), (sttm, "Data Mapping (RAW-CUR)")):
        assert wb.active.title == active
        assert [ws.title for ws in wb.worksheets if ws.sheet_view.tabSelected] == [active]


def test_a_change_log_is_found_by_its_headers() -> None:
    def with_document_info(wb: Workbook) -> None:
        info = wb.create_sheet("Document Info_Start Here", 0)
        info["B2"] = "Team Contacts:"
        info["B3"], info["B4"] = "Team Lead", "Data Analyst"
        info["B6"] = "Change Log"
        headers = ("Version", "Date", "Author", "Change Description", "Reviewer")
        for number, header in enumerate(headers, start=2):
            info.cell(7, number, header)
        info["B10"] = CellRichText(
            ["Read the ", TextBlock(InlineFont(b=True), "Values"), " first."]
        )

    templates = {
        **_template_files(),
        "data-dictionary.xlsx": _modified(_dd_template(), with_document_info),
    }
    approval = Approval(by="Analyst A", at=timestamp(NOW), message_id="m9", message="Approve.")
    for session, revision in (
        (_base_session(), ("1.0", "2026-11-03", None, "Draft, not approved", None)),
        (
            _base_session().model_copy(update={"approval": approval}),
            ("1.0", "2026-11-03", "Analyst A", "Approved", None),
        ),
    ):
        built = _built(templates=templates, session=session)
        info = load_workbook(io.BytesIO(built.workbooks[0].content), rich_text=True)[
            "Document Info_Start Here"
        ]
        assert tuple(info.cell(8, number).value for number in range(2, 7)) == revision
        # The team's contacts are not the session's to give.
        assert (info["C3"].value, info["C4"].value) == (None, None)
        note = info["B10"].value
        assert isinstance(note, CellRichText) and str(note) == "Read the Values first."
        assert [run.font.b for run in note if isinstance(run, TextBlock)] == [True]


def test_the_about_sheet_says_where_the_values_come_from() -> None:
    session = _base_session()
    dd, sttm = _books()
    facts = [
        ("Session", session.id),
        ("Version", "1"),
        ("Source system", "CoreBank"),
        ("Source", "daily accounts feed"),
        ("Status", "collecting inputs"),
        ("Review mode", "none chosen, so every review stops for the analyst"),
        ("Approval", "Not approved"),
        ("Validation", _VALIDATION),
        ("Built", "2026-11-03T12:00:00+00:00"),
    ]
    legend = [
        ("Cell fills", None, None),
        ("(any value)", "The agent's draft, for the analyst to confirm.", None),
        ("?", "Not decided yet.", None),
        ("REVIEW", "PII/PCI not decided yet: treat the attribute as sensitive.", None),
    ]
    details = [
        ("File detail", "Value", "Source"),
        ("Primary key", "CUSTOMER_ID", "the specification"),
        ("Join keys", None, "not given"),
        ("CMDB application number", "APP-1234", "the analyst"),
        ("File name pattern", "accounts_*.txt", "the analyst"),
        ("Delimiter", "pipe", "the analyst"),
        ("Header rows", "1", "the analyst"),
        ("Trailer rows", "0", "the analyst"),
        ("Frequency", "daily", "the agent's draft"),
        ("Expected arrival", None, "not given"),
    ]
    first = (
        "The values come from the STTM session: to change one, ask the agent. Edits made in "
        "this file are not read back."
    )
    template_notes = [
        "Template columns the session has no value for are left blank.",
        (
            "A value that matches a dropdown choice, apart from case and numbering, is written as "
            "that choice."
        ),
    ]
    sttm_note = "The STTM sheets list the attributes carried to Curated."
    blank = (None, None, None)
    for wb, template, notes in (
        (dd, "data-dictionary.xlsx", [first, *template_notes]),
        (sttm, "sttm.xlsx", [first, sttm_note, *template_notes]),
    ):
        about = wb["About"]
        assert _values(about) == [
            *[(label, value, None) for label, value in facts],
            ("Layout", f"The organization's template, {template}", None),
            blank,
            *legend,
            blank,
            *details,
            blank,
            ("Notes", notes[0], None),
            *[(None, note, None) for note in notes[1:]],
        ]
        assert _fills(about, min_row=1) == {
            "A13": "FFF2CC",
            "A14": "FFC7CE",
            "A15": "FFC7CE",
            "B25": "FFF2CC",
        }
        assert [about.column_dimensions[letter].width for letter in "ABC"] == [28, 80, 16]


def test_an_approved_session_shows_who_approved_it() -> None:
    approval = Approval(by="Analyst A", at=timestamp(NOW), message_id="m9", message="Approve.")
    dd, sttm = _books(session=_base_session().model_copy(update={"approval": approval}))
    assert dd["About"]["B7"].value == "Approved by Analyst A at 2026-11-03T12:00:00+00:00"
    assert _values(sttm["Document Info"])[5] == ("1.0", "2026-11-03", "Analyst A", "Approved")

    override = Approval(
        at=timestamp(NOW),
        message_id="m9",
        message="Approve it anyway.",
        override_reason="Checked by hand",
        failed_checks=["pii_classified", "required_values"],
    )
    replacement = _base_session().model_copy(
        update={"version": 2, "supersedes": "SESSIONONE", "approval": override}
    )
    built = _built(session=replacement)
    assert [book.name for book in built.workbooks] == [
        f"sttm-daily-accounts-feed-{replacement.id}-v2-data-dictionary.xlsx",
        f"sttm-daily-accounts-feed-{replacement.id}-v2-sttm.xlsx",
    ]
    dd, sttm = (_book(book) for book in built.workbooks)
    assert dd["About"]["B2"].value == "2, replaces approved session SESSIONONE"
    assert dd["About"]["B7"].value == (
        "Approved at 2026-11-03T12:00:00+00:00, overriding 2 failed checks with the analyst's "
        'reason "Checked by hand"'
    )
    assert _values(sttm["Document Info"])[5] == (
        "2.0",
        "2026-11-03",
        None,
        "Approved; replaces approved session SESSIONONE",
    )


def test_a_template_with_its_own_about_sheet_keeps_it() -> None:
    def with_about(wb: Workbook) -> None:
        wb.create_sheet("About")["A1"] = "The template's own notes."

    templates = {**_template_files(), "data-dictionary.xlsx": _modified(_dd_template(), with_about)}
    dd, _ = _books(templates=templates)
    assert dd.sheetnames[-2:] == ["About", "About 2"]
    assert _values(dd["About"]) == [("The template's own notes.",)]
    assert dd["About 2"]["A1"].value == "Session"


# -- without a usable template -----------------------------------------------------


def test_without_a_usable_template_the_session_columns_are_used() -> None:
    def without_sheet(wb: Workbook) -> None:
        wb["Data Dictionary"].title = "Dictionary"

    def other_columns(wb: Workbook) -> None:
        ws = wb["ASL Data Mapping"]
        ws.delete_rows(1)
        ws["A1"], ws["B1"] = "Foo", "Bar"

    cases = [
        (
            {},
            "the template data-dictionary.xlsx is not in this deployment",
            "the template sttm.xlsx is not in this deployment",
        ),
        (
            {"data-dictionary.xlsx": b"not a workbook", "sttm.xlsx": _sttm_template()},
            "the template data-dictionary.xlsx could not be read",
            None,
        ),
        (
            {
                "data-dictionary.xlsx": _modified(_dd_template(), without_sheet),
                "sttm.xlsx": _sttm_template(),
            },
            "the template data-dictionary.xlsx has no 'Data Dictionary' sheet",
            None,
        ),
        (
            {
                "data-dictionary.xlsx": _dd_template(),
                "sttm.xlsx": _modified(_sttm_template(), other_columns),
            },
            None,
            (
                "the template sttm.xlsx has none of the expected columns on its 'ASL Data Mapping' "
                "sheet"
            ),
        ),
    ]
    for templates, *reasons in cases:
        with _logged(workbook_mod.logger) as logged:
            built = _built(templates=templates)
        assert [book.reason for book in built.workbooks] == reasons
        assert [book.template for book in built.workbooks] == [r is None for r in reasons]
        # One line for each generic workbook, which says why.
        generic = [book.key for book in built.workbooks if not book.template]
        assert [line.rsplit(", so the ", 1)[-1] for line in logged] == [
            f"{key} is generic" for key in generic
        ], logged
        if templates.get("data-dictionary.xlsx") == b"not a workbook":
            assert logged[0].startswith(
                "STTM template data-dictionary.xlsx could not be read (BadZipFile: "
            ), logged
        for book, reason in zip(built.workbooks, reasons):
            if reason is not None:
                layout = _book(book)["About"]["B10"].value
                assert layout == f"Generic, from the session's own columns: {reason}"

    dd, sttm = _books(templates={})
    assert dd.sheetnames == ["Data Dictionary", "About"]
    assert sttm.sheetnames == ["Raw-to-Curated", "Curated-to-ASL", "About"]
    assert [dd["About"]["B28"].value, dd["About"]["B29"].value] == [
        (
            "The values come from the STTM session: to change one, ask the agent. Edits made in "
            "this file are not read back."
        ),
        None,
    ]


def test_the_generic_layout_shows_every_session_column() -> None:
    session = _base_session()
    built = _built(templates={})
    tables = Tables(session)
    sheets = {
        "data_dictionary": (0, "Data Dictionary"),
        "raw_to_curated": (1, "Raw-to-Curated"),
        "curated_to_asl": (1, "Curated-to-ASL"),
    }
    for table, (number, title) in sheets.items():
        ws = _book(built.workbooks[number])[title]
        columns, rows = COLUMNS[table], tables.rows(table)
        last = get_column_letter(len(columns))
        assert [cell.value for cell in ws[1]] == [column.label for column in columns]
        assert all(_fill(cell) == "D9E1F2" and cell.font.b for cell in ws[1])
        assert (ws.freeze_panes, ws.auto_filter.ref) == ("A2", f"A1:{last}{len(rows) + 1}")
        assert _validations(ws) == {
            f"{get_column_letter(n)}2:{get_column_letter(n)}{len(rows) + 1}": '"Y,N"'
            for n, column in enumerate(columns, start=1)
            if column.yes_no
        }
        assert ws.max_row == len(rows) + 1
        for row_number, row in enumerate(rows, start=2):
            for column_number, column in enumerate(columns, start=1):
                resolved = tables.resolve(table, row, column.key)
                if resolved.value is not None:
                    draft = resolved.source == "draft"
                    expected = (resolved.value.replace("\x07", ""), "FFF2CC" if draft else None)
                elif column.required or column.key in UNRESOLVED_LABELS:
                    expected = (UNRESOLVED_LABELS.get(column.key, "?"), "FFC7CE")
                else:
                    expected = (None, None)
                cell = ws.cell(row_number, column_number)
                assert (cell.value, _fill(cell)) == expected, (title, cell.coordinate)
    assert [(book.drafts, book.unresolved) for book in built.workbooks] == [(2, 5), (1, 33)]


def test_the_generic_layout_sizes_and_wraps_long_text() -> None:
    session = _session()
    with _rule_files({}):
        outcome = _cells(
            session,
            ("dd", "ACCOUNT_NO", "business_description", "A long description " * 4),
            message=_msg("m8", "Drafts."),
            source="draft",
        )
    assert not outcome.refused
    ws = _book(_built(templates={}, session=session).workbooks[0])["Data Dictionary"]
    # Business description: capped at 50 wide and wrapped; Attribute: its longest value + 2.
    assert ws.column_dimensions["H"].width == 50
    assert all(ws.cell(row, 8).alignment.wrap_text for row in range(2, 6))
    assert ws.column_dimensions["A"].width == len("CUSTOMER_ID") + 2
    assert not ws["A2"].alignment.wrap_text


def test_values_are_never_formulas() -> None:
    for templates in (None, {}):
        for book in _built(templates=templates).workbooks:
            with zipfile.ZipFile(io.BytesIO(book.content)) as archive:
                names = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
                assert names
                for name in names:
                    xml = archive.read(name).decode("utf-8")
                    assert "<f>" not in xml and "<f " not in xml, (book.name, name)
    ws = _book(_built(templates={}).workbooks[0])["Data Dictionary"]
    # ACCOUNT_NO's and OPEN_DATE's business descriptions, both the agent's drafts.
    hyperlink, opened = ws["H3"], ws["H4"]
    assert (hyperlink.value, hyperlink.data_type, _fill(hyperlink)) == (
        '=HYPERLINK("http://x.test","x")',
        "s",
        "FFF2CC",
    )
    # A control character Excel would refuse is dropped.
    assert opened.value == "Opened on"


def test_the_files_do_not_say_who_made_the_template() -> None:
    for templates in (None, {}):
        for book in _built(templates=templates).workbooks:
            core = _core(book)
            assert "creator" not in core and "lastModifiedBy" not in core, core
            assert "Template" not in core, core
            assert f"<dc:title>{book.label}: CoreBank / daily accounts feed</dc:title>" in core
            assert "2026-11-03T12:00:00Z</dcterms:created>" in core


def test_a_workbook_over_the_size_limit_is_refused() -> None:
    saved = workbook_mod.MAX_BYTES
    workbook_mod.MAX_BYTES = 1000
    try:
        _built()
    except SessionError as error:
        message, kind = str(error), type(error)
    else:
        raise AssertionError("the workbooks were built")
    finally:
        workbook_mod.MAX_BYTES = saved
    assert kind is SessionError
    assert re.fullmatch(
        r"The Data Dictionary workbook would be \d+\.\d MB, over the \d+ MB limit; nothing "
        r"was stored\.",
        message,
    ), message


def test_file_names_are_safe_and_short() -> None:
    session = _base_session()

    def named(source: str) -> str:
        return file_name(session.model_copy(update={"source": source}), "sttm")

    assert named("../Weird: name//with*stuff") == (
        f"sttm-Weird-name-with-stuff-{session.id}-v1-sttm.xlsx"
    )
    assert named("***") == f"sttm-source-{session.id}-v1-sttm.xlsx"
    assert named("a" * 50) == f"sttm-{'a' * 40}-{session.id}-v1-sttm.xlsx"
    # Cut at 40 characters, then trimmed of the dash the cut left at the end.
    assert named("x" * 39 + "-yz") == f"sttm-{'x' * 39}-{session.id}-v1-sttm.xlsx"
    assert file_name(session, "data_dictionary").endswith("-v1-data-dictionary.xlsx")


# -- layout steps, each on its own ---------------------------------------------------


def test_steps_are_stored_in_one_form() -> None:
    steps = normalize_steps(
        [
            {
                "color": "Orange",
                "value": "  Y ",
                "match": "equals",
                "columns": "PII_PCI",
                "sheet": "  Data   Dictionary ",
                "op": "highlight",
                "extra": 1,
            },
            {"op": "freeze", "sheet": "dd"},
            {"op": "filter", "sheet": "dd"},
            {"op": "sort", "sheet": "dd", "columns": ["Length"]},
            {
                "op": "highlight",
                "sheet": "dd",
                "columns": ["PII_PCI"],
                "match": "empty",
                "value": "x",
                "color": "#c6efce",
            },
            {
                "op": "format_columns",
                "sheet": "dd",
                "columns": ["Comments", "comments"],
                "width": 12.0,
            },
            {"op": "format_columns", "sheet": "dd", "columns": ["Comments"], "width": 12.34},
            {"op": "move_sheet", "sheet": "Values", "position": " 2 "},
            {
                "op": "dropdown",
                "sheet": "dd",
                "columns": ["PII_PCI"],
                "choices": ["Yes", " yes", 5],
            },
            {"op": "header", "color": "grey", "sheet": None},
            {"op": "header", "sheet": "r2c", "color": " Dark-Blue "},
        ]
    )
    assert steps == [
        {
            "op": "highlight",
            "sheet": "Data Dictionary",
            "columns": ["PII_PCI"],
            "match": "equals",
            "value": "Y",
            "color": "#FFCC99",
        },
        {"op": "freeze", "sheet": "dd", "on": True},
        {"op": "filter", "sheet": "dd", "on": True},
        {"op": "sort", "sheet": "dd", "columns": ["Length"], "descending": False},
        {
            "op": "highlight",
            "sheet": "dd",
            "columns": ["PII_PCI"],
            "match": "empty",
            "color": "#C6EFCE",
        },
        {"op": "format_columns", "sheet": "dd", "columns": ["Comments"], "width": 12},
        {"op": "format_columns", "sheet": "dd", "columns": ["Comments"], "width": 12.3},
        {"op": "move_sheet", "sheet": "Values", "position": 2},
        {"op": "dropdown", "sheet": "dd", "columns": ["PII_PCI"], "choices": ["Yes", "5"]},
        {"op": "header", "color": "#D9D9D9"},
        {"op": "header", "sheet": "r2c", "color": "#1F4E78"},
    ]
    assert list(steps[0]) == ["op", "sheet", "columns", "match", "value", "color"]
    assert type(steps[5]["width"]) is int
    # A stored step reads back as itself.
    assert normalize_steps(steps) == steps
    assert set(get_args(Op)) == set(workbook_mod._FIELDS)


def test_a_wrong_step_is_refused_with_its_number() -> None:
    dd = {"sheet": "dd"}
    one = {**dd, "columns": ["PII_PCI"]}
    cases = [
        ("x", "not a step."),
        (
            {"op": "explode"},
            (
                "unknown op 'explode'; use one of: rename_sheet, move_sheet, order_columns, "
                "hide_columns, show_columns, remove_columns, add_column, format_columns, freeze, "
                "filter, sort, highlight, header, dropdown."
            ),
        ),
        ({"op": "highlight", **dd}, "highlight needs columns and match and color."),
        ({"op": "rename_sheet", **dd, "name": None}, "rename_sheet needs name."),
        ({"op": "rename_sheet", **dd, "name": 5}, "name must be text."),
        ({"op": "filter", "sheet": "  "}, "sheet is empty."),
        ({"op": "move_sheet", **dd, "position": 0}, "position must be a whole number from 1."),
        ({"op": "move_sheet", **dd, "position": True}, "position must be a whole number from 1."),
        ({"op": "move_sheet", **dd, "position": 1.5}, "position must be a whole number from 1."),
        ({"op": "format_columns", **one, "width": 101}, "width must be from 4 to 100."),
        ({"op": "format_columns", **one, "width": "wide"}, "width must be a number."),
        ({"op": "format_columns", **one}, "format_columns needs a width, wrap or both."),
        ({"op": "filter", **dd, "on": "yes"}, "on must be true or false."),
        ({"op": "hide_columns", **dd, "columns": []}, "columns name at least one column."),
        ({"op": "hide_columns", **dd, "columns": ["--"]}, "columns '--' is not a column name."),
        (
            {"op": "highlight", **dd, "columns": ["A", "B"], "match": "empty", "color": "red"},
            "highlight looks at exactly one column.",
        ),
        (
            {"op": "highlight", **one, "match": "equals", "color": "red"},
            "match=equals needs a value.",
        ),
        (
            {"op": "highlight", **one, "match": "like", "value": "x", "color": "red"},
            "match use one of: equals, contains, empty, not_empty.",
        ),
        (
            {"op": "header", "color": "pink"},
            (
                "color use one of yellow, orange, red, green, blue, grey, gray, purple, each also "
                "as dark <color>, or a hex color such as #FFCC99."
            ),
        ),
        (
            {"op": "header", "color": "#12345"},
            (
                "color use one of yellow, orange, red, green, blue, grey, gray, purple, each also "
                "as dark <color>, or a hex color such as #FFCC99."
            ),
        ),
        (
            {"op": "dropdown", **one, "choices": ["a,b"]},
            "choices 'a,b': a choice cannot hold a comma or a double quote.",
        ),
        (
            {"op": "dropdown", **one, "choices": ['say "hi"']},
            "choices 'say \"hi\"': a choice cannot hold a comma or a double quote.",
        ),
        (
            {"op": "dropdown", **one, "choices": ["x" * 128, "y" * 127]},
            "choices over 255 characters together, which Excel does not allow.",
        ),
        ({"op": "dropdown", **one, "choices": {"a": 1}}, "choices must be a list."),
        ({"op": "dropdown", **one, "choices": [True]}, "choices must be text."),
        (
            {"op": "freeze", **one, "on": False},
            "on=false unfreezes the sheet and takes no columns.",
        ),
        (
            {"op": "rename_sheet", **dd, "name": "x" * 32},
            "a sheet name has at most 31 characters.",
        ),
        (
            {"op": "rename_sheet", **dd, "name": "a/b"},
            "a sheet name cannot hold [ ] : * ? / or \\.",
        ),
        (
            {"op": "rename_sheet", **dd, "name": "'quoted"},
            "a sheet name cannot start or end with an apostrophe.",
        ),
        (
            {"op": "rename_sheet", **dd, "name": "history"},
            "Excel keeps the sheet name History for itself.",
        ),
        ({"op": "rename_sheet", **dd, "name": "--"}, "a sheet name needs a letter or a digit."),
        (
            {"op": "add_column", **dd, "name": "n" * 101},
            "a column name has at most 100 characters.",
        ),
        ({"op": "add_column", **dd, "name": "--"}, "a column name needs a letter or a digit."),
    ]
    for step, reason in cases:
        assert _normalize_refusal([step]) == f"step 1: {reason}", step
    # 255 characters together, the commas included, is allowed.
    assert normalize_steps([{"op": "dropdown", **one, "choices": ["x" * 127, "y" * 127]}])
    # Numbered from where the new steps start.
    assert _normalize_refusal([{"op": "filter", **dd}, "x"], first=4) == "step 5: not a step."
    filter_step = {"op": "filter", **dd}
    assert len(normalize_steps([filter_step] * MAX_STEPS)) == MAX_STEPS
    assert _normalize_refusal([filter_step] * 3, first=MAX_STEPS - 1) == (
        "step 51: a layout holds at most 50 steps, and this one would have 51."
    )


def test_each_step_reads_as_a_few_words() -> None:
    cases = [
        (
            {"op": "rename_sheet", "sheet": "dd", "name": "Dictionary"},
            "rename sheet dd to Dictionary",
        ),
        ({"op": "move_sheet", "sheet": "Values", "position": 1}, "move sheet Values to position 1"),
        ({"op": "order_columns", "sheet": "dd", "columns": ["A", "B"]}, "dd: put A, B first"),
        ({"op": "hide_columns", "sheet": "dd", "columns": ["A"]}, "dd: hide A"),
        ({"op": "show_columns", "sheet": "dd", "columns": ["A"]}, "dd: show A"),
        ({"op": "remove_columns", "sheet": "dd", "columns": ["A"]}, "dd: leave out A"),
        (
            {"op": "add_column", "sheet": "dd", "name": "Notes"},
            "dd: add an empty column Notes at the end",
        ),
        (
            {"op": "add_column", "sheet": "dd", "name": "Notes", "position": 2},
            "dd: add an empty column Notes at position 2",
        ),
        (
            {"op": "format_columns", "sheet": "dd", "columns": ["A"], "width": 20, "wrap": True},
            "dd: width 20 and wrap text for A",
        ),
        (
            {"op": "format_columns", "sheet": "dd", "columns": ["A"], "wrap": False},
            "dd: no text wrap for A",
        ),
        ({"op": "freeze", "sheet": "dd"}, "dd: freeze the header row"),
        (
            {"op": "freeze", "sheet": "dd", "columns": ["A"]},
            "dd: freeze the header row and the columns through A",
        ),
        (
            {"op": "freeze", "sheet": "dd", "columns": ["A", "B"]},
            "dd: freeze the header row and the columns through the rightmost of A, B",
        ),
        ({"op": "freeze", "sheet": "dd", "on": False}, "dd: unfreeze"),
        ({"op": "filter", "sheet": "dd", "on": False}, "dd: filter off"),
        ({"op": "sort", "sheet": "dd", "columns": ["A"]}, "dd: sort by A"),
        (
            {"op": "sort", "sheet": "dd", "columns": ["A", "B"], "descending": True},
            "dd: sort by A, B (descending)",
        ),
        (
            {"op": "highlight", "sheet": "dd", "columns": ["A"], "match": "equals", "value": "Y"}
            | {"color": "orange"},
            "dd: highlight rows where A is 'Y' in orange",
        ),
        (
            {"op": "highlight", "sheet": "dd", "columns": ["A"], "match": "contains", "value": "x"}
            | {"color": "#123456"},
            "dd: highlight rows where A contains 'x' in #123456",
        ),
        (
            {"op": "highlight", "sheet": "dd", "columns": ["A"], "match": "not_empty"}
            | {"color": "gray"},
            "dd: highlight rows where A is not empty in grey",
        ),
        ({"op": "header", "color": "#d9d9d9"}, "header color grey on every mapping sheet"),
        ({"op": "header", "sheet": "r2c", "color": "#FF0000"}, "r2c: header color #FF0000"),
        ({"op": "header", "color": "#1f4e78"}, "header color dark blue on every mapping sheet"),
        ({"op": "header", "sheet": "r2c", "color": "#595959"}, "r2c: header color dark grey"),
        (
            {"op": "dropdown", "sheet": "dd", "columns": ["PII_PCI"], "choices": ["Yes", "No"]},
            "dd: dropdown on PII_PCI: Yes, No",
        ),
        (
            {"op": "dropdown", "sheet": "dd", "columns": ["PII_PCI"], "choices": []},
            "dd: no dropdown on PII_PCI",
        ),
    ]
    for step, text in cases:
        assert describe_step(normalize_steps([step])[0]) == text, step


# -- layout steps on the workbooks ----------------------------------------------------


def test_sheets_are_renamed_and_moved_within_their_workbook() -> None:
    plain_dd, _ = _books()
    dd, sttm = _books(
        [
            # "about" is both workbooks' About sheet.
            {"op": "rename_sheet", "sheet": "about", "name": "Read Me"},
            {"op": "move_sheet", "sheet": "Read Me", "position": 1},
            {"op": "rename_sheet", "sheet": "dd", "name": "Dictionary"},
            {"op": "move_sheet", "sheet": "Dictionary", "position": 99},
            {"op": "move_sheet", "sheet": "Values", "position": 1},
            {"op": "move_sheet", "sheet": "Document Info", "position": 1},
            {"op": "move_sheet", "sheet": "r2c", "position": 9},
        ]
    )
    assert dd.sheetnames == ["Values", "Read Me", "Instructions", "File Details", "Dictionary"]
    assert sttm.sheetnames == [
        "Document Info",
        "Read Me",
        "ASL Data Mapping",
        "Data Mapping (RAW-CUR)",
    ]
    assert (dd.active.title, sttm.active.title) == ("Dictionary", "ASL Data Mapping")
    assert _values(dd["Dictionary"]) == _values(plain_dd["Data Dictionary"])
    assert dd["Read Me"]["A1"].value == "Session"

    # Swapping two titles goes through unused names, so neither clashes, and a dropdown
    # that lists a renamed sheet's cells, on a mapping sheet or a form, follows it.
    def with_form_dropdown(wb: Workbook) -> None:
        _list(wb["File Details"], "D12", "Values!$A$2:$A$3")

    template = _modified(_dd_template(), with_form_dropdown)
    dd, _ = _books(
        [
            {"op": "rename_sheet", "sheet": "Values", "name": "Swap"},
            {"op": "rename_sheet", "sheet": "Instructions", "name": "Values"},
            {"op": "rename_sheet", "sheet": "Swap", "name": "Instructions"},
        ],
        templates={**_template_files(), "data-dictionary.xlsx": template},
    )
    assert dd["Values"]["A1"].value == "Fill in one row per attribute."
    assert dd["Instructions"]["A1"].value == "PII_PCI_Handling_Method_Values"
    assert _validations(dd["Data Dictionary"])["N2:N5"] == "'Instructions'!$A$2:$A$3"
    assert _validations(dd["File Details"]) == {"D12": "'Instructions'!$A$2:$A$3"}


def test_a_formula_follows_the_sheets_it_names_under_their_new_titles() -> None:
    swap = {"values": "Instructions", "instructions": "Values"}
    cases = [
        ("Values!$A$2:$A$3", swap, "'Instructions'!$A$2:$A$3"),
        ("VALUES!$B:$B", swap, "'Instructions'!$B:$B"),
        (
            "OFFSET(Values!$A$1,0,0,COUNTA(Instructions!$A:$A),1)",
            swap,
            "OFFSET('Instructions'!$A$1,0,0,COUNTA('Values'!$A:$A),1)",
        ),
        ("'My Values'!A2:A3", {"my values": "Bob's list"}, "'Bob''s list'!A2:A3"),
        # Text, a sheet no step renamed and the formula's own sheet stay as they are.
        ('INDIRECT("Values!A1")', swap, 'INDIRECT("Values!A1")'),
        ("Other!A1:A2", swap, "Other!A1:A2"),
        ("$A$2:$A$3", swap, "$A$2:$A$3"),
        ('"Yes,No"', swap, '"Yes,No"'),
        ("Values!A1", {}, "Values!A1"),
    ]
    for formula, titles, expected in cases:
        assert workbook_mod._repoint(formula, titles) == expected, formula


def test_columns_are_ordered_hidden_left_out_added_and_sized() -> None:
    built = _built(
        [
            {
                "op": "order_columns",
                "sheet": "dd",
                "columns": ["PII_PCI", "physical attribute name"],
            },
            {"op": "hide_columns", "sheet": "dd", "columns": ["Length", "Comments"]},
            {"op": "show_columns", "sheet": "dd", "columns": ["Data_Source_Name", "Comments"]},
            {"op": "remove_columns", "sheet": "dd", "columns": ["Steward_Notes"]},
            {"op": "add_column", "sheet": "dd", "name": "Reviewer notes", "position": 2},
            {
                "op": "format_columns",
                "sheet": "dd",
                "columns": ["Comments"],
                "width": 60,
                "wrap": True,
            },
        ]
    )
    headers = (
        "PII_PCI",
        "Reviewer notes",
        "Physical_Attribute_Name",
        "Sequence_Id",
        "Data_Source_Name",
        "Business_Attribute_Name",
        "Business_Key_Identifier",
        "Mandatory_Optional",
        "Source_Attribute_DataType",
        "Length",
        "Classification_Level",
        "PII_PCI_Handling_Method",
        "PII_PCI_Handling_Description",
        "Comments",
    )
    info = built.workbooks[0].sheets[2]
    assert (info.columns, info.hidden) == (headers, ("Length",))
    ws = _book(built.workbooks[0])["Data Dictionary"]
    assert tuple(cell.value for cell in ws[1]) == headers
    assert _column(ws, "PII_PCI") == ["Yes", "No", "REVIEW", "REVIEW"]
    assert _column(ws, "Physical_Attribute_Name") == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
        "OPEN_DATE",
        "FULL_KEY",
    ]
    # The added column is empty, styled like its left neighbour's header.
    assert _column(ws, "Reviewer notes") == [None] * 4
    assert (ws.column_dimensions["B"].width, _fill(ws["B1"]), ws["B1"].font.b) == (
        18,
        "1F4E78",
        True,
    )
    hidden = {letter for letter, dimension in ws.column_dimensions.items() if dimension.hidden}
    assert hidden == {"J"}
    assert ws.column_dimensions["N"].width == 60
    assert all(ws.cell(row, 14).alignment.wrap_text for row in range(2, 6))
    # Dropdowns move with their columns; the left-out column's goes with it.
    assert _validations(ws) == {
        "A2:A5": '"Yes,No"',
        "G2:G5": '"Yes,No"',
        "H2:H5": '"Mandatory,Optional"',
        "K2:K5": '"Secret,Confidential,Sensitive,Open"',
        "L2:L5": _HANDLING,
    }
    # The template froze the columns through Data_Source_Name, now the fifth.
    assert (ws.freeze_panes, ws.auto_filter.ref) == ("F2", "A1:N5")


def test_freeze_filter_and_sort() -> None:
    dd, _ = _books(
        [
            {"op": "freeze", "sheet": "dd", "columns": ["Physical_Attribute_Name", "Sequence_Id"]},
            {"op": "filter", "sheet": "dd", "on": False},
            {"op": "sort", "sheet": "dd", "columns": ["Classification_Level"], "descending": True},
        ]
    )
    ws = dd["Data Dictionary"]
    assert (ws.freeze_panes, ws.auto_filter.ref) == ("D2", None)
    # Undecided classifications go last; each row keeps its own values.
    assert _column(ws, "Physical_Attribute_Name") == [
        "ACCOUNT_NO",
        "CUSTOMER_ID",
        "OPEN_DATE",
        "FULL_KEY",
    ]
    assert _column(ws, "Sequence_Id") == [2, 1, 3, 4]
    assert _fills(ws)["D3"] == "FFF2CC"

    by_two = {
        "op": "sort",
        "sheet": "dd",
        "columns": ["Mandatory_Optional", "Physical_Attribute_Name"],
    }
    dd, _ = _books([by_two])
    names = _column(dd["Data Dictionary"], "Physical_Attribute_Name")
    assert names == ["ACCOUNT_NO", "CUSTOMER_ID", "OPEN_DATE", "FULL_KEY"]
    dd, _ = _books([{**by_two, "descending": True}])
    names = _column(dd["Data Dictionary"], "Physical_Attribute_Name")
    assert names == ["OPEN_DATE", "CUSTOMER_ID", "ACCOUNT_NO", "FULL_KEY"]

    dd, _ = _books([{"op": "freeze", "sheet": "dd", "on": False}])
    assert dd["Data Dictionary"].freeze_panes is None
    dd, _ = _books([{"op": "freeze", "sheet": "dd"}])
    assert dd["Data Dictionary"].freeze_panes == "A2"
    # Numbers sort as numbers and before text.
    values = ["16", "9", 20, "abc", "-1.5"]
    assert sorted(values, key=workbook_mod._sort_key) == ["-1.5", "9", "16", 20, "abc"]


def test_highlights_fill_matching_rows_but_never_hide_a_mark() -> None:
    built = _built(
        [
            {"op": "highlight", "sheet": "dd", "columns": ["PII_PCI"], "match": "equals"}
            | {"value": "Y", "color": "orange"},
            {"op": "highlight", "sheet": "dd", "columns": ["Comments"], "match": "contains"}
            | {"value": "DERIVED", "color": "#c6efce"},
            {"op": "highlight", "sheet": "dd", "columns": ["Handling"], "match": "equals"}
            | {"value": "Tokenized", "color": "red"},
        ]
    )
    assert built.notes == (
        "Step 3 (dd: highlight rows where Handling is 'Tokenized' in red) matched no rows.",
    )
    ws = _book(built.workbooks[0])["Data Dictionary"]
    fills = _fills(ws)
    # CUSTOMER_ID (PII "Yes") in orange, but for its drafted business name.
    assert {fills[f"{letter}2"] for letter in "ABCEFGHIJKLMN"} == {"FFCC99"}
    assert fills["D2"] == "FFF2CC"
    # FULL_KEY ("Derived: ...") in green, but for its undecided cells.
    assert {fills[f"{letter}5"] for letter in "ABCDEFHKLMN"} == {"C6EFCE"}
    assert {fills[f"{letter}5"] for letter in "GIJ"} == {"FFC7CE"}
    assert not any(coordinate[1:] == "3" for coordinate in fills)

    # A later highlight of the same row wins; undecided cells count as empty.
    dd, _ = _books(
        [
            {"op": "highlight", "sheet": "dd", "columns": ["Comments"], "match": "contains"}
            | {"value": "derived", "color": "green"},
            {"op": "highlight", "sheet": "dd", "columns": ["classification"], "match": "empty"}
            | {"color": "purple"},
        ]
    )
    fills = _fills(dd["Data Dictionary"])
    assert {fills["A4"], fills["A5"], fills["K5"]} == {"D9C3E9"}
    assert "A3" not in fills


def test_header_colors_and_dropdowns() -> None:
    dd, sttm = _books(
        [
            {"op": "header", "color": "grey"},
            {"op": "header", "sheet": "r2c", "color": "#ff0000"},
            {
                "op": "dropdown",
                "sheet": "dd",
                "columns": ["Steward_Notes"],
                "choices": ["Open", "Done"],
            },
            {"op": "dropdown", "sheet": "dd", "columns": ["PII_PCI"], "choices": []},
        ]
    )
    assert {_fill(cell) for cell in dd["Data Dictionary"][1]} == {"D9D9D9"}
    assert {_fill(cell) for cell in sttm["Data Mapping (RAW-CUR)"][1]} == {"FF0000"}
    assert {_fill(cell) for cell in sttm["ASL Data Mapping"][1]} == {"D9D9D9"}
    # The template's white header text turns black, to stay readable on the light fills.
    for ws in (dd["Data Dictionary"], sttm["Data Mapping (RAW-CUR)"], sttm["ASL Data Mapping"]):
        assert {(_text(cell), cell.font.b) for cell in ws[1]} == {("000000", True)}
    # Only the mapping sheets.
    assert _fill(dd["File Details"]["C1"]) is None and _fill(dd["About"]["A1"]) is None
    assert _validations(dd["Data Dictionary"]) == {
        "E2:E5": '"Yes,No"',
        "F2:F5": '"Mandatory,Optional"',
        "I2:I5": '"Secret,Confidential,Sensitive,Open"',
        "K2:K5": _HANDLING,
        "N2:N5": '"Open,Done"',
    }


def test_text_stays_readable_on_every_fill() -> None:
    # White on each dark shade, black on each light tint.
    for name, code in workbook_mod.COLORS.items():
        expected = "FFFFFFFF" if name.startswith("dark ") else "FF000000"
        assert workbook_mod._text_color(code) == expected, name

    dd, sttm = _books(
        [
            {"op": "header", "sheet": "dd", "color": "dark blue"},
            {"op": "highlight", "sheet": "dd", "columns": ["PII_PCI"], "match": "equals"}
            | {"value": "Y", "color": "dark red"},
        ]
    )
    ws = dd["Data Dictionary"]
    assert {(_fill(cell), _text(cell)) for cell in ws[1]} == {("1F4E78", "FFFFFF")}
    # CUSTOMER_ID's row in white on dark red, but for its drafted business name, which
    # keeps its light fill and black text.
    assert {(_fill(ws[f"{letter}2"]), _text(ws[f"{letter}2"])) for letter in "ABCEFGHIJKLMN"} == {
        ("C00000", "FFFFFF")
    }
    assert (_fill(ws["D2"]), _text(ws["D2"])) == ("FFF2CC", "000000")
    # A row no step fills keeps the template's text color.
    assert {_text(cell) for cell in ws[3]} == {None}
    # A sheet no step named keeps the template's white text on its dark header.
    assert {(_fill(cell), _text(cell)) for cell in sttm["ASL Data Mapping"][1]} == {
        ("1F4E78", "FFFFFF")
    }


def test_columns_are_named_by_header_or_by_the_session_column_they_show() -> None:
    built = _built(
        [{"op": "hide_columns", "sheet": "dd", "columns": ["classification", "Handling"]}]
    )
    hidden = built.workbooks[0].sheets[2].hidden
    assert hidden == ("Data_Source_Name", "Classification_Level", "PII_PCI_Handling_Method")
    # A layout made on one layout applies to the other: the template's title and the
    # generic one name the same sheet.
    step = {"op": "hide_columns", "sheet": "Data Mapping (RAW-CUR)", "columns": ["Curated table"]}
    generic = _built([step], templates={}).workbooks[1].sheets[0]
    assert (generic.title, generic.hidden) == ("Raw-to-Curated", ("Curated table",))
    template = _built([{**step, "sheet": "Raw-to-Curated"}]).workbooks[1].sheets[1]
    assert (template.title, template.hidden) == ("Data Mapping (RAW-CUR)", ("cur_table_name",))


def test_a_step_that_does_not_fit_is_refused_with_its_number() -> None:
    hide = {"op": "hide_columns", "sheet": "dd", "columns": ["Length"]}
    cases = [
        (
            [{"op": "filter", "sheet": "Mapping"}],
            (
                "step 1 (filter on 'Mapping'): there is no sheet 'Mapping'. Sheets: Data Dictionary "
                "workbook: Instructions, File Details, Data Dictionary, Values, About; STTM workbook: "
                "Document Info, Data Mapping (RAW-CUR), ASL Data Mapping, About."
            ),
        ),
        (
            [hide, hide, {"op": "hide_columns", "sheet": "r2c", "columns": ["Nope"]}],
            (
                "step 3 (hide_columns on 'r2c'): 'Data Mapping (RAW-CUR)' has no column 'Nope'. Its "
                "columns: id, data_source, raw_file_name, raw_column, in_cur(Y/N), cur_table_name, "
                "cur_column, cur_datatype, PII/PCI."
            ),
        ),
        (
            [{"op": "filter", "sheet": "Values"}],
            (
                "step 1 (filter on 'Values'): 'Values' is not a mapping sheet; only rename_sheet and "
                "move_sheet apply to it."
            ),
        ),
        (
            [{"op": "rename_sheet", "sheet": "dd", "name": "values"}],
            (
                "step 1 (rename_sheet on 'dd'): the Data Dictionary workbook already has a sheet "
                "'Values'."
            ),
        ),
        (
            [
                {"op": "remove_columns", "sheet": "dd", "columns": ["Comments"]},
                {"op": "hide_columns", "sheet": "dd", "columns": ["comments"]},
            ],
            (
                "step 2 (hide_columns on 'dd'): column 'comments' was left out by an earlier step; "
                "remove that step to bring it back."
            ),
        ),
        (
            [{"op": "remove_columns", "sheet": "r2c", "columns": list(_R2C_HEADERS)}],
            "step 1 (remove_columns on 'r2c'): that would leave the sheet with no columns.",
        ),
        (
            [{"op": "add_column", "sheet": "dd", "name": "comments"}],
            "step 1 (add_column on 'dd'): 'Data Dictionary' already has a column 'comments'.",
        ),
        (
            [{"op": "hide_columns", "sheet": "ASL Data Mapping", "columns": ["Reviewer Comments"]}],
            (
                "step 1 (hide_columns on 'ASL Data Mapping'): 'Reviewer Comments' could be Reviewer "
                "Comments, Reviewer Comments; name one of them."
            ),
        ),
    ]
    for steps, refusal in cases:
        assert _refusal(steps) == refusal, steps
    # A header color for every mapping sheet needs none to be named.
    assert _built([{"op": "header", "color": "blue"}]).notes == ()


# -- where the files are kept ---------------------------------------------------------


def _keep(
    store: Any, owner: str = "user:a", *, now: datetime = NOW, content: bytes = b"PK workbook"
) -> KeptFile:
    return asyncio.run(
        keep_file(
            store,
            file_id=workbook_file_id(new_file_id(), "sttm"),
            owner=owner,
            name="sttm-accounts-v1-sttm.xlsx",
            media_type=MEDIA_TYPE,
            content=content,
            now=now,
            thread_id="thread-1",
            session_id="SESSIONA",
            version=1,
        )
    )


def _get(store: Any, file_id: str, owner: str = "user:a", now: datetime = NOW) -> KeptFile | None:
    return asyncio.run(get_file(store, owner=owner, file_id=file_id, now=now))


class _Untouchable(InMemoryStore):
    def batch(self, ops: Any) -> Any:
        raise AssertionError("the store was read")

    async def abatch(self, ops: Any) -> Any:
        raise AssertionError("the store was read")


class _Down(InMemoryStore):
    async def abatch(self, ops: Any) -> Any:
        raise ConnectionError("the store is down")


class _Expiring(InMemoryStore):
    """A store that expires items itself, recording what it is asked."""

    supports_ttl = True

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[Any] = []

    async def abatch(self, ops: Any) -> Any:
        ops = list(ops)
        self.ops.extend(ops)
        return await super().abatch(ops)


def test_a_kept_file_is_its_owners_for_24_hours() -> None:
    store = InMemoryStore()
    kept = _keep(store)
    assert is_file_id(kept.id) and kept.expires_at == NOW + KEEP
    item = store.get((NAMESPACE, "user:a"), kept.id)
    assert item is not None and item.value == {
        "owner": "user:a",
        "thread_id": "thread-1",
        "session_id": "SESSIONA",
        "version": 1,
        "name": "sttm-accounts-v1-sttm.xlsx",
        "media_type": MEDIA_TYPE,
        "size": len(b"PK workbook"),
        "content": base64.b64encode(b"PK workbook").decode("ascii"),
        "created_at": "2026-11-03T12:00:00+00:00",
        "expires_at": "2026-11-04T12:00:00+00:00",
    }
    assert _get(store, kept.id) == kept
    assert _get(store, kept.id, now=NOW + KEEP - timedelta(seconds=1)) == kept
    assert _get(store, kept.id, now=NOW + KEEP) is None
    assert _get(store, kept.id, owner="user:b") is None
    # Under another owner's name, a file still names its own owner.
    store.put((NAMESPACE, "user:b"), kept.id, {**item.value})
    assert _get(store, kept.id, owner="user:b") is None
    assert _get(store, new_file_id()) is None


def test_file_ids_are_checked_before_the_store_is_read() -> None:
    file_id = new_file_id()
    assert is_file_id(file_id) and len(file_id) == 16
    # A build's files: its id and their workbook. Links given before carry the id alone.
    assert [workbook_file_id(file_id, workbook) for workbook in WORKBOOKS] == [
        f"{file_id}-data-dictionary",
        f"{file_id}-sttm",
    ]
    assert all(is_file_id(workbook_file_id(file_id, workbook)) for workbook in WORKBOOKS)
    for bad in (
        "",
        "abcdefghjkmnpqrs",
        "ABCDEFGHJKMNPQR",
        "ABCDEFGHJKMNPQRST",
        "ABCDEFGHJKMNPQR1",
        f"{file_id}-",
        f"{file_id}-STTM",
        f"{file_id}-data_dictionary",
        f"{file_id}-sttm-sttm",
        f"{file_id}-sttm.xlsx",
        "ABCDEFGHJKMNPQR-sttm",
    ):
        assert not is_file_id(bad), bad
    for letter in "ILO":
        assert not is_file_id("ABCDEFGHJKMNPQR" + letter)
    store = _Untouchable()
    assert _get(store, "../../etc/passwd") is None
    assert _get(store, file_id, owner="") is None


def test_a_store_that_expires_items_is_given_the_24_hours() -> None:
    store = _Expiring()
    kept = _keep(store)
    assert [type(op) for op in store.ops] == [PutOp]
    put = store.ops[0]
    assert (put.namespace, put.key, put.ttl, put.index) == (
        (NAMESPACE, "user:a"),
        kept.id,
        24 * 60.0,
        False,
    )
    assert not any(isinstance(op, SearchOp) for op in store.ops)


def test_a_store_that_cannot_expire_items_has_the_owners_expired_ones_deleted() -> None:
    store = InMemoryStore()
    mine, theirs = (NAMESPACE, "user:a"), (NAMESPACE, "user:b")
    store.put(mine, "OLD", {"owner": "user:a", "expires_at": timestamp(NOW - timedelta(seconds=1))})
    store.put(mine, "DUE", {"owner": "user:a", "expires_at": timestamp(NOW)})
    store.put(mine, "UNDATED", {"owner": "user:a"})
    store.put(mine, "LIVE", {"owner": "user:a", "expires_at": timestamp(NOW + timedelta(hours=1))})
    store.put(theirs, "OTHER", {"owner": "user:b", "expires_at": timestamp(NOW - KEEP)})
    kept = _keep(store)
    assert {item.key for item in store.search(mine)} == {"LIVE", kept.id}
    assert [item.key for item in store.search(theirs)] == ["OTHER"]


def test_a_failed_clean_up_keeps_the_file() -> None:
    class _NoSearch(InMemoryStore):
        async def asearch(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("search is down")

    store = _NoSearch()
    with _logged(files_mod.logger, logging.WARNING) as logged:
        kept = _keep(store)
    assert logged == ["Could not delete expired STTM files"]
    assert _get(store, kept.id) == kept


def test_a_file_without_readable_content_is_not_served() -> None:
    store = InMemoryStore()
    namespace, expires = (NAMESPACE, "user:a"), timestamp(NOW + KEEP)
    for content in ("not base64!", None):
        file_id = new_file_id()
        value = {"owner": "user:a", "expires_at": expires}
        if content is not None:
            value["content"] = content
        store.put(namespace, file_id, value)
        with _logged(files_mod.logger, logging.WARNING) as logged:
            assert _get(store, file_id) is None
        assert logged == [f"STTM file {file_id} has no readable content"]
    # A file without a name or a type still downloads, under its id.
    file_id = new_file_id()
    store.put(namespace, file_id, {"owner": "user:a", "expires_at": expires, "content": "UEs="})
    found = _get(store, file_id)
    assert found is not None
    assert (found.name, found.media_type, found.content) == (
        f"{file_id}.xlsx",
        "application/octet-stream",
        b"PK",
    )


def test_download_links_use_the_frontends_path() -> None:
    assert Settings.model_fields["sttm_download_base_url"].default == "/api/sttm/files"
    saved = files_mod.get_settings
    for base in ("/api/sttm/files", " https://chat.example.test/api/sttm/files/ "):
        files_mod.get_settings = lambda base=base: SimpleNamespace(sttm_download_base_url=base)
        try:
            link = download_url("QHXWKRTMBNPDAZGC")
        finally:
            files_mod.get_settings = saved
        assert link == f"{base.strip().rstrip('/')}/QHXWKRTMBNPDAZGC"


# -- the download route ---------------------------------------------------------------


def _request(identity: str | None = "user:a", *groups: str) -> SimpleNamespace:
    user = SimpleNamespace(groups=list(groups))
    if identity is not None:
        user.identity = identity
    return SimpleNamespace(user=user)


def _download(store: Any, file_id: str, request: Any) -> Any:
    router = create_sttm_files_router(store=lambda: store)
    assert [route.path for route in router.routes] == ["/sttm/files/{file_id}"]
    endpoint = next(route.endpoint for route in router.routes)
    return asyncio.run(endpoint(file_id=file_id, request=request))


def _answer(response: Any) -> tuple[int, bytes, str | None, str | None]:
    headers = response.headers
    return (
        response.status_code,
        response.body,
        headers.get("content-type"),
        headers.get("cache-control"),
    )


_NOT_FOUND = (404, NOT_FOUND.encode(), "text/plain; charset=utf-8", "no-store")


def test_the_owner_downloads_their_file() -> None:
    store = InMemoryStore()
    kept = _keep(store, now=datetime.now(UTC), content=b"PK\x03\x04 workbook")
    with _caller(_policy()):
        response = _download(store, kept.id, _request())
    assert _answer(response) == (200, b"PK\x03\x04 workbook", MEDIA_TYPE, "no-store")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        'attachment; filename="sttm-accounts-v1-sttm.xlsx"'
    )
    assert route_mod._attachment("sttm-a b.xlsx") == "attachment; filename*=utf-8''sttm-a%20b.xlsx"


def test_everyone_else_gets_the_same_not_found() -> None:
    store = InMemoryStore()
    now = datetime.now(UTC)
    kept = _keep(store, now=now)
    expired = new_file_id()
    store.put(
        (NAMESPACE, "user:a"),
        expired,
        {
            **store.get((NAMESPACE, "user:a"), kept.id).value,
            "expires_at": timestamp(now - timedelta(seconds=1)),
        },
    )
    no_user = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    cases = [
        (kept.id, _request("user:b")),
        (expired, _request()),
        (new_file_id(), _request()),
        ("../../etc/passwd", _request()),
        (kept.id, _request(None)),
        (kept.id, no_user),
    ]
    with _caller(_policy()), _logged(route_mod.logger) as logged:
        for file_id, request in cases:
            assert _answer(_download(store, file_id, request)) == _NOT_FOUND, file_id
    # A malformed id is not written to the log.
    assert "STTM download refused (no such file for this caller, or expired): bad id" in logged
    assert not any("passwd" in line for line in logged)

    # A caller the policy keeps from the sttm-agent, or from its workbook tool.
    member = _request("user:a", _TEAM_A)
    for blocked in (
        {"blocked_subagents": [_STTM]},
        {"blocked_tools": [TASK_TOOL_NAME]},
        {"blocked_tools": ["sttm_workbook"]},
    ):
        with _caller(_policy({_TEAM_A: blocked})):
            assert _answer(_download(store, kept.id, member)) == _NOT_FOUND, blocked
    with _caller(_policy(global_policy={"blocked_subagents": [_STTM]})):
        assert _answer(_download(store, kept.id, _request())) == _NOT_FOUND
    with _caller(_policy({_TEAM_A: {"blocked_tools": ["ai_search_tool"]}})):
        assert _download(store, kept.id, member).status_code == 200


def test_without_a_store_downloads_are_unavailable() -> None:
    with _caller(_policy()), _logged(route_mod.logger) as logged:
        response = _download(None, new_file_id(), _request())
        assert _answer(response) == (
            503,
            UNAVAILABLE.encode(),
            "text/plain; charset=utf-8",
            "no-store",
        )
        response = _download(_Down(), new_file_id(), _request())
        assert _answer(response)[:2] == (503, UNAVAILABLE.encode())
    assert logged == [
        "STTM download unavailable: this server has no store",
        "STTM download unavailable: the store could not be read",
    ]


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
