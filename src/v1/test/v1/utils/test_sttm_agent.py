"""Tests for the sttm-agent: its tools and its place in the agent.

The session engine is tested in ``test_sttm_engine.py``. These drive the layers
around it:

- the tools, called the way the agent calls them (a ``ToolRuntime`` over the
  sttm-agent's state, with the analyst's message as the sttm-agent read it from
  the conversation and the turn's run id in its config): a moderate session
  from the analyst's first message to approval and export, the review modes,
  approval by the signed-in user, new versions, what each tool says without the
  analyst's message or an id for it, which source each part of an update may
  come from, that only the analyst's message is recorded, not the task, that a
  PDF the analyst attaches is read like a pasted message, that a source keeps
  the session it has, that a review is accepted only in a later turn, that no
  STTM call runs after a session change in the same step, that of the STTM
  tasks sent in one step only the first to change a session may change any,
  which session a call works on, and what a call that changes nothing returns;
- the sttm-agent's input (:func:`prepare`): the task, the analyst's latest
  message read in full, their earlier messages and its previous answer, and
  the message reaching the tools of the graph it runs;
- the workbook tool: the files it keeps for the signed-in analyst and links in
  its answer, the layout it keeps for a session and its later versions (an
  approved session's too, which it leaves as it was), the steps it refuses by
  number, the same-step and same-task rules for a layout, and why it offers no
  workbook;
- registration: the setting, the roster (a fork whose graph has the stack of a
  declarative subagent), the sessions and layouts channels, the shipped
  access-control files (the sttm-agent is for INT only), the rule files the
  prompt lists and the safety classifier's STTM paragraph;
- the REAL agent (``_build_agent_sync``) around the stand-in model: an INT caller
  reaches the sttm-agent, which reads the analyst's message from the
  conversation for its tools to record; of two STTM tasks sent in one step only
  one changes the sessions; and an EXT caller is never offered the sttm-agent
  and cannot call it.

Runs standalone (``python test_sttm_agent.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import deepagents.middleware.subagents as deepagents_subagents
from deepagents.middleware.subagents import _FORK_TASK_PREAMBLE
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from openpyxl import Workbook, load_workbook
from openpyxl.utils import column_index_from_string
from pydantic import Field
from test_access_control import (
    _ADF,
    _REPO_ROOT,
    _SERVICENOW,
    _STTM,
    _TEAM_A,
    _TEAM_B,
    _agent,
    _calls,
    _delegate,
    _policy,
    _RecordingStandIn,
)
from test_patch_tool_calls import _user
from test_sttm_documents import _block, _pdf, _tsv
from test_sttm_engine import _INSTRUCTIONS, _SPEC, _inputs, _rule_files
from test_sttm_workbook import (
    _column,
    _Down,
    _fills,
    _logged,
    _template_files,
    _templates,
    _Untouchable,
)

import v1.core.tools.sttm.tools as tools_mod
import v1.core.tools.sttm.workbook as workbook_mod
from v1.core.config import Settings
from v1.core.middlewares.access_control import TASK_TOOL_NAME, load_access_policy
from v1.core.middlewares.azure_links import AzureLinksMiddleware
from v1.core.middlewares.safety import SAFETY_CLASSIFIER_SYSTEM_PROMPT
from v1.core.prompts.sttm import (
    STTM_SUBAGENT_DESCRIPTION,
    STTM_SUBAGENT_PROMPT,
    sttm_subagent_prompt,
)
from v1.core.subagents import ALL_SUBAGENTS, SERVICENOW_SUBAGENT, STTM_SUBAGENT
from v1.core.subagents.sttm import runnable as runnable_mod
from v1.core.subagents.sttm.runnable import (
    MAX_ANSWER_CHARS,
    MAX_EARLIER_CHARS,
    STTM_RUNNABLE,
    prepare,
)
from v1.core.tools.sttm import STTM_TOOL_NAMES, STTM_TOOLS
from v1.core.tools.sttm.files import KEEP, NAMESPACE, download_url
from v1.core.tools.sttm.rules import AUTHORING_DIR, AUTHORING_MOUNT, EXPECTED_RULE_FILES
from v1.core.tools.sttm.session import Session, timestamp
from v1.core.tools.sttm.state import merge_by_revision, merge_sessions
from v1.core.tools.sttm.tools import (
    MESSAGE_KEY,
    SESSION_CHANGING_TOOLS,
    sttm_advance,
    sttm_record_input,
    sttm_show,
    sttm_start_session,
    sttm_update,
    sttm_workbook,
)
from v1.core.tools.sttm.workbook import MEDIA_TYPE

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _first_message(words: str = "") -> str:
    """The analyst's first message: the request, the instructions, then the table on line 3."""

    return f"Start an STTM for CoreBank, daily accounts feed.{words}\n{_INSTRUCTIONS}\n{_SPEC}"


_M1 = _first_message(" Use moderate review.")
_M2 = (
    "CMDB is APP-1234, pipe delimited, no derived fields, no join keys. Carry every attribute "
    "forward."
)
_NO_MESSAGE = (
    "Not done: The analyst's message did not reach this tool, so nothing can be recorded. This "
    "is a configuration problem: tell the analyst, and do not retry."
)
_NO_TURN = (
    "Not done: This run has no run id to tell the analyst's messages apart, so nothing can be "
    "recorded. This is a configuration problem: tell the analyst, and do not retry."
)
_CLAIMED = (
    "Not done: Another STTM task sent at the same time is changing the sessions, and this task "
    "works on the copy from before it, so nothing it changes could be kept. Nothing was "
    "changed. Answer only that this task has to be sent again, on its own."
)
_TABLE_READ = (
    "- Table at line 3: 3 attribute(s); columns name = 'Field Name', data_type = 'Data Type', "
    "length = 'Length', required = 'Required', description = 'Description', key = 'Key'"
)


# -- the tools ---------------------------------------------------------------------


def _fork(content: str | list, task: str = "") -> list[HumanMessage]:
    """A fork's messages: the analyst's message ``content``, then the orchestrator's task."""

    return [HumanMessage(content=content), HumanMessage(content=_FORK_TASK_PREAMBLE + task)]


class _Conversation:
    """One conversation's state, with the tools called on it the way the agent calls them.

    Each analyst message is a turn: the sttm-agent's input prepared from it
    (:func:`prepare`), its tools called with the message it read, under the
    turn's id. The clock moves on a minute with each call, so the session
    changed last is always the one a call last changed. ``store`` is the
    platform's store the tools see (the workbook tool keeps its files there); by
    default there is none. Every call runs in the thread ``thread-1``.
    """

    def __init__(self, store: Any = None) -> None:
        self.state: dict[str, Any] = {
            "messages": [],
            "sttm_sessions": {},
            "sttm_layouts": {},
            "files": {},
        }
        self.store = store
        self.thread_id = "thread-1"
        self.turn: str | None = None
        # The analyst's message the tools are handed; none until the first turn.
        self.message: dict[str, Any] | None = None
        self.calls = 0
        # Whether the last call answered with a Command (a state update).
        self.updated = False

    def say(self, turn: str, content: str | list, task: str = "") -> None:
        """The analyst's message ``content`` in the turn ``turn``, with the task ``task``."""

        self.turn = turn
        inner, self.message = prepare({"messages": _fork(content, task)})
        self.state["messages"] = inner["messages"]

    def call(
        self,
        tool: Any,
        *,
        call_id: str | None = None,
        task: tuple[str, str] | None = None,
        **args: Any,
    ) -> str:
        """The tool's answer. A Command's updates go into the state, as the graph applies them.

        ``task``: the orchestrator's step and the STTM task it sent, as (step's
        checkpoint id, task id), named in the config as LangGraph names them; by
        default the call is made outside a graph.
        """

        self.calls += 1
        call_id = call_id or f"call_{self.calls}"
        at = NOW + timedelta(minutes=self.calls)
        config: dict[str, Any] = {"metadata": {"run_id": self.turn}} if self.turn else {}
        config["configurable"] = {"thread_id": self.thread_id}
        if self.message is not None:
            config["configurable"][MESSAGE_KEY] = self.message
        if task:
            step, task_id = task
            config["configurable"].update(
                checkpoint_ns=f"tools:{task_id}|tools:{call_id}",
                checkpoint_map={"": step, f"tools:{task_id}": f"{step}.{task_id}"},
            )
        runtime = ToolRuntime(
            state=self.state,
            context=None,
            config=config,
            stream_writer=lambda _: None,
            tool_call_id=call_id,
            store=self.store,
        )
        saved = tools_mod._now
        tools_mod._now = lambda: at
        try:
            if tool.coroutine is not None:
                out = asyncio.run(tool.coroutine(runtime, **args))
            else:
                out = tool.func(runtime, **args)
        finally:
            tools_mod._now = saved
        self.updated = isinstance(out, Command)
        if isinstance(out, str):
            return out
        update = out.update
        (message,) = update["messages"]
        assert message.tool_call_id == call_id
        self.state["sttm_sessions"] = merge_sessions(
            self.state["sttm_sessions"], update.get("sttm_sessions")
        )
        self.state["sttm_layouts"] = merge_by_revision(
            self.state["sttm_layouts"], update.get("sttm_layouts")
        )
        self.state["files"].update(update.get("files") or {})
        return message.content

    def session(self, session_id: str | None = None) -> Session:
        """The session ``session_id``; by default the only one."""

        sessions = self.state["sttm_sessions"]
        if session_id is None:
            (session_id,) = sessions
        return Session.model_validate(sessions[session_id])


def _cell(table: str, attribute: str, column: str, value: str | None) -> dict[str, Any]:
    return {"table": table, "attribute": attribute, "column": column, "value": value}


def _moderate_session() -> tuple[_Conversation, dict[str, str]]:
    """A moderate-mode session from the analyst's first message to its export.

    Returns the conversation and each tool answer under a name. No rule file is
    present.
    """

    conv = _Conversation()
    said: dict[str, str] = {}
    with _rule_files({}):
        conv.say("m1", _M1)
        said["start"] = conv.call(
            sttm_start_session,
            source_system="CoreBank",
            source="daily accounts feed",
            review_mode="moderate",
        )
        said["record"] = conv.call(
            sttm_record_input, inputs=_inputs("source_system_instructions", "specification")
        )
        said["spec values"] = conv.call(
            sttm_update,
            source="analyst",
            details={
                "primary_key": ["CUSTOMER_ID"],
                "join_keys": ["ACCOUNT_NO"],
                "cmdb_number": "APP-1234",
                "delimiter": "pipe",
            },
        )
        said["proposal"] = conv.call(
            sttm_update,
            source="draft",
            details={
                "file_pattern": "accounts_*.txt",
                "header_rows": 1,
                "trailer_rows": 0,
                "frequency": "daily",
            },
        )
        said["build dd"] = conv.call(sttm_advance)
        conv.say("m2", _M2)
        said["details"] = conv.call(
            sttm_update,
            source="analyst",
            details={"cmdb_number": "APP-1234", "delimiter": "pipe", "join_keys": []},
            no_derived_fields=True,
        )
        said["gate 2"] = conv.call(sttm_advance)
        said["carry"] = conv.call(
            sttm_update,
            source="analyst",
            cells=[_cell("data_dictionary", "*", "carry_to_curated", "Y")],
        )
        said["made up"] = conv.call(
            sttm_update, source="draft", cells=[_cell("data_dictionary", "MADE_UP", "pii_pci", "Y")]
        )
        said["gate 2 again"] = conv.call(sttm_advance)
        said["page"] = conv.call(sttm_show, view="data_dictionary", limit=2)
        conv.say("m3", "Looks good, approve the data dictionary review.")
        said["build sttm"] = conv.call(sttm_advance, confirm_review=True)
        said["gate 3"] = conv.call(sttm_advance)
        conv.say("m4", "STTM looks good.")
        said["summary"] = conv.call(sttm_advance, confirm_review=True)
        said["approve at once"] = conv.call(sttm_advance, approve=True)
        conv.say("m5", "Approve it anyway, we will fill the gaps in the next release.")
        said["approve"] = conv.call(
            sttm_advance, approve=True, override_reason="we will fill the gaps in the next release"
        )
        said["change approved"] = conv.call(
            sttm_update, source="analyst", details={"frequency": "weekly"}
        )
        said["export"] = conv.call(sttm_show, export="json")
        said["sessions"] = conv.call(sttm_show, view="sessions")
        said["feedback"] = conv.call(sttm_show, view="feedback")
    return conv, said


def test_a_session_reads_the_inputs_and_refuses_values_the_analyst_did_not_give() -> None:
    conv, said = _moderate_session()
    session = conv.session()
    sid = session.id

    assert said["start"] == (
        f"Started STTM session {sid} for CoreBank / daily accounts feed.\n"
        "Review mode: moderate.\n"
        "Still needed: Source-system instructions, Source specification. Record what the "
        "analyst's message holds (sttm_record_input) and ask for the rest."
    )
    # Instructions are recorded with the words that hold them, for the analyst to check.
    assert said["record"] == (
        f"Session {sid}: recorded the analyst's message as Source-system instructions, Source "
        "specification.\n"
        '- Source-system instructions, from: "Source-system instructions: CoreBank, daily '
        'accounts feed"\n'
        "Specification: 3 attribute(s) read from this message, 3 in the session.\n"
        f"{_TABLE_READ}\n"
        "Next: set the file details and derived fields the analyst gave (sttm_update), then "
        "sttm_advance."
    )
    # The specification's table is not the analyst's word for a key or a file detail. The
    # key it gives, sent again, is left as it gave it: nothing to refuse or ask.
    assert said["spec values"] == (
        f"Session {sid}: nothing changed. Refused (3):\n"
        "- Join keys: 'ACCOUNT_NO' is not in the analyst's messages to this session. Ask the "
        "analyst.\n"
        "- CMDB application number: 'APP-1234' is not in the analyst's messages to this "
        "session. Ask the analyst.\n"
        "- Delimiter: 'pipe' is not in the analyst's messages to this session. Ask the analyst."
    )
    assert said["proposal"] == (
        f"Session {sid}: nothing changed. Refused (1):\n"
        "- Nothing was proposed: proposals are confirmed at the input review, which this "
        "review mode skips. Ask the analyst for these values."
    )
    # Moderate mode skips the input review, so what is open goes to the analyst now.
    assert said["build dd"].startswith(
        f"Session {sid}: built the Data Dictionary.\n"
        "The input review is skipped in moderate mode, so tell the analyst these are still "
        "open:\n- Join keys: not given.\n- CMDB application number: not given.\n"
    )
    assert (
        "- Data Dictionary: 3 row(s); required, not decided yet: classification (3 row(s)), "
        "pii_pci (3 row(s)), carry_to_curated (3 row(s))"
    ) in said["build dd"]
    assert "no values from rule files (none is present)" in said["build dd"]
    assert said["details"] == (
        f"Session {sid}: 4 change(s):\n"
        "- file_details · Join keys · value: (none) → none (analyst)\n"
        "- file_details · CMDB application number · value: (none) → APP-1234 (analyst)\n"
        "- file_details · Delimiter · value: (none) → pipe (analyst)\n"
        "- derived_fields · (all) · none declared: (none) → yes"
    )
    # No row is ever added for an attribute the inputs do not have.
    assert said["made up"] == (
        f"Session {sid}: nothing changed. Refused (1):\n"
        "- Data Dictionary, MADE_UP, pii_pci: 'MADE_UP' is not a row of the Data Dictionary: "
        "it is not in the specification, the derived fields or the audit fields, and "
        "attributes are never added here."
    )
    assert [attribute.name for attribute in session.attributes] == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
        "OPEN_DATE",
    ]
    # What the session was built from is the analyst's messages, verbatim.
    assert session.messages["m1"] == _M1 and session.messages["m2"] == _M2
    assert {record.message_id for record in session.inputs} == {"m1"}


def test_a_moderate_session_stops_at_the_data_dictionary_and_sttm_reviews() -> None:
    conv, said = _moderate_session()
    session = conv.session()
    sid = session.id

    assert said["gate 2"].startswith(
        f"Session {sid}: the Data Dictionary review (gate 2) is open. Show the analyst the "
        "Data Dictionary, below,"
    )
    assert (
        "| CUSTOMER_ID | Specification | Y | VARCHAR | 20 | Y | Customer identifier | ? | ? "
        "| REVIEW | ? | ? | ? | ? |"
    ) in said["gate 2"]
    assert said["carry"] == (
        f"Session {sid}: 1 change(s):\n"
        "- Data Dictionary · * (table default, now used by 3 row(s)) · carry_to_curated: "
        "(none) → Y"
    )
    # The review opened in this message; only the analyst's answer can close it.
    assert said["gate 2 again"] == (
        f"Session {sid}: the Data Dictionary review (gate 2) is still open. It opened in this "
        "same analyst message; the analyst has to answer it first."
    )
    page = said["page"]
    assert "Rows 1-2 of 3." in page and "| OPEN_DATE |" not in page
    assert "| ACCOUNT_NO | Specification | ? | VARCHAR | 16 | Y | Account number |" in page
    assert "More: sttm_show(view='data_dictionary', offset=2, limit=2)." in page
    assert (
        "Table defaults (used where a row has no value of its own): Carry to curated = Y [a*]"
        in (page)
    )
    assert said["build sttm"].startswith(
        f"Session {sid}: built the Raw-to-Curated and Curated-to-ASL STTMs.\n"
        "- Raw-to-Curated STTM: 3 row(s); required, not decided yet: raw_path (3 row(s)),"
    )
    # The optional columns only the analyst or a rule file fills are named too.
    assert "; optional, still empty: logic_type\n" in said["build sttm"]
    assert "; optional, still empty: constraints, report_usage\n" in said["build sttm"]
    assert said["gate 3"].startswith(
        f"Session {sid}: the STTM review (gate 3) is open. Show the analyst the "
        "Raw-to-Curated and Curated-to-ASL STTMs, below,"
    )
    summary = said["summary"]
    assert summary.startswith(f"Session {sid}: every review is done. Show the analyst")
    assert "- Review mode: moderate (skips: Input review (gate 1))" in summary
    # Validation names the failing checks and, for each, the failing attributes.
    assert (
        "- Validation: 5 of 10 checks pass; failing: carry_forward_in_r2c (3), "
        "pii_classified (3), types_follow_mapping (9), file_details_complete (4), "
        "required_values (3)"
    ) in summary
    assert (
        "- FAIL Every attribute has a PII/PCI flag and a classification (pii_classified)\n"
        "  - CUSTOMER_ID: pii_pci, classification\n"
        "  - ACCOUNT_NO: pii_pci, classification\n"
        "  - OPEN_DATE: pii_pci, classification"
    ) in summary
    assert {gate: state.status for gate, state in session.gates.items()} == {
        "input_review": "skipped",
        "dd_review": "confirmed",
        "sttm_review": "confirmed",
    }


def test_a_session_is_approved_in_a_later_message_and_then_read_only() -> None:
    conv, said = _moderate_session()
    session = conv.session()
    sid = session.id
    approval = session.approval
    assert approval is not None

    assert said["approve at once"] == (
        "Not done: The final summary was produced in this same analyst message; the analyst "
        "has to see it and approve in a later message."
    )
    # No signed-in user outside a run, so no approver is named.
    assert said["approve"] == (
        f"Session {sid} (version 1) is approved at {approval.at}, and is now read-only.\n"
        "Approved over failed checks (carry_forward_in_r2c, pii_classified, "
        "types_follow_mapping, file_details_complete, required_values), for the analyst's "
        'reason: "we will fill the gaps in the next release".\n'
        f"Changing it takes a new version: sttm_start_session(new_version_of='{sid}'). "
        "sttm_workbook builds its workbooks for the analyst to download; "
        "sttm_show(export='json') writes it to files."
    )
    assert approval.message_id == "m5" and approval.by is None
    assert said["change approved"] == (
        f"Not done: Session {sid} (version 1) is approved and read-only. To change it, start a "
        f"new version: sttm_start_session(new_version_of='{sid}')."
    )
    assert said["sessions"] == (
        "### STTM sessions in this conversation\n"
        f"- {sid}: CoreBank / daily accounts feed, version 1, approved at {approval.at}, "
        f"read-only, updated {session.updated_at}"
    )
    feedback = said["feedback"]
    assert feedback.startswith(f"### Changes (session {sid}), newest first\n")
    assert feedback.index("message m2: Values (analyst): 1 changed") < feedback.index(
        "message m1: Inputs: 2 changed"
    )


def test_an_export_writes_the_session_to_the_conversations_files() -> None:
    conv, said = _moderate_session()
    sid = conv.session().id
    stem = f"/sttm/exports/{sid}-v1"

    assert said["export"] == (
        f"Exported session {sid} (version 1, approved) to the conversation's files:\n"
        f"- {stem}.json: The whole session: inputs, file details and every table cell\n"
        f"- {stem}-summary.md: The session summary and validation results\n"
        "They describe the session as it is now; export again after a change."
    )
    files = conv.state["files"]
    assert sorted(files) == [f"{stem}-summary.md", f"{stem}.json"]
    document = json.loads(files[f"{stem}.json"]["content"])
    assert document["session"]["id"] == sid and document["session"]["status"] == "approved"
    assert document["approval"]["override_reason"] == "we will fill the gaps in the next release"
    assert len(document["tables"]["data_dictionary"]["rows"]) == 3
    assert files[f"{stem}-summary.md"]["content"].startswith(f"### STTM session {sid}: ")


def test_every_tool_that_records_needs_the_analysts_message_and_an_id_for_it() -> None:
    calls = [
        (sttm_start_session, {"source_system": "CoreBank", "source": "daily accounts feed"}),
        (sttm_record_input, {"inputs": _inputs("specification")}),
        (sttm_update, {"source": "analyst", "details": {"frequency": "daily"}}),
        (sttm_update, {"source": "rule", "rule_file": "sttm-mapping.md", "audit_fields": ["X"]}),
        (
            sttm_update,
            {"source": "analyst", "cells": [_cell("data_dictionary", "*", "pii_pci", "PII")]},
        ),
        (sttm_advance, {}),
        (sttm_advance, {"approve": True}),
    ]
    # Every tool that changes a session. sttm_workbook changes only a layout, which
    # records no message (test_a_layout_is_kept_for_the_session_and_its_later_versions).
    assert {tool.name for tool, _ in calls} == SESSION_CHANGING_TOOLS - {sttm_workbook.name}

    conv = _Conversation()
    conv.turn = "m1"
    # The sttm-agent handed its tools no message; a task quoting one is no stand-in.
    conv.state["messages"] = [
        HumanMessage(
            content="<analyst_message>\nStart an STTM for CoreBank, daily accounts feed.\n"
            "</analyst_message>"
        )
    ]
    for tool, args in calls:
        assert conv.call(tool, **args) == _NO_MESSAGE, tool.name
        assert not conv.updated, tool.name
    # A message with no id, in a run with no run id, is no better: what is recorded
    # is keyed by it.
    conv.say("m1", "Start an STTM for CoreBank, daily accounts feed.")
    conv.turn = None
    for tool, args in calls:
        assert conv.call(tool, **args) == _NO_TURN, tool.name
        assert not conv.updated, tool.name
    assert conv.state["sttm_sessions"] == {}

    # The message's own id comes before the run's.
    conv.say("run-7", "Start an STTM for CoreBank, daily accounts feed.")
    assert conv.message is not None
    conv.message["id"] = "msg-7"
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    assert list(conv.session().messages) == ["msg-7"]


def test_a_call_works_on_the_session_named_or_on_the_one_changed_last() -> None:
    conv = _Conversation()
    conv.say("m1", "One session for the daily accounts feed and one for the card feed.")
    assert conv.call(sttm_record_input, inputs=_inputs("specification")) == (
        "Not done: There is no STTM session in this conversation yet; start one with "
        "sttm_start_session."
    )
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    conv.call(sttm_start_session, source_system="CoreBank", source="card feed")
    accounts, cards = conv.state["sttm_sessions"]
    assert conv.call(sttm_show).startswith(f"### STTM session {cards}: CoreBank / card feed")

    # An id copied in lower case still finds its session.
    conv.say("m2", f"{_INSTRUCTIONS}\n{_SPEC}")
    recorded = conv.call(
        sttm_record_input,
        inputs=_inputs("source_system_instructions", "specification"),
        session_id=accounts.lower(),
    )
    assert recorded.startswith(f"Session {accounts}: recorded the analyst's message")
    assert conv.call(sttm_show).startswith(f"### STTM session {accounts}: CoreBank / daily")
    assert len(conv.session(accounts).attributes) == 3
    assert conv.session(cards).attributes == []

    assert conv.call(sttm_show, session_id="20261103-ZZZZ") == (
        f"Not done: No STTM session '20261103-ZZZZ' in this conversation (sessions: {accounts}, "
        f"{cards})."
    )


def test_a_call_that_changes_nothing_leaves_the_state_alone() -> None:
    conv = _Conversation()
    conv.say("m1", _first_message())
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
    sid = conv.session().id
    before = json.dumps(conv.state["sttm_sessions"], sort_keys=True)

    assert conv.call(
        sttm_record_input, inputs=_inputs("source_system_instructions", "specification")
    ) == (
        f"Session {sid}: this message is already recorded as Source-system instructions, "
        "Source specification; nothing changed."
    )
    assert not conv.updated
    # An input refused next to them is reported, with why.
    assert conv.call(
        sttm_record_input,
        inputs=[*_inputs("specification"), {"kind": "derived_fields", "passage": "none"}],
    ) == (
        f"Session {sid}: this message is already recorded as Source specification; nothing "
        "changed.\n"
        "Not recorded: Derived-field definitions.\n"
        "- The passage for Derived-field definitions is missing, under 4 words, or not in the "
        "analyst's message (for instructions and derived fields, outside the specification's "
        "tables). Give each input but a specification with words of the message that hold it, "
        "copied exactly. If the message does not hold that input, do not record it: ask the "
        "analyst for it.\n"
        "- When the analyst says there are no derived fields, that is "
        "sttm_update(no_derived_fields=True), not an input."
    )
    assert not conv.updated
    assert conv.call(sttm_update, source="analyst") == "Not done: nothing was given to set."
    assert not conv.updated
    # The analyst named a daily feed, not a weekly file. With the input review
    # still ahead, the value may be proposed for the analyst to confirm there.
    assert conv.call(sttm_update, source="analyst", details={"frequency": "weekly"}) == (
        f"Session {sid}: nothing changed. Refused (1):\n"
        "- Frequency: 'weekly' is not in the analyst's messages to this session. Ask the "
        "analyst, or propose it (source='draft') for the analyst to confirm at the input review."
    )
    assert not conv.updated
    assert conv.call(sttm_show, view="validation").startswith(f"### Validation (session {sid})")
    assert not conv.updated
    assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == before


def test_a_review_mode_that_skips_reviews_has_to_be_named_by_the_analyst() -> None:
    conv = _Conversation()
    conv.say("m1", _first_message())
    started = conv.call(
        sttm_start_session,
        source_system="CoreBank",
        source="daily accounts feed",
        review_mode="fast",
    )
    sid = conv.session().id
    assert started == (
        f"Started STTM session {sid} for CoreBank / daily accounts feed.\n"
        "- Review mode not set: The fast review mode skips reviews, so the analyst has to ask "
        "for it by name; ask which mode they want.\n"
        "Review mode: none chosen, so every review stops.\n"
        "Still needed: Source-system instructions, Source specification. Record what the "
        "analyst's message holds (sttm_record_input) and ask for the rest."
    )
    assert conv.session().review_mode is None

    conv.say("m2", "Use fast mode.")
    assert conv.call(sttm_update, source="analyst", review_mode="fast") == (
        f"Session {sid}: 1 change(s):\n- session · review mode · value: default → fast"
    )


def test_every_review_stops_by_default_and_extensive_offers_every_row() -> None:
    gate_1 = {}
    for mode, words in (("default", ""), ("extensive", " Use extensive review.")):
        conv = _Conversation()
        with _rule_files({}):
            conv.say("m1", _first_message(words))
            conv.call(
                sttm_start_session,
                source_system="CoreBank",
                source="daily accounts feed",
                review_mode=None if mode == "default" else mode,
            )
            conv.call(
                sttm_record_input, inputs=_inputs("source_system_instructions", "specification")
            )
            gate_1[mode] = (conv.session().id, conv.call(sttm_advance))

    for mode, (sid, text) in gate_1.items():
        assert text.startswith(
            f"Session {sid}: the Input review (gate 1) is open. Show the analyst what was "
            "understood from their inputs, below, with the open questions, and ask them to "
            "confirm it or correct it."
        ), mode
        assert "| CUSTOMER_ID | 5 | VARCHAR | 20 | Y | PK | Customer identifier |" in text, mode
    assert gate_1["extensive"][1].endswith(
        "\n\nExtensive review: offer to list every row, not just this page (sttm_show with "
        "offset), so the analyst can review each field."
    )
    assert "Extensive review" not in gate_1["default"][1]


def _fast_session_at_summary() -> _Conversation:
    """A fast-mode session at its final summary, with validation failing."""

    conv = _Conversation()
    conv.say("m1", _first_message(" Use fast mode."))
    conv.call(
        sttm_start_session,
        source_system="CoreBank",
        source="daily accounts feed",
        review_mode="fast",
    )
    conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
    for _ in range(3):  # Data Dictionary, both STTMs, the final summary
        last = conv.call(sttm_advance)
    assert last.startswith(f"Session {conv.session().id}: every review is done."), last
    return conv


@contextmanager
def _signed_in(**fields: str) -> Iterator[None]:
    saved = tools_mod.caller_field
    tools_mod.caller_field = lambda key: fields.get(key)
    try:
        yield
    finally:
        tools_mod.caller_field = saved


def test_the_approver_is_the_signed_in_user_and_the_reason_is_the_analysts() -> None:
    conv = _fast_session_at_summary()
    sid = conv.session().id
    conv.say("m2", "Approve it anyway, the gaps are tracked elsewhere.")
    assert conv.call(sttm_advance, approve=True) == (
        "Not done: Validation failed (carry_forward_in_r2c, pii_classified, "
        "types_follow_mapping, file_details_complete, required_values). Approving anyway needs "
        "the analyst's reason, in their own words (override_reason)."
    )
    assert conv.call(
        sttm_advance, approve=True, override_reason="the analyst accepts the risk"
    ) == (
        "Not done: The override reason has to be the analyst's own words from the approving "
        "message; quote it, do not write one."
    )
    with _signed_in(name="Dana Analyst", email="dana@example.com"):
        approved = conv.call(
            sttm_advance, approve=True, override_reason="the gaps are tracked elsewhere"
        )

    approval = conv.session().approval
    assert approval is not None
    assert approved.startswith(
        f"Session {sid} (version 1) is approved by Dana Analyst (dana@example.com) at "
        f"{approval.at}, and is now read-only."
    )
    assert approval.by == "Dana Analyst (dana@example.com)"
    assert approval.message == "Approve it anyway, the gaps are tracked elsewhere."

    with _signed_in(email="dana@example.com"):
        assert tools_mod._approver() == "dana@example.com"
    with _signed_in(name="Dana Analyst"):
        assert tools_mod._approver() == "Dana Analyst"
    with _signed_in():
        assert tools_mod._approver() is None


def test_a_new_version_copies_an_approved_session_and_leaves_it_as_it_was() -> None:
    conv = _fast_session_at_summary()
    old = conv.session().id
    conv.say("m2", "Approve it anyway, the gaps are tracked elsewhere.")
    conv.call(sttm_advance, approve=True, override_reason="the gaps are tracked elsewhere")
    approved = conv.state["sttm_sessions"][old]

    conv.say("m3", "Start a new version; the file arrives weekly.")
    started = conv.call(sttm_start_session, new_version_of=old.lower())
    (new,) = set(conv.state["sttm_sessions"]) - {old}
    assert started == (
        f"Started session {new}: version 2 of CoreBank / daily accounts feed, a copy of "
        f"approved session {old}, which stays as it was. Make the analyst's changes, then "
        "sttm_advance to the final summary for approval.\n"
        "Review mode: fast."
    )
    assert conv.call(sttm_update, source="analyst", details={"frequency": "weekly"}) == (
        f"Session {new}: 1 change(s):\n- file_details · Frequency · value: (none) → weekly "
        "(analyst)"
    )
    assert conv.state["sttm_sessions"][old] == approved
    version_2 = conv.session(new)
    assert version_2.version == 2 and version_2.supersedes == old
    assert version_2.approval is None and version_2.status == "in_progress"
    assert [attribute.name for attribute in version_2.attributes] == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
        "OPEN_DATE",
    ]
    assert f"- {new}: CoreBank / daily accounts feed, version 2, in progress" in conv.call(
        sttm_show, view="sessions"
    )
    assert conv.call(sttm_start_session, new_version_of=new) == (
        f"Not done: Session {new} is not approved, so it can still be changed as it is; a new "
        "version is only for an approved session."
    )


def test_a_source_keeps_its_session_until_the_analyst_asks_for_a_new_one() -> None:
    conv = _Conversation()
    conv.say("m1", _first_message())
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
    (first,) = conv.state["sttm_sessions"]

    # A later message about the same source carries on with its session.
    conv.say("m2", "The daily accounts feed is pipe delimited.")
    assert conv.call(
        sttm_start_session, source_system="CoreBank", source="Daily Accounts Feed"
    ) == (
        f"Not done: CoreBank / Daily Accounts Feed already has session(s) {first}, not approved "
        f"yet, so no new session was started. Carry on with session {first}: record this "
        f"message's inputs and changes with session_id={first}. Only if the analyst asked for a "
        "new session, call again with new_session=true."
    )
    assert not conv.updated and list(conv.state["sttm_sessions"]) == [first]

    # Asked for, the new one takes the instructions but not the table.
    conv.say("m3", "Start another session for the same feed.")
    started = conv.call(
        sttm_start_session,
        source_system="CoreBank",
        source="Daily Accounts Feed",
        new_session=True,
    )
    (second,) = set(conv.state["sttm_sessions"]) - {first}
    assert started == (
        f"Started STTM session {second} for CoreBank / Daily Accounts Feed.\n"
        f"- Source-system instructions: carried over from session {first}\n"
        f"- Session(s) {first} for the same source are not approved yet; the analyst asked for a "
        "new one.\n"
        "Review mode: none chosen, so every review stops.\n"
        "Still needed: Source specification. Record what the analyst's message holds "
        "(sttm_record_input) and ask for the rest."
    )
    session = conv.session(second)
    assert session.attributes == []
    assert _INSTRUCTIONS in session.messages["m1"]
    assert "CUSTOMER_ID" not in session.messages["m1"]
    # The instructions keep the words they were recorded from.
    (carried,) = session.inputs
    assert carried.passage == _INSTRUCTIONS.removesuffix(".") and carried.inherited_from == first


def test_a_message_with_no_text_is_not_recorded() -> None:
    conv = _Conversation()
    conv.say("m0", "Start an STTM for CoreBank, daily accounts feed.")
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    # The analyst sent only a screenshot of the specification, which cannot be read.
    screenshot = {
        "type": "image",
        "base64": "aGk=",
        "mime_type": "image/png",
        "extras": {"filename": "spec.png"},
    }
    conv.say("m1", [screenshot], task="Record the specification the analyst sent.")
    unread = "spec.png was not read: images cannot be read. Ask for its content as text."
    # The sttm-agent is told why, and so is a tool asked to record it.
    assert conv.state["messages"][0].text.endswith(f"<notes>\n- {unread}\n</notes>")
    assert conv.call(sttm_record_input, inputs=_inputs("specification")) == (
        "Not done: The analyst's message has no text to record. Ask the analyst to paste the "
        "content as text or attach it as a PDF with selectable text; a specification as a table "
        f"with a header row, pipe- or tab-separated. {unread}"
    )
    assert conv.session().attributes == []


# _SPEC's rows of cells, for a PDF.
_SPEC_ROWS = [
    [cell.strip() for cell in line.strip("|").split("|")]
    for line in _SPEC.splitlines()
    if not line.startswith("|---")
]


def test_a_pdf_the_analyst_attaches_is_read_like_a_pasted_message() -> None:
    def recorded(content: str | list) -> tuple[_Conversation, str]:
        conv = _Conversation()
        conv.say("m1", content)
        conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
        answer = conv.call(
            sttm_record_input, inputs=_inputs("source_system_instructions", "specification")
        )
        return conv, answer

    def attributes(conv: _Conversation) -> list[dict[str, Any]]:
        return [attribute.model_dump(exclude={"line"}) for attribute in conv.session().attributes]

    pasted, _ = recorded(_first_message())
    request = {"type": "text", "text": "Start an STTM for CoreBank, daily accounts feed."}
    # The source-system instructions and the specification are in the PDF only.
    pdf = _pdf([_INSTRUCTIONS, "Specification:", _SPEC_ROWS])
    conv, answer = recorded([request, _block(pdf, "accounts spec.pdf")])
    assert len(attributes(conv)) == 3 and attributes(conv) == attributes(pasted)
    assert '- Source-system instructions, from: "Source-system instructions: CoreBank, ' in answer
    instructions, _ = conv.session().inputs
    assert instructions.passage == _INSTRUCTIONS.removesuffix(".")
    # The sttm-agent reads the PDF with the message.
    assert conv.state["messages"][0].text.endswith(
        "<analyst_message>\nStart an STTM for CoreBank, daily accounts feed.\n\n"
        '<document name="accounts spec.pdf" pages="1">\n'
        f"{_INSTRUCTIONS}\nSpecification:\n{_tsv(_SPEC_ROWS)}\n</document>\n</analyst_message>"
    )

    # A table without lines between its columns is not guessed at; the instructions with it
    # are recorded all the same.
    pdf = _pdf([_INSTRUCTIONS, _SPEC_ROWS], ruled=False)
    conv, answer = recorded([request, _block(pdf, "accounts spec.pdf")])
    assert (
        "Not recorded: Source specification.\n"
        "- The specification's table in accounts spec.pdf has no lines between its columns "
        "(line 5), so they cannot be told apart. Ask the analyst to paste the table with each "
        "row on its own line and its cells separated by tabs or pipes: copied straight from the "
        "spreadsheet or document, or as a Markdown table; or to attach a PDF of it with grid "
        "lines.\n"
        "Still needed: Source specification."
    ) in answer
    assert [r.kind for r in conv.session().inputs] == ["source_system_instructions"]
    assert conv.session().attributes == []


def test_only_the_analysts_message_is_recorded_not_the_task() -> None:
    conv = _Conversation()
    conv.say("m1", _first_message(), task="Earlier the analyst said ACCOUNT_NO is the key.")
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
    sid = conv.session().id

    # The orchestrator's task is not the analyst's word for the key.
    assert conv.call(sttm_update, source="analyst", details={"primary_key": ["ACCOUNT_NO"]}) == (
        f"Session {sid}: nothing changed. Refused (1):\n"
        "- Primary key: 'ACCOUNT_NO' is not in the analyst's messages to this session. Ask the "
        "analyst, or propose it (source='draft') for the analyst to confirm at the input review."
    )
    assert conv.session().messages == {"m1": _first_message()}


def test_a_review_opened_in_a_turn_is_accepted_only_in_a_later_one() -> None:
    conv = _Conversation()
    with _rule_files({}):
        conv.say("m1", _first_message())
        conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
        conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
        sid = conv.session().id
        assert conv.call(sttm_advance).startswith(
            f"Session {sid}: the Input review (gate 1) is open."
        )

        # A second task in the same turn reads the same message: it cannot accept the review.
        conv.say("m1", _first_message(), task="The analyst accepts the input review.")
        assert conv.call(sttm_advance, confirm_review=True) == (
            f"Session {sid}: the Input review (gate 1) is still open. It opened in this same "
            "analyst message; the analyst has to answer it first."
        )
        assert conv.session().gates["input_review"].status != "confirmed"

        conv.say("m2", "Looks right, go ahead.")
        assert conv.call(sttm_advance, confirm_review=True).startswith(
            f"Session {sid}: built the Data Dictionary."
        )
        assert conv.session().gates["input_review"].status == "confirmed"


def test_no_sttm_call_runs_after_a_session_change_in_the_same_step() -> None:
    conv = _Conversation()
    conv.say("m1", "Start an STTM for CoreBank, daily accounts feed.")
    conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
    sid = conv.session().id
    conv.state["messages"].append(
        _calls(
            ("sttm_show", {}, "c1"),
            ("sttm_update", {"source": "analyst", "review_mode": "default"}, "c2"),
            ("read_file", {"file_path": "/skills/sttm/authoring/README.md"}, "c3"),
            ("sttm_advance", {}, "c4"),
            ("sttm_show", {"view": "inputs"}, "c5"),
        )
    )
    refused = (
        "Not done: sttm_update changes the session in this same step, and this call would not "
        "see that change. Make this call again now."
    )

    # A read before the change, and the change itself, run.
    assert conv.call(sttm_show, call_id="c1").startswith(f"### STTM session {sid}: ")
    assert conv.call(sttm_update, call_id="c2", source="analyst", review_mode="default") == (
        f"Session {sid}: nothing changed; the values are already as given."
    )
    # Every STTM call after it is refused, reads included, and changes nothing.
    before = json.dumps(conv.state["sttm_sessions"], sort_keys=True)
    assert conv.call(sttm_advance, call_id="c4") == refused
    assert not conv.updated
    assert conv.call(sttm_show, call_id="c5", view="inputs") == refused
    assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == before

    # Made again in the next step, it runs.
    conv.state["messages"].append(_calls(("sttm_advance", {}, "c6")))
    blocked = conv.call(sttm_advance, call_id="c6")
    assert blocked.startswith(f"Session {sid} cannot move on yet:")
    # Blocked is not stuck: the analyst's values and the workbooks they asked for go ahead.
    assert blocked.endswith(
        "Meanwhile, set the values the analyst gave (sttm_update). If they asked for the "
        "workbooks, build them now (sttm_workbook): what is missing does not stop a build."
    )


def test_of_the_sttm_tasks_sent_in_one_step_only_the_first_to_change_a_session_may() -> None:
    tools_mod._CLAIMS.clear()
    conv = _Conversation()
    with _rule_files({}):
        conv.say("m1", _first_message())
        # Tasks a and b were sent together, in the orchestrator's step s1; a changes first.
        assert conv.call(
            sttm_start_session,
            task=("s1", "a"),
            source_system="CoreBank",
            source="daily accounts feed",
        ).startswith("Started STTM session ")
        sid = conv.session().id
        before = json.dumps(conv.state["sttm_sessions"], sort_keys=True)

        for tool, args in (
            (sttm_start_session, {"source_system": "CoreBank", "source": "card feed"}),
            (sttm_record_input, {"inputs": _inputs("source_system_instructions", "specification")}),
            (sttm_update, {"source": "analyst", "review_mode": "default"}),
            (sttm_advance, {}),
        ):
            assert conv.call(tool, task=("s1", "b"), **args) == _CLAIMED, tool.name
            assert not conv.updated, tool.name
        assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == before
        # b may still read, and a goes on.
        assert conv.call(sttm_show, task=("s1", "b")).startswith(f"### STTM session {sid}: ")
        assert conv.call(
            sttm_record_input,
            task=("s1", "a"),
            inputs=_inputs("source_system_instructions", "specification"),
        ).startswith(f"Session {sid}: recorded the analyst's message")
        # Sent again, in a later step, b runs; a call made outside a graph always does.
        assert conv.call(sttm_advance, task=("s2", "b")).startswith(
            f"Session {sid}: the Input review (gate 1) is open."
        )
        assert conv.call(sttm_show, view="sessions").startswith("### STTM sessions")

    # Only the latest steps' claims are kept.
    saved = tools_mod.MAX_CLAIMS
    tools_mod.MAX_CLAIMS = 2
    try:
        for step in ("s3", "s4", "s5"):
            conv.call(sttm_update, task=(step, "a"), source="analyst", review_mode="default")
    finally:
        tools_mod.MAX_CLAIMS = saved
    assert list(tools_mod._CLAIMS) == ["s4", "s5"]
    tools_mod._CLAIMS.clear()


def test_an_update_takes_each_value_only_from_a_source_it_may_come_from() -> None:
    conv = _Conversation()
    with _rule_files({"sttm-mapping.md": "Audit fields: LOAD_TS, BATCH_ID."}):
        conv.say("m1", _first_message(" No derived fields."))
        conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
        conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
        sid = conv.session().id

        # Each part is checked on its own; nothing here may come from a rule file.
        assert conv.call(
            sttm_update,
            source="rule",
            rule_file="sttm-mapping.md",
            review_mode="extensive",
            details={"frequency": "daily"},
            no_derived_fields=True,
        ) == (
            f"Session {sid}: nothing changed. Refused (3):\n"
            "- Review mode: only the analyst chooses it (source='analyst').\n"
            "- File details: not from a rule file. They are the analyst's (source='analyst'), or "
            "proposed for the analyst to confirm (source='draft').\n"
            "- Derived fields: only the analyst declares them (source='analyst')."
        )
        assert not conv.updated
        # What passes changes; what does not is refused, in one answer.
        assert conv.call(
            sttm_update,
            source="analyst",
            details={"frequency": "daily"},
            no_derived_fields=True,
            audit_fields=["LOAD_TS"],
        ) == (
            f"Session {sid}: 2 change(s):\n"
            "- file_details · Frequency · value: (none) → daily (analyst)\n"
            "- derived_fields · (all) · none declared: (none) → yes\n"
            "Refused (1); these did not change:\n"
            "- Audit fields: they come from a rule file (source='rule', with rule_file)."
        )
        assert conv.call(
            sttm_update, source="rule", rule_file="sttm-mapping.md", audit_fields=["LOAD_TS"]
        ) == (f"Session {sid}: 1 change(s):\n- audit_fields · (all) · names: (none) → LOAD_TS")

    feedback = conv.call(sttm_show, view="feedback")
    assert "message m1: File details, derived fields, audit fields: 2 changed, 1 refused" in (
        feedback
    )
    assert "message m1: Audit fields: 1 changed" in feedback


def test_the_sessions_reducer_keeps_the_highest_revision() -> None:
    existing = {"S1": {"revision": 3, "value": "kept"}, "S2": {"revision": 1, "value": "old"}}
    merged = merge_sessions(
        existing,
        {
            "S1": {"revision": 2, "value": "a stale copy"},
            "S2": {"revision": 1, "value": "the newer write"},
            "S3": {"revision": 0, "value": "new"},
        },
    )
    assert merged == {
        "S1": {"revision": 3, "value": "kept"},
        "S2": {"revision": 1, "value": "the newer write"},
        "S3": {"revision": 0, "value": "new"},
    }
    assert merge_sessions(existing, None) == existing and merge_sessions(None, None) == {}


# -- the sttm-agent's input --------------------------------------------------------


def _prepared(*messages: Any, task: str = "A task.") -> tuple[str, dict[str, Any]]:
    """The sttm-agent's input from a fork of ``messages`` with ``task``, and its message."""

    inner, message = prepare({"messages": [*messages, HumanMessage(_FORK_TASK_PREAMBLE + task)]})
    (prepared,) = inner["messages"]
    return prepared.text, message


def test_the_sttm_agent_reads_the_analysts_latest_message_in_full() -> None:
    latest = f"{_INSTRUCTIONS}\n{_SPEC}"
    sessions = {"S1": {"revision": 0}}
    inner, message = prepare(
        {
            "messages": [
                HumanMessage("Start an STTM for CoreBank, daily accounts feed.", id="h1"),
                _calls(_sttm_task("Start the session.", "call_1")),
                ToolMessage(
                    "Session S1 is started. Paste the specification.", tool_call_id="call_1"
                ),
                AIMessage("The session is started. Please paste the specification."),
                HumanMessage(latest, id="h2"),
                HumanMessage(_FORK_TASK_PREAMBLE + "Record the inputs."),
            ],
            "sttm_sessions": sessions,
        }
    )
    # The tools get the message, under its id; the model gets it with what led to it.
    assert message == {"id": "h2", "text": latest, "notes": []}
    (prepared,) = inner.pop("messages")
    assert inner == {"sttm_sessions": sessions}
    assert prepared.text == (
        "<task>\nRecord the inputs.\n</task>\n\n"
        "<earlier_messages>\n<message>\nStart an STTM for CoreBank, daily accounts feed.\n"
        "</message>\n</earlier_messages>\n\n"
        "<previous_answer>\nSession S1 is started. Paste the specification.\n</previous_answer>\n\n"
        f"<analyst_message>\n{latest}\n</analyst_message>"
    )

    # With no message from the analyst there is nothing to record, and nothing to keep it under.
    text, message = _prepared(task="")
    assert message == {"id": "", "text": "", "notes": []}
    assert text == "<task>\n\n</task>\n\n<analyst_message>\n\n</analyst_message>"
    assert prepare({"messages": []})[1] == message
    assert _prepared(HumanMessage("Hi."))[1] == {"id": "", "text": "Hi.", "notes": []}


def test_the_analysts_earlier_messages_are_the_most_recent_that_fit_without_tables() -> None:
    pdf = {
        "type": "file",
        "base64": "JVBERi0=",
        "mime_type": "application/pdf",
        "extras": {"filename": "accounts spec.pdf"},
    }
    # Two fit, the third does not.
    words = "word " * (MAX_EARLIER_CHARS // 12)
    summary = HumanMessage(
        "A summary of the conversation so far.", additional_kwargs={"lc_source": "summarization"}
    )
    text, message = _prepared(
        summary,
        HumanMessage(f"oldest {words}"),
        HumanMessage(f"older {words}"),
        HumanMessage(f"old {words}"),
        # Only a table: left out.
        HumanMessage(_SPEC),
        HumanMessage([{"type": "text", "text": f"The spec, and its PDF:\n{_SPEC}"}, pdf]),
        HumanMessage("Approve it.", id="h7"),
    )
    assert message["id"] == "h7" and message["text"] == "Approve it."
    earlier = text.split("<earlier_messages>\n", 1)[1].split("\n</earlier_messages>", 1)[0]
    assert earlier == "\n".join(
        [
            "(older messages left out)",
            f"<message>\nolder {words.strip()}\n</message>",
            f"<message>\nold {words.strip()}\n</message>",
            "<message>\nThe spec, and its PDF:\n[attached: accounts spec.pdf]\n</message>",
        ]
    )
    assert "summary" not in text and "CUSTOMER_ID" not in text


def test_the_previous_answer_is_the_end_of_the_sttm_agents_last_one() -> None:
    ending = "The session is ready for review."
    long_answer = "x" * MAX_ANSWER_CHARS + ending
    servicenow = (TASK_TOOL_NAME, {"description": "Find tickets.", "subagent_type": _SERVICENOW})
    text, _ = _prepared(
        HumanMessage("Start it."),
        _calls(_sttm_task("Start it.", "call_1")),
        ToolMessage("The first answer.", tool_call_id="call_1"),
        _calls(_sttm_task("Go on.", "call_2"), (*servicenow, "call_3")),
        ToolMessage(long_answer, tool_call_id="call_2"),
        ToolMessage("The ServiceNow agent's answer.", tool_call_id="call_3"),
        HumanMessage("Go on."),
    )
    previous = text.split("<previous_answer>\n", 1)[1].split("\n</previous_answer>", 1)[0]
    assert previous == "…" + long_answer[-MAX_ANSWER_CHARS:] and previous.endswith(ending)
    # None yet: no part.
    assert "<previous_answer>" not in _prepared(HumanMessage("Start it."))[0]


def test_the_sttm_agent_hands_its_graph_the_input_and_its_tools_the_message() -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def graph(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        seen.append((state, dict(config["configurable"])))
        return {"messages": [*state["messages"], AIMessage("Done.")]}

    fork = {
        "messages": [
            HumanMessage("Approve it.", id="h9"),
            HumanMessage(_FORK_TASK_PREAMBLE + "Go."),
        ]
    }
    config: RunnableConfig = {"configurable": {"thread_id": "thread-1"}}
    saved = STTM_RUNNABLE.graph
    try:
        STTM_RUNNABLE.graph = None
        try:
            STTM_RUNNABLE.invoke(fork, config)
        except RuntimeError as exc:
            assert str(exc) == "The sttm-agent's graph is not installed: build the agent first."
        else:
            raise AssertionError("ran without a graph")
        runnable_mod.install(RunnableLambda(graph))
        answers = [
            STTM_RUNNABLE.invoke(fork, config),
            asyncio.run(STTM_RUNNABLE.ainvoke(fork, config)),
        ]
    finally:
        STTM_RUNNABLE.graph = saved

    inner, message = prepare(fork)
    assert message == {"id": "h9", "text": "Approve it.", "notes": []}
    assert [state for state, _ in seen] == [inner, inner]
    assert [configurable for _, configurable in seen] == 2 * [
        {"thread_id": "thread-1", MESSAGE_KEY: message}
    ]
    assert [answer["messages"][-1].text for answer in answers] == ["Done.", "Done."]
    assert config == {"configurable": {"thread_id": "thread-1"}}


# -- the workbook tool -------------------------------------------------------------


_NOT_CONFIGURED = (
    "Not done: this run has no signed-in user or no store to keep files in, so no workbook can "
    "be offered for download. This is a configuration problem: tell the analyst, and do not "
    "retry."
)
_DD_COLUMNS = (
    "Attribute, Origin, PK, Data type, Length, Required, Technical description, Business "
    "description, Classification, PII/PCI, Handling, Carry to curated, Derivation logic, Join key"
)
_EACH_LINK = "Each link works until "
_COLUMNS_HEAD = "For your layout steps, not for the analyst: the columns of each mapping sheet."
_SHOW_LAYOUT = (
    "Show the analyst the layout below, numbered as here, so they can check how their request "
    "was read."
)
_LINK = re.compile(
    r"\[(Data Dictionary|STTM) workbook\]\(/api/sttm/files/([A-Z]{16}-(?:data-dictionary|sttm))\)"
)
_HIDE_LENGTH = {"op": "hide_columns", "sheet": "dd", "columns": ["Length"]}
_ORANGE = {
    "op": "highlight",
    "sheet": "dd",
    "columns": ["PII/PCI"],
    "match": "empty",
    "color": "orange",
}
_SORT = {"op": "sort", "sheet": "dd", "columns": ["Attribute"], "descending": True}
_NO_SUCH_COLUMN = {"op": "hide_columns", "sheet": "dd", "columns": ["Nope"]}


@contextmanager
def _analyst(templates: Mapping[str, bytes] | None = None) -> Iterator[None]:
    """Calls by the signed-in analyst ``user:a``, with ``templates`` in the deployment
    (none by default)."""

    with _signed_in(identity="user:a"), _templates(templates or {}):
        yield


def _recorded(store: Any = None) -> _Conversation:
    """A session started from the analyst's first message, which is recorded."""

    conv = _Conversation(store)
    with _rule_files({}):
        conv.say("m1", _first_message())
        conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
        conv.call(sttm_record_input, inputs=_inputs("source_system_instructions", "specification"))
    return conv


def _kept(store: Any, owner: str = "user:a") -> dict[str, dict[str, Any]]:
    """The files kept for ``owner``, by id."""

    return {item.key: item.value for item in store.search((NAMESPACE, owner), limit=100)}


def _links(answer: str) -> dict[str, str]:
    """The file id each workbook link of ``answer`` names, by workbook."""

    return dict(_LINK.findall(answer))


def _layout_part(answer: str) -> list[str]:
    """The lines of a workbook answer between its links and its column lists."""

    lines = answer.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith(_EACH_LINK)) + 1
    return lines[start : lines.index(_COLUMNS_HEAD)]


def _opened(store: Any, answer: str, workbook: str = "Data Dictionary") -> Workbook:
    """The workbook ``answer`` links, as the analyst ``user:a`` downloads it."""

    item = store.get((NAMESPACE, "user:a"), _links(answer)[workbook])
    return load_workbook(io.BytesIO(base64.b64decode(item.value["content"])))


def test_the_workbooks_are_kept_for_the_signed_in_analyst_and_linked_in_the_answer() -> None:
    store = InMemoryStore()
    conv = _recorded(store)
    sid = conv.session().id
    sessions = json.dumps(conv.state["sttm_sessions"], sort_keys=True)
    with _analyst():
        built = conv.call(sttm_workbook)
    at = NOW + timedelta(minutes=conv.calls)
    links = _links(built)
    # The two files share the build's id, and each link ends as its label begins: one copied
    # with the other's id still opens its own file.
    build_id = links["STTM"].removesuffix("-sttm")
    assert links == {"Data Dictionary": f"{build_id}-data-dictionary", "STTM": f"{build_id}-sttm"}
    assert built == (
        f"Session {sid} (version 1, not approved yet): built both workbooks from the session as "
        "it is now.\n"
        f"- [Data Dictionary workbook]({download_url(links['Data Dictionary'])}), laid out from "
        "the session's own columns, because the template data-dictionary.xlsx is not in this "
        "deployment. Sheets: Data Dictionary, About. Not decided yet: 9 cell(s), filled red.\n"
        f"- [STTM workbook]({download_url(links['STTM'])}), laid out from the session's own "
        "columns, because the template sttm.xlsx is not in this deployment. Sheets: "
        "Raw-to-Curated, Curated-to-ASL, About. Not decided yet: 51 cell(s), filled red.\n"
        "Each link works until 2026-11-04 12:03 UTC, for the signed-in analyst only. Give the "
        "analyst the links exactly as written here, each in its [label](path) form (the chat "
        "cannot open a bare path), and until when they work. The files hold the session as it "
        "is now: build them again after a change.\n"
        "Layout: the default, with no layout steps.\n"
        f"{_COLUMNS_HEAD}\n"
        f"- Data Dictionary: {_DD_COLUMNS}\n"
        "- Raw-to-Curated: Attribute, Raw path, Raw file, INTG path, INTG table, INTG column, "
        "INTG type, Curated path, Curated table, Curated column, Curated type, Transformation "
        "rule, Logic type, PII/PCI\n"
        "- Curated-to-ASL: Attribute, Curated table, Curated column, ASL table, ASL column, ASL "
        "type, Business attribute name, Constraints, Logic, Sensitivity, Report usage"
    )
    # A build changes neither the session nor its layout, and its bytes stay out of the state.
    assert not conv.updated
    assert conv.state["sttm_layouts"] == {} and conv.state["files"] == {}
    assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == sessions

    kept = _kept(store)
    assert set(kept) == set(links.values())
    stem = f"sttm-daily-accounts-feed-{sid}-v1"
    for workbook, name, sheets in (
        ("Data Dictionary", f"{stem}-data-dictionary.xlsx", ["Data Dictionary", "About"]),
        ("STTM", f"{stem}-sttm.xlsx", ["Raw-to-Curated", "Curated-to-ASL", "About"]),
    ):
        value = kept[links[workbook]]
        content = base64.b64decode(value["content"])
        assert value == {
            "owner": "user:a",
            "thread_id": "thread-1",
            "session_id": sid,
            "version": 1,
            "name": name,
            "media_type": MEDIA_TYPE,
            "size": len(content),
            "content": value["content"],
            "created_at": timestamp(at),
            "expires_at": timestamp(at + KEEP),
        }, workbook
        assert load_workbook(io.BytesIO(content)).sheetnames == sheets, workbook
    assert _kept(store, "user:b") == {}

    # Once the deployment has the organization's templates, the next build uses them.
    with _analyst(_template_files()):
        again = conv.call(sttm_workbook)
    assert re.search(
        r"^- \[Data Dictionary workbook\]\(/api/sttm/files/[A-Z]{16}-data-dictionary\), from the "
        r"organization's "
        r"template\. Sheets: Instructions, File Details, Data Dictionary, Values, About\.",
        again,
        re.MULTILINE,
    ), again
    assert len(_kept(store)) == 4


def test_workbooks_built_before_the_inputs_are_in_say_what_is_missing() -> None:
    conv = _Conversation(InMemoryStore())
    with _rule_files({}):
        conv.say("m1", _first_message())
        conv.call(sttm_start_session, source_system="CoreBank", source="daily accounts feed")
        conv.call(sttm_record_input, inputs=_inputs("specification"))
    with _analyst():
        lines = conv.call(sttm_workbook).splitlines()
    missing = (
        "Not complete yet (tell the analyst, with the links): Source-system instructions: not "
        "given yet."
    )
    # Right after the links, for the analyst to read with them.
    assert lines[lines.index(missing) - 1].startswith(_EACH_LINK)

    with _rule_files({}):
        conv.call(sttm_record_input, inputs=_inputs("source_system_instructions"))
    with _analyst():
        assert "Not complete yet" not in conv.call(sttm_workbook)


def test_a_layout_is_kept_for_the_session_and_its_later_versions() -> None:
    conv = _fast_session_at_summary()
    conv.store = InMemoryStore()
    sid = conv.session().id
    sessions = json.dumps(conv.state["sttm_sessions"], sort_keys=True)
    with _analyst():
        added = conv.call(sttm_workbook, steps=[_HIDE_LENGTH, _ORANGE])
        assert conv.updated
        assert _layout_part(added) == [
            f"Added steps 1 and 2. {_SHOW_LAYOUT}",
            "Layout: 2 step(s), applied to every build of this session and of its later versions:",
            "1. dd: hide Length",
            "2. dd: highlight rows where PII/PCI is empty in orange",
        ]
        assert f"- Data Dictionary: {_DD_COLUMNS} (hidden: Length)" in added.splitlines()
        # Kept in one form, apart from the session, which does not change.
        assert conv.state["sttm_layouts"] == {
            sid: {
                "revision": 1,
                "steps": [_HIDE_LENGTH, {**_ORANGE, "color": "#FFCC99"}],
                "updated_at": timestamp(NOW + timedelta(minutes=conv.calls)),
            }
        }
        assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == sessions

        assert (
            _layout_part(conv.call(sttm_workbook, steps=[_SORT]))[0]
            == f"Added step 3. {_SHOW_LAYOUT}"
        )
        # Numbered as the layout lists them; a number given twice removes one step.
        assert _layout_part(conv.call(sttm_workbook, remove_steps=[1, 1])) == [
            f"Removed step 1. {_SHOW_LAYOUT}",
            "Layout: 2 step(s), applied to every build of this session and of its later versions:",
            "1. dd: highlight rows where PII/PCI is empty in orange",
            "2. dd: sort by Attribute (descending)",
        ]
        assert conv.state["sttm_layouts"][sid]["revision"] == 3
        layouts = copy.deepcopy(conv.state["sttm_layouts"])
        assert conv.call(sttm_workbook, remove_steps=[3]) == (
            "Not done: there is no step 3 to remove: the layout has 2 step(s). Nothing was changed."
        )
        assert not conv.updated

        # Every build applies the kept steps.
        built = conv.call(sttm_workbook)
        assert not conv.updated and conv.state["sttm_layouts"] == layouts
    ws = _opened(conv.store, built)["Data Dictionary"]
    assert _column(ws, "Attribute") == ["OPEN_DATE", "CUSTOMER_ID", "ACCOUNT_NO"]
    # No PII/PCI flag is decided yet, so every row is orange, but for the undecided
    # cells, which stay red.
    cells = {f"{column}{row}" for column in "ABCDEFGHIJKLMN" for row in (2, 3, 4)}
    undecided = {f"{column}{row}" for column in "IJL" for row in (2, 3, 4)}
    assert _fills(ws) == {cell: "FFC7CE" if cell in undecided else "FFCC99" for cell in cells}
    assert conv.call(sttm_show, view="layout") == (
        f"Session {sid}: the workbooks' layout.\n"
        "Layout: 2 step(s), applied to every build of this session and of its later versions:\n"
        "1. dd: highlight rows where PII/PCI is empty in orange\n"
        "2. dd: sort by Attribute (descending)"
    )

    # An approved session is read-only, but not its layout, which changes without
    # touching the session.
    conv.say("m2", "Approve it anyway, the gaps are tracked elsewhere.")
    conv.call(sttm_advance, approve=True, override_reason="the gaps are tracked elsewhere")
    approved = json.dumps(conv.state["sttm_sessions"], sort_keys=True)
    with _analyst():
        header = conv.call(sttm_workbook, steps=[{"op": "header", "color": "grey"}])
    assert header.startswith(f"Session {sid} (version 1, approved): built both workbooks ")
    assert conv.updated and conv.state["sttm_layouts"][sid]["revision"] == 4
    assert json.dumps(conv.state["sttm_sessions"], sort_keys=True) == approved
    approved_layout = copy.deepcopy(conv.state["sttm_layouts"][sid])

    # A new version builds with the layout of the version it copies...
    conv.say("m3", "Start a new version; the file arrives weekly.")
    conv.call(sttm_start_session, new_version_of=sid)
    (new,) = set(conv.state["sttm_sessions"]) - {sid}
    inherited = [
        (
            f"Layout: 3 step(s) from version 1 (session {sid}); the first layout change gives "
            "this version its own copy:"
        ),
        "1. dd: highlight rows where PII/PCI is empty in orange",
        "2. dd: sort by Attribute (descending)",
        "3. header color grey on every mapping sheet",
    ]
    assert conv.call(sttm_show, view="layout").splitlines() == [
        f"Session {new}: the workbooks' layout.",
        *inherited,
    ]
    with _analyst():
        built = conv.call(sttm_workbook)
        assert built.startswith(f"Session {new} (version 2, not approved yet): built both ")
        assert _layout_part(built) == inherited
        assert not conv.updated and new not in conv.state["sttm_layouts"]
        # ...until its first layout change gives it its own copy.
        assert _layout_part(conv.call(sttm_workbook, remove_steps=[2])) == [
            f"Removed step 2. {_SHOW_LAYOUT}",
            "Layout: 2 step(s), applied to every build of this session and of its later versions:",
            "1. dd: highlight rows where PII/PCI is empty in orange",
            "2. header color grey on every mapping sheet",
        ]
        assert conv.state["sttm_layouts"][new]["revision"] == 1
        assert conv.state["sttm_layouts"][sid] == approved_layout
        assert _layout_part(conv.call(sttm_workbook, reset=True)) == [
            f"Dropped the 2 kept step(s). {_SHOW_LAYOUT}",
            "Layout: the default, with no layout steps.",
        ]
        assert conv.state["sttm_layouts"][new] == {
            "revision": 2,
            "steps": [],
            "updated_at": timestamp(NOW + timedelta(minutes=conv.calls)),
        }
        # Its own empty layout is not the earlier version's: a second reset changes nothing.
        assert _layout_part(conv.call(sttm_workbook, reset=True)) == [
            "Layout: the default, with no layout steps."
        ]
        assert not conv.updated
    assert conv.call(sttm_show, view="layout") == (
        f"Session {new}: the workbooks' layout.\nLayout: the default, with no layout steps."
    )


def test_a_layout_step_that_does_not_fit_changes_nothing() -> None:
    store = InMemoryStore()
    conv = _recorded(store)
    no_such_column = (
        "(hide_columns on 'dd'): 'Data Dictionary' has no column 'Nope'. Its columns: "
        f"{_DD_COLUMNS}."
    )
    with _analyst():
        conv.call(sttm_workbook, steps=[_HIDE_LENGTH])
        layouts = copy.deepcopy(conv.state["sttm_layouts"])
        cases = [
            (
                {"steps": [_SORT, {"op": "explode"}]},
                (
                    "new step 2: unknown op 'explode'; use one of: rename_sheet, move_sheet, "
                    "order_columns, hide_columns, show_columns, remove_columns, add_column, "
                    "format_columns, freeze, filter, sort, highlight, header, dropdown. Nothing "
                    "was changed."
                ),
            ),
            (
                {"steps": [_NO_SUCH_COLUMN]},
                f"new step 1 {no_such_column} Nothing was changed.",
            ),
            (
                {"steps": [{"op": "filter", "sheet": "Mapping"}]},
                (
                    "new step 1 (filter on 'Mapping'): there is no sheet 'Mapping'. Sheets: Data "
                    "Dictionary workbook: Data Dictionary, About; STTM workbook: Raw-to-Curated, "
                    "Curated-to-ASL, About. Nothing was changed."
                ),
            ),
            (
                {"steps": [_SORT] * 50},
                (
                    "a layout holds at most 50 steps, and this one would have 51. Nothing was "
                    "changed: remove some (remove_steps) or start over (reset=True)."
                ),
            ),
            (
                {"remove_steps": [0, 2]},
                "there is no step 0, 2 to remove: the layout has 1 step(s). Nothing was changed.",
            ),
        ]
        for args, refusal in cases:
            assert conv.call(sttm_workbook, **args) == f"Not done: {refusal}", args
            assert not conv.updated, args
        assert conv.state["sttm_layouts"] == layouts
        assert len(_kept(store)) == 2

    # A kept step can stop fitting: this one names a column of the organization's
    # template, which the deployment then no longer has.
    steward = {"op": "hide_columns", "sheet": "dd", "columns": ["Steward_Notes"]}
    with _analyst(_template_files()):
        assert (
            _layout_part(conv.call(sttm_workbook, steps=[steward]))[0]
            == f"Added step 2. {_SHOW_LAYOUT}"
        )
    kept_step_2 = (
        "Not done: kept step 2 (hide_columns on 'dd'): 'Data Dictionary' has no column "
        f"'Steward_Notes'. Its columns: {_DD_COLUMNS}. Nothing was changed. Remove that step "
        "(remove_steps=[2]) or start over (reset=True)."
    )
    with _analyst():
        layouts = copy.deepcopy(conv.state["sttm_layouts"])
        assert conv.call(sttm_workbook) == kept_step_2
        # Numbered as the layout lists it, though a step before it is removed in the call...
        assert conv.call(sttm_workbook, remove_steps=[1]) == kept_step_2
        # ...and a new step by its place among the new ones.
        assert conv.call(sttm_workbook, remove_steps=[2], steps=[_SORT, _NO_SUCH_COLUMN]) == (
            f"Not done: new step 2 {no_such_column} Nothing was changed."
        )
        assert not conv.updated and conv.state["sttm_layouts"] == layouts
        assert (
            _layout_part(conv.call(sttm_workbook, remove_steps=[2]))[0]
            == f"Removed step 2. {_SHOW_LAYOUT}"
        )
        assert conv.updated


def test_no_workbook_is_offered_without_a_signed_in_analyst_or_a_place_to_keep_it() -> None:
    conv = _recorded()
    sid = conv.session().id
    with _analyst():
        assert conv.call(sttm_workbook) == _NOT_CONFIGURED
    # Without a signed-in analyst the store is not even read.
    conv.store = _Untouchable()
    with _signed_in(), _templates({}):
        assert conv.call(sttm_workbook, steps=[_HIDE_LENGTH]) == _NOT_CONFIGURED
    assert not conv.updated

    conv.store = _Down()
    with _analyst(), _logged(tools_mod.logger) as logged:
        assert conv.call(sttm_workbook, steps=[_HIDE_LENGTH]) == (
            "Not done: the workbooks were built but could not be kept for download, because of a "
            "problem on our side, so nothing was changed. Tell the analyst, and do not retry now."
        )
    # One line says which build failed, how far it got, and why.
    (line,) = logged
    assert line.startswith(f"Could not keep the workbooks of STTM session {sid} v1 ("), line
    assert "data_dictionary generic (the template data-dictionary.xlsx is not in" in line, line
    assert (
        "): ConnectionError: the store is down at v1/test/v1/utils/test_sttm_workbook.py:" in line
    )
    assert line.endswith(" in abatch"), line
    assert not conv.updated

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("a bug")

    conv.store = InMemoryStore()
    saved = tools_mod.build_workbooks
    tools_mod.build_workbooks = broken
    try:
        with _analyst(), _logged(tools_mod.logger) as logged:
            assert conv.call(sttm_workbook, steps=[_HIDE_LENGTH]) == (
                "Not done: the workbooks could not be built, because of a problem on our side, "
                "so nothing was changed. Tell the analyst, and do not retry."
            )
    finally:
        tools_mod.build_workbooks = saved
    (line,) = logged
    assert line.startswith(f"Could not build the workbooks of STTM session {sid} v1 ("), line
    assert (
        ", with 1 layout step(s): ValueError: a bug at v1/test/v1/utils/test_sttm_agent.py:" in line
    )
    assert line.endswith(" in broken"), line
    assert not conv.updated and _kept(conv.store) == {}
    assert conv.state["sttm_layouts"] == {}


def test_a_failed_build_says_where_it_failed_and_a_good_one_is_logged() -> None:
    conv = _recorded(InMemoryStore())
    sid = conv.session().id

    def unwritable(*args: Any, **kwargs: Any) -> Any:
        return column_index_from_string("not a column")  # raises in openpyxl

    saved = workbook_mod._render
    workbook_mod._render = unwritable
    try:
        with _analyst(), _logged(tools_mod.logger) as logged:
            assert conv.call(sttm_workbook).startswith("Not done: the workbooks could not be built")
    finally:
        workbook_mod._render = saved
    # The library's frame, then ours, then what the build was doing.
    (line,) = logged
    assert line.startswith(f"Could not build the workbooks of STTM session {sid} v1 ("), line
    assert ", with 0 layout step(s): ValueError: " in line, line
    assert " at openpyxl/utils/cell.py:" in line, line
    assert " via v1/test/v1/utils/test_sttm_agent.py:" in line, line
    assert line.endswith(
        " in unwritable, while writing the data_dictionary workbook, laid out from the "
        "session's own columns"
    ), line

    with _analyst(_template_files()), _logged(tools_mod.logger) as logged:
        assert conv.call(sttm_workbook).startswith(f"Session {sid} ")
    (line,) = logged
    assert line.startswith(f"Built the workbooks of STTM session {sid} v1 ("), line
    assert re.search(
        r", with 0 layout step\(s\): data_dictionary from the template, \d+ bytes; "
        r"sttm from the template, \d+ bytes$",
        line,
    ), line


def test_a_workbook_offered_to_no_one_is_logged() -> None:
    conv = _recorded()
    with _analyst(), _logged(tools_mod.logger) as logged:
        assert conv.call(sttm_workbook) == _NOT_CONFIGURED
    with _signed_in(), _logged(tools_mod.logger) as logged_too:
        assert conv.call(sttm_workbook) == _NOT_CONFIGURED
    assert [line.split(": ", 1)[1] for line in logged + logged_too] == [
        "the run has no store",
        "the run has no signed-in user",
    ], (logged, logged_too)


def test_no_sttm_call_runs_after_a_layout_change_in_the_same_step() -> None:
    store = InMemoryStore()
    conv = _recorded(store)
    sid = conv.session().id
    conv.state["messages"].append(
        _calls(
            ("sttm_workbook", {}, "c1"),
            ("sttm_workbook", {"steps": [_HIDE_LENGTH]}, "c2"),
            ("sttm_show", {"view": "layout"}, "c3"),
            ("sttm_workbook", {}, "c4"),
        )
    )
    refused = (
        "Not done: sttm_workbook changes a layout in this same step, and this call would not see "
        "that change. Make this call again now."
    )
    with _analyst():
        # A build that changes no layout does not stop the calls after it.
        assert conv.call(sttm_workbook, call_id="c1").startswith(f"Session {sid} ")
        assert conv.call(sttm_workbook, call_id="c2", steps=[_HIDE_LENGTH]).startswith(
            f"Session {sid} "
        )
        layouts = copy.deepcopy(conv.state["sttm_layouts"])
        # Every STTM call after a layout change is refused, reads included.
        assert conv.call(sttm_show, call_id="c3", view="layout") == refused
        assert conv.call(sttm_workbook, call_id="c4") == refused
        assert not conv.updated and conv.state["sttm_layouts"] == layouts
        assert len(_kept(store)) == 4

        # A build after a session change in the same step would build the session as it was.
        conv.state["messages"].append(
            _calls(
                ("sttm_update", {"source": "analyst", "review_mode": "default"}, "d1"),
                ("sttm_workbook", {}, "d2"),
            )
        )
        conv.call(sttm_update, call_id="d1", source="analyst", review_mode="default")
        assert conv.call(sttm_workbook, call_id="d2") == (
            "Not done: sttm_update changes the session in this same step, and this call would not "
            "see that change. Make this call again now."
        )
        assert len(_kept(store)) == 4

    # Made again in the next step, it runs.
    conv.state["messages"].append(_calls(("sttm_show", {"view": "layout"}, "e1")))
    assert conv.call(sttm_show, call_id="e1", view="layout").endswith("\n1. dd: hide Length")


def test_of_the_sttm_tasks_sent_in_one_step_only_the_first_to_change_may_change_a_layout() -> None:
    tools_mod._CLAIMS.clear()
    store = InMemoryStore()
    conv = _recorded(store)
    sid = conv.session().id
    try:
        with _analyst():
            # Tasks a and b were sent together, in the orchestrator's step s1; a changes first.
            conv.call(sttm_update, task=("s1", "a"), source="analyst", review_mode="default")
            for args in ({"steps": [_HIDE_LENGTH]}, {"remove_steps": [1]}, {"reset": True}):
                assert conv.call(sttm_workbook, task=("s1", "b"), **args) == _CLAIMED, args
                assert not conv.updated, args
            assert conv.state["sttm_layouts"] == {} and _kept(store) == {}
            # A build that changes no layout is b's to make.
            assert conv.call(sttm_workbook, task=("s1", "b")).startswith(f"Session {sid} ")
            assert len(_kept(store)) == 2

            # In step s2 a's layout change comes first, and b may change no session.
            conv.call(sttm_workbook, task=("s2", "a"), steps=[_HIDE_LENGTH])
            assert conv.updated
            changed = conv.call(
                sttm_update, task=("s2", "b"), source="analyst", review_mode="default"
            )
            assert changed == _CLAIMED
    finally:
        tools_mod._CLAIMS.clear()


# -- registration ----------------------------------------------------------------


def test_the_sttm_agent_is_off_until_it_is_turned_on() -> None:
    assert Settings.model_fields["sttm_agent_enabled"].default is False
    off = Settings(_env_file=None, STTM_AGENT_ENABLED=False)
    assert not off.sttm_enabled
    assert ("sttm-agent", "STTM_AGENT_ENABLED") in off.unconfigured_capabilities
    on = Settings(_env_file=None, STTM_AGENT_ENABLED=True)
    assert on.sttm_enabled
    assert "sttm-agent" not in {name for name, _ in on.unconfigured_capabilities}


def test_the_sttm_agent_is_registered_as_a_fork_with_its_tools() -> None:
    assert STTM_SUBAGENT in ALL_SUBAGENTS
    assert STTM_SUBAGENT["name"] == "sttm-agent"
    # A fork is handed the conversation, from which it reads the analyst's message.
    assert STTM_SUBAGENT["runnable"] is STTM_RUNNABLE and STTM_SUBAGENT["mode"] == "fork"
    # Its prompt and middleware are its graph's (the next test).
    assert not {"system_prompt", "middleware"} & set(STTM_SUBAGENT)
    assert STTM_SUBAGENT["tools"] == STTM_TOOLS
    assert [tool.name for tool in STTM_TOOLS] == [
        "sttm_start_session",
        "sttm_record_input",
        "sttm_update",
        "sttm_advance",
        "sttm_show",
        "sttm_workbook",
    ]
    assert {tool.name for tool in STTM_TOOLS} == STTM_TOOL_NAMES
    assert STTM_TOOL_NAMES - SESSION_CHANGING_TOOLS == {"sttm_show"}


def test_the_sttm_agents_graph_has_the_stack_of_a_declarative_subagent() -> None:
    """The graph the sttm-agent runs, next to the one deepagents builds for a
    declarative subagent with its prompt and tools and, as every subagent has,
    only the Azure links check: the same model, middleware, tools and prompt."""

    declared = {
        "name": "sttm-declared",
        "description": "The sttm-agent, declared.",
        "system_prompt": STTM_SUBAGENT_PROMPT,
        "tools": STTM_TOOLS,
        "middleware": [AzureLinksMiddleware(label="sttm-agent's answer")],
    }
    built: dict[str, dict[str, Any]] = {}

    def recording(create_agent: Any) -> Any:
        def record(model: Any, **kwargs: Any) -> Any:
            built[kwargs["name"]] = {"model": model, **kwargs}
            return create_agent(model, **kwargs)

        return record

    saved = (deepagents_subagents.create_agent, runnable_mod.create_agent)
    deepagents_subagents.create_agent = recording(saved[0])
    runnable_mod.create_agent = recording(saved[1])
    model = _recorder([])
    try:
        with _agent(model, _policy({}), [SERVICENOW_SUBAGENT, STTM_SUBAGENT, declared]):
            pass
    finally:
        deepagents_subagents.create_agent, runnable_mod.create_agent = saved

    ours, theirs = built["sttm-agent"], built["sttm-declared"]
    assert ours["model"] is theirs["model"] is model
    assert [(type(m), m.name) for m in ours["middleware"]] == [
        (type(m), m.name) for m in theirs["middleware"]
    ]
    assert AzureLinksMiddleware in {type(m) for m in ours["middleware"]}
    assert [(tool.name, tool.description) for tool in ours["tools"]] == [
        (tool.name, tool.description) for tool in theirs["tools"]
    ]
    assert ours["system_prompt"] == theirs["system_prompt"]
    assert STTM_SUBAGENT_PROMPT in ours["system_prompt"]
    assert ours["state_schema"] is theirs["state_schema"]
    assert ours["response_format"] is theirs["response_format"] is None
    assert set(ours) == set(theirs)


def test_the_sessions_and_layouts_are_channels_of_the_agents_own_state() -> None:
    """``sttm_sessions`` and ``sttm_layouts`` come from ChatAgentState, with the
    sttm-agent or without it, and keep the higher revision of each entry."""

    policy = _policy({})
    for roster in ([SERVICENOW_SUBAGENT, STTM_SUBAGENT], [SERVICENOW_SUBAGENT]):
        with _agent(_recorder([]), policy, roster) as graph:
            channels = dict(graph.channels)
        assert "user_message" not in channels, sorted(channels)
        for name in ("sttm_sessions", "sttm_layouts"):
            assert channels[name].operator is merge_by_revision, name


def test_the_shipped_configs_keep_the_sttm_agent_from_exactly_one_group() -> None:
    """The sttm-agent is for INT only: one group (EXT) blocks it, no global list does."""

    for name in ("access-control.example.json", "access-control.json"):
        path = _REPO_ROOT / "config" / name
        if not path.exists():
            print(f"     (skipped: no config/{name})")
            continue
        config = load_access_policy(str(path))
        blocking = [key for key, group in config.groups.items() if _STTM in group.blocked_subagents]
        assert len(blocking) == 1, (name, blocking)
        assert len(config.groups) > 1, name
        assert _STTM not in config.global_policy.blocked_subagents, name


def test_the_prompt_lists_the_rule_files_this_deployment_has() -> None:
    with _rule_files({"pii-pci-handling.md": "PII/PCI flags: PII, PCI, NONE.", "local.md": "x"}):
        prompt = sttm_subagent_prompt()
    assert (
        f"- pii-pci-handling.md: present at {AUTHORING_MOUNT}pii-pci-handling.md (PII/PCI "
        "classification levels and how each is handled.)"
    ) in prompt
    assert "- data-type-mapping.md: NOT present (" in prompt
    assert f"- local.md: present at {AUTHORING_MOUNT}local.md (not one of the expected files)" in (
        prompt
    )
    for rule_file in EXPECTED_RULE_FILES:
        assert f"- {rule_file}: " in prompt, rule_file

    # The repository ships the README and no rule file: every one reads NOT present.
    with _rule_files({"README.md": (AUTHORING_DIR / "README.md").read_text(encoding="utf-8")}):
        prompt = sttm_subagent_prompt()
    assert prompt.count(": NOT present (") == len(EXPECTED_RULE_FILES)
    assert "README.md" not in prompt


def test_the_readme_names_every_expected_rule_file() -> None:
    readme = (AUTHORING_DIR / "README.md").read_text(encoding="utf-8")
    for rule_file in EXPECTED_RULE_FILES:
        assert f"| `{rule_file}` |" in readme, rule_file
    assert "`templates/data-dictionary.xlsx`" in readme and "`templates/sttm.xlsx`" in readme


def test_the_description_says_when_to_delegate_not_how_the_work_is_done() -> None:
    for words in (
        "Data Dictionary",
        "Raw-to-Curated STTM",
        "Curated-to-ASL STTM",
        "derived fields",
        "PII/PCI classification",
        "review modes (fast, moderate, extensive)",
        "Send it every analyst message that continues an STTM session",
        "Send it one task at a time",
        "typed, pasted or in an attached PDF",
        (
            "It reads the analyst's latest message itself, with the files attached to it, so "
            "the task need not copy it"
        ),
        "Pass its tables, questions, numbered layouts and download links on to the analyst",
        "Not for questions about how an existing field is already documented as mapped",
    ):
        assert words in STTM_SUBAGENT_DESCRIPTION, words
    assert "sttm_" not in STTM_SUBAGENT_DESCRIPTION


def test_the_safety_classifier_lets_sttm_input_through() -> None:
    assert "Do NOT flag STTM authoring input" in SAFETY_CLASSIFIER_SYSTEM_PROMPT
    assert "'ignore the trailer record'" in SAFETY_CLASSIFIER_SYSTEM_PROMPT


# -- the real agent -------------------------------------------------------------


class _Recorder(_RecordingStandIn):
    """The recording stand-in model, also keeping the messages of each call it notes."""

    seen: list[list] = Field(default_factory=list)

    def _note(self, messages: list, tools: list | None) -> None:
        noted = len(self.offered)
        super()._note(messages, tools)
        if len(self.offered) > noted:
            self.seen.append(list(messages))


def _recorder(replies: list[AIMessage]) -> _Recorder:
    return _Recorder(
        azure_endpoint="https://dummy.openai.azure.com",
        azure_deployment="gpt-test",
        api_version="2024-10-21",
        api_key="dummy-key",
        disable_streaming=True,
        replies=replies,
    )


# The task at the head of the sttm-agent's input (prepare).
_TASK = re.compile(r"\A<task>\n(.*?)\n</task>", re.DOTALL)


class _Scripted(_Recorder):
    """The recorder, replying by whose call it is rather than in turn.

    Tasks sent together run at the same time, so their model calls interleave.
    ``script`` holds each sttm-agent task's replies under the task's
    description, and the orchestrator's under "".
    """

    script: dict[str, list[AIMessage]] = Field(default_factory=dict)

    def _reply(self, messages: list) -> ChatResult:
        first = messages[0] if messages else None
        if isinstance(first, SystemMessage) and first.content == SAFETY_CLASSIFIER_SYSTEM_PROMPT:
            return super()._reply(messages)
        task = next((_TASK.match(m.text) for m in messages if isinstance(m, HumanMessage)), None)
        replies = self.script[task.group(1) if task else ""]
        return ChatResult(generations=[ChatGeneration(message=replies.pop(0))])


def _scripted(script: dict[str, list[AIMessage]]) -> _Scripted:
    return _Scripted(
        azure_endpoint="https://dummy.openai.azure.com",
        azure_deployment="gpt-test",
        api_version="2024-10-21",
        api_key="dummy-key",
        disable_streaming=True,
        script=script,
    )


# INT may use the sttm-agent; EXT may not. ls and grep are blocked for everyone.
_INT, _EXT = _TEAM_A, _TEAM_B
_E2E_POLICY = _policy(
    {_INT: {"priority": 200}, _EXT: {"priority": 100, "blocked_subagents": [_ADF, _STTM]}},
    {"blocked_tools": ["ls", "grep"]},
)
_ROSTER = [SERVICENOW_SUBAGENT, STTM_SUBAGENT]


def _say(graph: Any, groups: list[str], text: str) -> dict[str, Any]:
    """One turn on the test thread, under its own run id; returns the thread's state after it."""

    config = {
        "configurable": {
            "thread_id": "sttm",
            "run_id": str(uuid.uuid4()),
            "langgraph_auth_user": {"identity": "analyst", "groups": groups},
        }
    }
    asyncio.run(graph.ainvoke(_user(text), config))
    return graph.get_state(config).values


def _split(model: _Recorder) -> tuple[list[tuple[dict, list]], list[tuple[dict, list]]]:
    """The orchestrator's model calls and the subagents', each as (offered tools, messages)."""

    orchestrator, subagents = [], []
    for offered, messages in zip(model.offered, model.seen, strict=True):
        (orchestrator if "task" in offered else subagents).append((offered, messages))
    return orchestrator, subagents


def _sttm_task(description: str, call_id: str) -> tuple[str, dict, str]:
    """The orchestrator's task ``description`` to the sttm-agent."""

    return (TASK_TOOL_NAME, {"description": description, "subagent_type": _STTM}, call_id)


def test_an_int_caller_runs_an_sttm_session_through_the_orchestrator() -> None:
    turn_1 = "Start an STTM session for CoreBank, daily accounts feed."
    turn_2 = f"{_INSTRUCTIONS}\n{_SPEC}"
    # The tasks do not copy the analyst's messages: the sttm-agent reads them.
    task_1 = "Start the STTM session the analyst asks for."
    task_2 = "Record the inputs the analyst sent."
    model = _recorder(
        [
            _calls(_sttm_task(task_1, "call_task_1")),
            _calls(
                (
                    "sttm_start_session",
                    {"source_system": "CoreBank", "source": "daily accounts feed"},
                    "call_start",
                )
            ),
            AIMessage(content="Session started. Please paste the specification."),
            AIMessage(content="The session is started. Please paste the specification."),
            _calls(_sttm_task(task_2, "call_task_2")),
            _calls(
                (
                    "sttm_record_input",
                    {"inputs": _inputs("source_system_instructions", "specification")},
                    "call_record",
                )
            ),
            AIMessage(content="Recorded 3 attributes."),
            AIMessage(content="The specification is recorded: 3 attributes."),
        ]
    )
    with _rule_files({}), _agent(model, _E2E_POLICY, _ROSTER) as graph:
        _say(graph, [_INT], turn_1)
        state = _say(graph, [_INT], turn_2)

    assert model.replies == []
    assert state["messages"][-1].content == "The specification is recorded: 3 attributes."
    orchestrator, subagents = _split(model)
    assert len(orchestrator) == 4 and len(subagents) == 4, model.offered

    for offered, _ in orchestrator:
        assert f"- {_STTM}: STTM authoring agent." in offered["task"]
        assert not STTM_TOOL_NAMES & set(offered)
    for offered, _ in subagents:
        assert STTM_TOOL_NAMES <= set(offered), sorted(offered)
        assert "read_file" in offered  # for the rule files
        assert not {"ls", "grep", "task"} & set(offered), sorted(offered)

    # The sttm-agent's input is its task and the analyst's message as they sent it; from
    # the second turn on, also their earlier message and its previous answer.
    first = f"<task>\n{task_1}\n</task>\n\n<analyst_message>\n{turn_1}\n</analyst_message>"
    second = (
        f"<task>\n{task_2}\n</task>\n\n"
        f"<earlier_messages>\n<message>\n{turn_1}\n</message>\n</earlier_messages>\n\n"
        "<previous_answer>\nSession started. Please paste the specification.\n"
        "</previous_answer>\n\n"
        f"<analyst_message>\n{turn_2}\n</analyst_message>"
    )
    for (_, messages), expected in ((subagents[0], first), (subagents[2], second)):
        humans = [m.text for m in messages if isinstance(m, HumanMessage)]
        assert humans == [expected], humans

    # The session came back to the orchestrator's state, recorded from the analyst's
    # messages, each under its id in the conversation.
    (session,) = (Session.model_validate(data) for data in state["sttm_sessions"].values())
    assert [attribute.name for attribute in session.attributes] == [
        "CUSTOMER_ID",
        "ACCOUNT_NO",
        "OPEN_DATE",
    ]
    (specification,) = [record for record in session.inputs if record.kind == "specification"]
    assert session.messages[specification.message_id] == turn_2
    assert list(session.messages.values()) == [turn_1, turn_2]
    sent = [m.id for m in state["messages"] if isinstance(m, HumanMessage)]
    assert list(session.messages) == sent and all(sent), (list(session.messages), sent)
    assert "user_message" not in state

    results = {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["call_task_1"].content == "Session started. Please paste the specification."
    assert results["call_task_2"].content == "Recorded 3 attributes."


def test_of_two_sttm_tasks_sent_in_one_step_only_one_changes_the_sessions() -> None:
    turn = (
        "Start STTM sessions for CoreBank: the daily accounts feed, the card feed and the "
        "monthly balances."
    )
    accounts, cards, balances = (
        "Start the daily accounts feed's session.",
        "Start the card feed's session.",
        "Start the monthly balances' session, on its own.",
    )

    def start(source: str, call_id: str) -> list[AIMessage]:
        arguments = {"source_system": "CoreBank", "source": source}
        return [_calls(("sttm_start_session", arguments, call_id)), AIMessage(content="Done.")]

    model = _scripted(
        {
            "": [
                _calls(_sttm_task(accounts, "call_task_a"), _sttm_task(cards, "call_task_b")),
                _calls(_sttm_task(balances, "call_task_c")),
                AIMessage(content="The sessions are started."),
            ],
            accounts: start("daily accounts feed", "call_start_a"),
            cards: start("card feed", "call_start_b"),
            balances: start("monthly balances", "call_start_c"),
        }
    )
    with _rule_files({}), _agent(model, _E2E_POLICY, _ROSTER) as graph:
        state = _say(graph, [_INT], turn)

    assert not any(model.script.values()), model.script
    # What sttm_start_session answered in each task, by the task.
    answers: dict[str, str] = {}
    for _, messages in _split(model)[1]:
        task = next(_TASK.match(m.text) for m in messages if isinstance(m, HumanMessage))
        for message in messages:
            if isinstance(message, ToolMessage):
                answers[task.group(1)] = message.text
    together = [answers[accounts], answers[cards]]
    # Which of the two changes first is up to the scheduler; the other changes nothing.
    assert sorted(answer == _CLAIMED for answer in together) == [False, True], together
    assert answers[balances].startswith("Started STTM session "), answers
    assert len(state["sttm_sessions"]) == 2


def test_an_ext_caller_is_never_offered_the_sttm_agent_and_cannot_call_it() -> None:
    model = _recorder(
        [
            _calls(_delegate(_STTM, "call_sttm")),
            AIMessage(content="STTM authoring is not available to you."),
        ]
    )
    with _agent(model, _E2E_POLICY, _ROSTER) as graph:
        state = _say(graph, [_EXT], "Build the Data Dictionary for CoreBank.")

    assert model.replies == []
    orchestrator, subagents = _split(model)
    assert len(orchestrator) == 2 and subagents == [], model.offered
    for offered, _ in orchestrator:
        assert f"- {_STTM}:" not in offered["task"]
        assert f"- {_SERVICENOW}:" in offered["task"]

    results = {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["call_sttm"].content == (
        "We cannot invoke subagent sttm-agent because it does not exist, the only allowed types "
        f"are `{_SERVICENOW}`"
    )
    assert not state.get("sttm_sessions")


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
