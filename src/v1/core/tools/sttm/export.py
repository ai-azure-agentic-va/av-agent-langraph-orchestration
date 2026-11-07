"""Export an STTM session as files.

:class:`JsonExporter` writes the whole normalized session (every table cell with
its value and source) and the session summary. The Data Dictionary and STTM
workbooks are not an export: ``sttm_workbook`` builds them (:mod:`.workbook`)
and keeps them for the analyst to download.

An exporter only returns files; ``sttm_show(export=...)`` writes them into the
conversation's file store (the ``files`` state channel).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from v1.core.tools.sttm.render import summary_view, validation_view
from v1.core.tools.sttm.rules import available_rule_files
from v1.core.tools.sttm.session import (
    COLUMNS,
    DETAILS,
    GATES,
    INPUT_KINDS,
    TABLES,
    Session,
    SessionError,
    Tables,
    validate,
)

EXPORT_DIR = "/sttm/exports/"
FORMAT_VERSION = 1


@dataclass(frozen=True)
class ExportedFile:
    path: str
    content: str
    media_type: str
    description: str


class Exporter(Protocol):
    """Turns a session into files of one format."""

    format: str

    def export(self, session: Session) -> list[ExportedFile]: ...


def _stem(session: Session) -> str:
    return f"{EXPORT_DIR}{session.id}-v{session.version}"


def session_document(session: Session) -> dict[str, Any]:
    """The whole session as plain data: every table cell with its value and source."""

    tables = Tables(session)
    document: dict[str, Any] = {
        "format": "sttm-session",
        "format_version": FORMAT_VERSION,
        "session": {
            "id": session.id,
            "version": session.version,
            "supersedes": session.supersedes,
            "source_system": session.source_system,
            "source": session.source,
            "status": session.status,
            "review_mode": session.review_mode,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "reviews": {GATES[gate]: session.gates[gate].status for gate in GATES},
        },
        "approval": session.approval.model_dump(mode="json") if session.approval else None,
        "inputs": [
            {
                "kind": record.kind,
                "label": INPUT_KINDS[record.kind],
                "message_id": record.message_id,
                "recorded_at": record.recorded_at,
                "inherited_from": record.inherited_from,
                "text": session.messages.get(record.message_id, ""),
            }
            for record in session.inputs
        ],
        "file_details": {
            key: {"label": DETAILS[key], **value.model_dump(mode="json")}
            for key, value in session.details.items()
        },
        "derived_fields": [derived.model_dump(mode="json") for derived in session.derived_fields],
        "no_derived_fields": session.no_derived_fields is not None,
        "audit_fields": [audit.model_dump(mode="json") for audit in session.audit_fields],
        "tables": {},
        "validation": [result.model_dump(mode="json") for result in validate(session)],
        "feedback": [entry.model_dump(mode="json") for entry in session.feedback],
        "rule_files": sorted(available_rule_files()),
    }
    for table, label in TABLES.items():
        rows = []
        for row in tables.rows(table):
            resolved = {
                column.key: tables.resolve(table, row, column.key) for column in COLUMNS[table]
            }
            rows.append(
                {
                    "values": {key: cell.value for key, cell in resolved.items()},
                    "sources": {key: cell.source for key, cell in resolved.items()},
                }
            )
        document["tables"][table] = {
            "label": label,
            "columns": [
                {
                    "key": column.key,
                    "label": column.label,
                    "kind": column.kind,
                    "required": column.required,
                }
                for column in COLUMNS[table]
            ],
            "rows": rows,
        }
    return document


class JsonExporter:
    """The normalized session as JSON, and the session summary as Markdown."""

    format = "json"

    def export(self, session: Session) -> list[ExportedFile]:
        results = validate(session)
        summary = "\n\n".join(
            [summary_view(session, results=results), validation_view(session, results)]
        )
        return [
            ExportedFile(
                path=f"{_stem(session)}.json",
                content=json.dumps(session_document(session), indent=2, ensure_ascii=False),
                media_type="application/json",
                description="The whole session: inputs, file details and every table cell",
            ),
            ExportedFile(
                path=f"{_stem(session)}-summary.md",
                content=summary + "\n",
                media_type="text/markdown",
                description="The session summary and validation results",
            ),
        ]


EXPORTERS: dict[str, Exporter] = {exporter.format: exporter for exporter in (JsonExporter(),)}


def export_session(session: Session, format: str) -> list[ExportedFile]:
    """The files of ``session`` in ``format``; raises SessionError when it can't be done."""

    exporter = EXPORTERS.get(format.strip().casefold())
    if exporter is None:
        raise SessionError(f"Unknown export format {format!r}; use: {', '.join(EXPORTERS)}.")
    return exporter.export(session)


__all__ = [
    "EXPORTERS",
    "EXPORT_DIR",
    "FORMAT_VERSION",
    "ExportedFile",
    "Exporter",
    "JsonExporter",
    "export_session",
    "session_document",
]
