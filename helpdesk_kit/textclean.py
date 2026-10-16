"""Clean invisible and odd Unicode out of text before it is compared or matched.

Names copied out of back-office systems, email clients and PDFs carry characters nobody can
see: zero-width spaces, byte-order marks, soft hyphens, bidirectional marks, non-breaking
and thin spaces, curly quotes, and the occasional look-alike letter from another alphabet.
"Priya<ZWSP> Raman" is not equal to "Priya Raman" until those are gone, and the person
looking at the screen can not tell why a lookup failed.

`clean_text` normalises for display. `name_key` goes further and returns the string to
compare on (case-folded, punctuation-light, look-alike letters folded to Latin).
"""

from __future__ import annotations

import re
import unicodedata

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201a"): "'", ord("\u201b"): "'",
           ord("\u201c"): '"', ord("\u201d"): '"', ord("\u201e"): '"', ord("\u2032"): "'"}

# A small, deliberately partial map of Cyrillic and Greek letters that look like Latin ones.
_LOOKALIKES = {ord(k): v for k, v in {
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0456": "i", "\u0455": "s", "\u0458": "j", "\u04bb": "h",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0397": "H", "\u0399": "I", "\u039a": "K",
    "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T", "\u03a7": "X",
    "\u03bf": "o", "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M",
    "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X",
}.items()}

_INLINE_SPACE = re.compile(r"[^\S\n]+")


def clean_text(s: str | None, *, multiline: bool = False) -> str:
    """NFKC-normalise, drop format/control characters, turn every kind of space into a plain
    one, straighten dashes and quotes. `multiline` keeps newlines (ticket bodies)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in "\n\r":
            out.append("\n")
        elif ch == "\t" or cat == "Zs":
            out.append(" ")
        elif cat in ("Cf", "Cc", "Co", "Cn", "Zl", "Zp"):
            out.append("\n" if cat in ("Zl", "Zp") else "")
        else:
            out.append(ch)
    s = "".join(out).translate(_DASHES).translate(_QUOTES)
    if multiline:
        lines = (_INLINE_SPACE.sub(" ", line).strip() for line in s.split("\n"))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return _INLINE_SPACE.sub(" ", s.replace("\n", " ")).strip()


def name_key(s: str | None) -> str:
    """What to compare names on: 'Priya\u200b Raman', 'PRIYA  RAMAN' and 'priya raman' all match."""
    s = clean_text(s).translate(_LOOKALIKES)
    s = re.sub(r"[.,;:]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()
