"""Convert the ServiceNow intent test-case workbook into a reviewable JSON dataset.

The eval harness ([test_servicenow_eval.py](../../src/v1/test/v1/utils/test_servicenow_eval.py))
reads the generated JSON, NOT the binary ``.xlsx`` — so the test suite has no
``openpyxl`` dependency and the cases are diff-reviewable in version control.

Run after editing the workbook::

    .venv/bin/python docs/intents/convert_intents.py

Source of truth: ``docs/intents/servicenow_intent_test_cases_v4.xlsx``
Output:          ``src/v1/test/v1/fixtures/servicenow_intent_cases.json``
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import openpyxl

_ROOT = Path(__file__).resolve().parents[2]
_XLSX = _ROOT / "docs" / "intents" / "servicenow_intent_test_cases_v4.xlsx"
_OUT = _ROOT / "src" / "v1" / "test" / "v1" / "fixtures" / "servicenow_intent_cases.json"

_INC_RE = re.compile(r"INC\d{7}", re.IGNORECASE)


def _clean(value: object) -> str:
    """Collapse whitespace in a cell value to a single-line string."""

    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _incident_numbers(text: str) -> list[str]:
    """All INC#######, de-duplicated in first-seen order."""

    seen: dict[str, None] = {}
    for match in _INC_RE.findall(text or ""):
        seen.setdefault(match.upper(), None)
    return list(seen)


def main() -> int:
    workbook = openpyxl.load_workbook(_XLSX, data_only=True)
    sheet = workbook["Intent Test Cases"]
    rows = list(sheet.iter_rows(values_only=True))
    header = [_clean(cell) for cell in rows[0]]
    index = {name: position for position, name in enumerate(header)}

    def cell(row: tuple, column: str) -> str:
        return _clean(row[index[column]]) if column in index else ""

    cases: list[dict] = []
    for row in rows[1:]:
        test_id = cell(row, "Test ID")
        if not test_id:
            continue
        expected_text = cell(row, "Expected Incident Numbers Returned")
        cases.append(
            {
                "test_id": test_id,
                "query_type": cell(row, "Query Type"),
                "user_query": cell(row, "User Query"),
                "expected_intent": cell(row, "Expected Intent"),
                "route_to": cell(row, "Route To"),
                "data_source": cell(row, "Data Source (Resolved)"),
                "expected_filter": cell(row, "Expected ServiceNow Filter"),
                "expected_incidents_text": expected_text,
                "expected_incidents": _incident_numbers(expected_text),
                "expected_summary": cell(row, "Expected Output Summary"),
                "notes": cell(row, "Notes"),
                "status_in_workbook": cell(row, "Status"),
            }
        )

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases -> {_OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
