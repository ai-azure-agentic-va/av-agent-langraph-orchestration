"""Regression tests for the graph spec syntax (the "Slow graph load" root cause).

GRAPH-SPEC-MODULE: every registered graph must be declared with MODULE syntax
(``v1.core.agent:build_agent``), never a file path
(``./src/v1/core/agent.py:build_agent``).

langgraph-api picks its loader by looking for a ``/`` in the spec
(``langgraph_api/graph.py``, ``collect_graphs_from_env``)::

    if "/" in path_or_module:   # -> spec_from_file_location(synthetic_name, path)
    else:                       # -> importlib.import_module(module)

The path branch execs ``agent.py`` a SECOND time under a synthetic module name
derived from the path, producing a module object distinct from the installed
``v1.core.agent``. Each copy carries its own ``_agent`` / ``_chat_model``
globals, so the lifespan pre-warm in ``src/v1/api/main.py`` (a normal
``from v1.core.agent import build_agent``) warmed an object no registered graph
ever called, and every first request paid the full agent build — which is what
langgraph-api reports as "Slow graph load ... took NNNms". Module syntax routes
both specs through ``importlib.import_module``, which is ``sys.modules``-cached,
so there is exactly one module, one singleton and one Azure client.

Both declaration sites must stay in sync: ``langgraph.json`` (local ``langgraph
dev``) and the Dockerfile's baked ``LANGSERVE_GRAPHS`` (the deployed container
reads the env var, NOT langgraph.json).

Runs standalone (``python test_graph_spec_module_syntax.py``) or under pytest.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LANGGRAPH_JSON = _REPO_ROOT / "langgraph.json"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"

# Mirrors langgraph_api's own rule, so this test tracks the real loader semantics
# rather than a paraphrase of them. Argument order matters and is easy to invert
# (``"/".__contains__(spec)`` asks the reverse question and is vacuously false for
# every real spec) — ``test_path_spec_detector_direction`` pins the direction.
def _is_path_spec(spec: str) -> bool:
    return "/" in spec


def _graph_specs_from_langgraph_json() -> dict[str, str]:
    return json.loads(_LANGGRAPH_JSON.read_text())["graphs"]


def _graph_specs_from_dockerfile() -> dict[str, str]:
    """Extract the JSON baked into ``ENV LANGSERVE_GRAPHS='...'``."""

    match = re.search(
        r"^ENV\s+LANGSERVE_GRAPHS='(.*)'\s*$", _DOCKERFILE.read_text(), re.MULTILINE
    )
    assert match is not None, "Dockerfile has no ENV LANGSERVE_GRAPHS line"
    return json.loads(match.group(1))


def _assert_module_syntax(specs: dict[str, str], source: str) -> None:
    assert specs, f"{source} registers no graphs"
    for graph_id, spec in specs.items():
        module_or_path, _, variable = spec.rpartition(":")
        assert variable, f"{source}: graph {graph_id!r} spec {spec!r} has no variable"
        assert not _is_path_spec(module_or_path), (
            f"{source}: graph {graph_id!r} is registered by FILE PATH ({spec!r}). "
            "That makes langgraph-api exec the module a second time under a "
            "synthetic name, giving it a private _agent singleton the lifespan "
            "pre-warm never touches (the 'Slow graph load' bug). Use module "
            "syntax, e.g. 'v1.core.agent:build_agent'."
        )
        assert not module_or_path.endswith(".py"), (
            f"{source}: graph {graph_id!r} spec {spec!r} still names a .py file"
        )


def test_path_spec_detector_direction() -> None:
    """Guard the guard: an inverted ``_is_path_spec`` makes every other check vacuous."""

    for path_spec in (
        "./src/v1/core/agent.py",
        "/deps/langraph-agent-orchestration/src/v1/core/agent.py",
        "src/v1/core/agent",  # no .py suffix — still the path branch in langgraph-api
    ):
        assert _is_path_spec(path_spec), f"{path_spec!r} should read as a path spec"
    for module_spec in ("v1.core.agent", "v1.api.main", ""):
        assert not _is_path_spec(module_spec), f"{module_spec!r} should read as a module"


def test_langgraph_json_uses_module_syntax() -> None:
    _assert_module_syntax(_graph_specs_from_langgraph_json(), "langgraph.json")


def test_dockerfile_langserve_graphs_uses_module_syntax() -> None:
    _assert_module_syntax(_graph_specs_from_dockerfile(), "Dockerfile LANGSERVE_GRAPHS")


def test_every_spec_resolves_to_the_prewarmed_object() -> None:
    """The point of the fix: registered factory IS the one main.py warms.

    ``src/v1/api/main.py`` warms ``v1.core.agent.build_agent``. If any spec
    resolves to a different function object, that graph has a separate singleton
    and the pre-warm does not cover it.
    """

    import v1.core.agent as prewarmed

    # Checked per source, not merged: ``a | b`` would let the Dockerfile's value
    # for a shared id silently shadow langgraph.json's and go unresolved.
    for source, specs in (
        ("langgraph.json", _graph_specs_from_langgraph_json()),
        ("Dockerfile LANGSERVE_GRAPHS", _graph_specs_from_dockerfile()),
    ):
        for graph_id, spec in specs.items():
            module_name, _, variable = spec.rpartition(":")
            resolved = getattr(importlib.import_module(module_name), variable)
            assert resolved is getattr(prewarmed, variable), (
                f"{source}: graph {graph_id!r} resolves to a different {variable} "
                "than the one src/v1/api/main.py pre-warms — its singleton would "
                "stay cold."
            )


def test_declaration_sites_agree() -> None:
    """The deployed container reads LANGSERVE_GRAPHS; `langgraph dev` reads the JSON.

    Ids may differ (the Dockerfile keeps a vestigial ``agent`` alias), but any id
    present in both must point at the same factory, or local and prod diverge.
    """

    from_json = _graph_specs_from_langgraph_json()
    from_docker = _graph_specs_from_dockerfile()
    shared = set(from_json) & set(from_docker)
    assert shared, (
        f"langgraph.json ids {sorted(from_json)} and Dockerfile ids "
        f"{sorted(from_docker)} have nothing in common"
    )
    for graph_id in sorted(shared):
        assert from_json[graph_id] == from_docker[graph_id], (
            f"graph {graph_id!r} differs: langgraph.json has "
            f"{from_json[graph_id]!r}, Dockerfile has {from_docker[graph_id]!r}"
        )


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
