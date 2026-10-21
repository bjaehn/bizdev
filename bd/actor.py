"""Who is acting, for the duration of one signed-in request.

The hosted app sets it while a page is handled; the audit log, notes and jobs read it, so a change says who made it ("Ana Cruz", or "Owner" for the shared
password) without every function taking a person. Outside a request (the CLI, background jobs, tests) it is unset and callers keep the actor they were given."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("bd_actor", default=None)
_current_id: ContextVar[int | None] = ContextVar("bd_actor_id", default=None)
UI_DEFAULT = "ui"                      # what UI code passes when it does not name a person


def current() -> str | None:
    return _current.get()


def current_id() -> int | None:
    """The signed-in person's id (0 = the owner), or None outside a signed-in request."""
    return _current_id.get()


@contextmanager
def acting_as(name: str | None, user_id: int | None = None) -> Iterator[None]:
    token, token_id = _current.set(name or None), _current_id.set(user_id)
    try:
        yield
    finally:
        _current.reset(token)
        _current_id.reset(token_id)


def resolve(actor: str) -> str:
    """The person acting, when the caller only said "ui"; any other actor (a scanner, a script, a named step) is kept as given."""
    return (_current.get() or actor) if actor == UI_DEFAULT else actor
