"""Compatibility aggregate mixin for device toolkit operations."""

from __future__ import annotations

from .connectivity_ops import ConnectivityOpsMixin
from .interface_ops import InterfaceOpsMixin
from .log_ops import LogOpsMixin
from .routing_ops import RoutingOpsMixin


class DeviceOpsMixin(InterfaceOpsMixin, RoutingOpsMixin, LogOpsMixin, ConnectivityOpsMixin):
    """Aggregate device toolkit operations mixin."""


__all__ = [
    "ConnectivityOpsMixin",
    "DeviceOpsMixin",
    "InterfaceOpsMixin",
    "LogOpsMixin",
    "RoutingOpsMixin",
]
