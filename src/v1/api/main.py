from contextlib import asynccontextmanager

from fastapi import FastAPI

from v1.api.routes.feedback import create_feedback_router
from v1.api.routes.starter_prompts import create_starter_prompts_router


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
    from v1.core.agent import build_agent

    await build_agent()
    yield
    # Release pooled resources on shutdown (Postgres pool, ServiceNow + Azure
    # Search HTTP clients) so they do not leak across reload/redeploy. Imported
    # lazily so the HTTP app's importability is not coupled to the agent
    # module's import-time setup.
    from v1.core.agent import close_agent_resources

    await close_agent_resources()


app = FastAPI(lifespan=lifespan)
app.include_router(create_feedback_router())
app.include_router(create_starter_prompts_router())