"""Tools for the databricks-agent subagent. Every Databricks call authenticates as the app."""

from langchain_core.tools import BaseTool

from v1.core.config import Settings, databricks_registered, get_settings
from v1.core.tools.databricks.jobs.tools import build_jobs_tools
from v1.core.tools.databricks.sql.tools import build_sql_tools
from v1.core.tools.databricks.workspaces import build_workspace_tools


def build_databricks_tools(settings: Settings, *, enabled: bool | None = None) -> list[BaseTool]:
    """The subagent's SQL, job and workspace tools, or ``[]`` when Databricks is off or
    misconfigured.

    ``enabled`` defaults to ``settings.databricks_enabled``; the module-level list passes the
    process-wide ``databricks_registered()`` decision instead. The tools never depend on the
    workspace list (it changes at runtime): each call resolves the run's workspace, and a
    job tool called in a workspace with jobs off returns ``JOBS_DISABLED_MESSAGE``.
    """

    if not (settings.databricks_enabled if enabled is None else enabled):
        return []
    return [*build_sql_tools(settings), *build_jobs_tools(settings), *build_workspace_tools()]


DATABRICKS_TOOLS = build_databricks_tools(get_settings(), enabled=databricks_registered())

# Every Databricks tool name, wired here or not: the names an access-control file may
# block, so one file serves deployments with and without Databricks.
DATABRICKS_TOOL_NAMES = frozenset(
    tool.name for tool in build_databricks_tools(get_settings(), enabled=True)
)


async def close_databricks_resources() -> None:
    """App-shutdown hook for Databricks resources. Nothing is held open today: clients
    are built per call. Kept so app shutdown has one place to add any."""


__all__ = [
    "DATABRICKS_TOOLS",
    "DATABRICKS_TOOL_NAMES",
    "build_databricks_tools",
    "close_databricks_resources",
]
