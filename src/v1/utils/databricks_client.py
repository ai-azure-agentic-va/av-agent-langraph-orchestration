"""The only way Databricks code gets a ``WorkspaceClient``: as the app.

:func:`app_workspace_client` builds a client for a workspace host with an Entra token for
Azure Databricks (:data:`DATABRICKS_SCOPE`) from the shared credential in
``v1.utils.azure_credentials`` (the container app's managed identity when deployed,
``az login`` locally). There is no user token, PAT or CLI profile. Each client gets a
fresh token from the credential, which caches and refreshes it, so a client never holds a
stale one.

Who asked is still known: :func:`run_caller` (tools, from the run's
``langgraph_auth_user``) and :func:`request_caller` (HTTP routes, from ``request.user``)
return the :class:`Caller`, and raise :class:`DatabricksAccessError` when there is no
identity. :func:`audit_log` writes the one app log line per Databricks call.

Clients bound the SDK's HTTP and retry budgets by the longest tool timeout
(``DATABRICKS_SQL_TIMEOUT_SECONDS`` / ``DATABRICKS_JOBS_TIMEOUT_SECONDS``) instead of the
SDK's 300 s retry window, so an unreachable workspace can't hold a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config
from langgraph.config import get_config

from v1.core.config import get_settings
from v1.utils.azure_credentials import get_azure_credential

# ``get_config``, ``get_settings``, ``get_azure_credential`` and ``_new_workspace_client``
# are looked up on this module at call time so tests can replace them.

logger = logging.getLogger(__name__)

# Microsoft's documented Azure Databricks resource (application) ID.
DATABRICKS_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
CLIENT_UNAVAILABLE_MESSAGE = "Databricks is unavailable right now. Try again in a few minutes."
SIGN_IN_MESSAGE = "Couldn't tell who you are. Sign in again and resend your message."


class DatabricksAccessError(Exception):
    """There is no caller identity (outside a run, or no signed-in user)."""

    def __init__(self) -> None:
        super().__init__()


@dataclass(frozen=True)
class Caller:
    """Who asked: the hashed ``identity`` and the raw Entra object ID, when known."""

    identity: str
    object_id: str | None


def _new_workspace_client(**kwargs: Any) -> WorkspaceClient:
    # The timeouts are ``Config`` fields, and the ``Config`` constructor already does
    # HTTP (host metadata discovery), so they must be set here to bound it too.
    return WorkspaceClient(config=Config(**kwargs))


def _field(user: Any, name: str) -> str | None:
    """A non-blank string field of ``user`` (mapping or attributes), else ``None``."""

    try:
        if isinstance(user, Mapping):
            value = user.get(name)
        else:
            getter = getattr(user, "get", None)
            value = getter(name) if callable(getter) else getattr(user, name, None)
    except Exception:  # noqa: BLE001 - no auth backend, or a field the user lacks
        return None
    return value if isinstance(value, str) and value.strip() else None


def _caller(user: Any) -> Caller:
    identity = _field(user, "identity")
    if identity is None:
        raise DatabricksAccessError()
    return Caller(identity, _field(user, "object_id"))


def run_caller() -> Caller:
    """The run's caller, from ``langgraph_auth_user`` (stamped by ``v1.utils.auth``).

    Raises :class:`DatabricksAccessError` outside a run or without an identity.
    """

    try:
        config = get_config()
    except Exception:  # not inside a graph run
        raise DatabricksAccessError() from None
    return _caller(((config or {}).get("configurable") or {}).get("langgraph_auth_user"))


def request_caller(user: Any) -> Caller:
    """The HTTP caller, from ``request.user``. Raises :class:`DatabricksAccessError`
    without an identity."""

    return _caller(user)


async def app_workspace_client(host: str) -> WorkspaceClient:
    """Build a ``WorkspaceClient`` for ``host`` that acts as the app.

    Raises ``ValueError`` for a blank host. Credential errors propagate; callers map
    them to :data:`CLIENT_UNAVAILABLE_MESSAGE`.
    """

    host = host.strip() if isinstance(host, str) else ""
    if not host:
        raise ValueError("Databricks workspace host is blank")
    settings = get_settings()
    longest = max(settings.databricks_sql_timeout_seconds, settings.databricks_jobs_timeout_seconds)
    seconds = max(1, math.ceil(longest))
    # Building the credential and acquiring the token are blocking I/O: keep both off the
    # event loop.
    access = await asyncio.to_thread(lambda: get_azure_credential().get_token(DATABRICKS_SCOPE))
    kwargs = {
        "host": host,
        "token": access.token,
        "auth_type": "pat",
        "http_timeout_seconds": seconds,
        "retry_timeout_seconds": seconds,
    }
    # The constructor resolves config and may do I/O: keep it off the event loop.
    return await asyncio.to_thread(lambda: _new_workspace_client(**kwargs))


# The longest ``detail`` an audit line keeps; a longer one is cut and ends in "…".
MAX_AUDIT_DETAIL_CHARS = 2000
# C0 controls, DEL, C1 controls and the Unicode line/paragraph separators: anything that
# could end the line or rewrite the terminal. Each is written as a backslash escape.
_UNSAFE_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_char(match: re.Match[str]) -> str:
    char = match.group()
    code = ord(char)
    return _NAMED_ESCAPES.get(char) or (f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")


def _log_safe(text: str) -> str:
    """``text`` with every control character escaped, so it stays on one log line."""

    return _UNSAFE_CHARS.sub(_escape_char, str(text))


def audit_log(caller: Caller, *, workspace: str, action: str, detail: str) -> None:
    """The one app log line per Databricks call: who, where, what. Never a token.

    ``workspace``, ``action`` and ``detail`` can be steered by the user or the model, so
    control characters are escaped (nothing can forge a second line) and ``detail`` is
    cut to :data:`MAX_AUDIT_DETAIL_CHARS` (escapes counted), ending in "…" when cut.
    """

    detail = _log_safe(detail)
    if len(detail) > MAX_AUDIT_DETAIL_CHARS:
        detail = detail[:MAX_AUDIT_DETAIL_CHARS] + "…"
    logger.info(
        "databricks_call identity=%s oid=%s workspace=%s action=%s detail=%s",
        caller.identity,
        caller.object_id or "-",
        _log_safe(workspace),
        _log_safe(action),
        detail,
    )


def access_error_payload() -> str:
    """Tool result when :func:`run_caller` raises ``DatabricksAccessError``."""

    return json.dumps({"status": "error", "message": SIGN_IN_MESSAGE})


def client_error_payload() -> str:
    """Tool result when building the client fails for a reason other than access
    (e.g. the credential or the workspace is unreachable)."""

    return json.dumps({"status": "error", "message": CLIENT_UNAVAILABLE_MESSAGE})
