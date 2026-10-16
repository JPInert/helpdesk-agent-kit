"""The fixes the agent knows how to run, one entry per action name used in kb/rules.yaml.

An action says which system it writes to, how to run the write, how to confirm it took (by
re-reading, never by trusting the write's own answer), and what status the reply may then
claim. Whether an action may run at all is decided in safety.py, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .portal import FlipResult


@dataclass(frozen=True)
class Action:
    name: str
    system: str
    done_status: str                  # what the system shows when the fix really worked
    new_status: str                   # the word the reply may use once confirmed
    run: Callable[[object, dict], None]
    confirm: Callable[[object, dict, float], FlipResult]


def _retry_run(adapter, target: dict) -> None:
    adapter.retry_job(target["user_id"], target["id"])


def _retry_confirm(adapter, target: dict, timeout_s: float) -> FlipResult:
    return adapter.wait_for_flip(target["user_id"], target["id"], timeout_s=timeout_s)


def _close_run(adapter, target: dict) -> None:
    if not target.get("last_activity"):
        raise ValueError("no last activity recorded; cannot pick an end time")
    adapter.close_session(target["id"], target["last_activity"])


def _close_confirm(adapter, target: dict, timeout_s: float) -> FlipResult:
    return adapter.wait_for_flip(target["username"], target["id"], timeout_s=timeout_s)


ACTIONS: dict[str, Action] = {
    "retry_job": Action("retry_job", "provisioning", "done", "active", _retry_run, _retry_confirm),
    "close_session": Action("close_session", "sessions", "valid", "valid", _close_run, _close_confirm),
}
