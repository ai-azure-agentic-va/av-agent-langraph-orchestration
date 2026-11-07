"""The Data Dictionary and STTM workbooks of a session, as .xlsx files.

:func:`build_workbooks` turns a session into two workbooks, the Data
Dictionary workbook and the STTM workbook (Raw-to-Curated and Curated-to-ASL),
each from the organization's template in :data:`~.rules.TEMPLATES_DIR`:

* the template's mapping sheets are rewritten with the session's rows, keeping
  their headers, cell styles, widths, freeze, filter and dropdowns;
* its form sheets (file details, document information and its change log) are
  filled in;
* its other sheets are kept as they are; a dropdown that lists another sheet's
  cells follows that sheet when a step renames it;
* an About sheet says where the values come from and what the fills mean.

A workbook whose template is missing, unreadable or without its mapping sheet
is laid out from the session's own columns instead, and its About sheet says
why.

Values come only from the session (:class:`~.session.Tables`); a template
column the session has no value for stays blank. The analyst shapes the rest
with layout steps (rename or move a sheet; order, hide, show, leave out, add,
size or wrap columns; freeze; filter; sort; highlight rows; header color;
dropdowns): :func:`normalize_steps` checks each step on its own, and
:func:`build_workbooks` applies them in order and refuses one that does not fit
the workbooks (:class:`StepError`) before anything is built. No step changes a
value.

Every value is written as text or a whole number, never as a formula.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from copy import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal, get_args

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE, Cell, MergedCell
from openpyxl.cell.rich_text import CellRichText
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formula.tokenizer import Token, Tokenizer, TokenizerError
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.styles.cell_style import StyleArray
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import (
    column_index_from_string,
    coordinate_from_string,
    quote_sheetname,
    range_boundaries,
)
from openpyxl.worksheet.datavalidation import DataValidation, DataValidationList
from openpyxl.worksheet.filters import AutoFilter
from openpyxl.worksheet.worksheet import Worksheet

from v1.core.tools.sttm.render import review_mode_text, status_text
from v1.core.tools.sttm.rules import TEMPLATES_DIR
from v1.core.tools.sttm.session import (
    CHECKS,
    COLUMN_INDEX,
    COLUMNS,
    DETAILS,
    REQUIRED_DETAILS,
    UNRESOLVED_LABELS,
    CheckResult,
    Row,
    Session,
    SessionError,
    Tables,
    primary_key_names,
    table_key,
    timestamp,
    validate,
    yes_no,
)
from v1.core.tools.sttm.spec import normalize_name

logger = logging.getLogger(__name__)

# The two workbooks, in the order they are built and offered.
WORKBOOKS: dict[str, str] = {
    "data_dictionary": "Data Dictionary workbook",
    "sttm": "STTM workbook",
}
# Each workbook's template in TEMPLATES_DIR.
TEMPLATE_FILES: dict[str, str] = {"data_dictionary": "data-dictionary.xlsx", "sttm": "sttm.xlsx"}
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# A workbook larger than this is refused, not stored.
MAX_BYTES = 5 * 1024 * 1024
# The steps one layout may hold.
MAX_STEPS = 50
# Excel's limit on the characters in one cell.
MAX_CELL = 32_767

Op = Literal[
    "rename_sheet",
    "move_sheet",
    "order_columns",
    "hide_columns",
    "show_columns",
    "remove_columns",
    "add_column",
    "format_columns",
    "freeze",
    "filter",
    "sort",
    "highlight",
    "header",
    "dropdown",
]
Match = Literal["equals", "contains", "empty", "not_empty"]
MATCHES: tuple[str, ...] = get_args(Match)
# The color names a step may use, and the fill of each: a light tint, and a dark
# shade as "dark <name>".
COLORS: dict[str, str] = {
    "yellow": "#FFFF99",
    "orange": "#FFCC99",
    "red": "#FF9999",
    "green": "#C6EFCE",
    "blue": "#BDD7EE",
    "grey": "#D9D9D9",
    "gray": "#D9D9D9",
    "purple": "#D9C3E9",
    "dark yellow": "#7F6000",
    "dark orange": "#843C0C",
    "dark red": "#C00000",
    "dark green": "#375623",
    "dark blue": "#1F4E78",
    "dark grey": "#595959",
    "dark gray": "#595959",
    "dark purple": "#7030A0",
}
# The names as a step may write them: "Dark-Blue" and "darkblue" are dark blue.
_COLOR_NAMES = {name.replace(" ", ""): code for name, code in COLORS.items()}

# Op -> (the fields it needs, the fields it may take). Other fields are dropped.
_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "rename_sheet": (("sheet", "name"), ()),
    "move_sheet": (("sheet", "position"), ()),
    "order_columns": (("sheet", "columns"), ()),
    "hide_columns": (("sheet", "columns"), ()),
    "show_columns": (("sheet", "columns"), ()),
    "remove_columns": (("sheet", "columns"), ()),
    "add_column": (("sheet", "name"), ("position",)),
    "format_columns": (("sheet", "columns"), ("width", "wrap")),
    "freeze": (("sheet",), ("columns", "on")),
    "filter": (("sheet",), ("on",)),
    "sort": (("sheet", "columns"), ("descending",)),
    "highlight": (("sheet", "columns", "match", "color"), ("value",)),
    "header": (("color",), ("sheet",)),
    "dropdown": (("sheet", "columns", "choices"), ()),
}
# The order a stored step keeps its fields in.
_KEY_ORDER = (
    "op",
    "sheet",
    "columns",
    "name",
    "position",
    "width",
    "wrap",
    "on",
    "descending",
    "match",
    "value",
    "color",
    "choices",
)

# Fills of the cells the analyst has to look at, and of a generic header.
_DRAFT_FILL = "FFF2CC"
_UNRESOLVED_FILL = "FFC7CE"
_GENERIC_HEADER_FILL = "D9E1F2"
_MARK_FILLS = {"draft": _DRAFT_FILL, "unresolved": _UNRESOLVED_FILL}

# A list item's leading number ("5. Masked"), as the templates' dropdowns write it.
_NUMBERING = re.compile(r"^\s*\d+[.)]\s+(?=\S)")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_WHOLE_NUMBER = re.compile(r"^(0|[1-9][0-9]{0,14})$")
_HEX = re.compile(r"^#?([0-9a-f]{6})$")
_BAD_TITLE = re.compile(r"[\[\]:*?/\\]")
_NOT_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

_DELIMITER_TEXT = {
    "comma": ",",
    "pipe": "|",
    "semicolon": ";",
    "tab": "Tab",
    "fixed width": "None (fixed width)",
}


class StepError(SessionError):
    """A layout step that cannot be applied.

    ``number`` is its place among the steps given, and ``reason`` the message
    after "step <number>", for a caller that numbers the steps its own way.
    """

    def __init__(self, number: int, reason: str) -> None:
        super().__init__(f"step {number}{reason}")
        self.number = number
        self.reason = reason


class _Refused(Exception):
    """A step that does not fit the workbooks; the message says why."""


class _NoTemplate(Exception):
    """A template that cannot be used; the message follows "the template <file>"."""


# --- Results -----------------------------------------------------------------


@dataclass(frozen=True)
class SheetInfo:
    title: str
    kind: Literal["table", "fixed"]
    # Mapping sheets: every column header in order, and the hidden ones.
    columns: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuiltWorkbook:
    key: str
    label: str
    name: str
    content: bytes = field(repr=False)
    sheets: tuple[SheetInfo, ...] = ()
    # From the organization's template; when not, why the generic layout was used.
    template: bool = False
    reason: str | None = None
    # Cells filled as the agent's drafts, and as not decided.
    drafts: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class Built:
    workbooks: tuple[BuiltWorkbook, ...]
    # What the analyst should know about the steps, e.g. a highlight that matched no rows.
    notes: tuple[str, ...] = ()


# --- Layout steps ------------------------------------------------------------


def _check_text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("must be text.")
    text = " ".join(value.split())
    if not text:
        raise ValueError("is empty.")
    return text


def _check_value(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("must be text.")
    return _check_text(str(value))


def _check_columns(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("name at least one column.")
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = _check_text(item)
        key = normalize_name(name)
        if not key:
            raise ValueError(f"{name!r} is not a column name.")
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _check_position(value: Any) -> int:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be a whole number from 1.")
    return value


def _check_width(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("must be a number.")
    if not 4 <= value <= 100:
        raise ValueError("must be from 4 to 100.")
    return int(value) if float(value).is_integer() else round(float(value), 1)


def _check_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("must be true or false.")
    return value


def _check_match(value: Any) -> str:
    if value not in MATCHES:
        raise ValueError(f"use one of: {', '.join(MATCHES)}.")
    return value


def _check_color(value: Any) -> str:
    text = value.strip().casefold() if isinstance(value, str) else ""
    code = _COLOR_NAMES.get(re.sub(r"[\s_-]", "", text))
    if code:
        return code
    hex_value = _HEX.match(text)
    if hex_value:
        return "#" + hex_value.group(1).upper()
    light = [name for name in COLORS if not name.startswith("dark ")]
    raise ValueError(
        f"use one of {', '.join(light)}, each also as dark <color>, or a hex color such as #FFCC99."
    )


def _check_choices(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise TypeError("must be a list.")
    choices: list[str] = []
    seen: set[str] = set()
    for item in value:
        choice = _check_value(item)
        if "," in choice or '"' in choice:
            raise ValueError(f"{choice!r}: a choice cannot hold a comma or a double quote.")
        if _plain(choice) not in seen:
            seen.add(_plain(choice))
            choices.append(choice)
    if len(",".join(choices)) > 255:
        raise ValueError("over 255 characters together, which Excel does not allow.")
    return choices


_CHECKS: dict[str, Callable[[Any], Any]] = {
    "sheet": _check_text,
    "columns": _check_columns,
    "name": _check_text,
    "position": _check_position,
    "width": _check_width,
    "wrap": _check_bool,
    "on": _check_bool,
    "descending": _check_bool,
    "match": _check_match,
    "value": _check_value,
    "color": _check_color,
    "choices": _check_choices,
}


def _title_problem(name: str) -> str | None:
    """Why ``name`` cannot be a sheet name, or None."""

    if len(name) > 31:
        return "a sheet name has at most 31 characters."
    if _BAD_TITLE.search(name):
        return "a sheet name cannot hold [ ] : * ? / or \\."
    if name.startswith("'") or name.endswith("'"):
        return "a sheet name cannot start or end with an apostrophe."
    if name.casefold() == "history":
        return "Excel keeps the sheet name History for itself."
    if not normalize_name(name):
        return "a sheet name needs a letter or a digit."
    return None


def _normalize_step(number: int, item: Any) -> dict[str, Any]:
    def refused(text: str) -> StepError:
        return StepError(number, f": {text}")

    if not isinstance(item, Mapping):
        raise refused("not a step.")
    op = item.get("op")
    if op not in _FIELDS:
        raise refused(f"unknown op {op!r}; use one of: {', '.join(_FIELDS)}.")
    needed, optional = _FIELDS[op]
    missing = [name for name in needed if item.get(name) is None]
    if missing:
        raise refused(f"{op} needs {' and '.join(missing)}.")
    step: dict[str, Any] = {"op": op}
    for name in (*needed, *optional):
        if item.get(name) is not None:
            try:
                step[name] = _CHECKS[name](item[name])
            except (TypeError, ValueError) as error:
                raise refused(f"{name} {error}") from None
    if op == "rename_sheet":
        problem = _title_problem(step["name"])
        if problem:
            raise refused(problem)
    elif op == "add_column":
        if len(step["name"]) > 100:
            raise refused("a column name has at most 100 characters.")
        if not normalize_name(step["name"]):
            raise refused("a column name needs a letter or a digit.")
    elif op == "format_columns":
        if "width" not in step and "wrap" not in step:
            raise refused("format_columns needs a width, wrap or both.")
    elif op == "freeze":
        step.setdefault("on", True)
        if not step["on"] and "columns" in step:
            raise refused("on=false unfreezes the sheet and takes no columns.")
    elif op == "filter":
        step.setdefault("on", True)
    elif op == "sort":
        step.setdefault("descending", False)
    elif op == "highlight":
        if len(step["columns"]) != 1:
            raise refused("highlight looks at exactly one column.")
        if step["match"] in ("equals", "contains"):
            if "value" not in step:
                raise refused(f"match={step['match']} needs a value.")
        else:
            step.pop("value", None)
    return {key: step[key] for key in _KEY_ORDER if key in step}


def normalize_steps(raw: Iterable[Any], *, first: int = 1) -> list[dict[str, Any]]:
    """``raw`` steps checked on their own and in their stored form, numbered from ``first``.

    Raises :class:`StepError` for the first step that is wrong. Whether its
    sheet and columns exist is checked by :func:`build_workbooks`.
    """

    items = list(raw)
    total = first - 1 + len(items)
    if total > MAX_STEPS:
        raise StepError(
            MAX_STEPS + 1,
            f": a layout holds at most {MAX_STEPS} steps, and this one would have {total}.",
        )
    return [_normalize_step(number, item) for number, item in enumerate(items, start=first)]


def _color_text(code: str) -> str:
    return next((name for name, value in COLORS.items() if value == code), code)


def describe_step(step: Mapping[str, Any]) -> str:
    """A stored step in a few words."""

    op = step["op"]
    sheet = step.get("sheet")
    names = list(step.get("columns", ()))
    columns = ", ".join(names)
    if op == "rename_sheet":
        return f"rename sheet {sheet} to {step['name']}"
    if op == "move_sheet":
        return f"move sheet {sheet} to position {step['position']}"
    if op == "order_columns":
        return f"{sheet}: put {columns} first"
    if op == "hide_columns":
        return f"{sheet}: hide {columns}"
    if op == "show_columns":
        return f"{sheet}: show {columns}"
    if op == "remove_columns":
        return f"{sheet}: leave out {columns}"
    if op == "add_column":
        where = f"at position {step['position']}" if "position" in step else "at the end"
        return f"{sheet}: add an empty column {step['name']} {where}"
    if op == "format_columns":
        parts = []
        if "width" in step:
            parts.append(f"width {step['width']}")
        if "wrap" in step:
            parts.append("wrap text" if step["wrap"] else "no text wrap")
        return f"{sheet}: {' and '.join(parts)} for {columns}"
    if op == "freeze":
        if not step.get("on", True):
            return f"{sheet}: unfreeze"
        if not names:
            return f"{sheet}: freeze the header row"
        through = names[0] if len(names) == 1 else f"the rightmost of {columns}"
        return f"{sheet}: freeze the header row and the columns through {through}"
    if op == "filter":
        return f"{sheet}: filter {'on' if step.get('on', True) else 'off'}"
    if op == "sort":
        return f"{sheet}: sort by {columns}" + (" (descending)" if step.get("descending") else "")
    if op == "highlight":
        condition = {
            "equals": f"is '{step.get('value')}'",
            "contains": f"contains '{step.get('value')}'",
            "empty": "is empty",
            "not_empty": "is not empty",
        }[step["match"]]
        return (
            f"{sheet}: highlight rows where {columns} {condition} in {_color_text(step['color'])}"
        )
    if op == "header":
        if sheet:
            return f"{sheet}: header color {_color_text(step['color'])}"
        return f"header color {_color_text(step['color'])} on every mapping sheet"
    if step["choices"]:
        return f"{sheet}: dropdown on {columns}: {', '.join(step['choices'])}"
    return f"{sheet}: no dropdown on {columns}"


# --- The model of a workbook -------------------------------------------------


@dataclass(frozen=True)
class _Cell:
    value: str | int | None
    mark: Literal["draft", "unresolved"] | None = None


_BLANK = _Cell(None)


@dataclass(frozen=True)
class _Dropdown:
    """A list dropdown, as a template has it."""

    formula: str
    # The choices the formula lists ("a,b") or the range it names holds; None when
    # they cannot be read from the workbook.
    choices: tuple[str, ...] | None
    allow_blank: bool = True
    show_error: bool = True
    show_input: bool = False
    # Excel's showDropDown, which hides the in-cell arrow when set.
    hide_arrow: bool = False
    error_style: str | None = None
    error: str | None = None
    error_title: str | None = None
    prompt: str | None = None
    prompt_title: str | None = None


_YES_NO = _Dropdown('"Y,N"', ("Y", "N"))


@dataclass(eq=False)
class _Column:
    header: str
    # Other names a step may use for it: the session column it shows.
    aliases: frozenset[str] = frozenset()
    # Row index (in the session's row order) -> cell.
    cells: dict[int, _Cell] = field(default_factory=dict)
    width: float | None = None
    # None: as the template has it.
    wrap: bool | None = None
    hidden: bool = False
    # A step's choices: () removes the dropdown; None leaves the template's (base).
    dropdown: tuple[str, ...] | None = None
    base: _Dropdown | None = None
    added: bool = False
    header_style: StyleArray = field(default_factory=StyleArray)
    data_style: StyleArray = field(default_factory=StyleArray)


@dataclass(eq=False)
class _Sheet:
    book: _Book = field(repr=False)
    original: str
    title: str
    ws: Worksheet = field(repr=False)
    aliases: frozenset[str] = frozenset()
    # The session table of a mapping sheet; None for every other sheet.
    table: str | None = None
    columns: list[_Column] = field(default_factory=list)
    removed: list[_Column] = field(default_factory=list)
    # Row indexes in the order they are written.
    order: list[int] = field(default_factory=list)
    frozen: bool = False
    # The rightmost frozen column; None freezes the header row only.
    freeze_after: _Column | None = None
    filter: bool = False
    # Row index -> highlight fill.
    highlights: dict[int, str] = field(default_factory=dict)
    header_fill: str | None = None
    header_height: float | None = None


@dataclass(eq=False)
class _Book:
    key: str
    label: str
    wb: Workbook = field(repr=False)
    # The template file, or None for the generic layout and the reason for it.
    template: str | None = None
    reason: str | None = None
    sheets: list[_Sheet] = field(default_factory=list)
    about: _Sheet | None = None


@dataclass
class _Context:
    session: Session
    tables: Tables
    results: list[CheckResult]
    data_source: str
    quarter: str | None
    # Normalized primary key attribute names.
    pk: set[str]
    # Normalized attribute name -> its place in the specification.
    positions: dict[str, int]
    # Rows that have a Curated-to-ASL row.
    asl_keys: set[str]
    # Normalized PII/PCI handling method -> the template's description of it.
    handling: dict[str, str] = field(default_factory=dict)


def _plain(text: str) -> str:
    return " ".join(text.casefold().split())


def _snap(value: str, choices: Iterable[str]) -> str:
    """``value`` spelled as the dropdown choice it matches (case, spacing, numbering)."""

    plain = _plain(value)
    for choice in choices:
        if _plain(choice) == plain:
            return choice
    if not _NUMBERING.match(value):
        for choice in choices:
            if _plain(_NUMBERING.sub("", choice)) == plain:
                return choice
    return value


def _data_source_name(session: Session) -> str:
    system, source = session.source_system.strip(), session.source.strip()
    if not system or not source or system.casefold() == source.casefold():
        return system or source
    return f"{system}-{source}"


def _quarter(created_at: str) -> str | None:
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    return f"Q{(created.month - 1) // 3 + 1} {created.year}"


def _context(session: Session) -> _Context:
    tables = Tables(session)
    positions: dict[str, int] = {}
    for number, attribute in enumerate(session.attributes, start=1):
        positions.setdefault(normalize_name(attribute.name), number)
    return _Context(
        session=session,
        tables=tables,
        results=validate(session),
        data_source=_data_source_name(session),
        quarter=_quarter(session.created_at),
        pk={normalize_name(name) for name in primary_key_names(session)},
        positions=positions,
        asl_keys={row.key for row in tables.rows("curated_to_asl")},
    )


# --- What each template column holds -----------------------------------------

CellValue = Callable[[_Context, Row, int], _Cell]


@dataclass(frozen=True)
class _Field:
    header: str
    value: CellValue
    # The session column it shows, (table, column): its key and label name it too.
    alias: tuple[str, str] | None = None


def _session_cell(
    table: str, column: str, transform: Callable[[str], str | int | None] | None = None
) -> CellValue:
    """A session cell, marked when it is the agent's draft or not decided."""

    spec = COLUMN_INDEX[table][column]

    def value(ctx: _Context, row: Row, number: int) -> _Cell:
        resolved = ctx.tables.resolve(table, row, column)
        if resolved.value is None:
            if spec.required or column in UNRESOLVED_LABELS:
                return _Cell(UNRESOLVED_LABELS.get(column, "?"), "unresolved")
            return _BLANK
        text = transform(resolved.value) if transform else resolved.value
        if text is None:
            return _BLANK
        return _Cell(text, "draft" if resolved.source == "draft" else None)

    return value


def _yes_no_words(yes: str, no: str, *, other: bool) -> Callable[[str], str | None]:
    """Y and N as ``yes`` and ``no``; anything else as it is (``other``) or blank."""

    def transform(value: str) -> str | None:
        answer = yes_no(value)
        if answer is not None:
            return yes if answer == "Y" else no
        return value if other else None

    return transform


def _whole_number(value: str) -> str | int:
    text = value.strip()
    return int(text) if _WHOLE_NUMBER.match(text) else value


def _detail_cell(key: str) -> CellValue:
    def value(ctx: _Context, row: Row, number: int) -> _Cell:
        return _detail(ctx.session, key)

    return value


def _detail(session: Session, key: str, transform: Callable[[str], str] | None = None) -> _Cell:
    """A file detail; a required one that is not given shows as not decided."""

    detail = session.details.get(key)
    if detail is None:
        return _Cell("?", "unresolved") if key in REQUIRED_DETAILS else _BLANK
    text = transform(detail.value) if transform else detail.value
    return _Cell(text, "draft" if detail.source == "draft" else None)


def _pk(yes: str, no: str) -> CellValue:
    def value(ctx: _Context, row: Row, number: int) -> _Cell:
        detail = ctx.session.details.get("primary_key")
        if detail is None:
            return _BLANK
        return _Cell(
            yes if row.key in ctx.pk else no, "draft" if detail.source == "draft" else None
        )

    return value


def _spec_rows_only(value: CellValue) -> CellValue:
    def only(ctx: _Context, row: Row, number: int) -> _Cell:
        return value(ctx, row, number) if row.origin == "specification" else _BLANK

    return only


def _asl_rows_only(value: CellValue) -> CellValue:
    def only(ctx: _Context, row: Row, number: int) -> _Cell:
        return value(ctx, row, number) if row.key in ctx.asl_keys else _BLANK

    return only


def _row_number(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell(number)


def _attribute_position(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell(ctx.positions.get(row.key))


def _data_source(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell(ctx.data_source or None)


def _source_system(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell(ctx.session.source_system or None)


def _target_system(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell("ASL")


def _quarter_cell(ctx: _Context, row: Row, number: int) -> _Cell:
    return _Cell(ctx.quarter)


def _handling_description(ctx: _Context, row: Row, number: int) -> _Cell:
    method = ctx.tables.resolve("data_dictionary", row, "handling_method").value
    if not method:
        return _BLANK
    return _Cell(ctx.handling.get(_plain(_NUMBERING.sub("", method))))


def _comments(ctx: _Context, row: Row, number: int) -> _Cell:
    parts = []
    if row.derived is not None:
        parts.append(f"Derived: {row.derived.logic}" if row.derived.logic else "Derived field")
        if row.derived.join_key:
            parts.append(f"Join key: {row.derived.join_key}")
    if row.audit is not None:
        parts.append(f"Audit field from {row.audit.rule_file}")
    if ctx.tables.resolve("data_dictionary", row, "carry_to_curated").value == "N":
        parts.append("Not carried to Curated")
    text = " ".join(part.rstrip().rstrip(".") + "." for part in parts)
    return _Cell(text or None)


_MANDATORY = _yes_no_words("Mandatory", "Optional", other=True)
_NULL_SCENARIO = _yes_no_words("No", "Yes", other=False)
_DQ_RULE = _yes_no_words("Not Null", "Null", other=False)
_YES_OR_NO = _yes_no_words("Yes", "No", other=True)

_DD_FIELDS = (
    _Field("Sequence_Id", _row_number),
    _Field("Attribute_Sequence_Id", _spec_rows_only(_attribute_position)),
    _Field("Data_Source_Name", _data_source),
    _Field("File_Name", _detail_cell("file_pattern")),
    _Field("DD_Quarter_Year", _quarter_cell),
    _Field(
        "Physical_Attribute_Name",
        _session_cell("data_dictionary", "attribute_name"),
        ("data_dictionary", "attribute_name"),
    ),
    _Field(
        "Business_Attribute_Name",
        _asl_rows_only(_session_cell("curated_to_asl", "business_attribute_name")),
    ),
    _Field(
        "Attribute_Description",
        _session_cell("data_dictionary", "technical_description"),
        ("data_dictionary", "technical_description"),
    ),
    _Field("Business_Key_Identifier", _pk("Yes", "No"), ("data_dictionary", "primary_key")),
    _Field("Primary_Foreign_Composite_Key", _pk("Primary Key", "Not Applicable")),
    _Field(
        "Mandatory_Optional",
        _session_cell("data_dictionary", "required", _MANDATORY),
        ("data_dictionary", "required"),
    ),
    _Field("Is_Null_Scenario", _session_cell("data_dictionary", "required", _NULL_SCENARIO)),
    _Field(
        "Source_Attribute_DataType",
        _session_cell("data_dictionary", "data_type"),
        ("data_dictionary", "data_type"),
    ),
    _Field("Length", _session_cell("data_dictionary", "length", _whole_number)),
    _Field("Data_Quality_Rules", _session_cell("data_dictionary", "required", _DQ_RULE)),
    _Field(
        "Classification_Level",
        _session_cell("data_dictionary", "classification"),
        ("data_dictionary", "classification"),
    ),
    _Field("PII_PCI", _session_cell("data_dictionary", "pii_pci", _YES_OR_NO)),
    _Field(
        "PII_PCI_Handling_Method",
        _session_cell("data_dictionary", "handling_method"),
        ("data_dictionary", "handling_method"),
    ),
    _Field("PII_PCI_Handling_Description", _handling_description),
    _Field("Comments", _comments),
    _Field("Raw_File_Name", _spec_rows_only(_session_cell("raw_to_curated", "raw_file"))),
    _Field(
        "Raw_Attribute_Name", _spec_rows_only(_session_cell("data_dictionary", "attribute_name"))
    ),
)


def _r2c(header: str, column: str) -> _Field:
    return _Field(header, _session_cell("raw_to_curated", column), ("raw_to_curated", column))


_R2C_FIELDS = (
    _Field("id", _row_number),
    _Field("data_source", _data_source),
    _Field("main_source", _data_source),
    _Field("source_system", _source_system),
    _r2c("raw_path", "raw_path"),
    _r2c("raw_file_name", "raw_file"),
    _Field(
        "raw_column",
        _spec_rows_only(_session_cell("raw_to_curated", "attribute_name")),
        ("raw_to_curated", "attribute_name"),
    ),
    _r2c("int_path", "intg_path"),
    _r2c("int_table_name", "intg_table"),
    _r2c("int_column", "intg_column"),
    _r2c("int_datatype", "intg_type"),
    _Field(
        "in_cur(Y/N)",
        _session_cell("data_dictionary", "carry_to_curated"),
        ("data_dictionary", "carry_to_curated"),
    ),
    _r2c("cur_path", "curated_path"),
    _r2c("cur_table_name", "curated_table"),
    _r2c("cur_column", "curated_column"),
    _r2c("cur_datatype", "curated_type"),
    _r2c("cur_logic", "transformation_rule"),
    _Field(
        "PII/PCI",
        _session_cell("raw_to_curated", "pii_pci", _YES_OR_NO),
        ("raw_to_curated", "pii_pci"),
    ),
    _Field("DQ Rules", _session_cell("data_dictionary", "required", _DQ_RULE)),
    _Field(
        "Attribute Definition",
        _session_cell("data_dictionary", "business_description"),
        ("data_dictionary", "business_description"),
    ),
)


def _c2a(header: str, column: str) -> _Field:
    return _Field(header, _session_cell("curated_to_asl", column), ("curated_to_asl", column))


_ASL_FIELDS = (
    _Field("ID", _row_number),
    _Field("Target Original System", _source_system),
    _Field("Target System", _target_system),
    _c2a("Target Table Name", "asl_table"),
    _c2a("Target Attribute Name", "business_attribute_name"),
    _Field(
        "Target Attribute Definition",
        _session_cell("data_dictionary", "business_description"),
        ("data_dictionary", "business_description"),
    ),
    _c2a("Target Column", "asl_column"),
    _c2a("Target Datatype", "asl_type"),
    _c2a("Target Constraints", "constraints"),
    _Field("Source System", _data_source),
    _c2a("Source File/Table", "curated_table"),
    _c2a("Source Column", "curated_column"),
    _Field(
        "Source Datatype",
        _session_cell("raw_to_curated", "curated_type"),
        ("raw_to_curated", "curated_type"),
    ),
    _c2a("Transformation Logic Detail", "logic"),
    _Field(
        "Lookup Join",
        _session_cell("data_dictionary", "join_key"),
        ("data_dictionary", "join_key"),
    ),
)


@dataclass(frozen=True)
class _TableSheet:
    template_title: str
    generic_title: str
    fields: tuple[_Field, ...]


_TABLE_SHEETS: dict[str, _TableSheet] = {
    "data_dictionary": _TableSheet("Data Dictionary", "Data Dictionary", _DD_FIELDS),
    "raw_to_curated": _TableSheet("Data Mapping (RAW-CUR)", "Raw-to-Curated", _R2C_FIELDS),
    "curated_to_asl": _TableSheet("ASL Data Mapping", "Curated-to-ASL", _ASL_FIELDS),
}
# The session tables each workbook holds, in sheet order.
_BOOK_TABLES: dict[str, tuple[str, ...]] = {
    "data_dictionary": ("data_dictionary",),
    "sttm": ("raw_to_curated", "curated_to_asl"),
}


def _table_aliases(table: str) -> frozenset[str]:
    sheet = _TABLE_SHEETS[table]
    return frozenset({normalize_name(sheet.template_title), normalize_name(sheet.generic_title)})


# --- Opening a workbook ------------------------------------------------------


def _text(value: Any) -> str | None:
    """A cell's text, plain or rich; None for a cell that holds no text."""

    if isinstance(value, str):
        return value
    if isinstance(value, CellRichText):
        return str(value)
    return None


def _split_reference(reference: str) -> tuple[str | None, str]:
    """A reference's sheet title (None for the formula's own sheet) and its cells."""

    title, bang, cells = reference.rpartition("!")
    if not bang:
        return None, reference
    if len(title) > 1 and title.startswith("'") and title.endswith("'"):
        title = title[1:-1].replace("''", "'")
    return title, cells


def _list_choices(ws: Worksheet, formula: str) -> tuple[str, ...] | None:
    """A list dropdown's choices: those the formula lists ("a,b"), or the values in
    the one range it names, on ``ws`` or another sheet of its workbook."""

    text = formula.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return tuple(part.strip() for part in text[1:-1].split(",") if part.strip())
    try:
        tokens = Tokenizer(f"={text}").items
    except TokenizerError:
        return None
    if len(tokens) != 1 or tokens[0].subtype != Token.RANGE:
        return None
    title, cells = _split_reference(tokens[0].value)
    source = (
        ws
        if title is None
        else next(
            (sheet for sheet in ws.parent.worksheets if sheet.title.casefold() == title.casefold()),
            None,
        )
    )
    if source is None:
        return None
    try:
        min_col, min_row, max_col, max_row = range_boundaries(cells)
    except ValueError:
        # A defined name.
        return None
    choices: list[str] = []
    for row in source.iter_rows(
        min_row=min_row or 1,
        max_row=min(max_row or source.max_row, source.max_row),
        min_col=min_col or 1,
        max_col=min(max_col or source.max_column, source.max_column),
        values_only=True,
    ):
        choices.extend(str(value) for value in row if value is not None and str(value).strip())
    return tuple(choices)


def _repoint(formula: str, titles: Mapping[str, str]) -> str:
    """``formula`` with its references to renamed sheets under their new titles.

    ``titles``: a renamed sheet's original title, casefolded -> its title now.
    """

    if not titles or "!" not in formula:
        return formula
    try:
        tokenizer = Tokenizer(f"={formula}")
    except TokenizerError:
        return formula
    changed = False
    for token in tokenizer.items:
        if token.type != Token.OPERAND or token.subtype != Token.RANGE:
            continue
        title, cells = _split_reference(token.value)
        if title is not None and title.casefold() in titles:
            token.value = f"{quote_sheetname(titles[title.casefold()])}!{cells}"
            changed = True
    return tokenizer.render()[1:] if changed else formula


def _list_dropdowns(ws: Worksheet) -> dict[int, _Dropdown]:
    """Column number -> the list dropdown on its first data row."""

    found: dict[int, _Dropdown] = {}
    for validation in ws.data_validations.dataValidation:
        if validation.type != "list" or not validation.formula1:
            continue
        dropdown = _Dropdown(
            formula=validation.formula1,
            choices=_list_choices(ws, validation.formula1),
            allow_blank=bool(validation.allow_blank),
            show_error=bool(validation.showErrorMessage),
            show_input=bool(validation.showInputMessage),
            hide_arrow=bool(validation.showDropDown),
            error_style=validation.errorStyle,
            error=validation.error,
            error_title=validation.errorTitle,
            prompt=validation.prompt,
            prompt_title=validation.promptTitle,
        )
        for cells in validation.sqref.ranges:
            if cells.min_row <= 2 <= cells.max_row:
                for number in range(cells.min_col, cells.max_col + 1):
                    found.setdefault(number, dropdown)
    return found


def _dimensions(ws: Worksheet, last: int) -> tuple[dict[int, float], set[int]]:
    """Column number -> width, and the hidden column numbers, up to column ``last``."""

    widths: dict[int, float] = {}
    hidden: set[int] = set()
    for key, dimension in ws.column_dimensions.items():
        first = dimension.min or column_index_from_string(key)
        for number in range(first, min(dimension.max or first, last) + 1):
            if dimension.width:
                widths[number] = dimension.width
            if dimension.hidden:
                hidden.add(number)
    return widths, hidden


def _template_freeze(ws: Worksheet, columns: list[_Column]) -> tuple[bool, _Column | None]:
    if not ws.freeze_panes:
        return False, None
    letter, row = coordinate_from_string(ws.freeze_panes)
    number = column_index_from_string(letter)
    if number <= 1:
        return row > 1, None
    return True, columns[min(number - 2, len(columns) - 1)]


def _template_table(book: _Book, ws: Worksheet, table: str, ctx: _Context) -> _Sheet:
    fields = {normalize_name(entry.header): entry for entry in _TABLE_SHEETS[table].fields}
    headers = [ws.cell(1, number).value for number in range(1, ws.max_column + 1)]
    while headers and headers[-1] in (None, ""):
        headers.pop()
    if not any(normalize_name(str(header)) in fields for header in headers if header is not None):
        raise _NoTemplate(f"has none of the expected columns on its '{ws.title}' sheet")
    widths, hidden = _dimensions(ws, len(headers))
    dropdowns = _list_dropdowns(ws)
    rows = ctx.tables.rows(table)
    columns: list[_Column] = []
    for number, raw in enumerate(headers, start=1):
        header = "" if raw is None else str(raw)
        entry = fields.get(normalize_name(header)) if header else None
        base = dropdowns.get(number)
        cells: dict[int, _Cell] = {}
        if entry is not None:
            for index, row in enumerate(rows):
                cell = entry.value(ctx, row, index + 1)
                if (
                    base
                    and base.choices
                    and cell.mark != "unresolved"
                    and isinstance(cell.value, str)
                ):
                    cell = _Cell(_snap(cell.value, base.choices), cell.mark)
                if cell != _BLANK:
                    cells[index] = cell
        aliases: frozenset[str] = frozenset()
        if entry is not None and entry.alias is not None:
            spec = COLUMN_INDEX[entry.alias[0]][entry.alias[1]]
            aliases = frozenset({normalize_name(spec.key), normalize_name(spec.label)})
        columns.append(
            _Column(
                header=header,
                aliases=aliases,
                cells=cells,
                width=widths.get(number),
                hidden=number in hidden,
                base=base,
                header_style=copy(ws.cell(1, number)._style),
                data_style=copy(ws.cell(2, number)._style),
            )
        )
    frozen, after = _template_freeze(ws, columns)
    return _Sheet(
        book=book,
        original=ws.title,
        title=ws.title,
        ws=ws,
        aliases=_table_aliases(table),
        table=table,
        columns=columns,
        order=list(range(len(rows))),
        frozen=frozen,
        freeze_after=after,
        filter=bool(ws.auto_filter.ref),
        header_height=ws.row_dimensions[1].height if 1 in ws.row_dimensions else None,
    )


def _handling_descriptions(wb: Workbook) -> dict[str, str]:
    """Each PII/PCI handling method's description, from the template's list of them."""

    sheets = sorted(wb.worksheets, key=lambda ws: normalize_name(ws.title) != "values")
    for ws in sheets:
        for row in ws.iter_rows():
            for cell in row:
                if normalize_name(_text(cell.value) or "") != "piipcihandlingmethodvalues":
                    continue
                head = _text(ws.cell(cell.row, cell.column + 1).value)
                if normalize_name(head or "") != "piipcihandlingmethoddescription":
                    continue
                found: dict[str, str] = {}
                number = cell.row + 1
                while True:
                    method = _text(ws.cell(number, cell.column).value)
                    if not (method and method.strip()):
                        return found
                    description = _text(ws.cell(number, cell.column + 1).value)
                    if description and description.strip():
                        found[_plain(_NUMBERING.sub("", method))] = description.strip()
                    number += 1
    return {}


def _about(book: _Book) -> _Sheet:
    """An empty About sheet at the end of the workbook, filled in when it is written."""

    taken = {normalize_name(title) for title in book.wb.sheetnames}
    title, number = "About", 1
    while normalize_name(title) in taken:
        number += 1
        title = f"About {number}"
    sheet = _Sheet(book, title, title, book.wb.create_sheet(title), aliases=frozenset({"about"}))
    book.about = sheet
    return sheet


def _from_template(key: str, ctx: _Context, path: Path, wb: Workbook) -> _Book:
    book = _Book(key, WORKBOOKS[key], wb, template=path.name)
    tables: dict[str, str] = {}
    for table in _BOOK_TABLES[key]:
        title = _TABLE_SHEETS[table].template_title
        ws = next(
            (ws for ws in wb.worksheets if normalize_name(ws.title) == normalize_name(title)), None
        )
        if ws is None:
            raise _NoTemplate(f"has no '{title}' sheet")
        tables[ws.title] = table
    if key == "data_dictionary":
        ctx.handling = _handling_descriptions(wb)
    for ws in wb.worksheets:
        table = tables.get(ws.title)
        if table is not None:
            book.sheets.append(_template_table(book, ws, table, ctx))
        else:
            book.sheets.append(_Sheet(book, ws.title, ws.title, ws))
    book.sheets.append(_about(book))
    return book


def _generic_table(book: _Book, ws: Worksheet, table: str, ctx: _Context) -> _Sheet:
    rows = ctx.tables.rows(table)
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor=_GENERIC_HEADER_FILL)
    columns: list[_Column] = []
    for number, spec in enumerate(COLUMNS[table], start=1):
        value = _session_cell(table, spec.key)
        cells: dict[int, _Cell] = {}
        for index, row in enumerate(rows):
            cell = value(ctx, row, index + 1)
            if cell != _BLANK:
                cells[index] = cell
        texts = [str(cell.value) for cell in cells.values() if cell.value is not None]
        head = ws.cell(1, number)
        head.font = bold
        head.fill = fill
        columns.append(
            _Column(
                header=spec.label,
                aliases=frozenset({normalize_name(spec.key), normalize_name(spec.label)}),
                cells=cells,
                width=min(max(max(map(len, [spec.label, *texts])) + 2, 8), 50),
                wrap=True if any(len(text) > 50 for text in texts) else None,
                base=_YES_NO if spec.yes_no else None,
                header_style=copy(head._style),
            )
        )
    return _Sheet(
        book=book,
        original=ws.title,
        title=ws.title,
        ws=ws,
        aliases=_table_aliases(table),
        table=table,
        columns=columns,
        order=list(range(len(rows))),
        frozen=True,
        filter=True,
    )


def _generic(key: str, ctx: _Context, reason: str) -> _Book:
    """The workbook laid out from the session's own columns."""

    wb = Workbook()
    book = _Book(key, WORKBOOKS[key], wb, reason=reason)
    for number, table in enumerate(_BOOK_TABLES[key]):
        ws = wb.active if number == 0 else wb.create_sheet()
        ws.title = _TABLE_SHEETS[table].generic_title
        book.sheets.append(_generic_table(book, ws, table, ctx))
    book.sheets.append(_about(book))
    return book


def _open(key: str, ctx: _Context, folder: Path) -> _Book:
    path = folder / TEMPLATE_FILES[key]
    if not path.is_file():
        return _generic(key, ctx, f"the template {path.name} is not in this deployment")
    try:
        # rich_text keeps text that mixes fonts in one cell as the template has it.
        wb = load_workbook(path, rich_text=True)
    except Exception:
        logger.exception("STTM template %s could not be read", path.name)
        return _generic(key, ctx, f"the template {path.name} could not be read")
    try:
        return _from_template(key, ctx, path, wb)
    except _NoTemplate as problem:
        return _generic(key, ctx, f"the template {path.name} {problem}")


# --- Applying the steps ------------------------------------------------------


def _shown(header: str) -> str:
    return " ".join(header.split()) or "(blank)"


def _find_sheets(books: list[_Book], name: str) -> list[_Sheet]:
    """The sheets ``name`` refers to: by title, then original title, then alias."""

    target = normalize_name(name)
    table = table_key(name)
    sheets = [sheet for book in books for sheet in book.sheets]
    levels: tuple[Callable[[_Sheet], bool], ...] = (
        lambda sheet: normalize_name(sheet.title) == target,
        lambda sheet: normalize_name(sheet.original) == target,
        lambda sheet: target in sheet.aliases or (table is not None and sheet.table == table),
    )
    for matches in levels:
        found = [sheet for sheet in sheets if matches(sheet)]
        if found:
            return found
    listing = "; ".join(
        f"{book.label}: {', '.join(sheet.title.strip() for sheet in book.sheets)}" for book in books
    )
    raise _Refused(f"there is no sheet '{name}'. Sheets: {listing}.")


def _column(sheet: _Sheet, name: str) -> _Column:
    target = normalize_name(name)
    found = [column for column in sheet.columns if normalize_name(column.header) == target]
    if not found:
        found = [column for column in sheet.columns if target in column.aliases]
    if len(found) == 1:
        return found[0]
    if found:
        options = ", ".join(_shown(column.header) for column in found)
        raise _Refused(f"'{name}' could be {options}; name one of them.")
    if any(
        normalize_name(column.header) == target or target in column.aliases
        for column in sheet.removed
    ):
        raise _Refused(
            f"column '{name}' was left out by an earlier step; remove that step to bring it back."
        )
    listing = ", ".join(_shown(column.header) for column in sheet.columns)
    raise _Refused(f"'{sheet.title}' has no column '{name}'. Its columns: {listing}.")


def _columns(sheet: _Sheet, names: Iterable[str]) -> list[_Column]:
    found: list[_Column] = []
    for name in names:
        column = _column(sheet, name)
        if column not in found:
            found.append(column)
    return found


def _empty(cell: _Cell) -> bool:
    if cell.mark == "unresolved" or cell.value is None:
        return True
    return isinstance(cell.value, str) and not cell.value.strip()


def _sort_key(value: str | int | None) -> tuple[int, float | str]:
    if isinstance(value, int):
        return 0, float(value)
    text = str(value).strip()
    if _NUMBER.match(text):
        return 0, float(text)
    return 1, text.casefold()


def _matches(cell: _Cell, match: str, value: str) -> bool:
    if match == "empty":
        return _empty(cell)
    if match == "not_empty":
        return not _empty(cell)
    if cell.value is None:
        return False
    text = str(cell.value)
    if match == "contains":
        return _plain(value) in _plain(text)
    if _plain(text) == _plain(value):
        return True
    if not _NUMBERING.match(value) and _plain(_NUMBERING.sub("", text)) == _plain(value):
        return True
    if text.isalpha() and value.isalpha():
        answer = yes_no(text)
        return answer is not None and answer == yes_no(value)
    return False


def _order_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    named = _columns(sheet, step["columns"])
    sheet.columns = named + [column for column in sheet.columns if column not in named]


def _visibility_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    for column in _columns(sheet, step["columns"]):
        column.hidden = step["op"] == "hide_columns"


def _remove_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    named = _columns(sheet, step["columns"])
    kept = [column for column in sheet.columns if column not in named]
    if not kept:
        raise _Refused("that would leave the sheet with no columns.")
    sheet.columns = kept
    sheet.removed.extend(named)


def _add_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    name = step["name"]
    if any(normalize_name(column.header) == normalize_name(name) for column in sheet.columns):
        raise _Refused(f"'{sheet.title}' already has a column '{name}'.")
    index = min(step.get("position", len(sheet.columns) + 1), len(sheet.columns) + 1) - 1
    # Styled like its left neighbour, or its right one when it comes first.
    neighbour = sheet.columns[index - 1] if index else sheet.columns[0]
    column = _Column(
        header=name,
        width=max(12, min(len(name) + 4, 40)),
        added=True,
        header_style=copy(neighbour.header_style),
        data_style=copy(neighbour.data_style),
    )
    sheet.columns.insert(index, column)


def _format_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    for column in _columns(sheet, step["columns"]):
        if "width" in step:
            column.width = step["width"]
        if "wrap" in step:
            column.wrap = step["wrap"]


def _freeze_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    if not step["on"]:
        sheet.frozen, sheet.freeze_after = False, None
        return
    named = _columns(sheet, step.get("columns", ()))
    sheet.frozen = True
    sheet.freeze_after = max(named, key=sheet.columns.index) if named else None


def _filter_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    sheet.filter = step["on"]


def _sort_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    # One stable pass per column, the last first, so the first named one leads.
    # Blank and undecided cells go last either way.
    for column in reversed(_columns(sheet, step["columns"])):
        cells = column.cells
        filled = [index for index in sheet.order if not _empty(cells.get(index, _BLANK))]
        blank = [index for index in sheet.order if _empty(cells.get(index, _BLANK))]
        filled.sort(key=lambda index: _sort_key(cells[index].value), reverse=step["descending"])
        sheet.order = filled + blank


def _highlight_step(sheet: _Sheet, step: Mapping[str, Any]) -> int:
    column = _column(sheet, step["columns"][0])
    matched = 0
    for index in sheet.order:
        if _matches(column.cells.get(index, _BLANK), step["match"], step.get("value", "")):
            sheet.highlights[index] = step["color"]
            matched += 1
    return matched


def _header_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    sheet.header_fill = step["color"]


def _dropdown_step(sheet: _Sheet, step: Mapping[str, Any]) -> None:
    for column in _columns(sheet, step["columns"]):
        column.dropdown = tuple(step["choices"])


_TABLE_STEPS: dict[str, Callable[[_Sheet, Mapping[str, Any]], int | None]] = {
    "order_columns": _order_step,
    "hide_columns": _visibility_step,
    "show_columns": _visibility_step,
    "remove_columns": _remove_step,
    "add_column": _add_step,
    "format_columns": _format_step,
    "freeze": _freeze_step,
    "filter": _filter_step,
    "sort": _sort_step,
    "highlight": _highlight_step,
    "header": _header_step,
    "dropdown": _dropdown_step,
}


def _rename(sheet: _Sheet, name: str) -> None:
    clash = next(
        (
            other
            for other in sheet.book.sheets
            if other is not sheet and normalize_name(other.title) == normalize_name(name)
        ),
        None,
    )
    if clash is not None:
        raise _Refused(f"the {sheet.book.label} already has a sheet '{clash.title}'.")
    sheet.title = name


def _apply(books: list[_Book], step: Mapping[str, Any]) -> int | None:
    """Apply one step; returns the rows a highlight matched, None for other steps."""

    op = step["op"]
    if "sheet" not in step:
        # A header color with no sheet named: every mapping sheet.
        for book in books:
            for sheet in book.sheets:
                if sheet.table is not None:
                    sheet.header_fill = step["color"]
        return None
    sheets = _find_sheets(books, step["sheet"])
    if op == "rename_sheet":
        for sheet in sheets:
            _rename(sheet, step["name"])
        return None
    if op == "move_sheet":
        for sheet in sheets:
            order = sheet.book.sheets
            order.remove(sheet)
            order.insert(min(step["position"], len(order) + 1) - 1, sheet)
        return None
    for sheet in sheets:
        if sheet.table is None:
            raise _Refused(
                f"'{sheet.title}' is not a mapping sheet; only rename_sheet and move_sheet "
                "apply to it."
            )
    matched = None
    for sheet in sheets:
        count = _TABLE_STEPS[op](sheet, step)
        if count is not None:
            matched = (matched or 0) + count
    return matched


# --- Writing a workbook ------------------------------------------------------


def _write(cell: Cell, value: str | int | None) -> None:
    """``value`` as a number or as text, never as a formula or an error code."""

    if value is None:
        cell.value = None
    elif isinstance(value, int) and not isinstance(value, bool):
        cell.value = value
    else:
        cell.value = ILLEGAL_CHARACTERS_RE.sub("", str(value))[:MAX_CELL]
        # Set after the value: openpyxl takes "=..." as a formula and "#N/A" as an error.
        cell.data_type = "s"


@cache
def _fill(code: str) -> PatternFill:
    return PatternFill("solid", fgColor=code.lstrip("#"))


def _text_color(code: str) -> str:
    """Black or white, whichever has the higher contrast on a fill of ``code`` (WCAG)."""

    def linear(digits: str) -> float:
        channel = int(digits, 16) / 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    digits = code.lstrip("#")
    luminance = (
        0.2126 * linear(digits[0:2]) + 0.7152 * linear(digits[2:4]) + 0.0722 * linear(digits[4:6])
    )
    return "FF000000" if luminance > 0.179 else "FFFFFFFF"


def _paint(cell: Cell, code: str) -> None:
    """Fill a cell, its text black or white to stay readable on the fill: a template's
    white header text would vanish on a light fill, its black text on a dark one."""

    cell.fill = _fill(code)
    font = copy(cell.font)
    font.color = _text_color(code)
    cell.font = font


def _dropdown(column: _Column) -> _Dropdown | None:
    if column.dropdown is not None:
        if not column.dropdown:
            return None
        return _Dropdown('"' + ",".join(column.dropdown) + '"', column.dropdown)
    return column.base


def _write_table(sheet: _Sheet, titles: Mapping[str, str]) -> tuple[int, int]:
    """Rewrite a mapping sheet from the model; returns the draft and undecided cells.

    ``titles``: the renamed sheets' new titles, which its dropdowns follow (:func:`_repoint`).
    """

    ws = sheet.ws
    # Unmerged first: openpyxl cannot unmerge a range whose cells were deleted.
    for merged in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(merged))
    ws.delete_rows(1, ws.max_row)
    ws.column_dimensions.clear()
    ws.row_dimensions.clear()
    ws.data_validations = DataValidationList()
    ws.conditional_formatting = ConditionalFormattingList()
    ws.auto_filter = AutoFilter()
    ws.freeze_panes = None
    ws.sheet_properties.filterMode = False
    if sheet.header_height:
        ws.row_dimensions[1].height = sheet.header_height
    last_row = 1 + len(sheet.order)
    counts = {"draft": 0, "unresolved": 0}
    for number, column in enumerate(sheet.columns, start=1):
        letter = get_column_letter(number)
        head = ws.cell(1, number)
        head._style = copy(column.header_style)
        _write(head, column.header or None)
        if sheet.header_fill:
            _paint(head, sheet.header_fill)
        for row_number, index in enumerate(sheet.order, start=2):
            cell = ws.cell(row_number, number)
            cell._style = copy(column.data_style)
            value = column.cells.get(index, _BLANK)
            _write(cell, value.value)
            # A highlight never hides that a cell is a draft or undecided.
            code = _MARK_FILLS[value.mark] if value.mark else sheet.highlights.get(index)
            if code:
                _paint(cell, code)
            if value.mark:
                counts[value.mark] += 1
            if column.wrap is not None:
                alignment = copy(cell.alignment)
                alignment.wrap_text = column.wrap
                cell.alignment = alignment
        dimension = ws.column_dimensions[letter]
        if column.width is not None:
            dimension.width = column.width
        if column.hidden:
            dimension.hidden = True
        dropdown = _dropdown(column)
        if dropdown is not None:
            validation = DataValidation(
                type="list",
                formula1=_repoint(dropdown.formula, titles),
                allow_blank=dropdown.allow_blank,
                showErrorMessage=dropdown.show_error,
                showInputMessage=dropdown.show_input,
                showDropDown=dropdown.hide_arrow,
                errorStyle=dropdown.error_style,
                error=dropdown.error,
                errorTitle=dropdown.error_title,
                prompt=dropdown.prompt,
                promptTitle=dropdown.prompt_title,
            )
            validation.add(f"{letter}2:{letter}{last_row}" if last_row > 2 else f"{letter}2")
            ws.add_data_validation(validation)
    if sheet.frozen:
        after = sheet.freeze_after
        if after is not None and after in sheet.columns:
            ws.freeze_panes = f"{get_column_letter(sheet.columns.index(after) + 2)}2"
        else:
            ws.freeze_panes = "A2"
    if sheet.filter:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(sheet.columns))}{last_row}"
    return counts["draft"], counts["unresolved"]


def _file_details_values(ctx: _Context) -> dict[str, _Cell]:
    """The file-details form: normalized label -> value."""

    session = ctx.session
    attributes = sum(
        1 for row in ctx.tables.rows("data_dictionary") if row.origin == "specification"
    )
    return {
        "datasourcename": _Cell(ctx.data_source or None),
        "sourcesystemname": _Cell(session.source_system or None),
        "cmdbnumber": _detail(session, "cmdb_number"),
        "filename": _detail(session, "file_pattern"),
        "numberofattributes": _Cell(attributes or None),
        "dataformat": _detail(
            session,
            "delimiter",
            lambda value: "Fixed width" if value == "fixed width" else "Delimited",
        ),
        "isheaderpresent": _detail(
            session, "header_rows", lambda value: "N" if value.strip() == "0" else "Y"
        ),
        "delimiters": _detail(
            session, "delimiter", lambda value: _DELIMITER_TEXT.get(value, value)
        ),
        "frequency": _detail(session, "frequency"),
        "datetimeoffileexpectation": _detail(session, "expected_arrival"),
    }


def _file_details_cells(ws: Worksheet, ctx: _Context) -> Iterator[tuple[Cell, _Cell]]:
    """Labels in column C, answers in column D."""

    values = _file_details_values(ctx)
    for label, answer in ws.iter_rows(min_col=3, max_col=4):
        text = _text(label.value)
        if text and not isinstance(answer, MergedCell):
            value = values.get(normalize_name(text))
            if value is not None:
                yield answer, value


# A change log's columns, by normalized header: what the session's revision row holds.
_CHANGE_LOG = {
    "version": "version",
    "date": "date",
    "author": "author",
    "approvedby": "author",
    "comment": "comment",
    "comments": "comment",
    "notes": "comment",
    "changedescription": "comment",
    "changedescriptionnotes": "comment",
}


def _document_info_cells(ws: Worksheet, ctx: _Context) -> Iterator[tuple[Cell, _Cell]]:
    """Data source and source system: labels in column A, answers in column D. The
    session's revision goes in the row under the change log's headers, which start at
    "Version"; a header it has no value for stays blank."""

    session = ctx.session
    approval = session.approval
    comment = "Approved" if approval is not None else "Draft, not approved"
    if session.supersedes:
        comment += f"; replaces approved session {session.supersedes}"
    revision = {
        "version": f"{session.version}.0",
        "date": (approval.at if approval is not None else session.updated_at)[:10],
        "author": approval.by if approval is not None else None,
        "comment": comment,
    }
    logged = False
    for row in list(ws.iter_rows()):
        for label in row:
            key = normalize_name(_text(label.value) or "")
            if label.column == 1 and key == "datasource":
                yield ws.cell(label.row, 4), _Cell(ctx.data_source or None)
            elif label.column == 1 and key == "sourcesystem":
                yield ws.cell(label.row, 4), _Cell(session.source_system or None)
            elif key == "version" and not logged:
                logged = True
                for head in row[label.column - 1 :]:
                    text = _text(head.value)
                    if not (text and text.strip()):
                        break
                    name = _CHANGE_LOG.get(normalize_name(text))
                    if name is not None:
                        yield ws.cell(label.row + 1, head.column), _Cell(revision[name])


# The form sheets, by the start of their normalized title ("Document Info_Start Here").
_FORMS: dict[str, Callable[[Worksheet, _Context], Iterator[tuple[Cell, _Cell]]]] = {
    "filedetails": _file_details_cells,
    "documentinfo": _document_info_cells,
}


def _form(title: str) -> Callable[[Worksheet, _Context], Iterator[tuple[Cell, _Cell]]] | None:
    name = normalize_name(title)
    return next((cells for start, cells in _FORMS.items() if name.startswith(start)), None)


def _fill_form(pairs: Iterable[tuple[Cell, _Cell]]) -> tuple[int, int]:
    drafts = unresolved = 0
    for target, value in pairs:
        if isinstance(target, MergedCell):
            continue
        _write(target, value.value)
        # The template's own fill stays unless the analyst has to look at the cell.
        if value.mark:
            target.fill = _fill(_MARK_FILLS[value.mark])
            drafts += value.mark == "draft"
            unresolved += value.mark == "unresolved"
    return drafts, unresolved


def _detail_source(session: Session, key: str) -> str:
    detail = session.details.get(key)
    if detail is None:
        return "not given"
    if detail.source == "rule":
        return f"rule file {detail.ref}" if detail.ref else "a rule file"
    return {
        "specification": "the specification",
        "analyst": "the analyst",
        "draft": "the agent's draft",
    }.get(detail.source, detail.source)


def _write_about(sheet: _Sheet, ctx: _Context, now: datetime) -> None:
    ws, book, session = sheet.ws, sheet.book, ctx.session
    bold = Font(bold=True)
    top = Alignment(vertical="top")
    wrapped = Alignment(vertical="top", wrap_text=True)

    def put(
        row: int,
        column: int,
        value: str | int | None,
        *,
        heading: bool = False,
        fill: str | None = None,
    ) -> None:
        cell = ws.cell(row, column)
        _write(cell, value)
        cell.alignment = wrapped if column == 2 else top
        if heading:
            cell.font = bold
        if fill:
            cell.fill = _fill(fill)

    failing = [CHECKS[result.check] for result in ctx.results if not result.passed]
    checks = f"{len(ctx.results) - len(failing)} of {len(ctx.results)} checks pass"
    if failing:
        checks += ". Failing: " + "; ".join(failing)
    version = str(session.version)
    if session.supersedes:
        version += f", replaces approved session {session.supersedes}"
    approval = session.approval
    if approval is None:
        approved = "Not approved"
    else:
        approved = f"Approved by {approval.by}" if approval.by else "Approved"
        approved += f" at {approval.at}"
        if approval.override_reason:
            failed = len(approval.failed_checks)
            noun = "check" if failed == 1 else "checks"
            approved += (
                f", overriding {failed} failed {noun} with the analyst's reason "
                f'"{approval.override_reason}"'
            )
    if book.template:
        layout = f"The organization's template, {book.template}"
    else:
        layout = f"Generic, from the session's own columns: {book.reason}"
    facts = (
        ("Session", session.id),
        ("Version", version),
        ("Source system", session.source_system),
        ("Source", session.source),
        ("Status", status_text(session)),
        ("Review mode", review_mode_text(session)),
        ("Approval", approved),
        ("Validation", checks),
        ("Built", timestamp(now)),
        ("Layout", layout),
    )
    row = 0
    for label, value in facts:
        row += 1
        put(row, 1, label, heading=True)
        put(row, 2, value)

    row += 2
    put(row, 1, "Cell fills", heading=True)
    legend = (
        ("(any value)", _DRAFT_FILL, "The agent's draft, for the analyst to confirm."),
        ("?", _UNRESOLVED_FILL, "Not decided yet."),
        (
            UNRESOLVED_LABELS["pii_pci"],
            _UNRESOLVED_FILL,
            "PII/PCI not decided yet: treat the attribute as sensitive.",
        ),
    )
    for sample, code, meaning in legend:
        row += 1
        put(row, 1, sample, fill=code)
        put(row, 2, meaning)

    row += 2
    for column, heading in enumerate(("File detail", "Value", "Source"), start=1):
        put(row, column, heading, heading=True)
    for key, label in DETAILS.items():
        row += 1
        value = _detail(session, key)
        put(row, 1, label)
        put(row, 2, value.value, fill=_MARK_FILLS.get(value.mark) if value.mark else None)
        put(row, 3, _detail_source(session, key))

    notes = [
        (
            "The values come from the STTM session: to change one, ask the agent. Edits made in "
            "this file are not read back."
        )
    ]
    if any(sheet.table in ("raw_to_curated", "curated_to_asl") for sheet in book.sheets):
        notes.append("The STTM sheets list the attributes carried to Curated.")
    if book.template:
        notes.append("Template columns the session has no value for are left blank.")
        if any(
            column.base is not None and column.base.choices
            for table_sheet in book.sheets
            for column in table_sheet.columns
        ):
            notes.append(
                "A value that matches a dropdown choice, apart from case and numbering, is "
                "written as that choice."
            )
    row += 2
    put(row, 1, "Notes", heading=True)
    for note in notes:
        put(row, 2, note)
        row += 1
    for letter, width in (("A", 28), ("B", 80), ("C", 16)):
        ws.column_dimensions[letter].width = width


def _arrange(book: _Book) -> None:
    """Titles, sheet order and the active sheet as the steps left them."""

    wb = book.wb
    changed = [sheet for sheet in book.sheets if sheet.ws.title != sheet.title]
    # Through unused names first, so that swapping two titles never meets a clash.
    taken = {title.casefold() for title in wb.sheetnames}
    taken |= {sheet.title.casefold() for sheet in book.sheets}
    number = 0
    for sheet in changed:
        temporary = ""
        while not temporary or temporary.casefold() in taken:
            number += 1
            temporary = f"Sheet {number}"
        taken.add(temporary.casefold())
        sheet.ws.title = temporary
    for sheet in changed:
        sheet.ws.title = sheet.title
    for position, sheet in enumerate(book.sheets):
        wb.move_sheet(sheet.ws, offset=position - wb.sheetnames.index(sheet.ws.title))
    visible = [sheet for sheet in book.sheets if sheet.ws.sheet_state == "visible"]
    active = next((sheet for sheet in visible if sheet.table is not None), visible[0]).ws
    wb.active = active
    for ws in wb.worksheets:
        ws.sheet_view.tabSelected = ws is active
    # The tab bar starts at the first sheet (a template may have it scrolled).
    for view in wb.views:
        view.firstSheet = 0


def _render(book: _Book, ctx: _Context, now: datetime) -> BuiltWorkbook:
    session = ctx.session
    titles = {
        sheet.original.casefold(): sheet.title
        for sheet in book.sheets
        if sheet.title != sheet.original
    }
    drafts = unresolved = 0
    for sheet in book.sheets:
        if sheet.table is not None:
            counts = _write_table(sheet, titles)
        else:
            for validation in sheet.ws.data_validations.dataValidation:
                if validation.formula1:
                    validation.formula1 = _repoint(validation.formula1, titles)
                if validation.formula2:
                    validation.formula2 = _repoint(validation.formula2, titles)
            form = _form(sheet.original) if book.template else None
            if form is None:
                continue
            counts = _fill_form(form(sheet.ws, ctx))
        drafts += counts[0]
        unresolved += counts[1]
    if book.about is not None:
        _write_about(book.about, ctx, now)
    _arrange(book)
    stamp = (now.astimezone(UTC) if now.tzinfo else now).replace(tzinfo=None)
    properties = book.wb.properties
    properties.creator = None
    properties.lastModifiedBy = None
    properties.title = f"{book.label}: {session.source_system} / {session.source}"
    # openpyxl sets the modified time when it saves.
    properties.created = stamp
    buffer = io.BytesIO()
    book.wb.save(buffer)
    content = buffer.getvalue()
    if len(content) > MAX_BYTES:
        raise SessionError(
            f"The {book.label} would be {len(content) / 1_048_576:.1f} MB, over the "
            f"{MAX_BYTES // 1_048_576} MB limit; nothing was stored."
        )
    sheets = tuple(
        SheetInfo(
            sheet.title,
            "table",
            tuple(_shown(column.header) for column in sheet.columns),
            tuple(_shown(column.header) for column in sheet.columns if column.hidden),
        )
        if sheet.table is not None
        else SheetInfo(sheet.title, "fixed")
        for sheet in book.sheets
    )
    return BuiltWorkbook(
        key=book.key,
        label=book.label,
        name=file_name(session, book.key),
        content=content,
        sheets=sheets,
        template=book.template is not None,
        reason=book.reason,
        drafts=drafts,
        unresolved=unresolved,
    )


def file_name(session: Session, key: str) -> str:
    """A workbook's download name: source, session id, version and which workbook."""

    slug = re.sub(r"-{2,}", "-", _NOT_SLUG.sub("-", session.source)).strip("-.")
    slug = slug[:40].strip("-.") or "source"
    suffix = "data-dictionary" if key == "data_dictionary" else "sttm"
    return f"sttm-{slug}-{session.id}-v{session.version}-{suffix}.xlsx"


def build_workbooks(
    session: Session,
    steps: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    templates_dir: Path | None = None,
) -> Built:
    """Both workbooks of ``session``, with the layout ``steps`` applied in order.

    Raises :class:`StepError` for a step that does not fit the workbooks and
    :class:`~.session.SessionError` for a workbook over :data:`MAX_BYTES`.
    """

    checked = normalize_steps(steps)
    ctx = _context(session)
    folder = TEMPLATES_DIR if templates_dir is None else templates_dir
    books = [_open(key, ctx, folder) for key in WORKBOOKS]
    notes: list[str] = []
    for number, step in enumerate(checked, start=1):
        try:
            matched = _apply(books, step)
        except _Refused as refused:
            where = f"{step['op']} on '{step['sheet']}'" if "sheet" in step else step["op"]
            raise StepError(number, f" ({where}): {refused}") from None
        if matched == 0:
            notes.append(f"Step {number} ({describe_step(step)}) matched no rows.")
    return Built(tuple(_render(book, ctx, now) for book in books), tuple(notes))


__all__ = [
    "COLORS",
    "MATCHES",
    "MAX_BYTES",
    "MAX_STEPS",
    "MEDIA_TYPE",
    "TEMPLATE_FILES",
    "WORKBOOKS",
    "Built",
    "BuiltWorkbook",
    "Match",
    "Op",
    "SheetInfo",
    "StepError",
    "build_workbooks",
    "describe_step",
    "file_name",
    "normalize_steps",
]
