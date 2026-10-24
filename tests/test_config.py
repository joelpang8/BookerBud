from pathlib import Path

from bookerbud.config import CONFIG_DIR, PROJECT_ROOT, Settings, get_secret, load_settings, load_stat_aliases


def test_repo_settings_load():
    s = load_settings(CONFIG_DIR / "settings.yaml")
    assert s.sports == {"nba": "NBA", "mlb": "MLB", "nfl": "NFL"}
    assert s.bookmakers.all == ["pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper"]
    assert s.providers.sportsgameodds.requests_per_minute == 10
    assert s.tz.key == "America/New_York"


def test_relative_paths_resolve_to_project_root():
    s = Settings(data_dir=Path("data"), db_path=Path("data/x.sqlite"))
    assert s.raw_dir == PROJECT_ROOT / "data" / "raw"
    assert s.db_file == PROJECT_ROOT / "data" / "x.sqlite"


def test_missing_settings_file_gives_defaults(tmp_path):
    s = load_settings(tmp_path / "nope.yaml")
    assert s.provider == "sportsgameodds"


def test_get_secret_reads_env(monkeypatch):
    monkeypatch.setenv("BOOKERBUD_TEST_SECRET", "abc")
    assert get_secret("BOOKERBUD_TEST_SECRET") == "abc"
    monkeypatch.setenv("BOOKERBUD_TEST_SECRET", "")
    assert get_secret("BOOKERBUD_TEST_SECRET") is None


def test_stat_alias_file_has_all_sports():
    aliases = load_stat_aliases()
    assert set(aliases) == {"nba", "mlb", "nfl"}
    for sport in aliases.values():
        for spec in sport.values():
            assert "label" in spec and "aliases" in spec
