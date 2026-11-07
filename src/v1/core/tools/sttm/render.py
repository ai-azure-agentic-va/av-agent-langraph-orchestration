"""Markdown views of an STTM session, for the sttm-agent to show the analyst.

Every value shown carries where it came from, so the analyst can tell a value
they gave from one a rule file decided, one the agent drafted and one nobody
has decided yet (:data:`LEGEND`). Tables are paged; a page says how to get the
next one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from v1.core.tools.sttm.rules import available_rule_files
from v1.core.tools.sttm.session import (
    CHECKS,
    COLUMN_INDEX,
    COLUMNS,
    DETAILS,
    GATES,
    GENERATED,
    INPUT_KINDS,
    REQUIRED_INPUTS,
    REVIEW_MODES,
    STAGES,
    TABLES,
    UNRESOLVED_LABELS,
    Change,
    CheckResult,
    Column,
    Resolved,
    Session,
    SessionError,
    Tables,
    column_key,
    gaps,
    validate,
)

VIEWS: dict[str, str] = {
    "summary": "Where the session stands: inputs, reviews, PII/PCI, validation, open questions",
    "inputs": "The inputs recorded, and what was read from each",
    "attributes": "The attributes as the specification gives them",
    "file_details": "Keys, layout and CMDB number, with their sources",
    "data_dictionary": TABLES["data_dictionary"],
    "raw_to_curated": TABLES["raw_to_curated"],
    "curated_to_asl": TABLES["curated_to_asl"],
    "validation": "Every validation check, run now, with the attributes that fail it",
    "feedback": "Every change made, newest first, and the analyst message behind it",
    "sessions": "Every STTM session in this conversation",
}
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_MARKS = {"analyst": "a", "rule": "r", "draft": "d"}
LEGEND = (
    "Marks: [a] the analyst's words, [r] a rule file, [d] the agent's draft, for the analyst "
    "to confirm; [a*] and the like: the table default. Unmarked values come from the "
    "specification or are worked out from it. ? = not decided yet. REVIEW = PII/PCI not "
    "decided yet: treat the attribute as sensitive until it is."
)
_GATE_STATUS = {
    "pending": "not reached",
    "open": "open, waiting for the analyst",
    "confirmed": "confirmed",
    "skipped": "skipped by the review mode",
}


def escape(text: str) -> str:
    """``text`` as one Markdown table cell: pipes escaped, line breaks as spaces."""

    return " ".join(text.replace("|", "\\|").split())


def cell_text(resolved: Resolved, column: str) -> str:
    """A resolved cell as shown: the value and its mark, or the unresolved label."""

    if resolved.value is None:
        return UNRESOLVED_LABELS.get(column, "?")
    mark = _MARKS.get(resolved.source)
    if mark is None:
        return escape(resolved.value)
    return f"{escape(resolved.value)} [{mark}{'*' if resolved.from_default else ''}]"


def _value_text(value: str | None, source: str | None) -> str:
    if value is None:
        return "not given"
    mark = _MARKS.get(source or "")
    return f"{value} [{mark}]" if mark else f"{value} ({source})"


def _markdown_table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _page(offset: int, limit: int) -> tuple[int, int]:
    if offset < 0 or limit < 1:
        raise SessionError("offset must be 0 or more and limit at least 1.")
    return offset, min(limit, MAX_LIMIT)


def _more(view: str, total: int, offset: int, shown: int, limit: int) -> list[str]:
    """The line saying how to get the next page, if there is one."""

    if offset + shown >= total:
        return []
    return [f"More: sttm_show(view='{view}', offset={offset + shown}, limit={limit})."]


def _selected_columns(table: str, names: list[str] | None) -> list[Column]:
    if not names:
        return list(COLUMNS[table])
    selected, unknown = [], []
    for name in names:
        key = column_key(table, name)
        if key is None:
            unknown.append(name)
        elif key not in [column.key for column in selected]:
            selected.append(COLUMN_INDEX[table][key])
    if unknown:
        raise SessionError(
            f"Unknown column(s) of the {TABLES[table]}: {', '.join(unknown)}; the columns are "
            + ", ".join(column.key for column in COLUMNS[table])
            + "."
        )
    if selected[0].key != "attribute_name":
        selected.insert(0, COLUMN_INDEX[table]["attribute_name"])
    return selected


def unresolved_required(tables: Tables, table: str) -> dict[str, int]:
    """Required columns of ``table`` with rows left unresolved, and how many."""

    counts: dict[str, int] = {}
    for column in COLUMNS[table]:
        if not column.required:
            continue
        missing = sum(
            1 for row in tables.rows(table) if tables.resolve(table, row, column.key).value is None
        )
        if missing:
            counts[column.key] = missing
    return counts


def table_view(
    session: Session,
    table: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    columns: list[str] | None = None,
    tables: Tables | None = None,
) -> str:
    """One page of a table, its table defaults and its unresolved required columns."""

    tables = tables or Tables(session)
    rows = tables.rows(table)
    offset, limit = _page(offset, limit)
    selected = _selected_columns(table, columns)
    page = rows[offset : offset + limit]
    lines = [f"### {TABLES[table]} (session {session.id}, version {session.version})"]
    if table != "data_dictionary":
        left_out = len(tables.rows("data_dictionary")) - len(rows)
        lines.append(
            "Rows: the attributes carried to Curated (carry_to_curated Y, or not set yet)"
            + (f"; {left_out} not carried (N) are left out." if left_out else ".")
        )
    if not rows:
        lines.append("No rows.")
    else:
        lines.append(f"Rows {offset + 1}-{offset + len(page)} of {len(rows)}.")
        lines += _markdown_table(
            [column.label for column in selected],
            (
                [
                    cell_text(tables.resolve(table, row, column.key), column.key)
                    for column in selected
                ]
                for row in page
            ),
        )
        lines += _more(table, len(rows), offset, len(page), limit)
    defaults = [
        f"{COLUMN_INDEX[table][key].label} = {cell_text(tables.default(table, key), key)}"
        for key in session.cells.get(table, {}).get("*", {})
    ]
    if defaults:
        lines.append(
            "Table defaults (used where a row has no value of its own): " + "; ".join(defaults)
        )
    missing = unresolved_required(tables, table)
    if missing:
        lines.append(
            "Required, not decided yet: "
            + ", ".join(f"{key} ({count} row(s))" for key, count in missing.items())
        )
    lines.append(LEGEND)
    return "\n".join(lines)


def attributes_view(session: Session, *, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
    attributes = session.attributes
    offset, limit = _page(offset, limit)
    page = attributes[offset : offset + limit]
    lines = [f"### Specification attributes (session {session.id})"]
    if not attributes:
        lines.append("No specification recorded yet.")
        return "\n".join(lines)
    lines.append(
        f"Rows {offset + 1}-{offset + len(page)} of {len(attributes)}, exactly as the "
        "specification gives them (empty = not in the specification)."
    )
    lines += _markdown_table(
        ["Attribute", "Line", "Data type", "Length", "Required", "Key", "Description"],
        (
            [
                escape(attribute.name),
                str(attribute.line),
                escape(attribute.data_type or ""),
                escape(attribute.length or ""),
                escape(attribute.required or ""),
                escape(attribute.key or ""),
                escape(attribute.description or ""),
            ]
            for attribute in page
        ),
    )
    lines += _more("attributes", len(attributes), offset, len(page), limit)
    return "\n".join(lines)


def inputs_view(session: Session) -> str:
    lines = [f"### Inputs (session {session.id})"]
    if not session.inputs:
        lines.append("Nothing recorded yet.")
    for record in session.inputs:
        text = session.messages.get(record.message_id, "")
        excerpt = " ".join(text.split())
        excerpt = excerpt[:200] + ("…" if len(excerpt) > 200 else "")
        line = f"- {INPUT_KINDS[record.kind]}: message {record.message_id}, {record.recorded_at}"
        if record.inherited_from:
            line += f", carried over from session {record.inherited_from}"
        lines.append(line + (f': "{excerpt}"' if excerpt else ""))
        lines += [f"  - {table}" for table in record.tables]
        lines += [f"  - Note: {note}" for note in record.notes]
    recorded = {record.kind for record in session.inputs}
    missing = [INPUT_KINDS[kind] for kind in REQUIRED_INPUTS if kind not in recorded]
    if missing:
        lines.append("Required, not given yet: " + ", ".join(missing) + ".")
    return "\n".join(lines)


def file_details_view(session: Session) -> str:
    lines = [f"### File details (session {session.id})"]
    for key, label in DETAILS.items():
        value = session.details.get(key)
        if value is None:
            lines.append(f"- {label}: not given")
            continue
        text = _value_text(value.value, value.source)
        if value.source == "draft":
            text += ", proposed by the agent: the analyst confirms it at the input review"
        elif value.note:
            text += f", {value.note}"
        lines.append(f"- {label}: {text}")
    lines.append(_derived_line(session))
    if session.audit_fields:
        lines.append(
            "- Audit fields: "
            + ", ".join(f"{audit.name} ({audit.rule_file})" for audit in session.audit_fields)
        )
    return "\n".join(lines)


def _derived_line(session: Session) -> str:
    if session.derived_fields:
        parts = []
        for derived in session.derived_fields:
            facts = [
                f"logic: {derived.logic}" if derived.logic else "no logic",
                f"join key: {derived.join_key}" if derived.join_key else "no join key",
            ]
            parts.append(f"{derived.name} ({'; '.join(facts)})")
        return "- Derived fields: " + ", ".join(parts)
    if session.no_derived_fields is not None:
        return "- Derived fields: none (the analyst said so)"
    return "- Derived fields: not asked yet"


def validation_view(session: Session, results: list[CheckResult] | None = None) -> str:
    results = results if results is not None else validate(session)
    passed = sum(result.passed for result in results)
    lines = [
        f"### Validation (session {session.id}): {passed} of {len(results)} checks pass",
    ]
    for result in results:
        lines.append(
            f"- {'PASS' if result.passed else 'FAIL'} {CHECKS[result.check]} ({result.check})"
        )
        lines += [f"  - {failing}" for failing in result.failing]
        if result.note:
            lines.append(f"  - Note: {result.note}")
    return "\n".join(lines)


def feedback_view(session: Session, *, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
    entries = list(reversed(session.feedback))
    offset, limit = _page(offset, limit)
    page = entries[offset : offset + limit]
    lines = [f"### Changes (session {session.id}), newest first"]
    if not entries:
        lines.append("No changes recorded yet.")
    for entry in page:
        lines.append(f"- {entry.at}, message {entry.message_id}: {entry.summary}")
        lines += [f"  - {change_text(change)}" for change in entry.changes]
    lines += _more("feedback", len(entries), offset, len(page), limit)
    return "\n".join(lines)


def change_text(change: Change) -> str:
    """One change as "where: old → new"."""

    where = " · ".join((TABLES.get(change.table, change.table), change.attribute, change.column))
    return f"{where}: {change.old or '(none)'} → {change.new or '(none)'}"


def sessions_view(sessions: Mapping[str, Session]) -> str:
    lines = ["### STTM sessions in this conversation"]
    if not sessions:
        lines.append("None yet.")
    ordered = sorted(sessions.values(), key=lambda s: (s.updated_at, s.id), reverse=True)
    for session in ordered:
        line = (
            f"- {session.id}: {session.source_system} / {session.source}, version "
            f"{session.version}, {status_text(session)}, updated {session.updated_at}"
        )
        if session.supersedes:
            line += f", replaces {session.supersedes}"
        lines.append(line)
    return "\n".join(lines)


def status_text(session: Session) -> str:
    if session.locked and session.approval is not None:
        by = f" by {session.approval.by}" if session.approval.by else ""
        return f"approved{by} at {session.approval.at}, read-only"
    return {"draft": "collecting inputs", "in_progress": "in progress"}.get(
        session.status, session.status
    )


def review_mode_text(session: Session) -> str:
    if session.review_mode is None:
        return "none chosen, so every review stops for the analyst"
    skipped = REVIEW_MODES[session.review_mode]
    if not skipped:
        return f"{session.review_mode} (every review stops for the analyst)"
    names = ", ".join(GATES[gate] for gate in GATES if gate in skipped)
    return f"{session.review_mode} (skips: {names})"


def workflow_line(session: Session) -> str:
    """Each stage of the workflow, in order, and how far it got."""

    parts = []
    for stage in STAGES:
        if stage in GENERATED:
            built = "built" if session.generated[stage] else "not built"
            parts.append(f"{GENERATED[stage]}: {built}")
        else:
            parts.append(f"{GATES[stage]}: {_GATE_STATUS[session.gates[stage].status]}")
    return "; ".join(parts)


def summary_view(session: Session, *, results: list[CheckResult] | None = None) -> str:
    """Where the session stands, in one screen.

    PII/PCI and validation are left out until the Data Dictionary is built:
    before that every check fails and every attribute is REVIEW.
    """

    tables = Tables(session)
    built = session.generated["data_dictionary"]
    dd = tables.rows("data_dictionary")
    carried = tables.rows("raw_to_curated")
    origins = Counter(row.origin for row in dd)
    lines = [
        f"### STTM session {session.id}: {session.source_system} / {session.source}",
        f"- Version {session.version}"
        + (f", replacing approved session {session.supersedes}" if session.supersedes else "")
        + f"; {status_text(session)}",
        f"- Review mode: {review_mode_text(session)}",
        f"- Workflow: {workflow_line(session)}",
    ]
    recorded = Counter(record.kind for record in session.inputs)
    given = ", ".join(
        f"{INPUT_KINDS[kind]}{f' ({count} messages)' if count > 1 else ''}"
        for kind, count in recorded.items()
    )
    missing = [INPUT_KINDS[kind] for kind in REQUIRED_INPUTS if kind not in recorded]
    lines.append(
        f"- Inputs: {given or 'none yet'}"
        + (f"; still needed: {', '.join(missing)}" if missing else "")
    )
    lines.append(
        f"- Attributes: {origins['specification']} from the specification, "
        f"{origins['derived']} derived, {origins['audit']} audit; "
        f"{len(carried)} of {len(dd)} carried to Curated (or not decided yet)"
    )
    details = [
        f"{label} {_value_text(session.details[key].value, session.details[key].source)}"
        for key, label in DETAILS.items()
        if key in session.details
    ]
    lines.append("- File details: " + ("; ".join(details) if details else "none given yet"))
    lines.append(_derived_line(session))
    pii = Counter(
        tables.resolve("data_dictionary", row, "pii_pci").value or UNRESOLVED_LABELS["pii_pci"]
        for row in dd
    )
    if built and dd:
        lines.append(
            "- PII/PCI in the Data Dictionary: "
            + ", ".join(f"{value} {count}" for value, count in pii.most_common())
        )
    drafts = sum(
        1
        for table in TABLES
        for row in tables.rows(table)
        for column in COLUMNS[table]
        if column.kind == "descriptive" and tables.resolve(table, row, column.key).source == "draft"
    )
    proposed = [DETAILS[key] for key, value in session.details.items() if value.source == "draft"]
    if drafts or proposed:
        lines.append(
            "- Waiting for the analyst to confirm: "
            + "; ".join(
                part
                for part in (
                    f"{drafts} drafted description(s)" if drafts else "",
                    f"proposed {', '.join(proposed)}" if proposed else "",
                )
                if part
            )
        )
    if built:
        results = results if results is not None else validate(session)
        failing = [result for result in results if not result.passed]
        lines.append(
            f"- Validation: {len(results) - len(failing)} of {len(results)} checks pass"
            + (
                "; failing: "
                + ", ".join(f"{result.check} ({len(result.failing)})" for result in failing)
                if failing
                else ""
            )
        )
    questions = gaps(session)
    if questions:
        lines.append("- Open questions for the analyst:")
        lines += [f"  - {question}" for question in questions]
    present = ", ".join(available_rule_files()) or (
        "none, so values a rule decides stay unresolved unless the analyst gives them"
    )
    lines.append(f"- Rule files present: {present}")
    if session.approval is not None:
        approval = session.approval
        line = (
            f"- Approved by {approval.by or 'the analyst'} at {approval.at} "
            f"(message {approval.message_id})"
        )
        if approval.override_reason:
            line += (
                f", overriding failed checks ({', '.join(approval.failed_checks)}): "
                f'"{approval.override_reason}"'
            )
        lines.append(line)
    elif session.ready_on is not None:
        lines.append(
            "- Ready for approval: every review is done; the analyst approves in a later message."
        )
    return "\n".join(lines)


def render(
    session: Session,
    view: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    columns: list[str] | None = None,
) -> str:
    """One view of ``session`` (any of :data:`VIEWS` but ``sessions``)."""

    if view in TABLES:
        return table_view(session, view, offset=offset, limit=limit, columns=columns)
    if view == "summary":
        return summary_view(session)
    if view == "inputs":
        return inputs_view(session)
    if view == "attributes":
        return attributes_view(session, offset=offset, limit=limit)
    if view == "file_details":
        return file_details_view(session)
    if view == "validation":
        return validation_view(session)
    if view == "feedback":
        return feedback_view(session, offset=offset, limit=limit)
    raise SessionError(f"Unknown view {view!r}; use one of: {', '.join(VIEWS)}.")


__all__ = [
    "DEFAULT_LIMIT",
    "LEGEND",
    "MAX_LIMIT",
    "VIEWS",
    "attributes_view",
    "cell_text",
    "change_text",
    "escape",
    "feedback_view",
    "file_details_view",
    "inputs_view",
    "render",
    "sessions_view",
    "summary_view",
    "table_view",
    "unresolved_required",
    "validation_view",
    "workflow_line",
]
