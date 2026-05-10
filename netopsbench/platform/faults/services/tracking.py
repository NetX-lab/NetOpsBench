"""Helpers for structured active fault tracking."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from netopsbench.platform.faults.models import ActiveFault


class FaultTracker:
    """Manages active fault state and background fault control loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active_faults: list[ActiveFault] = []
        self._background_fault_controls: dict[str, Any] = {}

    def track(self, fault_info: dict) -> ActiveFault:
        fault = fault_info if isinstance(fault_info, ActiveFault) else ActiveFault.from_dict(fault_info)
        with self._lock:
            self.active_faults.append(fault)
        return fault

    def register_background_control(self, control_id: str, *, stop_event: Any, thread: Any) -> None:
        with self._lock:
            self._background_fault_controls[control_id] = {"stop_event": stop_event, "thread": thread}

    def stop_background(self, control_id: str, *, join_timeout: float = 1.0) -> bool:
        with self._lock:
            control = self._background_fault_controls.pop(control_id, None)
        if control is None:
            return True
        stop_event = control.get("stop_event")
        thread = control.get("thread")
        if stop_event is not None:
            stop_event.set()
        if thread is not None and hasattr(thread, "join") and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        return True

    def remove_faults(self, predicate: Callable[[ActiveFault], bool]) -> None:
        with self._lock:
            self.active_faults = [fault for fault in self.active_faults if not predicate(fault)]

    def active_fault_dicts(self) -> list[dict]:
        with self._lock:
            return [fault.to_dict() if isinstance(fault, ActiveFault) else dict(fault) for fault in self.active_faults]
