"""Send stage: turn an approved plain-text reply into the HTML that goes on the ticket.

Three things the raw template text can not be trusted to get right:

* The sign-off stays in one piece. "Thanks," and the name go in a single paragraph joined
  by a line break, so a list above it, or a blank line between them in the source text,
  can not strand the name on its own. A sign-off already in the body is moved, not doubled.
* Lines that were hard-wrapped inside a paragraph are reflowed into one, so the email does not
  break mid-sentence. Blank lines separate paragraphs.
* Every bare URL becomes a hyperlink (trailing punctuation stays outside the link). A link
  the text already has is left alone. Everything else is HTML-escaped; replies are plain text.
* Sanity words. Placeholders, "None", "null", "TODO" and the like mean something upstream
  went wrong; the send is refused rather than posted. House style (reply.lint) applies too.
"""

from __future__ import annotations

import html
import re

from .reply import lint

_URL = re.compile(r"https?://[^\s<>\"']+")
_ANCHOR = re.compile(r'(<a href="https?://[^"\s]+">[^<]*</a>)')
_TRAIL = ".,;:!?)]}"
_LIST_LINE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.*)$")
_SANITY = [
    (re.compile(r"\{\{|\}\}"), "unrendered template braces"),
    (re.compile(r"\b(?:None|null|undefined|NaN|nil)\b"), "a missing value leaked into the text"),
    (re.compile(r"\b(?:TODO|FIXME|TBD|lorem ipsum|XXX)\b", re.I), "draft marker left in the text"),
    (re.compile(r"\[[A-Za-z][A-Za-z _-]*\]"), "square-bracket placeholder"),
    (re.compile(r"<(?!/?a[ >])[^>]+>"), "raw HTML tag in a plain-text reply"),
]


def sanity_check(text: str) -> list[str]:
    """Why this text must not be sent, or an empty list. Includes the house-style lint."""
    problems = [why for pattern, why in _SANITY if pattern.search(text)]
    return problems + lint(text)


def linkify(text: str) -> str:
    """Escape `text` and turn every bare URL into an anchor. Existing plain anchors survive."""
    out = []
    for piece in _ANCHOR.split(text):
        if _ANCHOR.fullmatch(piece):
            out.append(piece)
            continue
        pos = 0
        for m in _URL.finditer(piece):
            url = m.group(0)
            trail = ""
            while url and url[-1] in _TRAIL:
                url, trail = url[:-1], url[-1] + trail
            out.append(html.escape(piece[pos:m.start()]))
            out.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>{html.escape(trail)}')
            pos = m.end()
        out.append(html.escape(piece[pos:]))
    return "".join(out)


def _split_sign_off(body: str, sign_off: str) -> str:
    """Remove a sign-off the body already carries, wherever it sits at the end."""
    lines = body.rstrip().split("\n")
    first = sign_off.split("\n")[0].strip().lower()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().lower() == first and len(lines) - i <= len(sign_off.split("\n")) + 2:
            return "\n".join(lines[:i]).rstrip()
    return body.rstrip()


def to_html(body: str, sign_off: str) -> str:
    body = _split_sign_off(body.strip(), sign_off)
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        lines = [ln for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        i = 0
        while i < len(lines):                       # a paragraph may mix prose lines and list lines
            if _LIST_LINE.match(lines[i]):
                items = []
                while i < len(lines) and _LIST_LINE.match(lines[i]):
                    items.append(_LIST_LINE.match(lines[i]).group(1))
                    i += 1
                blocks.append("<ul>" + "".join(f"<li>{linkify(x)}</li>" for x in items) + "</ul>")
            else:
                prose = []
                while i < len(lines) and not _LIST_LINE.match(lines[i]):
                    prose.append(linkify(lines[i].strip()))
                    i += 1
                blocks.append("<p>" + " ".join(prose) + "</p>")      # hard-wrapped template lines reflow
    blocks.append("<p>" + "<br>".join(linkify(ln.strip()) for ln in sign_off.split("\n") if ln.strip()) + "</p>")
    return "".join(blocks)


def prepare(reply: str, sign_off: str) -> tuple[str, list[str]]:
    """(html, problems). Send only when problems is empty."""
    problems = sanity_check(reply)
    return (to_html(reply, sign_off) if not problems else ""), problems
