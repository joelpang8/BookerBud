"""Typed loaders for config/edge.yaml, config/payouts.yaml, config/availability.yaml."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from bookerbud.config import CONFIG_DIR
from bookerbud.edge.devig import DevigMethod
from bookerbud.edge.lines import StatModel


class FairCfg(BaseModel):
    min_consensus_books: int = 2
    require_sharp: bool = False


class FilterCfg(BaseModel):
    min_edge_pct: float = 2.0
    max_line_age_minutes: float = 30
    max_ev_pct: float = 25.0
    bet_books: list[str] = Field(default_factory=lambda: ["draftkings", "fanduel", "prizepicks", "underdog", "sleeper"])


class BankrollCfg(BaseModel):
    amount: float = 1000
    kelly_fraction: float = 0.25
    max_bet_pct: float = 2.0


class SlipCfg(BaseModel):
    min_legs: int = 2
    max_legs: int = 6
    candidate_legs: int = 15
    same_game: str = "exclude"  # exclude | penalize
    same_game_penalty: float = 0.97
    min_teams: int = 2


class EdgeConfig(BaseModel):
    devig_method: DevigMethod = DevigMethod.POWER
    fair: FairCfg = Field(default_factory=FairCfg)
    filters: FilterCfg = Field(default_factory=FilterCfg)
    bankroll: BankrollCfg = Field(default_factory=BankrollCfg)
    slips: SlipCfg = Field(default_factory=SlipCfg)
    stat_models: dict = Field(default_factory=dict)

    def model_for(self, sport: str, key: str) -> StatModel:
        spec = (self.stat_models.get(sport) or {}).get(key) or self.stat_models.get("default") or {
            "kind": "normal", "cv": 0.35, "max_gap": 1.0,
        }
        return StatModel(**spec)


class SlipTable(BaseModel):
    source: str = ""
    checked: date | str = ""
    verified: bool = False
    variable_multipliers: bool = False
    slips: dict[str, dict[int, dict[int, float]]] = Field(default_factory=dict)


def _load(name: str, path: Path | None) -> dict:
    p = path or CONFIG_DIR / name
    return yaml.safe_load(p.read_text()) or {} if p.exists() else {}


def load_edge_config(path: Path | None = None) -> EdgeConfig:
    return EdgeConfig.model_validate(_load("edge.yaml", path))


def load_payouts(path: Path | None = None) -> dict[str, SlipTable]:
    return {app: SlipTable.model_validate(v) for app, v in _load("payouts.yaml", path).items()}


def load_availability(path: Path | None = None) -> dict[str, dict[str, str | None]]:
    return {state.upper(): books or {} for state, books in _load("availability.yaml", path).items()}
