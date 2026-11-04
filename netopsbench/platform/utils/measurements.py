"""Private operation counts, scoped to execution and propagated to worker tasks."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

_scopes = ContextVar("operation_measurements", default=())
_lock = Lock()


def count_operation(name, amount=1):
    with _lock:
        for record in _scopes.get():
            record[name] = record.get(name, 0) + amount


@contextmanager
def measurement_scope(record):
    token = _scopes.set((*_scopes.get(), record))
    try:
        yield record
    finally:
        _scopes.reset(token)
