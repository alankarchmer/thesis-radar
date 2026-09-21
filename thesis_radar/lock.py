"""An exclusive, non-blocking lock so two radar commands never run at once."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import portalocker


class LockHeld(RuntimeError):
    """Another radar command holds the workspace lock."""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            portalocker.lock(handle, portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING)
        except portalocker.exceptions.BaseLockException as exc:
            raise LockHeld(f"another radar command is running (lock: {path})") from exc
        try:
            yield
        finally:
            portalocker.unlock(handle)
    finally:
        handle.close()
