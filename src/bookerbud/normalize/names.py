"""Player-name normalisation and matching.

Different books spell the same player differently ("Jazz Chisholm Jr.",
"Jazz Chisholm", "Nikola Jokić", "C.J. Abrams"). We reduce every name to a
key of lowercase ASCII words with suffixes and punctuation removed, then match
against the event roster: exact key first, then a conservative fuzzy match
(same last name + high similarity). No match -> caller flags it.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
# Common first-name variants that books swap freely.
_NICKNAMES = {
    "josh": "joshua",
    "mike": "michael",
    "matt": "matthew",
    "nick": "nicholas",
    "chris": "christopher",
    "alex": "alexander",
    "ben": "benjamin",
    "zach": "zachary",
    "jake": "jacob",
    "will": "william",
    "dan": "daniel",
    "tony": "anthony",
    "cam": "cameron",
}

FUZZY_THRESHOLD = 0.88


def normalize_name(name: str) -> str:
    """'Jazz Chisholm Jr.' -> 'jazz chisholm'; 'C.J. Abrams' -> 'cj abrams'; 'Jokić' -> 'jokic'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\.)", "", s)  # c.j. -> cj.
    s = re.sub(r"[.'’`]", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w not in _SUFFIXES]
    return " ".join(words)


def _canonical_first(words: list[str]) -> list[str]:
    if words and words[0] in _NICKNAMES:
        return [_NICKNAMES[words[0]], *words[1:]]
    return words


def player_key(sport: str, name: str) -> str:
    return f"{sport}:{normalize_name(name).replace(' ', '_')}"


class PlayerMatcher:
    """Resolve arbitrary book spellings to roster names for one event."""

    def __init__(self, roster_names: list[str]):
        self.by_key: dict[str, list[str]] = {}
        for n in roster_names:
            self.by_key.setdefault(normalize_name(n), []).append(n)
        self.by_nick: dict[str, list[str]] = {}
        for n in roster_names:
            k = " ".join(_canonical_first(normalize_name(n).split()))
            self.by_nick.setdefault(k, []).append(n)

    def ambiguous_keys(self) -> set[str]:
        return {k for k, v in self.by_key.items() if len(set(v)) > 1}

    def match(self, name: str) -> str | None:
        key = normalize_name(name)
        if key in self.by_key:
            return self.by_key[key][0]
        nick = " ".join(_canonical_first(key.split()))
        if nick in self.by_nick and len(set(self.by_nick[nick])) == 1:
            return self.by_nick[nick][0]
        # Fuzzy: last name must match exactly, and only one candidate may clear the bar.
        words = key.split()
        if not words:
            return None
        last = words[-1]
        candidates = []
        for k, names in self.by_key.items():
            if k.split()[-1:] == [last]:
                ratio = SequenceMatcher(None, key, k).ratio()
                if ratio >= FUZZY_THRESHOLD:
                    candidates.append((ratio, names[0]))
        if len(candidates) == 1:
            return candidates[0][1]
        return None
