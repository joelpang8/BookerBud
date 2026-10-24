# BookerBud

A daily sports-betting **research** tool. It finds +EV player props and game
lines for NBA, MLB and NFL by comparing DraftKings, FanDuel, PrizePicks,
Underdog and Sleeper against sharp-book (Pinnacle) no-vig fair probabilities.

> **Research only.** BookerBud never places bets and never logs into a betting
> account. It reads odds from a data provider and writes reports.

Status: **All five phases are built** (data layer, edge engine, context filters, daily dashboard, tracking). See [CHANGELOG.md](CHANGELOG.md) and
[DECISIONS.md](DECISIONS.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```bash
git clone <this repo> && cd BookerBud
uv sync                      # creates .venv with Python 3.12 and all deps
cp .env.example .env         # then put your SportsGameOdds key in .env
uv run pytest                # run the test suite
```

Get a SportsGameOdds key at https://sportsgameodds.com/pricing. This project
is configured for the **free (Amateur) plan**. That plan has DraftKings,
FanDuel and 7 other US books, but **not** Pinnacle, PrizePicks, Underdog or
Sleeper. So fair odds come from a multi-book consensus, and pick'em slips stay
empty until a pick'em data source is added. See DECISIONS.md.

## Daily routine

```bash
uv run betedge daily     # settle yesterday, fetch odds + injuries/lineups/weather/rest, write the dashboard
uv run betedge serve     # open the printed http://<your-computer-ip>:8765 link on your phone
```

### First run on a new machine
`uv run betedge history --days 14` stores recent box scores so the
recent-form charts have data straight away (about 1 quota object per game).
After that, `daily` keeps them current.

## Tracking what you bet

```bash
uv run betedge log --list              # today's recommendations, numbered
uv run betedge log 3 --stake 20        # you took #3 at the shown price
uv run betedge log 3 --stake 20 --price -120 --line 25   # ...at a different price/line
uv run betedge log --player "jokic" --stat assists --side over --book fanduel --price -120 --stake 10   # not recommended
uv run betedge bets                    # your bets with CLV and results
uv run betedge performance --by stat   # ROI, CLV, hit rate, edge buckets, calibration
```

Every play the report shows is recorded automatically, whether or not you
bet it. About 4 hours after a game starts, `betedge settle` (run by `daily`)
grades it from the final stats. That costs about 1 quota object per game that
had a play. The dashboard's **Performance** tab shows the same numbers.

**CLV (closing-line value):** closing odds are off (`config/tracking.yaml`),
so CLV is measured against your own later snapshot. Run `betedge fetch`
again shortly before games start and CLV fills in for those plays; with one
fetch a day it stays blank.

`daily` writes `data/reports/YYYY-MM-DD.html` (the dashboard), a `.md` copy,
a `.json` copy, and `latest.html`. The dashboard is one self-contained file
that works on a phone (cards) and a computer (cards or a sortable-at-a-glance
table). It follows your light/dark setting and has filters for sport, book,
confidence and state. It needs no internet except for its fonts.

**Refresh odds button:** when you open the dashboard through the link
`betedge serve` prints (it includes a private key, `?k=...`), a **Refresh
odds** button appears at the top. It fetches odds, injuries and lineups and
rebuilds the page. There's a 5-minute cooldown between refreshes, and the
usual quota guard applies. Bookmark that link or add it to your phone's
home screen: the key is saved in `data/serve_key.txt`, so it keeps working.
`serve --no-refresh` hides the button.

**Phone access:** `betedge serve` shares only the reports folder, read-only, on
your home network. Phone and computer must be on the same Wi-Fi, and the
computer has to stay on while you browse. To keep it private to the computer,
use `--host 127.0.0.1`. For access away from home, a free private-network
tool such as Tailscale works without exposing anything to the internet.

## Usage

```bash
uv run betedge fetch --sport nba          # today's NBA slate -> SQLite snapshot (+ raw JSON in data/raw/)
uv run betedge fetch                      # all three sports
uv run betedge fetch --sport mlb --date 2026-10-25 --include-live

uv run betedge show-props --player "lebron"                 # latest lines, every book side by side
uv run betedge show-props --player "jokic" --stat pts_reb_ast --alt
uv run betedge show-props --player "lebron" --history       # line movement across snapshots

uv run betedge context                    # injuries, MLB lineups/starters/weather, NBA rest (also runs after fetch)
uv run betedge edges                      # +EV plays from the latest snapshot, context applied
uv run betedge edges --sport nba --min-edge 3 --book fanduel
uv run betedge slips --legs 2-3           # best pick'em slips (needs pick'em lines)
uv run betedge quota                      # rolling 30-day object usage vs. budget (no API call)
uv run betedge report                     # rebuild the dashboard from the latest snapshot (no API call)

uv run betedge unmatched                  # rows the matcher flagged (unknown stat label, unknown player, …)
uv run betedge runs                       # recent fetch runs, request counts, provider notices
uv run betedge usage                      # provider quota (costs one request)

# Re-ingest logged raw responses with no API calls (reproducible runs):
uv run betedge fetch --sport nba --replay data/raw/2026-10-24/sportsgameodds/
```

### Staying on the free tier
SGO's free plan allows 2,500 objects (≈ events) per rolling 30 days. `fetch`
makes one call for all leagues and refuses to go past 90% of that
(`config/settings.yaml → quota`). One fetch a day is roughly 300–700 objects,
so 2–3 fetches a day fit. Fetch close to when you'd bet: lines older than 30
minutes are filtered out of `edges`.

### Tuning
- `config/edge.yaml`: min edge, freshness, bankroll / Kelly fraction / cap,
  devig method, line-move models, and slip rules.
- `config/payouts.yaml`: pick'em payout tables. Re-verify these.
- `config/availability.yaml`: which books you can use in which state.
- `config/report.yaml`: plays per sport, slip sizes, output folder, quota warning level.
- `config/context.yaml`: what injuries, lineups, weather and rest do to a
  play (drop / downgrade / note).

### Context data sources (all free)
SportsGameOdds player status (in the odds response), the MLB Stats API
(individual non-commercial use), and Open-Meteo weather (CC-BY 4.0, weather
data by [Open-Meteo.com](https://open-meteo.com/)). NBA.com is deliberately
not used, because its terms forbid using its data in connection with
gambling. See DECISIONS.md.

## Layout

```
config/settings.yaml        timezone, books by role, provider settings
config/stat_aliases.yaml    canonical stat types + every label seen for them
config/edge.yaml            edge thresholds, bankroll, line models, slip rules
config/payouts.yaml         pick'em payout tables (with source + verified flag)
config/availability.yaml    which books are usable in NY / WI
config/context.yaml         injury / lineup / weather / rest rules
src/bookerbud/
  models.py                 Prop / RawProp / EventInfo / Flag
  config.py                 YAML + .env loading
  raw_log.py                writes every raw API response to data/raw/
  providers/base.py         OddsProvider interface (swap sources here)
  providers/sportsgameodds.py
  providers/registry.py
  normalize/names.py        player-name normalisation + roster matcher
  normalize/stats.py        stat-label matcher
  normalize/normalizer.py   RawProp -> Prop, flags, cross-book grouping
  storage/db.py             SQLite schema, append-only snapshots, queries
  pipeline.py               fetch -> log -> normalise -> store (+ quota guard)
  edge/odds.py, devig.py    price conversion, multiplicative/power devig
  edge/lines.py             moving fair probs across lines (Poisson / normal)
  edge/ev.py, kelly.py      EV (sportsbook + slips), Kelly sizing
  edge/engine.py            fair probability, filters -> Edge records
  edge/slips.py             pick'em slip optimizer
  context/mlb.py            MLB Stats API: starters, lineups, IL, venues
  context/weather.py        Open-Meteo game-time weather, wind in/out
  context/nba.py            rest days / back-to-backs from stored schedule
  context/apply.py          drop / downgrade / annotate plays
  report/build.py           report data: plays, slips, freshness, warnings, games
  report/text.py            labels + the two-sentence reason per play
  report/html.py            responsive dashboard (phone + desktop, light + dark)
  report/markdown.py        Markdown copy of the report
  report/serve.py           read-only LAN server for viewing on your phone
  storage/tracking.py       recommendations + bets tables
  tracking/settle.py        closing lines, CLV, grading (with snapshot fallback)
  tracking/performance.py   ROI, CLV, hit rate, edge buckets, calibration
  cli.py                    `betedge` commands
tests/                      pytest; fixtures/ has one real SGO response + a synthetic NBA slate
data/                       (gitignored) SQLite DB and raw API logs
```

## Adding a provider
Subclass `OddsProvider` (`providers/base.py`). Implement `fetch_slate()` and
`parse_payload()` so they return `RawProp`s in your source's own vocabulary
(player names/IDs, stat labels). Then register it in `providers/registry.py`.
Normalisation, matching, storage and the CLI all work unchanged. Add any new
stat labels to `config/stat_aliases.yaml`.
