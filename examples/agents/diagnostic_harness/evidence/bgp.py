"""BGP evidence semantics shared by trace adapters and live verification."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

_CONFIGURATION_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "peer_as_mismatch",
        re.compile(r"\b(?:bad peer as|remote[-_ ]?as mismatch|asn? mismatch|peer as mismatch)\b", re.IGNORECASE),
    ),
    (
        "authentication_mismatch",
        re.compile(
            r"\b(?:authentication (?:failure|mismatch)|password mismatch|md5 mismatch|no md5 digest)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "update_source_mismatch",
        re.compile(r"\b(?:update[-_ ]source mismatch|invalid update[-_ ]source)\b", re.IGNORECASE),
    ),
    ("explicit_configuration_error", re.compile(r"\bconfiguration error\b", re.IGNORECASE)),
)


def bgp_configuration_fault_reason(rows: Sequence[Mapping[str, object]]) -> str | None:
    """Return a concrete configuration-failure marker from neighbor detail.

    A non-Established state alone is deliberately insufficient: it is a
    control-plane symptom shared by link, peer-process, reachability, and
    configuration failures.
    """
    payload = json.dumps(list(rows), sort_keys=True, default=str)
    for reason, pattern in _CONFIGURATION_MARKERS:
        if pattern.search(payload):
            return reason
    return None


__all__ = ["bgp_configuration_fault_reason"]
