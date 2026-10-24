"""MLB context from the MLB Stats API (statsapi.mlb.com).

Free, no key. MLBAM's copyright notice allows "individual, non-commercial,
non-bulk use" (http://gdx.mlb.com/components/copyright.txt). We make a handful
of calls per day: one schedule call plus one 40-man roster call per team
playing.

Provides: probable pitchers, posted lineups, IL/roster status, venue
coordinates, roof type and field orientation (azimuthAngle = home plate -> CF
bearing) for the weather lookup.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from bookerbud.context.http import JsonClient
from bookerbud.context.models import GameContext, PlayerContext
from bookerbud.context.weather import OpenMeteo
from bookerbud.models import EventInfo
from bookerbud.normalize.names import normalize_name, player_key

BASE = "https://statsapi.mlb.com/api/v1"
HYDRATE = "probablePitcher,lineups,venue(location,fieldInfo)"

# 40-man roster status code -> our status vocabulary.
ROSTER_STATUS = {"A": "active", "D7": "injured_list", "D10": "injured_list", "D15": "injured_list", "D60": "injured_list"}


def _norm_team(name: str) -> str:
    return normalize_name(name)


def parse_schedule(body: dict[str, Any]) -> list[dict[str, Any]]:
    games = []
    for d in body.get("dates") or []:
        for g in d.get("games") or []:
            teams = g.get("teams") or {}
            venue = g.get("venue") or {}
            loc = venue.get("location") or {}
            coords = loc.get("defaultCoordinates") or {}
            lineups = g.get("lineups") or {}
            games.append({
                "game_pk": g.get("gamePk"),
                "start": datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00")),
                "home": teams.get("home", {}).get("team", {}).get("name", ""),
                "away": teams.get("away", {}).get("team", {}).get("name", ""),
                "home_id": teams.get("home", {}).get("team", {}).get("id"),
                "away_id": teams.get("away", {}).get("team", {}).get("id"),
                "home_pp": (teams.get("home", {}).get("probablePitcher") or {}).get("fullName"),
                "away_pp": (teams.get("away", {}).get("probablePitcher") or {}).get("fullName"),
                "home_lineup": [p.get("fullName") for p in lineups.get("homePlayers") or []],
                "away_lineup": [p.get("fullName") for p in lineups.get("awayPlayers") or []],
                "lat": coords.get("latitude"),
                "lon": coords.get("longitude"),
                "cf_bearing": loc.get("azimuthAngle"),
                "roof": (venue.get("fieldInfo") or {}).get("roofType"),
                "status": (g.get("status") or {}).get("detailedState"),
            })
    return games


def parse_roster(body: dict[str, Any], team_name: str) -> list[PlayerContext]:
    out = []
    for r in body.get("roster") or []:
        name = (r.get("person") or {}).get("fullName")
        st = r.get("status") or {}
        if not name:
            continue
        code = st.get("code", "")
        status = ROSTER_STATUS.get(code, "not_active_roster")
        out.append(PlayerContext(
            player_key=player_key("mlb", name), name=name, team=team_name, status=status,
            detail=st.get("description"), source="mlb-statsapi",
        ))
    return out


def match_game(events: list[EventInfo], game: dict[str, Any]) -> EventInfo | None:
    """Same two teams; if a doubleheader, the closest start time."""
    cands = [
        e for e in events
        if _norm_team(e.home_team) == _norm_team(game["home"]) and _norm_team(e.away_team) == _norm_team(game["away"])
    ]
    if not cands:
        return None
    return min(cands, key=lambda e: abs((e.start_time - game["start"]).total_seconds()))


class MLBContext:
    def __init__(self, client: JsonClient, weather: OpenMeteo | None):
        self.client = client
        self.weather = weather

    def build(self, events: list[EventInfo], day: date) -> tuple[list[GameContext], list[PlayerContext], list[str]]:
        warnings: list[str] = []
        body = self.client.get(f"{BASE}/schedule", {"sportId": 1, "date": day.isoformat(), "hydrate": HYDRATE},
                               f"schedule_{day}")
        games = parse_schedule(body)
        contexts: list[GameContext] = []
        players: list[PlayerContext] = []
        for g in games:
            ev = match_game(events, g)
            if ev is None:
                continue
            ctx = GameContext(
                event_id=ev.event_id, sport="mlb", home_team=ev.home_team, away_team=ev.away_team,
                probable_pitchers={"home": g["home_pp"], "away": g["away_pp"]},
                lineups_posted={"home": bool(g["home_lineup"]), "away": bool(g["away_lineup"])},
                lineups={"home": g["home_lineup"], "away": g["away_lineup"]},
                sources=["mlb-statsapi"],
            )
            if self.weather and g["lat"] is not None:
                try:
                    ctx.weather = self.weather.at(g["lat"], g["lon"], g["start"], cf_bearing=g["cf_bearing"], roof=g["roof"])
                    ctx.sources.append("open-meteo")
                except Exception as exc:  # weather is nice-to-have; never block the run
                    warnings.append(f"weather unavailable for {ev.away_team} @ {ev.home_team}: {exc}")
            contexts.append(ctx)
            for side in ("home", "away"):
                team_id, team_name = g[f"{side}_id"], getattr(ev, f"{side}_team")
                if team_id is None:
                    continue
                roster = self.client.get(f"{BASE}/teams/{team_id}/roster",
                                         {"rosterType": "40Man", "date": day.isoformat()}, f"roster_{team_id}")
                players += parse_roster(roster, team_name)
        unmatched = [e for e in events if e.event_id not in {c.event_id for c in contexts}]
        if unmatched:
            warnings.append(f"{len(unmatched)} MLB event(s) not found in the MLB schedule (no context)")
        return contexts, players, warnings
