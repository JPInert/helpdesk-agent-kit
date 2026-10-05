"""Drive a back-office portal that has no public API, with Playwright.

Reads do not click through pages. Once logged in, the agent calls the portal's own internal
JSON endpoints with fetch() from inside the page, exactly as the portal's scripts do, which
is faster and far less brittle than scraping tables. Writes go through the real UI (the
Retry button and its CSRF form) because that is the path the portal supports.

Rules this module keeps, each of which has broken a real automation:

* Never open the login URL to "check" the session. On many portals, this one included,
  that URL logs you out. Probe the site root; it redirects to login only if the session is
  really gone.
* A navigation wipes anything injected into `window`, so never rely on a patched page
  surviving a goto(). Call fetch() fresh each time.
* A read straight after a write can be stale. After a retry, poll until the status actually
  changes, with a deadline, and report "unconfirmed" rather than guessing.
* Close only the tabs this run opened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


class PortalError(RuntimeError):
    pass


@dataclass
class FlipResult:
    confirmed: bool
    status: str
    error: str | None
    waited_s: float


class Portal:
    def __init__(self, base_url: str, username: str, password: str, headless: bool = True):
        self.base = base_url.rstrip("/")
        self.username, self.password = username, password
        self.headless = headless
        self._pw = self._browser = self._context = self.page = None
        self._baseline: set = set()

    # ---- lifecycle ----------------------------------------------------------------------
    def __enter__(self) -> "Portal":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context()
        self._baseline = set(self._context.pages)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            for p in list(self._context.pages if self._context else []):
                if p not in self._baseline:  # reap only what this run opened
                    p.close()
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()

    # ---- session ------------------------------------------------------------------------
    def ensure_session(self) -> None:
        self.page.goto(self.base + "/")          # NOT /login: that would end the session
        if self.page.url.rstrip("/").endswith("/login"):
            self.page.fill("#username", self.username)
            self.page.fill("#password", self.password)
            self.page.click("#signin")
            self.page.wait_for_load_state()
            if self.page.url.rstrip("/").endswith("/login"):
                raise PortalError("portal login failed (check PORTAL_USER / PORTAL_PASSWORD)")

    def _api(self, path: str, _retried: bool = False):
        if not self.page.url.startswith(self.base) or self.page.url.rstrip("/").endswith("/login"):
            self.ensure_session()
        try:
            return self.page.evaluate("p => window.portalApi(p)", path)
        except Exception as e:
            if _retried:
                raise PortalError(f"internal API call failed: {path}") from None
            # Most often the session expired under us. Re-establish once, then give up.
            self.ensure_session()
            return self._api(path, _retried=True)

    # ---- reads --------------------------------------------------------------------------
    def find_users(self, query: str) -> list[dict]:
        from urllib.parse import quote
        return self._api(f"/internal/api/users?q={quote(query)}")

    def jobs(self, user_id: str) -> list[dict]:
        return self._api(f"/internal/api/users/{user_id}/jobs")

    def seats(self) -> list[dict]:
        return self._api("/internal/api/seats")

    # ---- writes (through the real UI) -----------------------------------------------------
    def retry_job(self, user_id: str, job_id: str) -> None:
        self.ensure_session()
        self.page.goto(f"{self.base}/users/{user_id}")
        button = self.page.locator(f"button.retry[data-job='{job_id}']")
        if button.count() != 1:
            raise PortalError(f"no Retry button for {job_id} (already processed?)")
        with self.page.expect_navigation():
            button.click()
        status = self.page.locator(f"#job-{job_id} .status").inner_text().strip()
        if status not in ("queued", "done"):
            raise PortalError(f"retry did not take: {job_id} shows '{status}'")

    def wait_for_flip(self, user_id: str, job_id: str, timeout_s: float = 20,
                      interval_s: float = 1.0) -> FlipResult:
        """Poll the (possibly stale) internal API until the job leaves pending/queued."""
        start = time.monotonic()
        job: dict = {}
        while True:
            job = next((j for j in self.jobs(user_id) if j["id"] == job_id), {})
            if job.get("status") not in ("pending", "queued"):
                return FlipResult(True, job.get("status", "missing"), job.get("error"),
                                  time.monotonic() - start)
            if time.monotonic() - start >= timeout_s:
                return FlipResult(False, job.get("status", "missing"), job.get("error"),
                                  time.monotonic() - start)
            time.sleep(interval_s)
