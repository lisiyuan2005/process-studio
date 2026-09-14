"""Stopping a process step that is already running.

A step is one call from the outside — ``device.deposit(...)`` — but inside
it walks hundreds of z samples and rebuilds the stack, which on a real
structure takes seconds to minutes. Without a way in, a caller that wants
to stop can only wait for the whole call to return, so a Stop button does
nothing for as long as the step lasts.

The hook is a callable installed for the duration of a call:
``with cancelling(flag.is_set): device.deposit(...)``. The loops that cost
the time call :func:`check_cancelled` every so often, and the step gives
up at the next check by raising :class:`Cancelled`. Nothing is written
back on the way out: the state a process function mutates is the caller's
working copy, which it discards.

It is a context variable rather than an argument threaded through every
signature: the geometry functions call each other several levels deep, and
the check belongs to "this thread is running this step", not to any one
function's contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

#: Set while a step runs; None means nothing can stop it (a library call
#: outside a worker, every test that does not ask for cancellation).
_cancel_check: ContextVar[Callable[[], bool] | None] = ContextVar(
    "deviceflow_cancel_check", default=None
)


class Cancelled(Exception):
    """A step gave up because the caller asked it to stop.

    Deliberately not a ``DeviceFlowError``: that is the family of "this
    process cannot be done", which callers translate into a message about
    the recipe. Stopping is neither an error in the recipe nor in the
    geometry, and must travel past those handlers untouched.
    """


@contextmanager
def cancelling(check: Callable[[], bool] | None) -> Iterator[None]:
    """Install ``check`` as the current cancellation check for this context."""
    token = _cancel_check.set(check)
    try:
        yield
    finally:
        _cancel_check.reset(token)


def check_cancelled() -> None:
    """Raise :class:`Cancelled` if the caller has asked this step to stop."""
    check = _cancel_check.get()
    if check is not None and check():
        raise Cancelled("The step was stopped before it finished.")
