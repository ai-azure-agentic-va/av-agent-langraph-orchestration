"""Core package. Its exports load on first use: importing ``v1.core.agent`` here
would pull in every tool, and the Databricks tools' ``v1.utils`` modules import
``v1.core.config``, which would then be re-entered half loaded.
"""

from typing import Any

__all__ = ["build_agent", "close_agent_resources", "get_settings"]


def __getattr__(name: str) -> Any:
    if name in ("build_agent", "close_agent_resources"):
        from v1.core import agent

        return getattr(agent, name)
    if name == "get_settings":
        from v1.core.config import get_settings

        return get_settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
