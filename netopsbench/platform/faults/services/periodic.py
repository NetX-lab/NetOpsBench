"""One cancellable controller for periodic link and prefix faults."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from netopsbench.platform.utils.control import OperationCancelled, OperationControl, operation_scope


class PeriodicController:
    def __init__(self, fault: Callable[[], None], healthy: Callable[[], None], *, down_time: float, up_time: float):
        if min(down_time, up_time) <= 0:
            raise ValueError("Periodic fault intervals must be positive")
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.history = [{"phase": "fault", "time": datetime.now(UTC).isoformat()}]
        self._fault, self._healthy = fault, healthy
        self._down, self._up = down_time, up_time
        self.thread = threading.Thread(target=self._run, daemon=True, name="network-periodic-fault")

    def start(self) -> None:
        # The first fault phase belongs to the injection transaction.
        started_at = datetime.now(UTC).isoformat()
        self._fault()
        self.history[0] = {"phase": "fault", "started_at": started_at, "time": datetime.now(UTC).isoformat()}
        self.thread.start()

    def _run(self) -> None:
        try:
            with operation_scope(OperationControl(cancel_event=self.stop_event)):
                while not self.stop_event.wait(self._down):
                    started_at = datetime.now(UTC).isoformat()
                    self._healthy()
                    self.history.append(
                        {"phase": "healthy", "started_at": started_at, "time": datetime.now(UTC).isoformat()}
                    )
                    if self.stop_event.wait(self._up):
                        break
                    started_at = datetime.now(UTC).isoformat()
                    self._fault()
                    self.history.append(
                        {"phase": "fault", "started_at": started_at, "time": datetime.now(UTC).isoformat()}
                    )
                    self.history = self.history[-50:]
        except OperationCancelled:
            # Recovery cancels an in-flight controllable command before restoring
            # the original state under its own remaining recovery budget.
            pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.set()
