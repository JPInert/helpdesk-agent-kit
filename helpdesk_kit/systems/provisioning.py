"""The provisioning portal (users, jobs, seat pools), driven in a real browser.

Wraps `portal.Portal`: reads go through the portal's internal JSON from inside the page,
writes go through the real Retry button. It is also the identity system: it is how a name
or an email becomes a user, and later adapters look people up by what it found.
"""

from __future__ import annotations

from ..classify import TicketType
from ..facts import Facts
from ..portal import FlipResult, Portal
from .base import AdapterError, Subject, SystemAdapter


class ProvisioningAdapter(SystemAdapter):
    name = "provisioning"
    serves = frozenset({TicketType.LOGIN_FAILED.value, TicketType.ACCESS_PENDING.value,
                        TicketType.LICENSE_NOT_ASSIGNED.value, TicketType.ACCOUNT_SUSPENDED.value,
                        TicketType.BILLING_QUESTION.value})

    def __init__(self, portal: Portal):
        self.portal = portal

    def __enter__(self) -> "ProvisioningAdapter":
        self.portal.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.portal.__exit__(*exc)

    # identity
    def find_users(self, query: str) -> list[dict]:
        return self.portal.find_users(query)

    # reads
    def collect(self, subject: Subject) -> Facts:
        user_id = subject.ids.get(self.name)
        if not user_id:
            raise AdapterError("no portal account to read jobs for")
        facts = Facts()
        facts.set("jobs", self.portal.jobs(user_id), self.name)
        facts.set("seats", self.portal.seats(), self.name)
        return facts

    # writes
    def retry_job(self, user_id: str, job_id: str) -> None:
        self.portal.retry_job(user_id, job_id)

    def wait_for_flip(self, user_id: str, job_id: str, timeout_s: float) -> FlipResult:
        return self.portal.wait_for_flip(user_id, job_id, timeout_s=timeout_s)
