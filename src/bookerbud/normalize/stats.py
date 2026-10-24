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


# Box-score keys that differ from the market's label (seen in SGO MLB results:
# a batter's runs are stored as `points`, the sport-wide "winner" stat).
_ALTERNATES = {"batting_runs": ["points"], "runs": ["points"]}


def _lookup(lower: dict, key: str) -> float | None:
    k = key.lower()
    if lower.get(k) is not None:
        return lower[k]
    for alt in _ALTERNATES.get(k, []):
        if lower.get(alt) is not None:
            return lower[alt]
    if k == "batting_singles":  # not reported; derive it
        parts = [lower.get(x) for x in ("batting_hits", "batting_doubles", "batting_triples", "batting_homeruns")]
        if all(v is not None for v in parts):
            return parts[0] - parts[1] - parts[2] - parts[3]
    return None


def stat_value_from(stats: dict, label: str) -> float | None:
    """Value of a provider stat label in a box-score dict, summing combos.

    'points+rebounds+assists' -> points + rebounds + assists
    'batting_hits+runs+rbi'   -> batting_hits + batting_runs + batting_RBI
    (later parts inherit the first part's 'batting_' prefix; keys match
    case-insensitively). None if any part is missing: never guessed."""
    if not stats or not label:
        return None
    lower = {str(k).lower(): v for k, v in stats.items()}
    parts = label.split("+")
    prefix = parts[0].split("_", 1)[0] + "_" if "_" in parts[0] else ""
    total = 0.0
    for i, part in enumerate(parts):
        cands = [part] if i == 0 or "_" in part or not prefix else [prefix + part, part]
        val = next((v for v in (_lookup(lower, c) for c in cands) if v is not None), None)
        if val is None:
            return None
        try:
            total += float(val)
        except (TypeError, ValueError):
            return None
    return total
