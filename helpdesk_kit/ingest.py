"""Ingest: turn the API's ticket + users + comments into one clean `tickets` row.

The API alone is not enough for every ticket. These cases need the screenshot (vision) path,
and each was found the hard way:

* Messaging-widget tickets (via.channel == "native_messaging"): the description is a stub
  like "Conversation with Web User"; the real transcript lives in a later comment, and any
  image the user sent shows up only as a plain-text line naming the file.
* Image attachments on any comment.
* Screenshots pasted into an email: they arrive as an <img> inside html_body with NO entry
  in attachments[]. Checking attachments alone misses them.

Triage reads the WHOLE thread, never just the first message, and never trusts the subject.
Names, subjects and text are cleaned of invisible Unicode here (textclean.py), once, so
every later stage compares clean strings.
"""

from __future__ import annotations

import difflib
import html
import re
from datetime import datetime

from . import db
from .textclean import clean_text

MACHINE_SENDERS = ("noreply", "no-reply", "donotreply", "alerts", "monitoring")
IMAGE_NAME = re.compile(r"\b[\w.-]+\.(?:png|jpe?g|gif|webp)\b", re.I)
IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
TAGS = re.compile(r"<[^>]+>")
REPLY_MARKERS = re.compile(r"^(?:>|On .+ wrote:|-{2,}\s*Original Message|From: )", re.M)


def html_to_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s or "", flags=re.I)
    return html.unescape(TAGS.sub("", s)).strip()


def split_reply(text: str) -> tuple[str, str]:
    """(new text, quoted text). For replies to automated report emails the structured data
    is in the quoted part and the user's intent is in the new part above it."""
    m = REPLY_MARKERS.search(text or "")
    if not m:
        return (text or "").strip(), ""
    return text[:m.start()].strip(), text[m.start():].strip()


def vision_reasons(ticket: dict, comments: list[dict]) -> list[str]:
    reasons: list[str] = []
    if (ticket.get("via") or {}).get("channel") == "native_messaging":
        reasons.append("messaging_channel")
        if any(IMAGE_NAME.search(c.get("body") or "") for c in comments):
            reasons.append("messaging_image_mention")
    for c in comments:
        if any((a.get("content_type") or "").startswith("image/") for a in c.get("attachments") or []):
            reasons.append("image_attachment")
        if "<img" in (c.get("html_body") or "").lower():
            reasons.append("inline_image")
    return sorted(set(reasons))


def collect_images(comments: list[dict]) -> list[dict]:
    images: list[dict] = []
    seen: set[str] = set()
    for c in comments:
        for a in c.get("attachments") or []:
            if (a.get("content_type") or "").startswith("image/") and a.get("content_url") not in seen:
                seen.add(a["content_url"])
                images.append({"url": a["content_url"], "content_type": a["content_type"],
                               "file_name": a.get("file_name", "")})
        for src in IMG_SRC.findall(c.get("html_body") or ""):
            if src not in seen:
                seen.add(src)
                ext = src.rsplit(".", 1)[-1].lower()
                images.append({"url": src, "content_type": f"image/{'jpeg' if ext == 'jpg' else ext}",
                               "file_name": src.rsplit("/", 1)[-1]})
    return images


def normalize(payload: dict, comments: list[dict]) -> dict:
    ticket = payload["ticket"]
    users = {u["id"]: u for u in payload.get("users") or []}
    requester = users.get(ticket.get("requester_id"), {})
    thread = [{"author": users.get(c.get("author_id"), {}).get("name", "unknown"),
               "public": bool(c.get("public", True)),
               "text": clean_text(c.get("plain_body") or html_to_text(c.get("html_body") or c.get("body") or ""),
                                  multiline=True)}
              for c in comments]
    channel = (ticket.get("via") or {}).get("channel", "")
    if channel == "native_messaging":
        # The description is a stub; the transcript is the real body.
        body = "\n".join(t["text"] for t in thread if t["text"])
    else:
        body = clean_text(ticket.get("description"), multiline=True) or (thread[0]["text"] if thread else "")
    return {
        "id": str(ticket["id"]),
        "subject": clean_text(ticket.get("subject")),
        "body": body,
        "thread_json": db.dumps(thread),
        "requester_name": clean_text(requester.get("name", "")),
        "requester_email": (requester.get("email") or "").lower(),
        "channel": channel,
        "created_at": ticket.get("created_at") or "",
        "vision_reasons_json": db.dumps(vision_reasons(ticket, comments)),
        "images_json": db.dumps(collect_images(comments)),
        "skipped_reason": skip_reason(ticket, requester),
    }


def skip_reason(ticket: dict, requester: dict) -> str | None:
    local = (requester.get("email") or "").split("@")[0].lower()
    if any(local.startswith(m) for m in MACHINE_SENDERS) or "monitoring_alert" in (ticket.get("tags") or []):
        return "machine_generated"
    if ticket.get("status") in ("solved", "closed", "pending"):
        return f"status_{ticket['status']}"
    return None


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def find_duplicate(conn, row: dict, window_minutes: int = 30) -> str | None:
    """Same person filing the same thing twice within minutes, often from a typo'd address
    (gmial.com). Match on display name + near-identical body, not on email."""
    created = _ts(row["created_at"])
    if not created or not row["requester_name"]:
        return None
    for other in conn.execute("SELECT id, body, created_at FROM tickets WHERE requester_name=? AND id<>?",
                              (row["requester_name"], row["id"])):
        other_ts = _ts(other["created_at"])
        if not other_ts or other_ts >= created:
            continue
        if (created - other_ts).total_seconds() > window_minutes * 60:
            continue
        if difflib.SequenceMatcher(None, other["body"].lower(), row["body"].lower()).ratio() >= 0.85:
            return other["id"]
    return None


def run(conn, source, ticket_id: str) -> dict:
    row = normalize(source.get_ticket(ticket_id), source.get_comments(ticket_id))
    row["duplicate_of"] = find_duplicate(conn, row)
    db.upsert(conn, "tickets", row, "id")
    db.log_event(conn, "ingest", True, ticket_id, channel=row["channel"],
                 vision=row["vision_reasons_json"], skipped=row["skipped_reason"],
                 duplicate_of=row["duplicate_of"])
    return row
