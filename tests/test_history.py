from datetime import UTC, datetime

from bookerbud.models import Sport
from bookerbud.storage import connect, recent_games, upsert_game_logs
from tests.conftest import MLB, load_fixture


def test_box_scores_parsed_from_finished_events(offline_provider):
    r = offline_provider.parse_payload(load_fixture("sgo_openapi_mlb_example.json"), MLB, [])
    teams = {g["entity_name"]: g for g in r.game_logs if g["entity_key"].startswith("team:")}
    assert teams["New York Yankees"]["stats"]["points"] == 11
    assert teams["New York Yankees"]["stats"]["points_allowed"] == 2
    wood = next(g for g in r.game_logs if g["entity_name"] == "James Wood")
    assert wood["entity_key"] == "mlb:james_wood"
    assert (wood["team"], wood["opponent"]) == ("Washington Nationals", "New York Yankees")


def test_unfinished_events_have_no_box_scores(nba_result):
    assert nba_result.game_logs == []  # upcoming games


def test_recent_games_newest_first_and_before_cutoff(settings, offline_provider):
    conn = connect(settings.db_file)
    rows = []
    for i, day in enumerate([1, 3, 5, 7]):
        rows.append({"source": "sgo", "sport": "nba", "event_id": f"E{i}", "start_time": f"2026-10-0{day}T23:00:00+00:00",
                     "entity_key": "nba:x", "entity_name": "X", "team": "T", "opponent": "O", "stats": {"points": 10 + i}})
    upsert_game_logs(conn, rows)
    upsert_game_logs(conn, rows[:1])  # idempotent
    got = recent_games(conn, ["nba:x"], before=datetime(2026, 10, 6, tzinfo=UTC), limit=2)
    assert [g["event_id"] for g in got] == ["E2", "E1"] and got[0]["stats"]["points"] == 12
