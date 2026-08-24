"""Small, shared semantics for interpreting interface log events."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_CYCLE_RE = re.compile(r"\b(?:down\s+(?:then|and)\s+up|up\s+(?:then|and)\s+down)\b", re.IGNORECASE)
_UP_TO_DOWN_RE = re.compile(r"\b(?:up\s+to\s+down|link\s+down)\b", re.IGNORECASE)
_DOWN_TO_UP_RE = re.compile(r"\b(?:down\s+to\s+up|link\s+up)\b", re.IGNORECASE)
_FLAP_RE = re.compile(r"\b(?:flap|flapping|session_flap)\b", re.IGNORECASE)
_STATE_RE = re.compile(r"\b(?:oper state|status)[\"':=\s]+(?:up|down)\b", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"\b(?:oper error|no_rx_reachability|crc(?:_rate)?|fec|ber|code_group_error|"
    r"data_unit_(?:misalignment|size)|signal_local_error)\b",
    re.IGNORECASE,
)


def classify_log_signal(message: str) -> str | None:
    """Classify a log without conflating port errors with a flap cycle."""
    text = str(message or "")
    if not text:
        return None
    if _CYCLE_RE.search(text):
        return "state_cycle"
    if _UP_TO_DOWN_RE.search(text):
        return "up_to_down"
    if _DOWN_TO_UP_RE.search(text):
        return "down_to_up"
    if _FLAP_RE.search(text):
        return "flap_marker"
    if _STATE_RE.search(text):
        return "state_change"
    if _ERROR_RE.search(text):
        return "error_signal"
    return None


def signal_from_value(value: Any) -> str | None:
    """Read a normalized temporal signal from Evidence.value."""
    if not isinstance(value, Mapping):
        return None
    declared = str(value.get("temporal_signal") or "")
    if declared:
        return declared
    return classify_log_signal(str(value.get("message") or value.get("observation") or ""))


def is_temporal_signal(signal: str | None) -> bool:
    return signal in {"state_cycle", "up_to_down", "down_to_up", "flap_marker", "state_change"}


def is_repeated_flap_sequence(signals: Iterable[str | None]) -> bool:
    """Require a state alternation or an explicit complete flap cycle."""
    observed = [signal for signal in signals if signal]
    kinds = set(observed)
    return "state_cycle" in kinds or {"up_to_down", "down_to_up"}.issubset(kinds) or observed.count("flap_marker") >= 2


def is_repeated_bgp_state_sequence(states: Iterable[object] | None) -> bool:
    """Require a BGP state cycle, not the single down transition of a config fault."""
    if states is None or isinstance(states, (str, bytes)):
        return False
    observed = [str(state).upper() for state in states if state is not None]
    return len(observed) >= 3 and len(set(observed)) >= 2 and observed[0] == observed[-1]
