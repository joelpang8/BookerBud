# Changelog

## Phase 1 — Data layer (2026-10-24)

### Added
- Project scaffold: uv / Python 3.12, `betedge` CLI (typer), pytest suite (93 tests).
- `OddsProvider` interface (`providers/base.py`) and a **SportsGameOdds v2**
  provider:
  - fetches today's NBA/MLB/NFL events, game lines and player props for
    Pinnacle, DraftKings, FanDuel, PrizePicks, Underdog and Sleeper
  - cursor pagination, alt lines, client-side rate limiting, and retry/backoff
    on 429/5xx
  - clear errors for auth/plan problems
  - captures plan-filtering `notice`s
- Raw response logging to `data/raw/YYYY-MM-DD/<provider>/` (API key redacted),
  plus `fetch --replay` to re-ingest logs offline.
- Canonical `Prop` record and normalisation:
  - player-name matcher (accents, suffixes, nicknames, conservative fuzzy match)
  - stat-label matcher driven by `config/stat_aliases.yaml`
  - cross-book market grouping that tolerates different lines
  - flags instead of drops: `unmapped_stat`, `player_not_in_roster`,
    `unknown_team`, `ambiguous_player`, `no_line`, `unparseable_price`
- SQLite store with append-only, timestamped snapshots (line movement) and a
  fetch-run audit table.
- CLI: `fetch`, `show-props` (with `--history`/`--alt`), `unmatched`, `runs`, `usage`.
- README, DECISIONS.md, .env.example.
