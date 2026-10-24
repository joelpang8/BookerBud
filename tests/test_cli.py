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


def test_quota_command(cli_env):
    res = runner.invoke(app, ["quota"])
    assert res.exit_code == 0 and "0 objects used in the last 30 days; budget 2250" in res.output


def test_edges_and_slips_commands(cli_env):
    assert runner.invoke(app, ["fetch", "--sport", "nba", "--replay", str(cli_env)]).exit_code == 0
    res = runner.invoke(app, ["edges", "--as-of", "2026-10-24T18:10:00-04:00"])
    assert res.exit_code == 0, res.output
    assert "LeBron James over points 24.5" in res.output and "consensus(4)" in res.output
    res = runner.invoke(app, ["slips", "--as-of", "2026-10-24T18:10:00-04:00", "--legs", "2-3"])
    assert res.exit_code == 0 and "UNVERIFIED" in res.output
    stale = runner.invoke(app, ["edges", "--as-of", "2026-10-24T20:00:00-04:00"])
    assert "No plays pass the filters" in stale.output and "stale" in stale.output


def test_edges_without_snapshot(cli_env):
    res = runner.invoke(app, ["edges"])
    assert res.exit_code == 1 and "betedge fetch" in res.output


def test_context_command_and_notes_in_edges(cli_env):
    assert runner.invoke(app, ["fetch", "--sport", "nba", "--replay", str(cli_env)]).exit_code == 0
    res = runner.invoke(app, ["context", "--sport", "nba", "--no-backfill", "--date", "2026-10-24"])
    assert res.exit_code == 0, res.output
    assert "rest unknown" in res.output  # no yesterday data and backfill disabled
    res = runner.invoke(app, ["edges", "--as-of", "2026-10-24T18:10:00-04:00"])
    assert "status: probable" in res.output  # LeBron's SGO status flows into notes
    assert "status: probable; status: probable" not in res.output  # applied once per play
    bare = runner.invoke(app, ["edges", "--as-of", "2026-10-24T18:10:00-04:00", "--no-context"])
    assert "status: probable" not in bare.output


def test_report_command_writes_files(cli_env, tmp_path):
    assert runner.invoke(app, ["fetch", "--sport", "nba", "--replay", str(cli_env)]).exit_code == 0
    res = runner.invoke(app, ["report", "--as-of", "2026-10-24T18:10:00-04:00"])
    assert res.exit_code == 0, res.output
    out = tmp_path / "data" / "reports"
    for name in ("2026-10-24.html", "latest.html", "2026-10-24.md", "2026-10-24.json"):
        assert (out / name).exists(), name
    assert "LeBron James" in (out / "latest.html").read_text()


def test_serve_without_report(cli_env):
    res = runner.invoke(app, ["serve"])
    assert res.exit_code == 1 and "betedge report" in res.output


def test_log_bets_and_performance_flow(cli_env):
    assert runner.invoke(app, ["fetch", "--sport", "nba", "--replay", str(cli_env)]).exit_code == 0
    assert runner.invoke(app, ["report", "--as-of", "2026-10-24T18:10:00-04:00"]).exit_code == 0
    listing = runner.invoke(app, ["log", "--list", "--date", "2026-10-24"])
    assert "#1" in listing.output and "edge +" in listing.output
    res = runner.invoke(app, ["log", "1", "--stake", "20"])
    assert res.exit_code == 0 and "Logged bet #1" in res.output
    res = runner.invoke(app, ["log", "--player", "jokic", "--stat", "assists", "--side", "over", "--book", "fanduel",
                              "--price", "-120", "--stake", "5"])
    assert res.exit_code == 0, res.output
    assert "Nikola Jokić over 9.5 assists @ fanduel -120 for $5.00" in res.output
    ambiguous = runner.invoke(app, ["log", "--player", "lebron", "--stat", "points", "--side", "over", "--book", "nobody",
                                    "--stake", "5"])
    assert ambiguous.exit_code != 0
    assert runner.invoke(app, ["log", "1"]).exit_code != 0  # stake required
    bets = runner.invoke(app, ["bets"])
    assert "Nikola Jokić" in bets.output and "fanduel" in bets.output
    perf = runner.invoke(app, ["performance"])
    assert perf.exit_code == 0 and "Your bets" in perf.output and "2 bets" in perf.output


def test_settle_command_nothing_pending(cli_env):
    res = runner.invoke(app, ["settle"])
    assert res.exit_code == 0 and "nothing to settle" in res.output
