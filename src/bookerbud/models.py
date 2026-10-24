"""Canonical data records shared by every provider and downstream module."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Sport(StrEnum):
    NBA = "nba"
    MLB = "mlb"
    NFL = "nfl"


class MarketKind(StrEnum):
    PLAYER_PROP = "player_prop"
    GAME_LINE = "game_line"


class PayoutType(StrEnum):
    # Priced per side with American odds (DraftKings, FanDuel, Pinnacle).
    AMERICAN_ODDS = "american_odds"
    # Pick'em app: the line is what matters, payout comes from the app's slip
    # payout table (PrizePicks, Underdog, Sleeper).
    PICKEM = "pickem"


class Flag(StrEnum):
    """Reasons a record couldn't be fully matched. Flagged rows are kept, never dropped."""

    UNMAPPED_STAT = "unmapped_stat"  # stat label not in config/stat_aliases.yaml
    PLAYER_NOT_IN_ROSTER = "player_not_in_roster"  # provider gave a player ID we can't resolve to a name
    UNKNOWN_TEAM = "unknown_team"  # player's team isn't either team in the event
    AMBIGUOUS_PLAYER = "ambiguous_player"  # two players in one event normalise to the same key
    NO_LINE = "no_line"  # over/under or spread with no line value
    UNPARSEABLE_PRICE = "unparseable_price"


class EventInfo(BaseModel):
    provider: str
    event_id: str
    sport: Sport
    league: str
    start_time: datetime
    home_team: str
    away_team: str
    home_team_id: str | None = None
    away_team_id: str | None = None
    status: str | None = None


class RosterPlayer(BaseModel):
    provider_player_id: str
    name: str
    team_id: str | None = None
    status: str | None = None  # injury/active status if the provider supplies it


class RawProp(BaseModel):
    """One bookmaker's price on one side of one market, still in provider vocabulary."""

    provider: str
    event: EventInfo
    market_kind: MarketKind
    provider_market_id: str
    stat_label: str
    period: str
    bet_type: str
    side: str
    # Whose stat decides the bet: "match" for moneyline/spread (side says
    # which team), "home"/"away" for team totals, "all" for game totals,
    # "player" for player props.
    entity: str
    line: float | None = None
    price_american: int | None = None
    bookmaker: str
    available: bool = True
    is_main_line: bool | None = None
    is_alt_line: bool = False
    book_updated_at: datetime | None = None
    deeplink: str | None = None
    # player props only
    provider_player_id: str | None = None
    player_name: str | None = None
    player_team_id: str | None = None
    player_status: str | None = None
    parse_flags: list[Flag] = Field(default_factory=list)


class Prop(BaseModel):
    """Canonical prop/line record. The same market at different books shares
    `market_key`, plus the same side."""

    sport: Sport
    league: str
    provider: str
    event_id: str
    start_time: datetime
    market_kind: MarketKind
    entity: str
    player: str | None = None
    player_key: str | None = None
    provider_player_id: str | None = None
    player_status: str | None = None
    team: str | None = None
    opponent: str | None = None
    stat_type: str
    stat_label_raw: str
    period: str
    bet_type: str
    side: str
    line: float | None = None
    price_american: int | None = None
    payout_type: PayoutType
    bookmaker: str
    available: bool = True
    is_main_line: bool | None = None
    is_alt_line: bool = False
    book_updated_at: datetime | None = None
    fetched_at: datetime
    provider_market_id: str
    deeplink: str | None = None
    flags: list[Flag] = Field(default_factory=list)

    @property
    def market_key(self) -> tuple:
        """Identity of a market (both sides) across books. Line is excluded on
        purpose: books often hang different lines on the same market."""
        return (
            self.sport,
            self.event_id,
            self.market_kind,
            self.entity,
            self.player_key,
            self.stat_type,
            self.period,
            self.bet_type,
        )
