from netopsbench.platform.toolkit.public import public_tool_result
from netopsbench.platform.toolkit.toolkit import AgentToolkit

_toolkit: AgentToolkit | None = None


def get_toolkit() -> AgentToolkit:
    global _toolkit
    if _toolkit is None:
        _toolkit = AgentToolkit()
    return _toolkit


def as_payload(result):
    payload = public_tool_result(result)
    return payload["data"] if payload["success"] else {"error": payload["error"]}
