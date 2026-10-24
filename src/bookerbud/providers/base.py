"""Provider interface. Every odds source implements OddsProvider and returns
provider-vocabulary RawProps; bookerbud.normalize turns them into canonical Props."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from bookerbud.models import EventInfo, RawProp, RosterPlayer, Sport


class ProviderError(RuntimeError):
    pass


class ProviderAuthError(ProviderError):
    """Bad/missing key or plan doesn't allow the request. Not retried."""


@dataclass
class FetchResult:
    events: list[EventInfo] = field(default_factory=list)
    props: list[RawProp] = field(default_factory=list)
    rosters: dict[str, list[RosterPlayer]] = field(default_factory=dict)  # event_id -> players
    raw_paths: list[Path] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)  # provider warnings, e.g. plan-filtered data
    skipped_markets: Counter = field(default_factory=Counter)  # out-of-scope bet types, by reason
    requests_made: int = 0

    def extend(self, other: FetchResult) -> None:
        self.events += other.events
        self.props += other.props
        self.rosters.update(other.rosters)
        self.raw_paths += other.raw_paths
        self.notices += other.notices
        self.skipped_markets.update(other.skipped_markets)
        self.requests_made += other.requests_made


class OddsProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    def fetch_slate(
        self,
        sport: Sport,
        starts_after: datetime,
        starts_before: datetime,
        bookmakers: list[str],
        *,
        include_started: bool = False,
        league_id: str | None = None,
    ) -> FetchResult:
        """Fetch every event for `sport` starting in [starts_after, starts_before)
        with game lines and player props for `bookmakers` (canonical IDs).
        `league_id` overrides the provider's default league for the sport."""

    @abstractmethod
    def parse_payload(self, payload: dict[str, Any], sport: Sport, bookmakers: list[str]) -> FetchResult:
        """Parse one raw response body (as logged to data/raw). Used for replays."""

    def usage(self) -> dict[str, Any] | None:
        """Quota/rate-limit info, if the provider exposes it."""
        return None

    def close(self) -> None:  # noqa: B027 - optional hook
        pass
