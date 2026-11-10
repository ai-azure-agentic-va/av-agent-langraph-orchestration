"""Whether a failed run was since fixed by a later run of the same job.

Judged by the job's latest run: a later success followed by another failure is "failed again",
not fixed. One-off runs (submitted, or child notebook runs) have no job history to check.
"""

from typing import Any

from databricks.sdk.service.jobs import BaseRun, Run, RunType

from v1.core.tools.databricks.jobs.client import JobsClient
from v1.core.tools.databricks.jobs.reports import run_phase
from v1.core.tools.databricks.jobs.tools import MAX_RUNS_SCANNED, iso_time

ONE_OFF_TYPES = frozenset({RunType.SUBMIT_RUN, RunType.WORKFLOW_RUN})


def _ref(run: BaseRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "start_time": iso_time(run.start_time),
        "run_url": run.run_page_url,
    }


async def fixed_status(client: JobsClient, run: Run) -> dict[str, Any]:
    """The fixed status of a failed run; errors propagate to the caller."""
    if run.run_type in ONE_OFF_TYPES or await client.job_name(run.job_id) is None:
        return {"status": "one_off"}
    runs = await client.list_runs(run.job_id, limit=MAX_RUNS_SCANNED)
    started = run.start_time or 0
    newer = sorted(
        (r for r in runs if r.start_time and r.start_time > started),
        key=lambda r: r.start_time,
        reverse=True,
    )
    # Only runs that ran to a result count: a cancelled or skipped run is neither fix nor failure.
    decided = [r for r in newer if run_phase(r.state) in ("succeeded", "failed")]
    fix = next((r for r in decided if run_phase(r.state) == "succeeded"), None)
    if fix is None:
        return {"status": "not_fixed"}
    latest = decided[0]
    if latest is fix:
        return {"status": "fixed", "fix": _ref(fix)}
    return {"status": "failed_again", "fix": _ref(fix), "latest": _ref(latest)}
