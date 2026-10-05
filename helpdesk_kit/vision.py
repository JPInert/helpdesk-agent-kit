"""Screenshot -> structured fields.

With Anthropic credentials set, a vision model reads the image. Without them (the offline
demo, and the tests) a sidecar JSON next to the image stands in for the model's answer, so
everything downstream runs the same way.

One cleanup matters more than it looks: models narrate what they looked for and did NOT see
("No 'Pending' badge was visible."). Downstream checks match on words, so that sentence
reads as if the badge WAS there. `strip_narrated_absences` drops those sentences. Text the
model quotes from the screen never counts as its own negation: an on-screen message such as
"No seats available" is evidence, not narration.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field


class ScreenshotFields(BaseModel):
    screen: str = Field(description="Which page or app the screenshot shows, in a few words")
    error_text: str | None = Field(None, description="Exact error/status text visible on screen, quoted")
    username: str | None = Field(None, description="Username or email visible on screen, if any")
    record_id: str | None = Field(None, description="Job, request or record id visible on screen, if any")
    status: str | None = Field(None, description="Status shown for the record (pending, failed, ...)")
    observations: str | None = Field(None, description="Anything else relevant, one or two sentences")


PROMPT = """This screenshot was attached to an IT help desk ticket. Extract only what is
actually visible. Quote on-screen text exactly. Leave a field null if it is not on screen.
Do not describe things that are absent."""

_QUOTED = re.compile(r"(?<!\w)'[^']*'(?!\w)|\"[^\"]*\"|“[^”]*”")
_NEGATION = re.compile(r"\b(?:no|not|none|nothing|without|never)\b", re.I)
_SEEN = re.compile(r"\b(?:visible|shown|showing|displayed|present|seen|found|appears?|appeared)\b", re.I)


def _sentences(text: str) -> list[str]:
    """Split on . ; ! ? but never inside quotes, so "'Saving...'" stays one piece."""
    out, buf, quote = [], [], None
    for i, ch in enumerate(text):
        buf.append(ch)
        if quote:
            if ch == quote:
                quote = None
        elif ch == '"' or (ch == "'" and (i == 0 or not text[i - 1].isalnum())):
            quote = ch
        elif ch == "“":
            quote = "”"
        elif ch in ".;!?":
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s.strip(" .;!?")]


def strip_narrated_absences(text: str | None) -> str | None:
    if not text:
        return text
    kept = []
    for sentence in _sentences(text):
        own_words = _QUOTED.sub(" ", sentence)
        if _NEGATION.search(own_words) and _SEEN.search(own_words):
            continue
        kept.append(sentence)
    cleaned = " ".join(kept).strip()
    return cleaned or None


def clean(fields: ScreenshotFields) -> ScreenshotFields:
    return fields.model_copy(update={
        "error_text": strip_narrated_absences(fields.error_text),
        "observations": strip_narrated_absences(fields.observations),
    })


class SidecarVision:
    """Offline stand-in: reads <image>.json written when the synthetic screenshot was made."""

    def __init__(self, fixtures_dir: Path):
        self.dir = Path(fixtures_dir)

    def extract(self, url: str, image: bytes, media_type: str) -> ScreenshotFields:
        sidecar = self.dir / (url.removeprefix("fixture://") + ".json")
        return clean(ScreenshotFields.model_validate(json.loads(sidecar.read_text())))


class ClaudeVision:
    def __init__(self, model: str, client=None):
        import anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model

    def extract(self, url: str, image: bytes, media_type: str) -> ScreenshotFields:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=8192,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(image).decode()}},
                {"type": "text", "text": PROMPT},
            ]}],
            output_format=ScreenshotFields,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"vision extraction returned no fields (stop_reason={response.stop_reason})")
        return clean(response.parsed_output)


def run(conn, source, vision, ticket: dict) -> dict:
    """Read every image on the ticket; merge into one dict (first non-null wins per field)."""
    from . import db
    merged: dict = {}
    for img in json.loads(ticket["images_json"]):
        try:
            fields = vision.extract(img["url"], source.fetch_attachment(img["url"]), img["content_type"])
        except Exception as e:  # one bad image must not sink the ticket
            db.log_event(conn, "vision", False, ticket["id"], file=img["file_name"], error=type(e).__name__)
            continue
        for k, v in fields.model_dump().items():
            if v and not merged.get(k):
                merged[k] = v
        db.log_event(conn, "vision", True, ticket["id"], file=img["file_name"])
    conn.execute("UPDATE tickets SET screenshot_json=? WHERE id=?", (db.dumps(merged), ticket["id"]))
    return merged
