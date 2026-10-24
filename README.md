# BookerBud

A daily sports-betting **research** tool. It finds +EV player props and game
lines for NBA, MLB and NFL by comparing DraftKings, FanDuel, PrizePicks,
Underdog and Sleeper against sharp-book (Pinnacle) no-vig fair probabilities.

> **Research only.** BookerBud never places bets and never logs into a betting
> account. It reads odds from a data provider and writes reports.

Status: **Phases 1 (data layer) and 2 (edge engine) are done.** See [CHANGELOG.md](CHANGELOG.md) and
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

## Usage

```bash
uv run betedge fetch --sport nba          # today's NBA slate -> SQLite snapshot (+ raw JSON in data/raw/)
uv run betedge fetch                      # all three sports
uv run betedge fetch --sport mlb --date 2026-10-25 --include-live

uv run betedge show-props --player "lebron"                 # latest lines, every book side by side
uv run betedge show-props --player "jokic" --stat pts_reb_ast --alt
uv run betedge show-props --player "lebron" --history       # line movement across snapshots

uv run betedge edges                      # +EV plays from the latest snapshot
uv run betedge edges --sport nba --min-edge 3 --book fanduel
uv run betedge slips --legs 2-3           # best pick'em slips (needs pick'em lines)
uv run betedge quota                      # rolling 30-day object usage vs. budget (no API call)

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

## Layout

```
config/settings.yaml        timezone, books by role, provider settings
config/stat_aliases.yaml    canonical stat types + every label seen for them
config/edge.yaml            edge thresholds, bankroll, line models, slip rules
config/payouts.yaml         pick'em payout tables (with source + verified flag)
config/availability.yaml    which books are usable in NY / WI
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
