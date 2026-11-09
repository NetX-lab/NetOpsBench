"""Bundled diagnostic agents; backend dependencies load only when selected."""

__all__ = ["MinimalDeepAgent", "CodexAgent"]


def __getattr__(name):
    if name == "MinimalDeepAgent":
        from .minimal_deepagent.agent import MinimalDeepAgent
        return MinimalDeepAgent
    if name == "CodexAgent":
        from .codex_agent.agent import CodexAgent
        return CodexAgent
    raise AttributeError(name)
