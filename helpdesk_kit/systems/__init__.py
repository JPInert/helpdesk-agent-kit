"""Adapter registry: which back-office systems this run can talk to.

    register("name", factory)      factory(settings) -> adapter, or None when not configured
    SystemSet.from_settings(s)     the adapters that are configured, opened lazily

An adapter is only opened (browser launched, session created) the first time something asks
for it, and everything is closed together when the ticket is done. Adding a system is one
class and one `register` call; the pipeline does not change.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Callable

from ..config import Settings
from ..portal import Portal
from .activity import SessionLogAdapter
from .base import AdapterError, Subject, SystemAdapter
from .entitlements import ApprovalsAdapter
from .provisioning import ProvisioningAdapter

__all__ = ["AdapterError", "Subject", "SystemAdapter", "SystemSet", "register", "REGISTRY"]

Factory = Callable[[Settings], "SystemAdapter | None"]
REGISTRY: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    REGISTRY[name] = factory


register("provisioning", lambda s: ProvisioningAdapter(Portal(s.portal_url, s.portal_user, s.portal_password))
         if s.portal_url else None)
register("approvals", lambda s: ApprovalsAdapter(s.approvals_url, s.approvals_user, s.approvals_password)
         if s.approvals_url else None)
register("sessions", lambda s: SessionLogAdapter(s.sessions_url, s.sessions_user, s.sessions_password)
         if s.sessions_url else None)


class SystemSet:
    """The adapters configured for one run, in registry order (earlier systems win a conflict)."""

    def __init__(self, adapters: dict[str, SystemAdapter]):
        self._adapters = adapters
        self._open: dict[str, SystemAdapter] = {}
        self._stack = ExitStack()

    @classmethod
    def from_settings(cls, s: Settings) -> "SystemSet":
        built = {name: f(s) for name, f in REGISTRY.items()}
        return cls({n: a for n, a in built.items() if a is not None})

    @property
    def names(self) -> list[str]:
        return list(self._adapters)

    def serving(self, ticket_type: str) -> list[str]:
        return [n for n, a in self._adapters.items() if ticket_type in a.serves]

    def get(self, name: str) -> SystemAdapter | None:
        """The opened adapter, or None if this run has no such system."""
        if name not in self._adapters:
            return None
        if name not in self._open:
            self._open[name] = self._stack.enter_context(self._adapters[name])
        return self._open[name]

    def __enter__(self) -> "SystemSet":
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()
