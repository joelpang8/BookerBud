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


def test_top_picks_rank_by_confidence_weighted_edge(report):
    tops = report.top_picks
    assert [p["rank"] for p in tops] == [1, 2, 3]
    scores = [p["score"] for p in tops]
    assert scores == sorted(scores, reverse=True)
    w = ReportCfg().confidence_weight
    for p in tops:
        assert p["score"] == pytest.approx(p["edge_pct"] * w[p["confidence"]])
    # one pick per player / game line
    assert len({(p["subject"], p["matchup"]) for p in tops}) == len(tops)
    # nothing left out scores higher than the last pick (excluding a player already picked)
    picked = {(p["subject"], p["matchup"]) for p in tops}
    rest = [p for ps in report.plays.values() for p in ps if (p["subject"], p["matchup"]) not in picked]
    assert all(p["edge_pct"] * w[p["confidence"]] <= scores[-1] + 1e-9 for p in rest)


def test_top_picks_on_dashboard_and_markdown(report):
    doc = render_html(report)
    top = doc.index('aria-label="Top picks"')
    assert top < doc.index('class="board"')  # above everything else on the Today tab
    first = report.top_picks[0]
    assert f'href="#{first["anchor"]}"' in doc and f'id="{first["anchor"]}"' in doc  # jumps to the full card
    assert "## Top 3 picks" in render_markdown(report)


def test_top_picks_empty_state(settings, nba_result):
    conn = connect(settings.db_file)
    _store(conn, nba_result, FETCHED)
    r = build_report(conn, settings, [Sport.NBA], now=datetime(2026, 10, 25, 3, tzinfo=UTC), rcfg=ReportCfg())
    assert r.top_picks == [] and "No plays pass the filters right now" in render_html(r)


# ------------------------------------------------------------- form + reasoning
from bookerbud.normalize.names import player_key as _pk, team_key as _tk  # noqa: E402
from bookerbud.report.html import _form_svg  # noqa: E402
from bookerbud.storage import upsert_game_logs  # noqa: E402


def _log(entity_key, name, day, stats, team="T", opp="O"):
    return {"source": "test", "sport": "nba", "event_id": f"{name}-{day}", "start_time": f"2026-10-{day:02d}T23:00:00+00:00",
            "entity_key": entity_key, "entity_name": name, "team": team, "opponent": opp, "stats": stats}


@pytest.fixture
def report_with_history(settings, nba_result):
    conn = connect(settings.db_file)
    _store(conn, nba_result, FETCHED)
    pts = [30, 22, 26, 25, 28]  # vs LeBron's 24.5: hit, miss, hit, hit, hit
    upsert_game_logs(conn, [_log(_pk("nba", "LeBron James"), "LeBron James", 10 + i, {"points": v}) for i, v in enumerate(pts)]
                     + [_log(_pk("nba", "LeBron James"), "LeBron James", 30, {"points": 99})])  # after tip-off: ignored
    upsert_game_logs(conn, [_log(_tk("nba", "Denver Nuggets"), "Denver Nuggets", 10 + i, {"points": 110 + i, "points_allowed": 106})
                            for i in range(3)])  # margins 4, 5, 6
    return build_report(conn, settings, [Sport.NBA], now=AS_OF, rcfg=ReportCfg())


def _find(r, subject, book, line):
    return next(p for p in r.plays["nba"] if p["subject"] == subject and p["book"] == book and p["line"] == line)


def test_form_for_player_prop(report_with_history):
    p = _find(report_with_history, "LeBron James", "draftkings", 24.5)
    f = p["form"]
    assert [g["value"] for g in f["games"]] == [30, 22, 26, 25, 28]  # oldest first, post-start game excluded
    assert (f["threshold"], f["hits"], f["n"]) == (24.5, 4, 5)
    assert f["avg"] == pytest.approx(26.2)
    assert any(w["label"] == "Recent form" and "4 of the last 5" in w["text"] for w in p["why"])


def test_form_for_team_spread(settings):
    from bookerbud.report.form import form_for
    from tests.test_context_apply import edge

    conn = connect(settings.db_file)
    upsert_game_logs(conn, [_log(_tk("nba", "Denver Nuggets"), "Denver Nuggets", 10 + i,
                                 {"points": 110 + i, "points_allowed": 106}) for i in range(3)])  # margins 4, 5, 6
    e = edge(sport="nba", player=None, team="Denver Nuggets", stat="points")
    e.market_kind, e.bet_type, e.side, e.line, e.entity = "game_line", "sp", "home", -4.5, "match"
    e.start_time = AS_OF
    f = form_for(conn, e, {}, 10)
    # covers -4.5 when margin > 4.5: 4 miss, 5 hit, 6 hit
    assert f["what"] == "margin" and f["threshold"] == 4.5
    assert [g["result"] for g in f["games"]] == ["miss", "hit", "hit"]


def test_why_breaks_down_consensus(report_with_history):
    curry = next(p for p in report_with_history.plays["nba"] if p["subject"] == "Stephen Curry" and p["book"] == "fanduel")
    why = {w["label"]: w["text"] for w in curry["why"]}
    assert set(why) >= {"Price", "Fair value", "Line shopping", "Recent form", "Risk"}
    assert "4 other books" in why["Fair value"] and "BetMGM 53.4%" in why["Fair value"]
    assert why["Line shopping"].startswith("Best price at this line: FanDuel +105 vs")
    assert "no Pinnacle price" in why["Risk"]
    assert curry["driver"].startswith("FanDuel is the outlier: +105 vs")
    assert "No game log stored yet" in why["Recent form"]  # Curry has no history in this fixture


def test_form_chart_is_to_scale():
    form = {"games": [{"date": "10/1", "opp": "X", "value": 10, "result": "miss"},
                      {"date": "10/3", "opp": "Y", "value": 30, "result": "hit"}],
            "threshold": 20, "n": 2, "hits": 1}
    svg = _form_svg(form)
    import re

    heights = [float(h) for h in re.findall(r'class="bar \w+" x="[\d.]+" y="[\d.]+" width="[\d.]+" height="([\d.]+)"', svg)]
    assert heights[1] == pytest.approx(3 * heights[0], rel=1e-2)  # 30 vs 10, shared zero baseline
    ys = re.findall(r'class="thr" x1="[\d.]+" y1="([\d.]+)"', svg)
    assert ys  # dashed line at the threshold
    assert ">20<" in svg and "10/1" in svg and "10/3" in svg


def test_dashboard_shows_form_and_why(report_with_history):
    doc = render_html(report_with_history)
    assert "Last 5 games" in doc and "Why this pick" in doc
    assert doc.count('<details class="why" open>') == len(report_with_history.top_picks)  # top picks open by default
