from netopsbench.platform.toolkit.toolkit import AgentToolkit

_toolkit: AgentToolkit | None = None


def get_toolkit() -> AgentToolkit:
    global _toolkit
    if _toolkit is None:
        _toolkit = AgentToolkit()
    return _toolkit


def as_payload(result):
    return result.data if result.success else {"error": result.error}
