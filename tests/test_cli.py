import json

import pytest
import yaml
from typer.testing import CliRunner

from bookerbud.cli import app
from tests.conftest import load_fixture

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data"), "db_path": str(tmp_path / "data" / "cli.sqlite")}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("BOOKERBUD_SETTINGS", str(path))
    monkeypatch.delenv("SPORTSGAMEODDS_API_KEY", raising=False)
    monkeypatch.setattr("bookerbud.config.load_dotenv", lambda *a, **k: None)
    raw = tmp_path / "raw"
    raw.mkdir()
    for i, name in enumerate(["sgo_nba_page1.json", "sgo_nba_page2.json"]):
        (raw / f"p{i}.json").write_text(json.dumps({"fetched_at": "2026-10-24T22:00:00+00:00", "body": load_fixture(name)}))
    return raw


def test_fetch_replay_then_show_props(cli_env):
    res = runner.invoke(app, ["fetch", "--sport", "nba", "--replay", str(cli_env)])
    assert res.exit_code == 0, res.output
    assert "2 events" in res.output and "flagged rows" in res.output and "provider notice" in res.output

    res = runner.invoke(app, ["show-props", "--player", "lebron", "--stat", "points"])
    assert res.exit_code == 0, res.output
    for book in ["pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper"]:
        assert book in res.output
    assert "pick'em" in res.output and "Los Angeles Lakers vs Denver Nuggets" in res.output

    res = runner.invoke(app, ["unmatched"])
    assert "doubleDoubles" in res.output and "player_not_in_roster" in res.output

    res = runner.invoke(app, ["runs"])
    assert "replay" in res.output


def test_show_props_no_data(cli_env):
    res = runner.invoke(app, ["show-props", "--player", "nobody"])
    assert res.exit_code == 1 and "No props found" in res.output


def test_fetch_without_key_fails_cleanly(cli_env):
    res = runner.invoke(app, ["fetch", "--sport", "nba"])
    assert res.exit_code == 1
    assert "SPORTSGAMEODDS_API_KEY" in res.output


def test_bad_sport(cli_env):
    res = runner.invoke(app, ["fetch", "--sport", "nhl"])
    assert res.exit_code != 0
