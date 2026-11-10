"""Tests for the async Statement Execution wrapper (ported from ``test_statements.py``).

A fake ``statement_execution`` API stands in for Databricks. No network.
Runs standalone (``python test_databricks_sql_statements.py``).
"""

from __future__ import annotations

import asyncio
import contextlib

from databricks.sdk.errors import (
    BadRequest,
    InvalidParameterValue,
    NotFound,
    PermissionDenied,
    Unauthenticated,
)
from databricks.sdk.service.sql import (
    ColumnInfo,
    Disposition,
    Format,
    ResultData,
    ResultManifest,
    ResultSchema,
    ServiceError,
    ServiceErrorCode,
    StatementParameterListItem,
    StatementResponse,
    StatementState,
    StatementStatus,
)
from databricks_test_support import LogCapture, captured_logs, run_tests

from v1.core.tools.databricks.sql.statements import StatementClient, caller_tags
from v1.utils.databricks_client import Caller


def response(state, *, rows=None, columns=("n",), truncated=False, error=None, error_code=None):
    service_error = ServiceError(message=error, error_code=error_code) if error else None
    status = StatementStatus(state=state, error=service_error)
    if state != StatementState.SUCCEEDED:
        return StatementResponse(statement_id="s1", status=status)
    return StatementResponse(
        statement_id="s1",
        status=status,
        manifest=ResultManifest(
            schema=ResultSchema(columns=[ColumnInfo(name=c) for c in columns]), truncated=truncated
        ),
        result=ResultData(data_array=[["1"]] if rows is None else rows),
    )


class FakeStatementAPI:
    def __init__(self, first=None, *polls, execute_error=None, first_execute_error=None):
        self.first = first
        self.polls = list(polls)
        self.execute_error = execute_error
        self.first_execute_error = first_execute_error
        self.executed = []
        self.cancelled = []

    def execute_statement(self, **kwargs):
        self.executed.append(kwargs)
        if self.first_execute_error and len(self.executed) == 1:
            raise self.first_execute_error
        if self.execute_error:
            raise self.execute_error
        return self.first

    def get_statement(self, statement_id):
        return self.polls.pop(0)

    def cancel_execution(self, statement_id):
        self.cancelled.append(statement_id)


class FakeWorkspace:
    def __init__(self, api):
        self.statement_execution = api


async def no_sleep(_seconds):
    return None


def run(
    api,
    sql="SELECT 1",
    *,
    max_rows=5,
    clock=lambda: 0.0,
    sleep=no_sleep,
    query_tags=None,
    **kwargs,
):
    """``execute`` on a StatementClient over ``api`` with a 10 s timeout and no real waits."""
    client = StatementClient(
        FakeWorkspace(api),
        "wh-1",
        timeout_s=10,
        poll_interval_s=0,
        sleep=sleep,
        clock=clock,
        query_tags=query_tags,
    )
    return asyncio.run(client.execute(sql, max_rows=max_rows, **kwargs))


def test_immediate_success() -> None:
    api = FakeStatementAPI(response(StatementState.SUCCEEDED, rows=[["7"]]))
    result = run(api, "SELECT 7 AS n", max_rows=100)
    assert (result.status, result.columns, result.rows, result.truncated) == (
        "completed",
        ["n"],
        [["7"]],
        False,
    )
    call = api.executed[0]
    assert call["statement"] == "SELECT 7 AS n"
    assert call["warehouse_id"] == "wh-1"
    assert call["wait_timeout"] == "0s"
    assert call["row_limit"] == 100
    assert call["disposition"] == Disposition.INLINE
    assert call["format"] == Format.JSON_ARRAY
    assert call["parameters"] is None


def test_parameters_are_bound() -> None:
    api = FakeStatementAPI(response(StatementState.SUCCEEDED))
    run(api, "SELECT :k", parameters={"k": "%taxi%"})
    assert api.executed[0]["parameters"] == [StatementParameterListItem(name="k", value="%taxi%")]


def test_polls_until_done_and_reports_each_state_once() -> None:
    api = FakeStatementAPI(
        response(StatementState.PENDING),
        response(StatementState.PENDING),
        response(StatementState.RUNNING),
        response(StatementState.SUCCEEDED),
    )
    states = []
    result = run(api, on_state=states.append)
    assert result.status == "completed"
    assert states == [StatementState.PENDING, StatementState.RUNNING]


def test_truncation_comes_from_the_manifest() -> None:
    api = FakeStatementAPI(response(StatementState.SUCCEEDED, truncated=True))
    assert run(api, max_rows=1).truncated is True


def test_failed_statements_are_classified() -> None:
    cases = [
        (
            "[TABLE_OR_VIEW_NOT_FOUND] The table or view `a`.`b`.`c` cannot be found.",
            "permission_or_not_found",
        ),
        (
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: no USE CATALOG",
            "permission_or_not_found",
        ),
        (
            "PERMISSION_DENIED: User does not have USE CATALOG on Catalog 'x'.",
            "permission_or_not_found",
        ),
        ("[SCHEMA_NOT_FOUND] The schema `a`.`b` cannot be found.", "permission_or_not_found"),
        ("[UNRESOLVED_COLUMN.WITH_SUGGESTION] `nope` cannot be resolved.", "failed"),
    ]
    for message, status in cases:
        api = FakeStatementAPI(response(StatementState.FAILED, error=message))
        result = run(api)
        assert (result.status, result.message) == (status, message), (message, result)


def test_long_error_messages_are_capped() -> None:
    api = FakeStatementAPI(response(StatementState.FAILED, error="x" * 5000))
    assert len(run(api).message) == 1000


def test_api_errors_on_execute() -> None:
    error = NotFound("warehouse wh-1 not found")
    result = run(FakeStatementAPI(execute_error=error))
    assert (result.status, result.message) == ("failed", str(error)), result


# 403 (no CAN USE on the warehouse, no workspace entitlement) and 401 (the app's token is
# refused by the workspace): one status, and none of Databricks' text is carried on.
ACCESS_ERRORS = (
    PermissionDenied("SDK-TEXT no CAN USE on warehouse"),
    Unauthenticated("SDK-TEXT invalid access token"),
)


def test_access_errors_on_execute_are_permission_denied_without_sdk_text() -> None:
    for error in ACCESS_ERRORS:
        result = run(FakeStatementAPI(execute_error=error))
        assert result.status == "permission_denied", (error, result)
        assert "SDK-TEXT" not in (result.message or ""), (error, result)


def test_access_errors_while_polling_are_permission_denied_and_cancel() -> None:
    for error in ACCESS_ERRORS:

        class PollDenied(FakeStatementAPI):
            def get_statement(self, statement_id, error=error):
                raise error

        api = PollDenied(response(StatementState.RUNNING))
        result = run(api)
        assert result.status == "permission_denied", (error, result)
        assert "SDK-TEXT" not in (result.message or ""), (error, result)
        assert api.cancelled == ["s1"], error


def test_unauthenticated_failed_statement_is_permission_denied() -> None:
    api = FakeStatementAPI(
        response(
            StatementState.FAILED,
            error="SDK-TEXT credential rejected",
            error_code=ServiceErrorCode.UNAUTHENTICATED,
        )
    )
    result = run(api)
    assert result.status == "permission_denied", result
    assert "SDK-TEXT" not in (result.message or ""), result


def test_timeout_cancels_the_statement() -> None:
    times = iter([0.0, 5.0, 11.0])
    api = FakeStatementAPI(response(StatementState.RUNNING), response(StatementState.RUNNING))
    result = run(api, clock=lambda: next(times))
    assert result.status == "timeout"
    assert api.cancelled == ["s1"]


def test_cancelled_task_cancels_statement() -> None:
    async def disconnected(_seconds):
        raise asyncio.CancelledError

    api = FakeStatementAPI(response(StatementState.RUNNING))
    try:
        run(api, sleep=disconnected)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("execute did not propagate CancelledError")
    assert api.cancelled == ["s1"]


def test_cancel_after_disconnect_runs_off_the_event_loop() -> None:
    """A slow cancel call must not block the loop that serves every other request."""

    import threading

    threads = {}

    async def disconnected(_seconds):
        threads["loop"] = threading.get_ident()
        raise asyncio.CancelledError

    class RecordsThread(FakeStatementAPI):
        def cancel_execution(self, statement_id):
            threads["cancel"] = threading.get_ident()
            super().cancel_execution(statement_id)

    api = RecordsThread(response(StatementState.RUNNING))
    with contextlib.suppress(asyncio.CancelledError):
        run(api, sleep=disconnected)
    assert api.cancelled == ["s1"]  # asyncio.run waits for the executor before returning
    assert threads["cancel"] != threads["loop"]


def test_network_errors_fail_without_raising() -> None:
    """The SDK raises TimeoutError / requests.ConnectionError once its retries run out."""

    import requests

    secret = "https://adb-1.azuredatabricks.net/?token=dapi-secret"
    for error in (TimeoutError(secret), requests.ConnectionError(secret), ValueError(secret)):
        api = FakeStatementAPI(execute_error=error)
        with _captured_logs() as logs:
            result = run(api)
        assert result.status == "failed", (error, result)
        assert result.message and "dapi-secret" not in result.message
        captured = logs.getvalue()
        assert type(error).__name__ in captured and "dapi-secret" not in captured, captured


def test_network_error_while_polling_fails_and_cancels() -> None:
    import requests

    class PollFails(FakeStatementAPI):
        def get_statement(self, statement_id):
            raise requests.ConnectionError("reset by peer")

    api = PollFails(response(StatementState.RUNNING))
    result = run(api)
    assert result.status == "failed" and "reset by peer" not in (result.message or "")
    assert api.cancelled == ["s1"]


def test_cancel_failure_never_masks_the_timeout() -> None:
    import requests

    class CancelFails(FakeStatementAPI):
        def cancel_execution(self, statement_id):
            raise requests.ConnectionError("down")

    times = iter([0.0, 11.0])
    api = CancelFails(response(StatementState.RUNNING))
    result = run(api, clock=lambda: next(times))
    assert result.status == "timeout"


TAGS = {"app": "langgraph-agent", "user_oid": "o1"}


def test_tags_sent_with_statement() -> None:
    api = FakeStatementAPI(response(StatementState.SUCCEEDED))
    run(api, query_tags=TAGS)
    assert [(t.key, t.value) for t in api.executed[0]["query_tags"]] == [
        ("app", "langgraph-agent"),
        ("user_oid", "o1"),
    ]


def test_no_tags_sends_none() -> None:
    api = FakeStatementAPI(response(StatementState.SUCCEEDED))
    run(api)
    assert api.executed[0]["query_tags"] is None


def test_rejected_tags_retry_once_untagged() -> None:
    for error in (
        InvalidParameterValue("Unknown field query_tags"),
        BadRequest("Invalid QUERY_TAG value"),
    ):
        api = FakeStatementAPI(
            response(StatementState.PENDING),
            response(StatementState.SUCCEEDED),
            first_execute_error=error,
        )
        with _captured_logs() as logs:
            result = run(api, "SELECT :k", parameters={"k": "v"}, query_tags=TAGS)
        assert result.status == "completed", (error, result)
        assert len(api.executed) == 2
        first, second = api.executed
        assert second["query_tags"] is None
        assert {k: v for k, v in first.items() if k != "query_tags"} == {
            k: v for k, v in second.items() if k != "query_tags"
        }
        assert "databricks_query_tags_rejected warehouse=wh-1" in logs.getvalue()
        assert TAGS["user_oid"] not in logs.getvalue()


def test_rejected_retry_happens_at_most_once() -> None:
    error = InvalidParameterValue("Unknown field query_tags")
    api = FakeStatementAPI(execute_error=error)
    result = run(api, query_tags=TAGS)
    assert (result.status, result.message) == ("failed", str(error))
    assert len(api.executed) == 2


def test_tag_error_without_tags_not_retried() -> None:
    api = FakeStatementAPI(execute_error=InvalidParameterValue("Unknown field query_tags"))
    assert run(api).status == "failed"
    assert len(api.executed) == 1


def test_other_bad_request_not_retried() -> None:
    api = FakeStatementAPI(execute_error=BadRequest("warehouse is stopped"))
    result = run(api, query_tags=TAGS)
    assert result.status == "failed" and len(api.executed) == 1


def test_permission_denied_not_retried() -> None:
    api = FakeStatementAPI(execute_error=PermissionDenied("no CAN USE"))
    assert run(api, query_tags=TAGS).status == "permission_denied"
    assert len(api.executed) == 1


def test_caller_tags_omit_missing_oid() -> None:
    assert "user_oid" not in caller_tags(Caller("user:x", None), workspace="dev", source="agent")
    assert caller_tags(Caller("user:x", "o1"), workspace="dev", source="api") == {
        "app": "langgraph-agent",
        "source": "api",
        "workspace": "dev",
        "user_oid": "o1",
    }


def _captured_logs() -> contextlib.AbstractContextManager[LogCapture]:
    return captured_logs("v1.core.tools.databricks.sql.statements")


if __name__ == "__main__":
    raise SystemExit(run_tests(globals()))
