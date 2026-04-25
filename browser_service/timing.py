"""Performance measurement helpers."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable

log = logging.getLogger("browser_service.timing")


def _emit(label: str, elapsed: float) -> None:
    # Centralized formatter keeps decorator/context-manager output consistent.
    log.info("\u23f1 %s: %.2fs", label, elapsed)


def timed(func_or_label: Any = None, *, label: str | None = None) -> Any:
    """Decorator that logs the wall-clock duration of a function.

    Usage:
        @timed
        def f(): ...

        @timed(label="custom")
        def g(): ...
    """
    def _wrap(fn: Callable[..., Any], chosen_label: str) -> Callable[..., Any]:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            # perf_counter provides monotonic, high-resolution timing.
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                _emit(chosen_label, time.perf_counter() - start)
        return inner

    if callable(func_or_label):
        fn = func_or_label
        return _wrap(fn, label or fn.__qualname__)

    chosen = label or (func_or_label if isinstance(func_or_label, str) else None)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return _wrap(fn, chosen or fn.__qualname__)

    return decorator


@contextmanager
def step_timer(label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        _emit(label, time.perf_counter() - start)
