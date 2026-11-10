from contextlib import asynccontextmanager

from fastapi import FastAPI

from v1.api.routes.databricks import create_databricks_router
from v1.api.routes.databricks_admin import create_databricks_admin_router
from v1.api.routes.feedback import create_feedback_router
from v1.api.routes.starter_prompts import create_starter_prompts_router
from v1.api.routes.sttm_files import create_sttm_files_router
from v1.core.config import get_settings
from v1.core.workspace_registry.snapshot import start_registry, stop_registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the process-wide agent singleton at startup. ``build_agent`` is the
    # graph factory LangGraph re-invokes per request; its first call builds the
    # ``AzureChatOpenAI`` client (~200ms, the bulk of the cost) plus the deep
    # agent, then caches both. LangGraph times that first await inside the graph
    # factory and logs a "Slow graph load" warning. Building here — the custom
    # app's lifespan runs at startup, before Studio's first schema/graph fetch —
    # moves the cost off the request path so the first graph access hits the
    # cached instance. Runs once; subsequent ``build_agent`` calls are ~0ms.
    #
    # THIS ONLY WORKS BECAUSE THE GRAPH IS REGISTERED WITH MODULE SYNTAX
    # ("v1.core.agent:build_agent") in langgraph.json and the Dockerfile's
    # LANGSERVE_GRAPHS. A file-path spec makes langgraph-api load agent.py via
    # spec_from_file_location() under a synthetic module name — a second module
    # object with its own ``_agent`` singleton — so the import below would warm a
    # copy the registered graph never calls, silently restoring the warning. See
    # the comment above LANGSERVE_GRAPHS in the Dockerfile, and
    # test_graph_spec_module_syntax.py which guards both specs.
    from v1.core.agent import build_agent

    # The Databricks workspaces are read from the registry before the agent is built:
    # the Databricks tools it builds read them. Never fails startup (see start_registry).
    await start_registry(get_settings())
    await build_agent()
    yield
    await stop_registry()
    # Release pooled resources on shutdown (Postgres pool, ServiceNow + Azure
    # Search HTTP clients) so they do not leak across reload/redeploy. Imported
    # lazily so the HTTP app's importability is not coupled to the agent
    # module's import-time setup.
    from v1.core.agent import close_agent_resources

    await close_agent_resources()


app = FastAPI(lifespan=lifespan)
app.include_router(create_feedback_router())
app.include_router(create_starter_prompts_router())
# The sttm-agent's workbook downloads, only where the sttm-agent is turned on.
if get_settings().sttm_enabled:
    app.include_router(create_sttm_files_router())
app.include_router(create_databricks_router())
app.include_router(create_databricks_admin_router())
