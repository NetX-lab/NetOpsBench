"""Interface parsing helpers for device toolkit internals."""

from __future__ import annotations

import re
from typing import Any

from netopsbench.platform.utils.interface_names import resolve_interface_metric_identities

from .text_parsers import coerce_value, extract_interface_name, normalize_key


def merge_interface_tables(
    status_rows: list[dict[str, str]], counter_rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    def add_rows(rows: list[dict[str, str]]):
        for row in rows:
            name = extract_interface_name(row)
            if not name:
                continue
            entry = merged.setdefault(name, {"name": name})
            for key, value in row.items():
                if key in {"Interface", "IFACE", "Port", "PORT", "Name", "Iface"}:
                    continue
                entry[normalize_key(key)] = coerce_value(value)

    add_rows(status_rows)
    add_rows(counter_rows)
    return list(merged.values())


def parse_ip_link_stats(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    lines = text.splitlines()
    results: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = re.match(r"^\d+:\s+([^:]+):", line)
        if not match:
            i += 1
            continue
        name = match.group(1).split("@", 1)[0]
        entry: dict[str, Any] = {"name": name}
        mtu_match = re.search(r"\bmtu\s+(\d+)", line)
        if mtu_match:
            entry["mtu"] = int(mtu_match.group(1))
        state = re.search(r"\bstate\s+(\S+)", line)
        if state:
            entry["state"] = state[1]
        flags = re.search(r"<([^>]*)>", line)
        if flags:
            entry["admin"] = "up" if "UP" in flags[1].split(",") else "down"
        i += 1
        while i < len(lines) and lines[i].startswith(" "):
            sub = lines[i].strip()
            if sub.startswith("RX:"):
                headers = sub.replace("RX:", "").split()
                if i + 1 < len(lines):
                    values = lines[i + 1].strip().split()
                    for h, v in zip(headers, values, strict=False):
                        entry[f"rx_{normalize_key(h)}"] = coerce_value(v)
                i += 2
                continue
            if sub.startswith("TX:"):
                headers = sub.replace("TX:", "").split()
                if i + 1 < len(lines):
                    values = lines[i + 1].strip().split()
                    for h, v in zip(headers, values, strict=False):
                        entry[f"tx_{normalize_key(h)}"] = coerce_value(v)
                i += 2
                continue
            i += 1
        results.append(entry)
    return results


def select_live_interface(interfaces: list[dict], interface: str) -> dict[str, Any] | None:
    candidate_names = set(resolve_interface_metric_identities(interface)["names"])
    for entry in interfaces:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name in candidate_names:
            return entry
    return None


def active_interface_names(interfaces: list[dict]) -> list[str]:
    active: list[str] = []
    for entry in interfaces:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name.startswith("Ethernet"):
            continue
        oper = str(entry.get("oper") or entry.get("state") or "").strip().lower()
        admin = str(entry.get("admin") or "").strip().lower()
        if oper in {"up", "u"} or (not oper and admin in {"up", "u"}):
            active.append(name)
    return sorted(set(active))
