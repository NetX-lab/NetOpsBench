"""Text/table parsing helpers for device toolkit internals."""

from __future__ import annotations

import re


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def coerce_value(value: str):
    if value is None:
        return value
    text = str(value).strip()
    if text == "":
        return text
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def preview_items(items: list[str], limit: int = 8) -> str:
    if not items:
        return "none"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... (+{len(items) - limit} more)"


def parse_table(text: str) -> list[dict[str, str]]:
    if not text:
        return []
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header_idx = None
    for idx, line in enumerate(lines):
        if re.search(r"\b(Interface|IFACE|Port)\b", line):
            header_idx = idx
            break
    if header_idx is None:
        header_idx = 0
    header_line = lines[header_idx].strip()
    headers = re.split(r"\s{2,}|\t+", header_line)
    if len(headers) <= 1:
        headers = header_line.split()
    rows: list[dict[str, str]] = []
    for line in lines[header_idx + 1 :]:
        stripped_line = line.strip()
        dashed_tokens = re.split(r"\s{2,}|\t+|\s+", stripped_line)
        if dashed_tokens and all(token and set(token) == {"-"} for token in dashed_tokens):
            continue
        parts = re.split(r"\s{2,}|\t+", stripped_line)
        if len(parts) < len(headers):
            parts = line.split()
        if len(parts) < len(headers):
            continue
        if len(parts) > len(headers):
            parts = parts[: len(headers) - 1] + [" ".join(parts[len(headers) - 1 :])]
        rows.append(dict(zip(headers, parts, strict=False)))
    return rows


def extract_interface_name(row: dict[str, str]) -> str | None:
    for key in ("Interface", "IFACE", "Port", "PORT", "Name", "Iface"):
        if key in row:
            name = str(row[key]).strip()
            if name and set(name) != {"-"}:
                return name
            return None
    fallback = next(iter(row.values()), None)
    if fallback is None:
        return None
    fallback_name = str(fallback).strip()
    if fallback_name and set(fallback_name) != {"-"}:
        return fallback_name
    return None
