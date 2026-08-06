"""Event loop selection for psycopg's async driver.

On Windows, asyncio defaults to ``ProactorEventLoop``, which psycopg cannot drive
in async mode. The supported fix is to run on a selector loop.

``asyncio.run(..., loop_factory=...)`` is used rather than
``set_event_loop_policy``, which is deprecated from Python 3.14. On non-Windows
platforms the default loop is already compatible and is left alone.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

IS_WINDOWS = sys.platform == "win32"


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Return the loop factory to use, or None to accept the platform default."""
    return asyncio.SelectorEventLoop if IS_WINDOWS else None


def run(coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with a database-compatible loop."""
    factory = loop_factory()
    if factory is None:
        return asyncio.run(coro)
    return asyncio.run(coro, loop_factory=factory)
