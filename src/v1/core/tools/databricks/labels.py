"""Names the Databricks workspace in a tool's JSON answer, so the model can always tell the
user which workspace an answer came from."""

import json

WORKSPACE_KEY = "workspace"


def with_workspace(payload: str, label: str) -> str:
    """``payload`` with ``"workspace": label`` added (right after ``status``, if any).

    An answer that already names a workspace keeps it; a payload that isn't a JSON object
    comes back unchanged.
    """

    try:
        content = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(content, dict) or WORKSPACE_KEY in content:
        return payload
    labelled = {"status": content["status"]} if "status" in content else {}
    labelled[WORKSPACE_KEY] = label
    labelled.update(content)
    return json.dumps(labelled, default=str)
