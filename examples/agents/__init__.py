"""Bundled diagnostic agents; backend dependencies load only when selected."""

__all__ = ["MinimalDeepAgent", "CodexAgent", "AntigravityAgent"]


def __getattr__(name):
    if name == "MinimalDeepAgent":
        from .minimal_deepagent.agent import MinimalDeepAgent

        return MinimalDeepAgent
    if name == "CodexAgent":
        from .codex_agent.agent import CodexAgent

        return CodexAgent
    if name == "AntigravityAgent":
        from .antigravity_agent import AntigravityAgent

        return AntigravityAgent
    raise AttributeError(name)
