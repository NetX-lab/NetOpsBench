"""Official public example agents for NetOpsBench.

Agent implementations are imported lazily so deterministic wrappers can be
used without importing optional LLM dependencies.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .minimal_deepagent.agent import MinimalDeepAgent


def __getattr__(name: str) -> Any:
    if name == "MinimalDeepAgent":
        from .minimal_deepagent.agent import MinimalDeepAgent

        return MinimalDeepAgent
    raise AttributeError(name)


__all__ = ["MinimalDeepAgent"]
