"""One facts object per issue, merged from every back-office system, with provenance.

Each system an adapter talks to says things about the same person and records. `Facts`
keeps every value together with the system that said it, so a reply, a finding or the review
dashboard can always answer "who told us that?". When two systems disagree about the same
key the first one registered keeps the value and the disagreement is recorded as a conflict;
analysis sends conflicts to a person instead of picking a side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Fact:
    key: str
    value: Any
    source: str


@dataclass
class Facts:
    _facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)

    def set(self, key: str, value: Any, source: str) -> None:
        old = self._facts.get(key)
        if old is None:
            self._facts[key] = Fact(key, value, source)
        elif old.value != value and old.source != source:
            self.conflicts.append({"key": key, "values": {old.source: old.value, source: value}})

    def update(self, values: dict[str, Any], source: str) -> None:
        for k, v in values.items():
            self.set(k, v, source)

    def merge(self, other: "Facts") -> None:
        for f in other._facts.values():
            self.set(f.key, f.value, f.source)
        self.conflicts.extend(other.conflicts)

    def get(self, key: str, default: Any = None) -> Any:
        f = self._facts.get(key)
        return f.value if f else default

    def source(self, key: str) -> str | None:
        f = self._facts.get(key)
        return f.source if f else None

    def __contains__(self, key: str) -> bool:
        return key in self._facts

    def values(self) -> dict[str, Any]:
        return {k: f.value for k, f in self._facts.items()}

    def provenance(self) -> dict[str, str]:
        return {k: f.source for k, f in self._facts.items()}

    def to_json(self) -> dict:
        return {"facts": {k: {"value": f.value, "source": f.source} for k, f in self._facts.items()},
                "conflicts": self.conflicts}

    @classmethod
    def from_json(cls, data: dict) -> "Facts":
        out = cls()
        for k, f in (data.get("facts") or {}).items():
            out._facts[k] = Fact(k, f["value"], f["source"])
        out.conflicts = list(data.get("conflicts") or [])
        return out
