"""Read the attribute table out of a pasted source specification.

Specifications arrive as web pages, documents, spreadsheets and notes. What an
analyst pastes from them is a table with a header row: Markdown
(pipe-separated), copied cells (tab-separated, as an attached PDF's tables are
read too) or HTML (a web page's source, MDX). Every table in the text whose
header names an attribute column is read, in order; anything else in the text
(instructions, notes) is left alone, and so is a later table that only gives
values for attributes an earlier one named. Names are kept exactly as
written. The other columns are recognized by common header names and
normalized: a ``nullable``/``optional`` column becomes a required flag, a type
such as ``VARCHAR(50)`` is split into type and length when the table has no
length column, and a key column marks primary keys.

Nothing is guessed: an empty cell stays empty, a column the parser does not
recognize is reported, not mapped, and a table that lost its structure when it
was copied (its columns separated by spaces, or its rows run into one line) is
reported, not read.
"""

from __future__ import annotations

import html
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import accumulate

# Header names (normalized: lowercase, punctuation as spaces) for each role, in
# order of preference when a table has more than one candidate column.
ROLE_HEADERS: dict[str, tuple[str, ...]] = {
    "name": (
        "attribute name",
        "attribute",
        "field name",
        "field",
        "column name",
        "column",
        "source attribute",
        "source field",
        "source column",
        "physical name",
        "physical column name",
        "element name",
        "element",
        "property name",
        "property",
        "name",
    ),
    "data_type": (
        "data type",
        "datatype",
        "type",
        "field type",
        "column type",
        "attribute type",
        "data format",
        "format",
    ),
    "length": (
        "length precision",
        "length",
        "precision",
        "size",
        "max length",
        "maximum length",
        "width",
        "len",
    ),
    "required": (
        "required",
        "mandatory",
        "required flag",
        "is required",
        "nullable",
        "is nullable",
        "null allowed",
        "optional",
        "null",
    ),
    "description": (
        "description",
        "attribute description",
        "field description",
        "column description",
        "definition",
        "desc",
        "comments",
        "comment",
        "notes",
        "meaning",
    ),
    "key": ("primary key", "pk", "key", "is key", "is pk", "key type"),
}
# A "nullable" or "optional" column says the opposite of "required".
_INVERTED_HEADERS = frozenset({"nullable", "is nullable", "null allowed", "optional", "null"})
# Headers of a derived-field table, which is not a specification.
_DERIVATION_HEADERS = frozenset(
    {
        "logic",
        "derivation",
        "derivation logic",
        "formula",
        "calculation",
        "transformation",
        "transformation logic",
        "transformation rule",
        "join key",
    }
)

# Required-flag cells. Words say the same thing in any column; yes/no answers
# the column's own question, so a "nullable" column inverts them.
_REQUIRED_WORDS = frozenset({"required", "mandatory", "not null", "not nullable", "m", "r"})
_OPTIONAL_WORDS = frozenset({"optional", "nullable", "null", "not required", "not mandatory", "o"})
_TRUE = frozenset({"y", "yes", "true", "1", "x"})
_FALSE = frozenset({"n", "no", "false", "0"})
_PRIMARY = frozenset({"pk", "y", "yes", "true", "1", "x", "p", "primary", "primary key"})

_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_TYPE_WITH_SIZE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ ]*?)\s*\(\s*([0-9][0-9 ,]*)\s*\)\s*$")
_MARKUP = re.compile(r"^(\*\*|__|`)(.+)\1$")
# What a header says in brackets ("Data Type (length)") is read only when the
# rest of it names no column.
_BRACKETED = re.compile(r"\([^()]*\)")
# A line copied from the chat starts with "You:", after the sender's initials.
_CHAT_PREFIX = re.compile(r"^\s*(?:[A-Z]{1,3}\s+)?You:\s*")
_HTML_TABLE = re.compile(r"<table\b[^>]*>(.*?)</table\s*>", re.IGNORECASE | re.DOTALL)
_HTML_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
_HTML_CELL = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>")
# Header names a line has to start with to be a flattened table's header.
_FLATTENED_HEADERS = 3


def normalize_name(name: str) -> str:
    """How attribute names are compared: case, spaces and punctuation ignored."""

    return re.sub(r"[^0-9a-z]+", "", name.casefold())


def _normalize_header(header: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", header.casefold()).strip()


def _header_forms(header: str) -> tuple[str, str]:
    """A header cell as written, and without what it says in brackets."""

    return _normalize_header(header), _normalize_header(_BRACKETED.sub(" ", header))


@dataclass(frozen=True)
class ParsedAttribute:
    """One row of the specification, as normalized."""

    name: str
    line: int
    data_type: str | None = None
    length: str | None = None
    required: str | None = None
    description: str | None = None
    key: str | None = None


@dataclass
class SpecParse:
    """What :func:`parse_specification` found."""

    attributes: list[ParsedAttribute] = field(default_factory=list)
    # One line per table read: where it starts, its row count and column mapping.
    tables: list[str] = field(default_factory=list)
    # Tables skipped, rows skipped, cells not read.
    notes: list[str] = field(default_factory=list)
    # Lines of rows in an attribute table that were not read (no attribute name).
    skipped_lines: list[int] = field(default_factory=list)
    # Every line of the attribute tables read, header and separators included.
    table_lines: set[int] = field(default_factory=set)
    # The first and last line of each attribute table, read or skipped for naming
    # only earlier tables' attributes; each can be read alone (``table=``).
    table_spans: list[tuple[int, int]] = field(default_factory=list)
    # Lines that look like a table that lost its structure when it was copied.
    flattened: list[int] = field(default_factory=list)

    def duplicates(self) -> dict[str, list[ParsedAttribute]]:
        """Attributes sharing a normalized name, keyed by the first one's name."""

        return duplicate_names(self.attributes)


def duplicate_names(attributes: list[ParsedAttribute] | list) -> dict[str, list]:
    """Items with a ``name`` that share a normalized name, keyed by the first one's name."""

    seen: dict[str, list] = {}
    for attribute in attributes:
        seen.setdefault(normalize_name(attribute.name), []).append(attribute)
    return {group[0].name: group for group in seen.values() if len(group) > 1}


@dataclass
class _Row:
    line: int
    cells: list[str]


@dataclass
class _Block:
    """A table: its rows (header first, separators dropped) and the lines it spans."""

    rows: list[_Row]
    first: int
    last: int


def _cells(line: str) -> tuple[str, list[str]] | None:
    """(delimiter, cells) for a table line, or None for any other line."""

    line = _CHAT_PREFIX.sub("", line.rstrip("\r\n"))
    if "\t" in line:
        return "\t", [cell.strip() for cell in line.split("\t")]
    stripped = line.strip()
    if "|" in stripped:
        text = stripped.replace("\\|", "\x00")
        text = text.removeprefix("|").removesuffix("|")
        cells = [cell.replace("\x00", "|").strip() for cell in text.split("|")]
        if len(cells) >= 2:
            return "|", cells
    return None


def _html_cell(cell: str) -> str:
    return " ".join(html.unescape(_HTML_TAG.sub(" ", cell)).replace("`", "").split())


def _html_blocks(text: str) -> tuple[list[_Block], set[int]]:
    """The HTML tables in ``text`` (a web page's source, MDX), and the lines they span."""

    # Where each line starts, for the line a match is on.
    starts = list(accumulate(len(line) for line in text.splitlines(keepends=True)))

    def line_of(index: int) -> int:
        return bisect_right(starts, index) + 1

    blocks: list[_Block] = []
    spanned: set[int] = set()
    for table in _HTML_TABLE.finditer(text):
        first, last = line_of(table.start()), line_of(table.end() - 1)
        spanned.update(range(first, last + 1))
        rows = []
        for row in _HTML_ROW.finditer(text, table.start(1), table.end(1)):
            cells = [_html_cell(cell.group(1)) for cell in _HTML_CELL.finditer(row.group(1))]
            if any(cells):
                rows.append(_Row(line_of(row.start()), cells))
        if len(rows) >= 2:
            blocks.append(_Block(rows, first, last))
    return blocks, spanned


def _blocks(text: str) -> list[_Block]:
    """The tables in ``text``, in order.

    A text table is a run of consecutive table lines that share a delimiter,
    separator rows dropped.
    """

    blocks, spanned = _html_blocks(text)
    runs: list[list[_Row]] = []
    current: list[_Row] = []
    delimiter = None
    for number, line in enumerate(text.splitlines(), start=1):
        parsed = None if number in spanned else _cells(line)
        if parsed is None or parsed[0] != delimiter:
            if current:
                runs.append(current)
            current, delimiter = [], None
            if parsed is None:
                continue
            delimiter = parsed[0]
        cells = parsed[1]
        if all(not cell or _SEPARATOR_CELL.match(cell) for cell in cells):
            continue
        current.append(_Row(number, cells))
    if current:
        runs.append(current)
    # A lone line with a pipe or a tab in it is prose, not a table.
    blocks += [_Block(rows, rows[0].line, rows[-1].line) for rows in runs if len(rows) >= 2]
    return sorted(blocks, key=lambda block: block.first)


def _roles(header: list[str]) -> dict[str, int]:
    forms = [_header_forms(cell) for cell in header]
    roles: dict[str, int] = {}
    for role, names in ROLE_HEADERS.items():
        # A header as written first; without its brackets only if none matches.
        for form in (0, 1):
            index = next(
                (
                    i
                    for name in names
                    for i, cell in enumerate(forms)
                    if cell[form] == name and i not in roles.values()
                ),
                None,
            )
            if index is not None:
                roles[role] = index
                break
    return roles


def _derivation(header: list[str]) -> bool:
    return any(set(_header_forms(cell)) & _DERIVATION_HEADERS for cell in header)


def _attribute_header(cells: list[str]) -> bool:
    """Whether ``cells`` are the header row of a table :func:`parse_specification` reads."""

    roles = _roles(cells)
    return "name" in roles and len(roles) >= 2 and not _derivation(cells)


def starts_another_table(header: list[str], row: list[str]) -> bool:
    """Whether ``row``, first after a table headed ``header``, is a header of its own.

    It is when it differs from ``header`` and names columns as a specification's
    or a derived-field table's header does, or heads the names column as
    ``header`` does ("Field Name" over a classification table's columns).
    """

    if row == header:
        return False
    roles = _roles(row)
    name = _roles(header).get("name")
    return (
        ("name" in roles and len(roles) >= 2)
        or _derivation(row)
        or (name is not None and name < len(row) and row[name] == header[name])
    )


def _known_headers() -> list[list[str]]:
    names = {name for names in ROLE_HEADERS.values() for name in names}
    return sorted((name.split() for name in names), key=len, reverse=True)


_KNOWN_HEADERS = _known_headers()


def _flattened(line: str) -> bool:
    """Whether ``line`` looks like a table that lost its structure when it was copied.

    That is a line of words starting with several column headers (the tabs
    between cells became spaces), or a pipe line holding a header and a
    ``|---|`` separator run (the line breaks were lost).
    """

    parsed = _cells(line)
    if parsed is None:
        # Up to the first comma: "Name, type and length are below" is a sentence.
        head = re.split(r"[,;]", _CHAT_PREFIX.sub("", line), maxsplit=1)[0]
        words = _normalize_header(head).split()
        at = 0
        for _ in range(_FLATTENED_HEADERS):
            name = next(
                (name for name in _KNOWN_HEADERS if words[at : at + len(name)] == name), None
            )
            if name is None:
                return False
            at += len(name)
        return True
    delimiter, cells = parsed
    if delimiter != "|":
        return False
    for i in range(1, len(cells) - 1):
        if _SEPARATOR_CELL.match(cells[i]) and _SEPARATOR_CELL.match(cells[i + 1]):
            return _attribute_header([cell for cell in cells[:i] if cell])
    return False


def _clean(value: str) -> str | None:
    value = value.strip()
    match = _MARKUP.match(value)
    if match:
        value = match.group(2).strip()
    return value or None


def _required(value: str | None, *, inverted: bool) -> str | None:
    if value is None:
        return None
    lowered = value.casefold().strip()
    if lowered in _REQUIRED_WORDS:
        return "Y"
    if lowered in _OPTIONAL_WORDS:
        return "N"
    if lowered in _TRUE:
        return "N" if inverted else "Y"
    if lowered in _FALSE:
        return "Y" if inverted else "N"
    return value


def _key(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.casefold().strip()
    if lowered in _PRIMARY:
        return "PK"
    if lowered.startswith(("fk", "foreign")):
        return "FK"
    return value


def parse_specification(text: str, *, table: int | None = None) -> SpecParse:
    """The attributes in every attribute table of ``text``.

    With ``table``, only the table that starts on that line is read (a message
    may hold several sources' specifications), and no other line of ``text`` is
    looked at for a table that lost its structure.
    """

    result = SpecParse()
    blocks = [block for block in _blocks(text) if table is None or block.first == table]
    # Names read so far: a later table that only names these gives their values.
    read_names: set[str] = set()
    for block in blocks:
        header, body, start = block.rows[0].cells, block.rows[1:], block.first
        roles = _roles(header)
        named = ", ".join(cell for cell in header if cell) or "an empty header"
        if "name" not in roles:
            result.notes.append(
                f"Skipped the table at line {start}: no column in its header ({named}) "
                "names the attributes."
            )
            continue
        if _derivation(header):
            result.notes.append(
                f"Skipped the table at line {start}: its columns ({named}) describe derived "
                "fields, not specification attributes."
            )
            continue
        if len(roles) < 2:
            result.notes.append(
                f"Skipped the table at line {start}: besides the names it has no type, "
                f"length, required or description column ({named})."
            )
            continue
        result.table_spans.append((block.first, block.last))
        names = {
            normalize_name(cells[roles["name"]])
            for cells in (row.cells for row in body)
            if roles["name"] < len(cells) and _clean(cells[roles["name"]])
        }
        if read_names and names and names <= read_names:
            result.notes.append(
                f"Skipped the table at line {start}: every attribute in it is in an earlier "
                "table, so it gives values for them, not the specification."
            )
            continue
        inverted = "required" in roles and bool(
            set(_header_forms(header[roles["required"]])) & _INVERTED_HEADERS
        )
        result.table_lines.update(range(block.first, block.last + 1))
        read = 0
        for row in body:
            if len(row.cells) > len(header):
                result.notes.append(
                    f"Line {row.line} has {len(row.cells)} cells but the header has "
                    f"{len(header)}; the extra cells were not read."
                )
            cells = (row.cells + [""] * len(header))[: len(header)]
            value = {role: _clean(cells[index]) for role, index in roles.items()}
            name = value.get("name")
            if name is None:
                result.notes.append(f"Line {row.line} has no attribute name and was not read.")
                result.skipped_lines.append(row.line)
                continue
            data_type, length = value.get("data_type"), value.get("length")
            if data_type and "length" not in roles:
                sized = _TYPE_WITH_SIZE.match(data_type)
                if sized:
                    data_type, length = sized.group(1).strip(), sized.group(2).replace(" ", "")
            result.attributes.append(
                ParsedAttribute(
                    name=name,
                    line=row.line,
                    data_type=data_type,
                    length=length,
                    required=_required(value.get("required"), inverted=inverted),
                    description=value.get("description"),
                    key=_key(value.get("key")),
                )
            )
            read_names.add(normalize_name(name))
            read += 1
        mapped = ", ".join(f"{role} = {header[index]!r}" for role, index in roles.items())
        unread = [cell for i, cell in enumerate(header) if cell and i not in roles.values()]
        result.tables.append(
            f"Table at line {start}: {read} attribute(s); columns {mapped}"
            + (f"; not read: {', '.join(unread)}" if unread else "")
        )
    if table is not None:
        return result
    spanned = {line for block in blocks for line in range(block.first, block.last + 1)}
    for number, line in enumerate(text.splitlines(), start=1):
        if number not in spanned and _flattened(line):
            result.flattened.append(number)
            result.notes.append(
                f"Line {number} looks like a table that lost its columns or line breaks when "
                "it was copied, so it was not read."
            )
    return result


def without_attribute_tables(text: str) -> str:
    """``text`` with the attribute tables :func:`parse_specification` reads taken out."""

    skip = parse_specification(text).table_lines
    return "\n".join(
        line for number, line in enumerate(text.splitlines(), start=1) if number not in skip
    )


__all__ = [
    "ParsedAttribute",
    "SpecParse",
    "duplicate_names",
    "normalize_name",
    "parse_specification",
    "starts_another_table",
    "without_attribute_tables",
]
