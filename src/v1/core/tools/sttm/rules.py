"""Where the STTM rule files go, and which of them this deployment has.

The organization's STTM standards (PII/PCI classification, data type mapping,
naming conventions, abbreviations, review gates) are kept in nine Markdown
files that are not part of this repository. They are dropped into
:data:`AUTHORING_DIR` as they become available, with no code change: the
sttm-agent reads a present file with ``read_file`` under
:data:`AUTHORING_MOUNT`, and the STTM tools accept a value as ``rule``-sourced
only when it cites a file that is present. A column whose authoritative rule is
missing stays unresolved unless the analyst gives the value.

The organization's Data Dictionary and STTM workbook templates are in
:data:`TEMPLATES_DIR`; :mod:`.workbook` builds the workbooks from them.

``AUTHORING_DIR`` sits one level below a skill directory, so the skills loader,
which reads ``/skills/<name>/SKILL.md`` only, never offers these files to the
orchestrator, not even one named ``SKILL.md``.
"""

from __future__ import annotations

from pathlib import Path

from v1.core.skills import SKILLS_DIR, SKILLS_MOUNT

AUTHORING_DIR: Path = SKILLS_DIR / "sttm" / "authoring"
AUTHORING_MOUNT = f"{SKILLS_MOUNT}sttm/authoring/"
TEMPLATES_DIR: Path = AUTHORING_DIR / "templates"

# The rule files the STTM workflow is built around, and what each one decides.
EXPECTED_RULE_FILES: dict[str, str] = {
    "SKILL.md": (
        "Main instruction set: ground rules, field rules, section definitions, "
        "validation checks, output format and the workflow phases."
    ),
    "field-columns.md": "The field record columns: order, derivation rule and allowed values.",
    "data-type-mapping.md": (
        "Data types from Raw to INTG to Curated to ASL, including audit field types."
    ),
    "pii-pci-handling.md": "PII/PCI classification levels and how each is handled.",
    "ASL_Abbreviations.md": "Abbreviations that turn logical names into ASL physical names.",
    "example-output.md": "Reference output: representative fields and naming conventions.",
    "sttm-mapping.md": (
        "Raw-to-Curated and Curated-to-ASL column rules, exclusions, ETL/audit fields."
    ),
    "review-gates.md": "The review gates and the correction workflow.",
    "virtual_source_input.md": (
        "Turning text or non-standard input into the standard input template."
    ),
}


def available_rule_files() -> dict[str, Path]:
    """The rule files present, by file name: every ``*.md`` here except the README."""

    if not AUTHORING_DIR.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(AUTHORING_DIR.glob("*.md"))
        if path.is_file() and path.name.casefold() != "readme.md"
    }


def rule_file_text(name: str) -> str | None:
    """The text of a present rule file, or None when it is not present."""

    path = available_rule_files().get(name)
    return path.read_text(encoding="utf-8") if path else None


def rule_files_summary() -> str:
    """One line per expected rule file, saying whether it is present and where."""

    present = available_rule_files()
    lines = [
        f"- {name}: {'present at ' + AUTHORING_MOUNT + name if name in present else 'NOT present'}"
        f" ({purpose})"
        for name, purpose in EXPECTED_RULE_FILES.items()
    ]
    lines += [
        f"- {name}: present at {AUTHORING_MOUNT}{name} (not one of the expected files)"
        for name in present
        if name not in EXPECTED_RULE_FILES
    ]
    return "\n".join(lines)


__all__ = [
    "AUTHORING_DIR",
    "AUTHORING_MOUNT",
    "EXPECTED_RULE_FILES",
    "TEMPLATES_DIR",
    "available_rule_files",
    "rule_file_text",
    "rule_files_summary",
]
