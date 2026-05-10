"""Helpers for normalizing interface names across SONiC, Linux, and legacy labels."""

import re

_SONIC_INTERFACE_RE = re.compile(r"^ethernet(\d+)$", re.IGNORECASE)
_LINUX_INTERFACE_RE = re.compile(r"^eth(\d+)$", re.IGNORECASE)
_E_STYLE_INTERFACE_RE = re.compile(r"^e(\d+)-(\d+)$", re.IGNORECASE)
_VENDOR_STYLE_INTERFACE_RE = re.compile(r"^ethernet-(\d+)/(\d+)$", re.IGNORECASE)


def normalize_interface_name(interface_name: str | None) -> str:
    """Normalize known interface aliases to a compact ethN-style token."""
    if not interface_name:
        return ""

    raw_name = str(interface_name).strip()
    if not raw_name:
        return ""

    sonic_match = _SONIC_INTERFACE_RE.match(raw_name)
    if sonic_match:
        port_idx = int(sonic_match.group(1))
        if port_idx % 4 == 0:
            return f"eth{(port_idx // 4) + 1}"
        return raw_name.lower()

    linux_match = _LINUX_INTERFACE_RE.match(raw_name)
    if linux_match:
        return f"eth{int(linux_match.group(1))}"

    e_style_match = _E_STYLE_INTERFACE_RE.match(raw_name)
    if e_style_match:
        return f"eth{int(e_style_match.group(2))}"

    vendor_style_match = _VENDOR_STYLE_INTERFACE_RE.match(raw_name)
    if vendor_style_match:
        return f"eth{int(vendor_style_match.group(2))}"

    normalized = raw_name.lower().replace("ethernet-", "eth").replace("ethernet", "eth")
    return normalized.replace("/", "").replace("-", "")


def to_linux_interface(interface_name: str | None) -> str:
    """Convert a known interface label to Linux ethN form when possible."""
    normalized = normalize_interface_name(interface_name)
    linux_match = _LINUX_INTERFACE_RE.match(normalized)
    if linux_match:
        return f"eth{int(linux_match.group(1))}"
    return str(interface_name or "").strip()


def to_sonic_interface(interface_name: str | None) -> str:
    """Convert a known interface label to SONiC EthernetX form when possible."""
    normalized = normalize_interface_name(interface_name)
    linux_match = _LINUX_INTERFACE_RE.match(normalized)
    if linux_match:
        eth_idx = int(linux_match.group(1))
        if eth_idx >= 1:
            return f"Ethernet{(eth_idx - 1) * 4}"

    sonic_match = _SONIC_INTERFACE_RE.match(str(interface_name or "").strip())
    if sonic_match:
        return f"Ethernet{int(sonic_match.group(1))}"
    return str(interface_name or "").strip()


def interface_aliases(interface_name: str | None) -> set[str]:
    """Return equivalent labels for one front-panel interface."""
    aliases: set[str] = set()
    if not interface_name:
        return aliases

    raw_name = str(interface_name).strip()
    if not raw_name:
        return aliases
    aliases.add(raw_name)

    normalized = normalize_interface_name(raw_name)
    if normalized:
        aliases.add(normalized)

    linux_match = _LINUX_INTERFACE_RE.match(normalized or "")
    if linux_match:
        eth_idx = int(linux_match.group(1))
        if eth_idx >= 1:
            aliases.add(f"eth{eth_idx}")
            aliases.add(f"e1-{eth_idx}")
            aliases.add(f"ethernet-1/{eth_idx}")
            aliases.add(f"Ethernet{(eth_idx - 1) * 4}")

    sonic_name = to_sonic_interface(raw_name)
    if sonic_name:
        aliases.add(sonic_name)

    linux_name = to_linux_interface(raw_name)
    if linux_name:
        aliases.add(linux_name)

    return aliases


def are_interfaces_equivalent(left: str | None, right: str | None) -> bool:
    """Return True when two labels refer to the same front-panel interface."""
    if not left or not right:
        return False
    if normalize_interface_name(left) == normalize_interface_name(right):
        return True
    return bool(interface_aliases(left) & interface_aliases(right))
