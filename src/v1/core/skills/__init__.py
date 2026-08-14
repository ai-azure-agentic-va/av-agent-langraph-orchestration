"""Bundled agent-skills library and its backend mount configuration.

Skills follow Anthropic's Agent Skills pattern as implemented by
``deepagents.middleware.SkillsMiddleware``: each skill is a subdirectory of
this package containing a ``SKILL.md`` with YAML frontmatter (its ``name`` must
match the subdirectory name).

The running agent uses an in-memory ``StateBackend`` (correct for an
API-served graph), which cannot read these files from disk. So we expose this
directory to the agent's backend through a ``CompositeBackend`` route: the
virtual path :data:`SKILLS_MOUNT` is served by a ``FilesystemBackend`` scoped
(``virtual_mode=True``) to this directory. Skill ``read_file`` calls and the
``SkillsMiddleware`` loader both resolve through that route; everything else
stays in memory.
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend

# Virtual path at which the skills library is mounted in the agent backend.
# Both the CompositeBackend route and each consumer's ``skills`` source must
# use this exact prefix so loader paths round-trip.
SKILLS_MOUNT = "/skills/"

# On-disk directory holding the skill packages (one subdir per skill). Resolved
# relative to this file so it works regardless of CWD — local run, ``langgraph
# dev``, or the generated Docker image.
SKILLS_DIR = Path(__file__).resolve().parent

# Source list handed to ``create_deep_agent(skills=...)`` for the main agent
# (a subagent's ``skills`` field would take the same value).
SKILLS_SOURCES: list[str] = [SKILLS_MOUNT]


def build_skills_backend() -> FilesystemBackend:
    """Filesystem backend scoped to the bundled skills directory.

    ``virtual_mode=True`` anchors all paths under :data:`SKILLS_DIR` and blocks
    traversal (``..``, ``~``) so the route cannot reach outside the library.
    """
    return FilesystemBackend(root_dir=str(SKILLS_DIR), virtual_mode=True)


__all__ = ["SKILLS_MOUNT", "SKILLS_DIR", "SKILLS_SOURCES", "build_skills_backend"]
