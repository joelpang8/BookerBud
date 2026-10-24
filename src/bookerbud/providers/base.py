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
    skipped_markets: Counter = field(default_factory=Counter)  # out-of-scope bet types/leagues, by reason
    requests_made: int = 0
    objects_used: int = 0  # provider billing units (SGO: events returned, min 1 per response)
    truncated: bool = False  # stopped early to stay inside the quota budget
    # Box scores of finished games (see bookerbud.storage.history), one dict per player/team.
    game_logs: list[dict] = field(default_factory=list)

    def extend(self, other: FetchResult) -> None:
        self.events += other.events
        self.props += other.props
        self.rosters.update(other.rosters)
        self.raw_paths += other.raw_paths
        self.notices += other.notices
        self.skipped_markets.update(other.skipped_markets)
        self.requests_made += other.requests_made
        self.objects_used += other.objects_used
        self.truncated = self.truncated or other.truncated
        self.game_logs += other.game_logs


@dataclass
class OddSettlement:
    """Closing prices and the graded stat for one side of one market."""

    odd_id: str
    opposing_odd_id: str | None
    stat_id: str
    stat_entity: str
    period: str
    bet_type: str
    side: str
    score: float | None = None  # the stat value that decides the bet, when the provider supplies it
    scoring_supported: bool | None = None
    cancelled: bool = False
    close: dict[str, tuple[int | None, float | None]] = field(default_factory=dict)  # book -> (price, line)


@dataclass
class EventSettlement:
    event_id: str
    started: bool
    ended: bool
    finalized: bool
    cancelled: bool
    results: dict = field(default_factory=dict)  # {period: {entity: {statID: value}}}
    odds: dict[str, OddSettlement] = field(default_factory=dict)


class OddsProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    def fetch_slate(
        self,
        leagues: dict[Sport, str],
        starts_after: datetime,
        starts_before: datetime,
        bookmakers: list[str],
        *,
        include_started: bool = False,
        max_objects: int | None = None,
    ) -> FetchResult:
        """Fetch every event in `leagues` (sport -> provider league ID) starting in
        [starts_after, starts_before), with game lines and player props for
        `bookmakers` (canonical IDs). Stop before exceeding `max_objects` billing
        units and set `truncated`."""

    @abstractmethod
    def parse_payload(self, payload: dict[str, Any], leagues: dict[Sport, str], bookmakers: list[str]) -> FetchResult:
        """Parse one raw response body (as logged to data/raw). Used for replays."""

    def fetch_schedule(
        self, leagues: dict[Sport, str], starts_after: datetime, starts_before: datetime, *, max_objects: int | None = None
    ) -> FetchResult:
        """Events only (no odds), including finished games. Used for rest/back-to-back."""
        raise NotImplementedError(f"{self.name} has no schedule endpoint")

    def fetch_settlement(
        self, event_ids: list[str], bookmakers: list[str], *, include_close: bool = True
    ) -> tuple[dict[str, EventSettlement], FetchResult]:
        """Results (and optionally closing odds) for specific, usually finished, events."""
        raise NotImplementedError(f"{self.name} has no settlement endpoint")

    def usage(self) -> dict[str, Any] | None:
        """Quota/rate-limit info, if the provider exposes it."""
        return None

    def close(self) -> None:  # noqa: B027 - optional hook
        pass
