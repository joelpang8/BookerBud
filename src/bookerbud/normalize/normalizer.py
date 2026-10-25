"""RawProp (provider vocabulary) -> Prop (canonical), with match flags."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from bookerbud.models import Flag, MarketKind, PayoutType, Prop, RawProp, RosterPlayer
from bookerbud.normalize.names import PlayerMatcher, normalize_name, player_key
from bookerbud.normalize.stats import StatMatcher, normalize_label


@dataclass
class NormalizeReport:
    total: int = 0
    flags: Counter = field(default_factory=Counter)
    unmapped_stats: Counter = field(default_factory=Counter)  # (sport, raw label) -> rows
    unmatched_players: Counter = field(default_factory=Counter)  # (sport, raw name) -> rows
    markets: int = 0
    single_book_markets: int = 0  # markets only one of our books offers (nothing to compare)

    @property
    def flagged_rows(self) -> int:
        return sum(self.flags.values())


def normalize_props(
    raw_props: list[RawProp],
    rosters: dict[str, list[RosterPlayer]],
    *,
    fetched_at: datetime,
    pickem_books: set[str],
) -> tuple[list[Prop], NormalizeReport]:
    report = NormalizeReport()
    stat_matchers: dict[str, StatMatcher] = {}
    player_matchers: dict[str, PlayerMatcher] = {}
    roster_ids: dict[str, dict[str, RosterPlayer]] = {
        eid: {p.provider_player_id: p for p in players} for eid, players in rosters.items()
    }
    out: list[Prop] = []

    for raw in raw_props:
        ev = raw.event
        sport = ev.sport.value
        flags = list(raw.parse_flags)

        sm = stat_matchers.setdefault(sport, StatMatcher(sport))
        if raw.market_kind is MarketKind.GAME_LINE:
            # Game lines are named by their own stat ("points", "touchdowns", ...). Player
            # aliases must not apply: game-level "touchdowns" is total TDs, not Anytime TD.
            stat_type = normalize_label(raw.stat_label).replace(" ", "_") or "points"
        else:
            stat_type = sm.match(raw.stat_label)
        if stat_type is None:
            stat_type = normalize_label(raw.stat_label).replace(" ", "_")
            flags.append(Flag.UNMAPPED_STAT)
            report.unmapped_stats[(sport, raw.stat_label)] += 1

        player = pkey = team = opponent = None
        team_id = raw.player_team_id
        if raw.market_kind is MarketKind.PLAYER_PROP:
            roster = rosters.get(ev.event_id, [])
            ids = roster_ids.get(ev.event_id, {})
            pm = player_matchers.setdefault(ev.event_id, PlayerMatcher([p.name for p in roster]))
            if raw.provider_player_id and raw.provider_player_id in ids:
                rp = ids[raw.provider_player_id]
                player, team_id = rp.name, team_id or rp.team_id
            elif raw.player_name and (matched := pm.match(raw.player_name)):
                player = matched
                rp = next((p for p in roster if p.name == matched), None)
                team_id = team_id or (rp.team_id if rp else None)
            else:
                player = raw.player_name or raw.provider_player_id or "?"
                if Flag.PLAYER_NOT_IN_ROSTER not in flags:
                    flags.append(Flag.PLAYER_NOT_IN_ROSTER)
                report.unmatched_players[(sport, player)] += 1
            if normalize_name(player) in pm.ambiguous_keys():
                flags.append(Flag.AMBIGUOUS_PLAYER)
            pkey = player_key(sport, player)
            if team_id and team_id == ev.home_team_id:
                team, opponent = ev.home_team, ev.away_team
            elif team_id and team_id == ev.away_team_id:
                team, opponent = ev.away_team, ev.home_team
            else:
                flags.append(Flag.UNKNOWN_TEAM)
        else:
            who = raw.side if raw.entity == "match" else raw.entity
            if who == "home":
                team, opponent = ev.home_team, ev.away_team
            elif who == "away":
                team, opponent = ev.away_team, ev.home_team

        prop = Prop(
            sport=ev.sport,
            league=ev.league,
            provider=raw.provider,
            event_id=ev.event_id,
            start_time=ev.start_time,
            market_kind=raw.market_kind,
            entity=raw.entity,
            player=player,
            player_key=pkey,
            provider_player_id=raw.provider_player_id,
            player_status=raw.player_status,
            team=team,
            opponent=opponent,
            stat_type=stat_type,
            stat_label_raw=raw.stat_label,
            period=raw.period,
            bet_type=raw.bet_type,
            side=raw.side,
            line=raw.line,
            price_american=raw.price_american,
            payout_type=PayoutType.PICKEM if raw.bookmaker in pickem_books else PayoutType.AMERICAN_ODDS,
            bookmaker=raw.bookmaker,
            available=raw.available,
            is_main_line=raw.is_main_line,
            is_alt_line=raw.is_alt_line,
            book_updated_at=raw.book_updated_at,
            fetched_at=fetched_at,
            provider_market_id=raw.provider_market_id,
            deeplink=raw.deeplink,
            flags=flags,
        )
        report.flags.update(flags)
        out.append(prop)

    report.total = len(out)
    groups = group_markets(out)
    report.markets = len(groups)
    report.single_book_markets = sum(1 for ps in groups.values() if len({p.bookmaker for p in ps}) < 2)
    return out, report


def group_markets(props: list[Prop]) -> dict[tuple, list[Prop]]:
    """Line the same market up across books: market_key -> every book's rows (both sides)."""
    groups: dict[tuple, list[Prop]] = defaultdict(list)
    for p in props:
        groups[p.market_key].append(p)
    return dict(groups)
