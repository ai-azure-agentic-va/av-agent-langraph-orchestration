# Pin the base image by digest: the version tag still floats forward on every base
# rebuild, and this digest is the ONLY effective langgraph-api lock. langgraph-api
# cannot be pinned from pyproject.toml (listing it makes the app install re-resolve
# it against /api/constraints.txt, which is unsatisfiable on the grpcio pin — see the
# note in pyproject.toml). Refresh the digest deliberately when bumping the version.
#
# 0.14.2-py3.11 (digest resolved 2026-09-23) ships langgraph-api 0.14.2,
# langgraph-checkpoint 4.2.0, langgraph-sdk 0.4.4 — same Debian 13 / Python 3.11.16
# and a byte-identical /api/constraints.txt as the 0.14.3 digest it replaces, whose
# aget_delta_channel_history bytecode it also matches exactly (so the fix below
# holds). Do NOT go below 0.14.1: THE 0.13.0 -> 0.14.x BUMP IS LOAD-BEARING, do not
# roll it back independently of the ChatAgentState change in src/v1/core/agent.py:
# the platform serves /threads/{id}/state and /history from its OWN compiled checkpointer
# (/storage/langgraph_runtime_postgres/checkpoint.pyc), NOT from the pip-installed
# langgraph-checkpoint-postgres. Through 0.13.0 that checkpointer's
# aget_delta_channel_history branched on `isinstance(seed, _DeltaSnapshot)` and
# mishandled a DeltaChannel seeded by any other reducer, which is the 400 that
# ChatAgentState used to work around; 0.14.1+ drops that branch entirely (verified by
# disassembly: _DeltaSnapshot / DELTA_CHANNEL_SUPPORT no longer appear in the
# function). Reverting only the digest re-breaks every long thread.
FROM langchain/langgraph-api:0.14.2-py3.11@sha256:b3af10222c64095fe3f1a9509407ee9a789226036a1fac8de8a2286041e51219



# -- Adding local package . --
ADD . /deps/langraph-agent-orchestration
# -- End of local package . --

# -- Installing all local dependencies --
RUN for dep in /deps/*; do             echo "Installing $dep";             if [ -d "$dep" ]; then                 echo "Installing $dep";                 (cd "$dep" && PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir -c /api/constraints.txt -e .);             fi;         done
# -- End of local dependencies install --
ENV LANGGRAPH_AUTH='{"path": "/deps/langraph-agent-orchestration/src/v1/utils/auth.py:auth"}'
ENV LANGGRAPH_HTTP='{"app": "/deps/langraph-agent-orchestration/src/v1/api/main.py:app", "enable_custom_route_auth": true}'
# MODULE syntax ("v1.core.agent:build_agent"), NOT a file path. Keep it that way.
# langgraph-api decides how to load a graph by looking for a "/" in the spec: with a
# path it calls importlib.util.spec_from_file_location() under a synthetic module name
# derived from the path, which executes agent.py a SECOND time as a module object
# distinct from the installed `v1.core.agent` package. That copy gets its own `_agent`
# / `_chat_model` globals, so the lifespan pre-warm in src/v1/api/main.py (which does a
# normal `from v1.core.agent import ...`) warmed an object no graph ever used, and every
# first request paid the full ~200ms agent build -> "Slow graph load" WARNING/ERROR.
# With module syntax the loader uses importlib.import_module(), which is sys.modules-
# cached, so both ids below share ONE module, ONE singleton and ONE AzureChatOpenAI
# client -- and the pre-warm actually takes effect.
# (The "agent" id is a vestigial alias of "chat"; every client uses "chat". Under module
# syntax it is free -- same module object -- so it is kept only for compatibility.)
ENV LANGSERVE_GRAPHS='{"chat": "v1.core.agent:build_agent", "agent": "v1.core.agent:build_agent"}'



# -- Ensure user deps didn't inadvertently overwrite langgraph-api
RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license && touch /api/langgraph_api/__init__.py /api/langgraph_runtime/__init__.py /api/langgraph_license/__init__.py
RUN PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir --no-deps -e /api
# -- End of ensuring user deps didn't inadvertently overwrite langgraph-api --
# -- Removing build deps from the final image ~<:===~~~ --
RUN pip uninstall -y pip setuptools wheel
RUN rm -rf /usr/local/lib/python*/site-packages/pip* /usr/local/lib/python*/site-packages/setuptools* /usr/local/lib/python*/site-packages/wheel* && find /usr/local/bin -name "pip*" -delete || true
RUN rm -rf /usr/lib/python*/site-packages/pip* /usr/lib/python*/site-packages/setuptools* /usr/lib/python*/site-packages/wheel* && find /usr/bin -name "pip*" -delete || true
RUN uv pip uninstall --system pip setuptools wheel && rm /usr/bin/uv /usr/bin/uvx

WORKDIR /deps/langraph-agent-orchestration