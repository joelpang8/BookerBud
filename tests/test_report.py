import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from bookerbud.context.models import GameContext
from bookerbud.models import Sport
from bookerbud.report.build import ReportCfg, build_report, report_dir
from bookerbud.report.html import render_html
from bookerbud.report.markdown import render_markdown
from bookerbud.report.serve import make_server
from bookerbud.report.text import american, market, reason
from bookerbud.storage import connect, insert_context
from tests.test_storage import _store

FETCHED = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
AS_OF = datetime(2026, 10, 24, 22, 10, tzinfo=UTC)


@pytest.fixture
def report(settings, nba_result):
    conn = connect(settings.db_file)
    _store(conn, nba_result, FETCHED)
    insert_context(conn, FETCHED, "nba", "game", [(
        "SYNTH_NBA_LAL_DEN",
        GameContext(event_id="SYNTH_NBA_LAL_DEN", sport="nba", home_team="Denver Nuggets",
                    away_team="Los Angeles Lakers", rest_days={"home": 1, "away": 0},
                    notes=["Los Angeles Lakers on a back-to-back"]).model_dump_json(),
    )])
    return build_report(conn, settings, [Sport.NBA, Sport.MLB], now=AS_OF, rcfg=ReportCfg())


def test_report_contents(report):
    nba = report.plays["nba"]
    assert nba and nba == sorted(nba, key=lambda p: -p["edge_pct"])
    assert report.plays["mlb"] == []
    top = nba[0]
    for key in ("subject", "market", "book_label", "price", "fair", "implied", "edge_pct", "stake", "confidence", "reason"):
        assert top[key] not in (None, "")
    assert top["matchup"] == "Lakers @ Nuggets" and top["start_label"] == "9:00 PM"  # ET
    lebron = next(p for p in nba if p["subject"] == "LeBron James")
    assert "Los Angeles Lakers on a back-to-back" in lebron["notes"]
    assert report.totals["plays"] == len(nba)
    assert report.totals["total_stake"] == round(sum(p["stake"] for p in nba if p["kind"] != "pickem"), 2)


def test_freshness_and_warnings(report):
    nba = next(f for f in report.freshness if f["sport"] == "nba")
    assert nba["age_min"] == pytest.approx(10) and not nba["stale"] and nba["events"] == 2
    mlb = next(f for f in report.freshness if f["sport"] == "mlb")
    assert mlb["fetched_at"] is None and mlb["stale"]
    texts = " ".join(w["text"] for w in report.warnings)
    assert "No MLB snapshot yet" in texts
    assert "Upgrade your API key" not in texts  # the free-plan notice is summarised, not repeated
    assert report.quota["budget"] == 2250


def test_stale_snapshot_warns_and_hides(settings, nba_result):
    conn = connect(settings.db_file)
    _store(conn, nba_result, FETCHED)
    r = build_report(conn, settings, [Sport.NBA], now=datetime(2026, 10, 25, 1, tzinfo=UTC), rcfg=ReportCfg())
    assert r.plays["nba"] == []
    assert any("3.0 h old" in w["text"] for w in r.warnings)


def test_reason_is_two_sentences(report):
    for p in report.plays["nba"]:
        sentences = [s for s in re.split(r"(?<=\.)\s+(?=[A-Z])", p["reason"]) if s]
        assert len(sentences) == 2, p["reason"]
        assert p["reason"].endswith(".")


def test_text_helpers(report):
    assert american(120) == "+120" and american(-115) == "−115" and american(None) == ""
    p = next(x for x in report.plays["nba"] if x["subject"] == "Stephen Curry")
    assert p["market"] == "Over 4.5 3-PT Made"
    assert "average of 4 other books (BetMGM, Caesars, DraftKings, William Hill)" in p["reason"]


def test_markdown(report):
    md = render_markdown(report)
    assert md.startswith("# BookerBud — Saturday October 24, 2026")
    assert "## NBA — top plays" in md and "| Play | Book · price |" in md
    assert "## MLB — top plays" in md and "_No plays pass the filters._" in md
    assert "Best pick'em slips" in md and "Open-Meteo" in md


def test_html_dashboard(report):
    doc = render_html(report)
    assert doc.startswith("<!doctype html>") and "viewport-fit=cover" in doc
    assert "<title>BookerBud Oct 24</title>" in doc
    assert doc.count("data-play ") == 2 * len(report.plays["nba"])  # card + table row per play
    assert 'data-f="sport" data-v="nba"' in doc and 'data-f="state" data-v="NY"' in doc
    assert "prefers-color-scheme: dark" in doc and ':root[data-theme="dark"]' in doc
    assert not re.search(r"<script[^>]+src=", doc)  # no external scripts
    frag = render_html(report, standalone=False)
    assert "<html" not in frag and "<body" not in frag and 'claude:viewport-fit-sides' in frag


def test_html_escapes_text(report):
    report.plays["nba"][0]["subject"] = '<img src=x onerror="alert(1)">'
    doc = render_html(report)
    assert "<img src=x" not in doc and "&lt;img src=x" in doc


def test_report_dir_defaults_to_data_dir(settings):
    assert report_dir(settings, ReportCfg()) == settings.raw_dir.parent / "reports"


def test_serve_redirects_and_stays_in_folder(tmp_path):
    (tmp_path / "latest.html").write_text("<p>hi</p>")
    (tmp_path.parent / "secret.txt").write_text("nope")
    srv = make_server(tmp_path, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:  # follows the redirect
            assert r.read() == b"<p>hi</p>" and r.headers["Cache-Control"] == "no-store"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../secret.txt")
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_has_performance_tab(report):
    assert report.performance is not None and report.performance["recs"]["n"] == 0
    doc = render_html(report)
    assert 'data-tab="performance"' in doc and 'data-panel="performance"' in doc
    assert "Nothing settled yet" in doc
    assert "## Performance" in render_markdown(report)
