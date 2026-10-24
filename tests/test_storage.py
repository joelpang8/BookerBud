from datetime import UTC, datetime, timedelta

from bookerbud.normalize import normalize_props
from bookerbud.storage import connect, finish_run, flagged_summary, insert_props, latest_runs, query_player_props, start_run, upsert_events

T0 = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
PICKEM = {"prizepicks", "underdog", "sleeper"}


def _store(conn, result, fetched_at, mutate=None):
    props, _ = normalize_props(result.props, result.rosters, fetched_at=fetched_at, pickem_books=PICKEM)
    if mutate:
        props = [mutate(p) for p in props]
    run = start_run(conn, "sportsgameodds", "nba", "replay", fetched_at)
    upsert_events(conn, result.events, fetched_at)
    insert_props(conn, run, props)
    finish_run(conn, run, finished_at=fetched_at, requests_made=0, n_events=len(result.events), n_props=len(props),
               n_flagged=sum(1 for p in props if p.flags), notices=result.notices, skipped=dict(result.skipped_markets),
               raw_paths=[])
    return run, props


def test_snapshots_track_line_movement(tmp_path, nba_result):
    conn = connect(tmp_path / "db.sqlite")
    _store(conn, nba_result, T0)

    def move(p):  # PrizePicks moves LeBron 25.5 -> 26.5 an hour later
        if p.bookmaker == "prizepicks" and p.player_key == "nba:lebron_james" and p.stat_type == "points":
            return p.model_copy(update={"line": 26.5})
        return p

    _store(conn, nba_result, T0 + timedelta(hours=1), mutate=move)

    latest = query_player_props(conn, "lebron", stat_type="points", bookmaker="prizepicks")
    assert set(latest["line"]) == {26.5} and len(latest) == 2  # over + under, latest only

    hist = query_player_props(conn, "lebron", stat_type="points", bookmaker="prizepicks", history=True)
    over = hist[hist["side"] == "over"].sort_values("fetched_at")
    assert list(over["line"]) == [25.5, 26.5]


def test_search_is_accent_insensitive_and_filters(tmp_path, nba_result):
    conn = connect(tmp_path / "db.sqlite")
    _store(conn, nba_result, T0)
    df = query_player_props(conn, "JOKIC")
    assert set(df["player"]) == {"Nikola Jokić"}
    assert set(df["bookmaker"]) == {"pinnacle", "draftkings", "fanduel", "underdog", "sleeper"}
    lebron_dk = query_player_props(conn, "lebron james", stat_type="points", bookmaker="draftkings")
    assert lebron_dk["is_alt_line"].eq(0).all() and len(lebron_dk) == 2
    with_alt = query_player_props(conn, "lebron james", stat_type="points", bookmaker="draftkings", include_alt=True)
    assert sorted(with_alt[with_alt["is_alt_line"] == 1]["line"]) == [23.5, 25.5]
    assert query_player_props(conn, "nobody").empty


def test_runs_and_flag_summary(tmp_path, nba_result):
    conn = connect(tmp_path / "db.sqlite")
    run, props = _store(conn, nba_result, T0)
    runs = latest_runs(conn)
    assert runs.iloc[0]["run_id"] == run and runs.iloc[0]["n_props"] == len(props)
    flags = flagged_summary(conn)
    assert set(flags["stat_label_raw"]) == {"doubleDoubles", "points"}
    assert "player_not_in_roster" in ",".join(flags["flags"])


def test_events_upsert_is_idempotent(tmp_path, nba_result):
    conn = connect(tmp_path / "db.sqlite")
    upsert_events(conn, nba_result.events, T0)
    upsert_events(conn, nba_result.events, T0 + timedelta(minutes=5))
    rows = conn.execute("SELECT event_id, last_seen_at FROM events").fetchall()
    assert len(rows) == 2 and all(r["last_seen_at"].startswith("2026-10-24T18:05") for r in rows)


def test_flag_summary_defaults_to_latest_run_per_sport(tmp_path, nba_result, mlb_result):
    conn = connect(tmp_path / "db.sqlite")
    _store(conn, nba_result, T0)
    # a later, clean MLB run must not hide the NBA flags
    props, _ = normalize_props(mlb_result.props, mlb_result.rosters, fetched_at=T0, pickem_books=PICKEM)
    run = start_run(conn, "sportsgameodds", "mlb", "replay", T0)
    insert_props(conn, run, props)
    assert set(flagged_summary(conn)["sport"]) == {"nba"}
    assert flagged_summary(conn, run_id=run).empty
