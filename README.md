# Agent Orchestration

A LangGraph-based agent orchestration backend. It exposes a chat agent that can
reason over a knowledge base and delegate specialized work to task-focused
subagents, served through the LangGraph platform API.

## Features

- **Deep-agent orchestration** — a top-level agent plans and delegates to
  subagents, each with its own tools and prompt.
- **Knowledge-base retrieval** — grounded answers with cited sources.
- **Tool integrations** — subagents call external systems through a typed tool
  layer with mocked fixtures for local development.
- **Streaming** — token and progress events stream to the client, including
  subagent activity indicators.

## Getting started

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync --frozen        # install dependencies
uv run langgraph dev    # run the LangGraph server locally (graph: "chat")
```

The server entry points are declared in `langgraph.json`.

## Configuration

All environment-specific settings — model endpoints, search indexes, auth, and
integration credentials — are supplied via environment variables, loaded from a
`.env` file at startup. No client- or environment-specific values are committed
to the source tree.

## Testing

Deterministic, offline test suites live under `src/`; run them with your Python
test runner, for example:

```bash
uv run pytest src
```

## Development workflow

Changes flow through a promotion pipeline:

```
feature/* → develop → stage → main
```

- **develop** — integration branch; every merge auto-deploys to the dev
  environment.
- **stage** — pre-release; the exact dev-built image is promoted (build once,
  deploy many).
- **main** — the versioned, release-ready source.

Every change lands via a pull request with a Conventional-Commit title and a
filled-in description.

## Releases

Public releases are cut as `release/X.Y.Z` branches with a matching `vX.Y.Z`
tag. Each release is a sanitized, versioned snapshot of the source.
Release branches are immutable: pushed once, never force-updated.

## License

See [LICENSE](LICENSE) if present; otherwise all rights reserved by the project
maintainers.
