"""Builtin fault spec families grouped by domain."""

from .acl_specs import build_acl_fault_specs
from .impairment_specs import build_impairment_fault_specs
from .link_specs import build_link_fault_specs
from .routing_specs import build_routing_fault_specs
from .system_specs import build_system_fault_specs

__all__ = [
    "build_acl_fault_specs",
    "build_impairment_fault_specs",
    "build_link_fault_specs",
    "build_routing_fault_specs",
    "build_system_fault_specs",
]
