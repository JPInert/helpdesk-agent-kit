"""Every setting comes from the environment (or a .env you load yourself). Nothing secret
has a default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env support (KEY=value lines). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    # Zendesk (only needed for --live; the demo reads fixtures)
    zendesk_subdomain: str = field(default_factory=lambda: os.environ.get("ZENDESK_SUBDOMAIN", ""))
    zendesk_email: str = field(default_factory=lambda: os.environ.get("ZENDESK_EMAIL", ""))
    zendesk_api_token: str = field(default_factory=lambda: os.environ.get("ZENDESK_API_TOKEN", ""))
    zendesk_view_id: str = field(default_factory=lambda: os.environ.get("ZENDESK_VIEW_ID", ""))

    # Model. Unset ANTHROPIC credentials => offline mode (keyword classifier, sidecar vision).
    model: str = field(default_factory=lambda: os.environ.get("HDK_MODEL", "claude-opus-5-5"))

    # The back-office portal the agent drives with a browser
    portal_url: str = field(default_factory=lambda: os.environ.get("PORTAL_URL", "http://127.0.0.1:8750"))
    portal_user: str = field(default_factory=lambda: os.environ.get("PORTAL_USER", "demo"))
    portal_password: str = field(default_factory=lambda: os.environ.get("PORTAL_PASSWORD", "demo-password"))

    # Extra back-office systems. Empty URL = the adapter is not registered for this run.
    approvals_url: str = field(default_factory=lambda: os.environ.get("APPROVALS_URL", ""))
    approvals_user: str = field(default_factory=lambda: os.environ.get("APPROVALS_USER", "demo"))
    approvals_password: str = field(default_factory=lambda: os.environ.get("APPROVALS_PASSWORD", "demo-password"))
    sessions_url: str = field(default_factory=lambda: os.environ.get("SESSIONS_URL", ""))
    sessions_user: str = field(default_factory=lambda: os.environ.get("SESSIONS_USER", "demo"))
    sessions_password: str = field(default_factory=lambda: os.environ.get("SESSIONS_PASSWORD", "demo-password"))

    # Requesters at this domain are "internal" and may file on behalf of someone else
    staff_domain: str = field(default_factory=lambda: os.environ.get("STAFF_DOMAIN", "example-corp.test"))

    db_path: Path = field(default_factory=lambda: Path(os.environ.get("HDK_DB", ROOT / "data" / "kit.db")))
    kb_path: Path = field(default_factory=lambda: Path(os.environ.get("HDK_KB", ROOT / "kb" / "rules.yaml")))
    fixtures_dir: Path = ROOT / "fixtures"

    # Rules a person approved from the proposals queue are written here and loaded with kb_path
    approved_rules_path: Path = field(default_factory=lambda: Path(
        os.environ.get("HDK_APPROVED_RULES", ROOT / "data" / "approved-rules.yaml")))

    # Sign-off appended to every reply by the send stage
    sign_off: str = "Thanks,\nIT Help Desk"

    # Review dashboard. No default: the API routes stay off until you set a token.
    dashboard_token: str = field(default_factory=lambda: os.environ.get("DASHBOARD_TOKEN", ""))

    # A provisioning job pending longer than this is "stuck"
    stuck_after_minutes: int = 30
    # How long to keep re-reading after a fix before calling it unconfirmed
    flip_timeout_s: float = 20.0


def llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
