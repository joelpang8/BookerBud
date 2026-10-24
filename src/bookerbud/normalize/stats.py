"""Stat-label matching: provider/book labels -> canonical stat_type."""

from __future__ import annotations

import re

from bookerbud.config import load_stat_aliases


def normalize_label(label: str) -> str:
    """'batting_totalBases' -> 'batting totalbases'; 'Pts + Rebs' -> 'pts+rebs'."""
    s = label.strip().lower()
    s = re.sub(r"\s*\+\s*", "+", s)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"[^a-z0-9+ ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


class StatMatcher:
    def __init__(self, sport: str, aliases: dict[str, dict[str, dict]] | None = None):
        table = (aliases if aliases is not None else load_stat_aliases()).get(sport, {})
        self.labels: dict[str, str] = {}
        self._lookup: dict[str, str] = {}
        for canonical, spec in table.items():
            self.labels[canonical] = spec.get("label", canonical)
            for a in [canonical, *spec.get("aliases", [])]:
                self._lookup[normalize_label(str(a))] = canonical

    def match(self, label: str) -> str | None:
        return self._lookup.get(normalize_label(label))

    def display(self, stat_type: str) -> str:
        return self.labels.get(stat_type, stat_type)
