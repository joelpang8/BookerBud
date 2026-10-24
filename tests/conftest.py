from __future__ import annotations

import json
from pathlib import Path

import pytest

from bookerbud.config import Settings
from bookerbud.models import Sport
from bookerbud.providers.sportsgameodds import SportsGameOddsProvider
from bookerbud.raw_log import RawLogger
from bookerbud.config import SportsGameOddsSettings

FIXTURES = Path(__file__).parent / "fixtures"
ALL_BOOKS = ["pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper"]
CONSENSUS = ["betmgm", "caesars", "espnbet", "bovada", "unibet", "pointsbet", "williamhill"]
NBA = {Sport.NBA: "NBA"}
MLB = {Sport.MLB: "MLB"}
ALL_LEAGUES = {Sport.NBA: "NBA", Sport.MLB: "MLB", Sport.NFL: "NFL"}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", db_path=tmp_path / "data" / "test.sqlite")


@pytest.fixture
def offline_provider(tmp_path: Path) -> SportsGameOddsProvider:
    """Provider with no network access; only parse_payload is used."""
    return SportsGameOddsProvider("test-key", SportsGameOddsSettings(), RawLogger(tmp_path / "raw"))


@pytest.fixture
def nba_result(offline_provider):
    r1 = offline_provider.parse_payload(load_fixture("sgo_nba_page1.json"), NBA, ALL_BOOKS + CONSENSUS)
    r2 = offline_provider.parse_payload(load_fixture("sgo_nba_page2.json"), NBA, ALL_BOOKS + CONSENSUS)
    r1.extend(r2)
    return r1


@pytest.fixture
def mlb_result(offline_provider):
    return offline_provider.parse_payload(load_fixture("sgo_openapi_mlb_example.json"), MLB, ALL_BOOKS)
