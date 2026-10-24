"""Apply context to Edge records: drop, downgrade confidence, or annotate."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from bookerbud.config import CONFIG_DIR
from bookerbud.context.models import GameContext, PlayerContext
from bookerbud.edge.engine import Edge
from bookerbud.normalize.names import normalize_name, player_key

DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}

# SportsGameOdds Player.status enum -> our vocabulary.
SGO_STATUS = {"ir": "injured_list", "out": "out", "suspended": "suspended", "doubtful": "doubtful",
              "questionable": "questionable", "probable": "probable", "active": "active"}


class MLBRules(BaseModel):
    not_in_posted_lineup: str = "drop"
    not_probable_starter: str = "drop"
    starter_unknown: str = "downgrade"
    pitcher_stats: list[str] = Field(default_factory=lambda: ["pitcher_strikeouts", "pitcher_outs", "earned_runs"])
    rain_risk_pct: float = 50
    rain_risk: str = "downgrade"
    wind_note_mph: float = 8


class NBARules(BaseModel):
    back_to_back: str = "note"
    backfill_yesterday: bool = True


class ContextRules(BaseModel):
    use_provider_status: bool = False  # SGO players[].status; MLB roster status is separate and always used
    status: dict[str, str] = Field(default_factory=dict)
    mlb: MLBRules = Field(default_factory=MLBRules)
    nba: NBARules = Field(default_factory=NBARules)


def load_context_rules(path: Path | None = None) -> ContextRules:
    p = path or CONFIG_DIR / "context.yaml"
    return ContextRules.model_validate(yaml.safe_load(p.read_text()) or {} if p.exists() else {})


def normalise_status(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return SGO_STATUS.get(r, r)


def _act(edge: Edge, action: str, note: str, dropped: Counter, reason: str) -> bool:
    """Apply one action. Returns False if the edge is dropped."""
    if action == "drop":
        dropped[reason] += 1
        return False
    if action == "downgrade":
        edge.confidence = DOWNGRADE[edge.confidence]
        note += " (confidence lowered)"
    edge.context_notes.append(note)
    return True


def _in_lineup(name: str, lineup: list[str]) -> bool:
    key = normalize_name(name)
    return any(normalize_name(n) == key for n in lineup)


def apply_context(
    edges: list[Edge],
    games: dict[str, GameContext],
    players: dict[str, PlayerContext],
    rules: ContextRules,
) -> tuple[list[Edge], Counter]:
    kept: list[Edge] = []
    dropped: Counter = Counter()
    for e in edges:
        ok = True
        g = games.get(e.event_id)
        if e.player:
            # Worst of the statuses we know (odds-provider status, league roster status).
            pc = players.get(player_key(e.sport, e.player))
            provider_status = normalise_status(e.player_status) if rules.use_provider_status else None
            statuses = [s for s in (provider_status, pc.status if pc else None) if s]
            for st in statuses:
                action = rules.status.get(st)
                if action:
                    detail = f" ({pc.detail})" if pc and pc.status == st and pc.detail else ""
                    ok = _act(e, action, f"status: {st}{detail}", dropped, f"status:{st}")
                    if not ok:
                        break
        if ok and g and e.sport == "mlb" and e.player and e.team:
            side = "home" if e.team == g.home_team else "away" if e.team == g.away_team else None
            if side and e.stat_type in rules.mlb.pitcher_stats:
                starter = g.probable_pitchers.get(side)
                if starter is None:
                    ok = _act(e, rules.mlb.starter_unknown, "probable starter not announced", dropped, "starter_unknown")
                elif normalize_name(starter) != normalize_name(e.player):
                    ok = _act(e, rules.mlb.not_probable_starter, f"not the probable starter ({starter})",
                              dropped, "not_probable_starter")
            elif side and g.lineups_posted.get(side):
                if not _in_lineup(e.player, g.lineups.get(side, [])):
                    ok = _act(e, rules.mlb.not_in_posted_lineup, "not in posted lineup", dropped, "not_in_lineup")
                else:
                    e.context_notes.append("in posted lineup")
        if ok and g and g.weather and e.sport == "mlb":
            w = g.weather
            if w.roof != "Dome" and w.precip_prob is not None and w.precip_prob >= rules.mlb.rain_risk_pct:
                ok = _act(e, rules.mlb.rain_risk, f"rain risk {w.precip_prob:.0f}% at first pitch", dropped, "rain_risk")
            if ok and w.wind_out_mph is not None and abs(w.wind_out_mph) >= rules.mlb.wind_note_mph:
                e.context_notes.append(
                    f"wind blowing {'out' if w.wind_out_mph > 0 else 'in'} {abs(w.wind_out_mph):.0f} mph"
                )
        if ok and g and e.sport == "nba":
            for side, b2b in g.back_to_back.items():
                team = g.home_team if side == "home" else g.away_team
                if b2b and (e.team == team or not e.player):
                    ok = _act(e, rules.nba.back_to_back, f"{team} on a back-to-back", dropped, "back_to_back")
                    if not ok:
                        break
        if ok:
            kept.append(e)
    return kept, dropped
