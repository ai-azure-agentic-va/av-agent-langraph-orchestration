"""Read the attribute table out of a pasted source specification.

Specifications arrive as web pages, documents, spreadsheets and notes. What an
analyst pastes from them is a table with a header row: Markdown
(pipe-separated) or copied cells (tab-separated). Every table in the text
whose header names an attribute column is read, in order; anything else in
the text (instructions, notes) is left alone. Names are kept exactly as
written. The other columns are recognized by common header names and
normalized: a ``nullable``/``optional`` column becomes a required flag, a type
such as ``VARCHAR(50)`` is split into type and length when the table has no
length column, and a key column marks primary keys.

Nothing is guessed: an empty cell stays empty, and a column the parser does
not recognize is reported, not mapped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


def normalize_name(name: str) -> str:
    """How attribute names are compared: case, spaces and punctuation ignored."""

    return re.sub(r"[^0-9a-z]+", "", name.casefold())


def _normalize_header(header: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", header.casefold()).strip()


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


def _cells(line: str) -> tuple[str, list[str]] | None:
    """(delimiter, cells) for a table line, or None for any other line."""

    line = line.rstrip("\r\n")
    if "\t" in line:
        return "\t", [cell.strip() for cell in line.split("\t")]
    stripped = line.strip()
    if "|" in stripped:
        text = stripped.replace("\\|", "\x00")
        text = text[1:] if text.startswith("|") else text
        text = text[:-1] if text.endswith("|") else text
        cells = [cell.replace("\x00", "|").strip() for cell in text.split("|")]
        if len(cells) >= 2:
            return "|", cells
    return None


def _blocks(text: str) -> list[list[_Row]]:
    """Runs of consecutive table lines that share a delimiter, separator rows dropped."""

    blocks: list[list[_Row]] = []
    current: list[_Row] = []
    delimiter = None
    for number, line in enumerate(text.splitlines(), start=1):
        parsed = _cells(line)
        if parsed is None or parsed[0] != delimiter:
            if current:
                blocks.append(current)
            current, delimiter = [], None
            if parsed is None:
                continue
            delimiter = parsed[0]
        cells = parsed[1]
        if all(not cell or _SEPARATOR_CELL.match(cell) for cell in cells):
            continue
        current.append(_Row(number, cells))
    if current:
        blocks.append(current)
    # A lone line with a pipe or a tab in it is prose, not a table.
    return [block for block in blocks if len(block) >= 2]


def _roles(header: list[str]) -> dict[str, int]:
    normalized = [_normalize_header(cell) for cell in header]
    roles: dict[str, int] = {}
    for role, names in ROLE_HEADERS.items():
        for name in names:
            index = next(
                (
                    i
                    for i, cell in enumerate(normalized)
                    if cell == name and i not in roles.values()
                ),
                None,
            )
            if index is not None:
                roles[role] = index
                break
    return roles


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


def parse_specification(text: str) -> SpecParse:
    """The attributes in every attribute table of ``text``."""

    result = SpecParse()
    for block in _blocks(text):
        header, body, start = block[0].cells, block[1:], block[0].line
        roles = _roles(header)
        named = ", ".join(cell for cell in header if cell) or "an empty header"
        if "name" not in roles:
            result.notes.append(
                f"Skipped the table at line {start}: no column in its header ({named}) "
                "names the attributes."
            )
            continue
        if {_normalize_header(cell) for cell in header} & _DERIVATION_HEADERS:
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
        inverted = (
            "required" in roles
            and _normalize_header(header[roles["required"]]) in _INVERTED_HEADERS
        )
        result.table_lines.update(range(start, block[-1].line + 1))
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
            read += 1
        mapped = ", ".join(f"{role} = {header[index]!r}" for role, index in roles.items())
        unread = [cell for i, cell in enumerate(header) if cell and i not in roles.values()]
        result.tables.append(
            f"Table at line {start}: {read} attribute(s); columns {mapped}"
            + (f"; not read: {', '.join(unread)}" if unread else "")
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
    "without_attribute_tables",
]
