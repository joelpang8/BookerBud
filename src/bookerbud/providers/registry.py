from __future__ import annotations

from bookerbud.config import Settings
from bookerbud.providers.base import OddsProvider
from bookerbud.raw_log import RawLogger


def get_provider(settings: Settings, name: str | None = None) -> OddsProvider:
    name = name or settings.provider
    logger = RawLogger(settings.raw_dir)
    if name == "sportsgameodds":
        from bookerbud.providers.sportsgameodds import SportsGameOddsProvider

        return SportsGameOddsProvider.from_settings(settings, logger)
    raise ValueError(f"Unknown provider: {name!r}")
