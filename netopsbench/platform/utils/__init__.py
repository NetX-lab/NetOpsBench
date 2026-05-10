"""Platform utility subsystem."""

from .interface_names import (
    are_interfaces_equivalent,
    interface_aliases,
    normalize_interface_name,
    to_linux_interface,
    to_sonic_interface,
)
from .proc import sudo_prefix

__all__ = [
    "are_interfaces_equivalent",
    "interface_aliases",
    "normalize_interface_name",
    "to_linux_interface",
    "to_sonic_interface",
    "sudo_prefix",
]
