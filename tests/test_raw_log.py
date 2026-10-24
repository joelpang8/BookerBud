from datetime import UTC, datetime

from bookerbud.raw_log import RawLogger, read_raw, redact


def test_redact_hides_keys_case_insensitively():
    assert redact({"apiKey": "s3cret", "X-Api-Key": "s3cret", "leagueID": "NBA"}) == {
        "apiKey": "***", "X-Api-Key": "***", "leagueID": "NBA",
    }


def test_write_and_read_roundtrip(tmp_path):
    logger = RawLogger(tmp_path)
    now = datetime(2026, 10, 24, 18, 5, 9, 123456, tzinfo=UTC)
    path = logger.write(
        "sportsgameodds", "events NBA/p0", url="https://x/v2/events",
        params={"apiKey": "s3cret", "leagueID": "NBA"}, status=200, body={"success": True, "data": []}, now=now,
    )
    assert path.parent == tmp_path / "2026-10-24" / "sportsgameodds"
    assert path.name == "180509_123456_events_NBA_p0.json"
    assert "s3cret" not in path.read_text()
    rec = read_raw(path)
    assert rec["status"] == 200 and rec["body"] == {"success": True, "data": []}
    assert rec["params"]["apiKey"] == "***"
    assert rec["fetched_at"] == now.isoformat()
