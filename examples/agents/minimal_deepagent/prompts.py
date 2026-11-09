"""DeepAgent output framing over the shared diagnosis prompt."""

from examples.agents.diagnosis import SYSTEM_GUIDANCE, build_user_prompt

DEFAULT_SYSTEM_PROMPT = SYSTEM_GUIDANCE + "Return one fenced JSON block matching DiagnosisOutput."

__all__ = ["DEFAULT_SYSTEM_PROMPT", "build_user_prompt"]
