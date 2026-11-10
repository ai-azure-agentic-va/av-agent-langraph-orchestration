"""Which Unity Catalog objects the agent may use.

`DATA_ALLOWLIST` lists `catalog`, `catalog.schema` or `catalog.schema.table` entries. Empty means
open scope: everything the app can access. Unity Catalog stores names lowercased, so
all comparisons use lowercased parts.
"""

import re
from dataclasses import dataclass

_PLAIN = re.compile(r"^[a-z_][a-z0-9_]*$")
_COLUMNS = ("table_catalog", "table_schema", "table_name")
OUTSIDE_SCOPE = "{} is outside the data this assistant may use"


def split_name(name: str) -> tuple[str, ...]:
    """Split a dotted name into lowercased parts. Backticks quote parts with special characters."""
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "`":
            if quoted and name[i + 1 : i + 2] == "`":  # `` inside quotes is a literal backtick
                buf.append("`")
                i += 2
                continue
            quoted = not quoted
        elif ch == "." and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quoted:
        raise ValueError(f"unbalanced backtick in {name!r}")
    parts.append("".join(buf))
    cleaned = tuple(part.strip().lower() for part in parts)
    if any(not part for part in cleaned):
        raise ValueError(f"empty name part in {name!r}")
    return cleaned


def format_name(*parts: str) -> str:
    """Join name parts into SQL, backticking any part that isn't a plain identifier."""
    return ".".join(p if _PLAIN.match(p) else "`" + p.replace("`", "``") + "`" for p in parts)


def table_parts(name: str) -> tuple[str, ...] | None:
    """The lowercased (catalog, schema, table) of a full table name, or None if it isn't one."""
    try:
        parts = split_name(name)
    except ValueError:
        return None
    return parts if len(parts) == 3 else None


@dataclass(frozen=True)
class DataScope:
    entries: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def parse(cls, raw: str | None) -> "DataScope":
        if raw is None or not raw.strip():
            return cls()
        entries = []
        for item in raw.split(","):
            if not item.strip():
                continue  # tolerate a trailing comma
            parts = split_name(item)
            if len(parts) > 3:
                raise ValueError(f"allowlist entry {item.strip()!r} has more than 3 parts")
            entries.append(parts)
        if not entries:
            # " , " must never quietly become open scope.
            raise ValueError("DATA_ALLOWLIST is set but contains no entries")
        return cls(tuple(entries))

    @property
    def is_open(self) -> bool:
        return not self.entries

    def covers(self, catalog: str, schema: str, table: str) -> bool:
        if self.is_open:
            return True
        name = (catalog.lower(), schema.lower(), table.lower())
        return any(name[: len(entry)] == entry for entry in self.entries)

    def search_filter(self) -> tuple[str, dict[str, str]]:
        """A predicate over information_schema.tables columns, with its named parameters."""
        if self.is_open:
            return "TRUE", {}
        clauses = []
        params: dict[str, str] = {}
        for i, entry in enumerate(self.entries):
            conditions = []
            for column, value in zip(_COLUMNS, entry, strict=False):
                param = f"s{i}_{column}"
                conditions.append(f"{column} = :{param}")
                params[param] = value
            clauses.append("(" + " AND ".join(conditions) + ")")
        return "(" + " OR ".join(clauses) + ")", params

    def describe(self) -> str:
        return "OPEN" if self.is_open else f"allowlist ({len(self.entries)} entries)"
