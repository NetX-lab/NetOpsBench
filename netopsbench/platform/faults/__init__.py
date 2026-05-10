"""Fault injection module for NetOpsBench."""

from .injector import FaultInjector
from .scenario_execution import inject_fault, recover_fault

__all__ = ["FaultInjector", "inject_fault", "recover_fault"]
