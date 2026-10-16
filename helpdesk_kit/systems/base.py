"""The adapter interface every back-office system implements.

An adapter owns everything about talking to ONE system: how it logs in, how it notices the
session died, which header it needs, and how to turn its answers into facts. The pipeline
never knows any of that; it asks the registry for the adapters that serve a ticket type and
merges what they return.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..facts import Facts


class AdapterError(RuntimeError):
    """A system could not be read. Not fatal: the issue is routed to a person with the reason."""


@dataclass
class Subject:
    """Who the lookup is about, as far as the earlier systems have established."""
    name: str = ""
    username: str = ""
    email: str = ""
    ids: dict[str, str] = field(default_factory=dict)      # system name -> that system's id for the person


class SystemAdapter:
    name: str = ""
    serves: frozenset[str] = frozenset()                    # TicketType values this adapter is read for

    def __enter__(self) -> "SystemAdapter":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def collect(self, subject: Subject) -> Facts:
        raise NotImplementedError
