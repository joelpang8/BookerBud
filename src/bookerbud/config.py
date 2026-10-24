"""Settings: YAML for behaviour, .env for secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class BookmakerRoles(BaseModel):
    sharp: list[str] = Field(default_factory=lambda: ["pinnacle"])
    sportsbooks: list[str] = Field(default_factory=lambda: ["draftkings", "fanduel"])
    pickem: list[str] = Field(default_factory=lambda: ["prizepicks", "underdog", "sleeper"])
    # Priced books we never bet but use for consensus fair odds (the rest of
    # SportsGameOdds' free tier).
    consensus: list[str] = Field(
        default_factory=lambda: ["betmgm", "caesars", "espnbet", "bovada", "unibet", "pointsbet", "williamhill"]
    )

    @property
    def all(self) -> list[str]:
        return list(dict.fromkeys([*self.sharp, *self.sportsbooks, *self.pickem, *self.consensus]))


class QuotaSettings(BaseModel):
    """Provider billing guard. SGO bills 'objects' (events returned, min 1 per
    response) over a rolling 30-day window from first use."""

    monthly_objects: int = 2500
    window_days: int = 30
    # Refuse to fetch past this share of the allowance (headroom for mistakes).
    budget_fraction: float = 0.9

    @property
    def budget(self) -> int:
        return int(self.monthly_objects * self.budget_fraction)


class SportsGameOddsSettings(BaseModel):
    base_url: str = "https://api.sportsgameodds.com/v2"
    requests_per_minute: int = 10
    page_limit: int = 25
    include_alt_lines: bool = True
    timeout_seconds: float = 30
    max_retries: int = 4


class ProviderSettings(BaseModel):
    sportsgameodds: SportsGameOddsSettings = Field(default_factory=SportsGameOddsSettings)


class Settings(BaseModel):
    timezone: str = "America/New_York"
    data_dir: Path = Path("data")
    db_path: Path = Path("data/bookerbud.sqlite")
    provider: str = "sportsgameodds"
    plan: str = "amateur"
    # Where you can actually place bets; drives config/availability.yaml lookups.
    states: list[str] = Field(default_factory=lambda: ["NY", "WI"])
    quota: QuotaSettings = Field(default_factory=QuotaSettings)
    sports: dict[str, str] = Field(default_factory=lambda: {"nba": "NBA", "mlb": "MLB", "nfl": "NFL"})
    bookmakers: BookmakerRoles = Field(default_factory=BookmakerRoles)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def raw_dir(self) -> Path:
        return self._abs(self.data_dir) / "raw"

    @property
    def db_file(self) -> Path:
        return self._abs(self.db_path)

    @staticmethod
    def _abs(p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("BOOKERBUD_SETTINGS", CONFIG_DIR / "settings.yaml"))
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Settings.model_validate(data or {})


def get_secret(name: str) -> str | None:
    """Read a secret from the environment, loading .env from the project root first."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    value = os.environ.get(name)
    return value or None


def load_yaml_config(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text()) or {}


@lru_cache
def load_stat_aliases(path: Path | None = None) -> dict[str, dict[str, dict]]:
    path = path or CONFIG_DIR / "stat_aliases.yaml"
    return yaml.safe_load(path.read_text()) or {}
