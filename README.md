# BookerBud

A daily sports-betting **research** tool. It finds +EV player props and game
lines for NBA, MLB and NFL by comparing DraftKings, FanDuel, PrizePicks,
Underdog and Sleeper against sharp-book (Pinnacle) no-vig fair probabilities.

> **Research only.** BookerBud never places bets and never logs into a betting
> account. It reads odds from a data provider and writes reports.

Status: **Phase 1 (data layer) is done.** See [CHANGELOG.md](CHANGELOG.md) and
[DECISIONS.md](DECISIONS.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```bash
git clone <this repo> && cd BookerBud
uv sync                      # creates .venv with Python 3.12 and all deps
cp .env.example .env         # then put your SportsGameOdds key in .env
uv run pytest                # run the test suite
```

Get a SportsGameOdds key at https://sportsgameodds.com/pricing. **Plan matters.**
Pinnacle and PrizePicks are only on the Pro plan, and Underdog needs Rookie or
higher. See the plan table in DECISIONS.md.

## Usage

```bash
uv run betedge fetch --sport nba          # today's NBA slate -> SQLite snapshot (+ raw JSON in data/raw/)
uv run betedge fetch                      # all three sports
uv run betedge fetch --sport mlb --date 2026-10-25 --include-live

uv run betedge show-props --player "lebron"                 # latest lines, every book side by side
uv run betedge show-props --player "jokic" --stat pts_reb_ast --alt
uv run betedge show-props --player "lebron" --history       # line movement across snapshots

uv run betedge unmatched                  # rows the matcher flagged (unknown stat label, unknown player, …)
uv run betedge runs                       # recent fetch runs, request counts, provider notices
uv run betedge usage                      # provider quota (costs one request)

# Re-ingest logged raw responses with no API calls (reproducible runs):
uv run betedge fetch --sport nba --replay data/raw/2026-10-24/sportsgameodds/
```

### Quota tip (free tier)
The free tier gives 2,500 "objects" a month, and each event returned counts as
one. A full NBA + MLB + NFL day is roughly 15 + 15 + 16 events, so one fetch per
day uses about 1,000–1,400 objects a month.

## Layout

```
config/settings.yaml        timezone, books by role, provider settings
config/stat_aliases.yaml    canonical stat types + every label seen for them
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
  pipeline.py               fetch -> log -> normalise -> store
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
