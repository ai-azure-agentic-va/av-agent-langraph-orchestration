"""Tests for CitationsMiddleware: knowledge-base citations from answer to UI.

It works at three points of a turn, each tested here: the answer (``[n]``
markers the model invented are removed), a subagent's searches (its ``task``
result carries what ``ai_search_tool`` gives its own) and the end of the turn
(the turn's documents are persisted into ``all_sources``).

The subagent tests are regression tests. When the orchestrator delegated
(``task``) and the subagent called ``ai_search_tool``, the answer kept its
``[n]`` markers but the UI showed them as bare text with no "Referenced
Sources". Nothing of the search left the subagent: its live ``documents`` event
is dropped (the UI's normal submit does not stream subgraphs), its ToolMessage
and artifact stay in the subagent's own messages, and only adf-agent accumulated
its sources into ``all_sources``. The markers survived the citation check
because the orchestrator and subagent share one per-run numbering registry.

The end-to-end tests build the REAL agent (``_build_agent_sync``) around the
scripted stand-in model from test_patch_tool_calls.py, stub only the Azure
Search call (``_run_search``), and delegate to the ServiceNow subagent, which
does not run the middleware itself, or to adf-agent, which does.

Runs standalone (``python test_citations.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from langchain.agents.middleware import ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from test_patch_tool_calls import _AzureStandIn, _stand_in_model, _user

import v1.core.agent as agent_mod
from v1.core.middlewares import citations
from v1.core.middlewares.citations import CitationsMiddleware
from v1.core.middlewares.subagent_access import TASK_TOOL_NAME
from v1.core.subagents import ADF_SUBAGENT, SERVICENOW_SUBAGENT
from v1.core.tools.ai_search import ai_search as ai_search_mod

_SUBAGENT = "servicenow-ticket-agent"
_VPN = {"title": "VPN reset runbook", "url": "https://kb.example.test/vpn-reset"}
_MFA = {"title": "MFA token resync", "url": "https://kb.example.test/mfa-resync"}
_KB = {"vpn reset": _VPN, "mfa resync": _MFA}


def _fake_run_search(
    query: str,
    top_k: int,
    index_name: str,
    semantic_configuration: str | None,
    documents: Any,
    section: str | None = None,
) -> str:
    """Stand-in for the Azure Search call: numbers one document like _run_search."""

    hit = _KB[query]
    index = documents.assign(hit["url"], {**hit, "source_key": hit["url"]})
    return f"[{index}] {hit['title']}\nThe documented steps."


@contextmanager
def _agent(model: _AzureStandIn, subagents: list[dict] | None = None) -> Iterator[Any]:
    # Only the Azure Search call is stubbed: the real ai_search_tool numbers the
    # documents in the run's registry. The model stays patched for the whole test
    # because the safety classifier fetches it on every turn. adf-agent is
    # registered only when ADF is configured, so a test that needs it says so.
    saved = (agent_mod.get_azure_chat_model, ai_search_mod._run_search)
    saved_subagents = agent_mod.ENABLED_SUBAGENTS
    agent_mod.get_azure_chat_model = lambda: model
    ai_search_mod._run_search = _fake_run_search
    if subagents is not None:
        agent_mod.ENABLED_SUBAGENTS = subagents
    try:
        yield agent_mod._build_agent_sync(InMemorySaver())
    finally:
        agent_mod.get_azure_chat_model, ai_search_mod._run_search = saved
        agent_mod.ENABLED_SUBAGENTS = saved_subagents


def _delegated_search(
    turn: int, query: str, marker: int, subagent: str = _SUBAGENT, answer: str | None = None
) -> list[AIMessage]:
    """One turn's replies: the orchestrator delegates, the subagent searches and
    cites ``[marker]`` (or answers ``answer``), and the orchestrator relays it."""

    task_call = {
        "name": TASK_TOOL_NAME,
        "args": {
            "description": f"Find the documented steps for {query}",
            "subagent_type": subagent,
        },
        "id": f"call_task_{turn}",
    }
    search_call = {"name": "ai_search_tool", "args": {"query": query}, "id": f"call_search_{turn}"}
    return [
        AIMessage(content="", tool_calls=[task_call]),
        AIMessage(content="", tool_calls=[search_call]),
        AIMessage(content=answer or f"The runbook covers it [{marker}]."),
        AIMessage(content=f"Here are the steps [{marker}]."),
    ]


def _turn_config(thread_id: str) -> dict[str, Any]:
    # A fresh run_id per turn, as langgraph-api assigns; the numbering registry
    # ai_search_tool and CitationsMiddleware share is keyed on it.
    return {"configurable": {"thread_id": thread_id, "run_id": str(uuid.uuid4())}}


async def _stream_turn(graph: Any, text: str, config: dict[str, Any]) -> list[list[dict]]:
    """Run one turn streamed like the UI's normal submit (no subgraphs) and return
    the document lists of the ``documents`` events that reached the client."""

    events: list[list[dict]] = []
    async for mode, chunk in graph.astream(_user(text), config, stream_mode=["custom", "values"]):
        if mode == "custom" and isinstance(chunk, dict) and chunk.get("type") == "documents":
            events.append(chunk["documents"])
    return events


def _indices(documents: Any) -> list[int]:
    return sorted(d["index"] for d in documents or [])


def _tool_message(messages: list[BaseMessage], call_id: str) -> ToolMessage:
    return next(m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == call_id)


def test_a_subagent_search_reaches_the_ui() -> None:
    model = _stand_in_model(_delegated_search(1, "vpn reset", 1))
    config = _turn_config("subagent-search")

    async def _scenario(graph: Any) -> None:
        events = await _stream_turn(graph, "How do I reset my VPN?", config)
        state = (await graph.aget_state(config)).values

        # Live: the client gets the documents event an orchestrator search sends.
        assert [_indices(docs) for docs in events] == [[1]], (
            f"no top-level documents event for the subagent's search: {events}"
        )
        # Reload: the task result carries them, the way ai_search_tool's does.
        task_result = _tool_message(state["messages"], "call_task_1")
        assert task_result.name == TASK_TOOL_NAME
        assert _indices(task_result.artifact) == [1], "the task result has no sources artifact"
        assert task_result.artifact[0]["url"] == _VPN["url"]
        # Next turn: the document is part of the conversation's numbering.
        assert _indices(state.get("all_sources")) == [1]
        assert state["messages"][-1].content == "Here are the steps [1]."

    with _agent(model) as graph:
        asyncio.run(_scenario(graph))
    assert not model.replies, "the scripted turn did not run to the end"


def test_the_next_turn_continues_the_numbering() -> None:
    model = _stand_in_model(
        [*_delegated_search(1, "vpn reset", 1), *_delegated_search(2, "mfa resync", 2)]
    )
    thread = "subagent-numbering"

    async def _scenario(graph: Any) -> None:
        await _stream_turn(graph, "How do I reset my VPN?", _turn_config(thread))
        config = _turn_config(thread)
        events = await _stream_turn(graph, "And how do I resync my MFA token?", config)
        state = (await graph.aget_state(config)).values

        # The new document is [2], not a second [1] beside the VPN runbook.
        numbered = {d["index"]: d["url"] for d in state.get("all_sources") or []}
        assert numbered == {1: _VPN["url"], 2: _MFA["url"]}, f"numbering restarted: {numbered}"
        assert [_indices(docs) for docs in events] == [[1, 2]]
        assert _indices(_tool_message(state["messages"], "call_task_2").artifact) == [1, 2]
        assert state["messages"][-1].content == "Here are the steps [2]."

    with _agent(model) as graph:
        asyncio.run(_scenario(graph))
    assert not model.replies, "the scripted turns did not run to the end"


def test_inside_adf_agent_its_own_citation_is_kept_and_an_invented_one_removed() -> None:
    """adf-agent runs the middleware too, so its answer is checked before the
    orchestrator sees it. Its fresh [1] is valid there: the run id, and with it the
    shared registry, reaches the subagent's model call. [9] points at nothing."""

    model = _stand_in_model(
        _delegated_search(
            1, "vpn reset", 1, subagent="adf-agent", answer="The runbook covers it [1] [9]."
        )
    )
    config = _turn_config("adf-agent-search")

    async def _scenario(graph: Any) -> None:
        events = await _stream_turn(graph, "How do I reset my VPN?", config)
        state = (await graph.aget_state(config)).values

        task_result = _tool_message(state["messages"], "call_task_1")
        assert task_result.content == "The runbook covers it [1].", task_result.content
        assert _indices(task_result.artifact) == [1]
        assert [_indices(docs) for docs in events] == [[1]]
        assert _indices(state.get("all_sources")) == [1]

    with _agent(model, subagents=[SERVICENOW_SUBAGENT, ADF_SUBAGENT]) as graph:
        asyncio.run(_scenario(graph))
    assert not model.replies, "the scripted turn did not run to the end"


# --- The middleware alone, around a fake call ----------------------------------------


@contextmanager
def _in_run(run_id: str | None) -> Iterator[None]:
    # Outside a graph run there is no config to read the run id from.
    saved = citations._current_run_id
    citations._current_run_id = lambda: run_id
    try:
        yield
    finally:
        citations._current_run_id = saved


def _request(name: str, events: list[dict], all_sources: list[dict] | None = None) -> Any:
    return SimpleNamespace(
        tool_call={"name": name, "args": {"subagent_type": _SUBAGENT}, "id": "call_x"},
        state={"messages": [], "all_sources": all_sources or []},
        runtime=SimpleNamespace(stream_writer=events.append),
    )


def _handler(run_id: str, result: Any, *hits: dict) -> Callable[[Any], Any]:
    """A tool handler that numbers ``hits`` in the run's registry, as a search
    does, then returns ``result``."""

    def handler(request: Any) -> Any:
        registry = ai_search_mod._turn_documents(run_id)
        registry.seed(request.state["all_sources"])
        for hit in hits:
            registry.assign(hit["url"], {**hit, "source_key": hit["url"]})
        return result

    return handler


def _task_result(text: str) -> Command:
    return Command(update={"messages": [ToolMessage(text, tool_call_id="call_x")]})


def _numbered(index: int, hit: dict) -> dict:
    return {"index": index, **hit, "source_key": hit["url"]}


# The answer.


def _checked(
    message: AIMessage, run_id: str | None, all_sources: list[dict] | None = None
) -> AIMessage:
    """``message`` as the model returned it, after the middleware checked it."""

    response = ModelResponse(result=[message])
    request = SimpleNamespace(state={"messages": [], "all_sources": all_sources or []})
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_model_call(request, lambda _: response)
    return out.result[0]


def test_the_answer_keeps_only_markers_that_point_at_a_document() -> None:
    # An earlier turn persisted [1]; this run's search numbered [2].
    run_id, persisted = str(uuid.uuid4()), [_numbered(1, _VPN)]
    _handler(run_id, None, _MFA)(_request(TASK_TOOL_NAME, [], all_sources=persisted))

    answer = AIMessage(content="VPN [1], MFA [2], invented [7] [12].")
    assert _checked(answer, run_id, persisted).content == "VPN [1], MFA [2], invented."
    # A follow-up that searched nothing keeps an earlier turn's marker.
    follow_up = AIMessage(content="As before [1], see [3].")
    assert _checked(follow_up, str(uuid.uuid4()), persisted).content == "As before [1], see."
    blocks = AIMessage(content=[{"type": "text", "text": "See [1] [8]."}])
    assert _checked(blocks, None, persisted).content == [{"type": "text", "text": "See [1]."}]


def test_markers_are_left_alone_in_a_conversation_without_sources() -> None:
    answer = AIMessage(content="Step [1] of the runbook.")
    assert _checked(answer, str(uuid.uuid4())) is answer


def test_only_a_final_answer_is_checked() -> None:
    call = {"name": "ai_search_tool", "args": {"query": "vpn reset"}, "id": "call_s"}
    calling = AIMessage(content="Looking for [7].", tool_calls=[call])
    assert _checked(calling, None, [_numbered(1, _VPN)]) is calling


# A subagent's searches.


def test_other_tool_calls_pass_through() -> None:
    # Even a Command (deepagents' file tools return one) while the registry gains a
    # document, e.g. from an orchestrator search, which surfaces its own documents.
    run_id, events = str(uuid.uuid4()), []
    result = _task_result("Updated /notes.md")
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_tool_call(
            _request("write_file", events), _handler(run_id, result, _VPN)
        )
    assert out is result and events == []


def test_a_blocked_delegation_passes_through() -> None:
    run_id, events = str(uuid.uuid4()), []
    blocked = ToolMessage("Not available for your access.", tool_call_id="call_x", status="error")
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_tool_call(
            _request(TASK_TOOL_NAME, events), _handler(run_id, blocked, _VPN)
        )
    assert out is blocked and events == []


def test_sources_the_turn_already_has_are_left_alone() -> None:
    # The orchestrator searched before delegating; the subagent re-found its document.
    run_id, events = str(uuid.uuid4()), []
    _handler(run_id, None, _VPN)(_request(TASK_TOOL_NAME, []))
    result = _task_result("The runbook covers it [1].")
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_tool_call(
            _request(TASK_TOOL_NAME, events), _handler(run_id, result, _VPN)
        )
    assert out is result and events == []


def test_sources_the_conversation_already_has_are_left_alone() -> None:
    # An earlier turn persisted the document; the subagent re-found it.
    run_id, events = str(uuid.uuid4()), []
    result = _task_result("The runbook covers it [1].")
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_tool_call(
            _request(TASK_TOOL_NAME, events, all_sources=[_numbered(1, _VPN)]),
            _handler(run_id, result, _VPN),
        )
    assert out is result and events == []


def test_new_sources_attach_to_the_task_result() -> None:
    run_id, events = str(uuid.uuid4()), []
    persisted = [_numbered(1, _VPN)]
    result = Command(
        update={
            "messages": [ToolMessage("The token steps [2].", tool_call_id="call_x")],
            # deepagents copies the subagent's state keys back, all_sources included.
            "all_sources": persisted,
        }
    )
    with _in_run(run_id):
        out = CitationsMiddleware().wrap_tool_call(
            _request(TASK_TOOL_NAME, events, all_sources=persisted),
            _handler(run_id, result, _MFA),
        )

    message = out.update["messages"][0]
    assert message.content == "The token steps [2].", "the model must see the same text"
    assert message.artifact == [persisted[0], _numbered(2, _MFA)]
    assert events == [{"type": "documents", "documents": message.artifact}]
    assert agent_mod.merge_sources(persisted, out.update["all_sources"]) == message.artifact
    assert result.update["messages"][0].artifact is None, "the handler's result was mutated"


def test_the_snapshot_never_creates_a_registry() -> None:
    run_id = str(uuid.uuid4())
    assert ai_search_mod.turn_documents_snapshot(None) == []
    assert ai_search_mod.turn_documents_snapshot(run_id) == []
    assert run_id not in ai_search_mod._document_registries
    ai_search_mod._turn_documents(run_id).assign(_VPN["url"], dict(_VPN))
    assert ai_search_mod.turn_documents_snapshot(run_id) == [{"index": 1, **_VPN}]


# The end of the turn.


def _search_result(call_id: str, artifact: Any) -> ToolMessage:
    return ToolMessage("grounding", tool_call_id=call_id, name="ai_search_tool", artifact=artifact)


def test_the_turn_s_searches_are_persisted() -> None:
    vpn, mfa = _numbered(1, _VPN), _numbered(2, _MFA)
    # The tool's wrapper shape, and a task result: the subagent hook already
    # persisted that one.
    task = ToolMessage(
        "The steps [3].", tool_call_id="call_t", name=TASK_TOOL_NAME, artifact=[{"index": 3}]
    )
    state = {
        "messages": [
            HumanMessage("How do I reset my VPN?"),
            _search_result("call_s1", [vpn]),
            AIMessage("Here are the steps [1]."),
            HumanMessage("And my MFA token?"),
            _search_result("call_s2", {"documents": [vpn, mfa]}),
            task,
            AIMessage("Resync it like this [2]."),
        ]
    }
    assert CitationsMiddleware().after_agent(state, None) == {"all_sources": [vpn, mfa]}


def test_a_turn_without_a_search_persists_nothing() -> None:
    state = {
        "messages": [
            HumanMessage("How do I reset my VPN?"),
            _search_result("call_s1", [_numbered(1, _VPN)]),
            AIMessage("Here are the steps [1]."),
            HumanMessage("Thanks!"),
            AIMessage("You're welcome."),
        ]
    }
    assert CitationsMiddleware().after_agent(state, None) is None


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
