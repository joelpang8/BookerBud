# Changelog

## Phase 2 — Edge engine (2026-10-24)

### Added
- `edge/odds.py` (price conversions) and `edge/devig.py` (multiplicative and
  power devig).
- `edge/lines.py`: Poisson/normal line adjustment with push handling and a
  per-stat max gap.
- `edge/ev.py`: sportsbook EV, Poisson-binomial slip EV and per-leg break-even.
- `edge/kelly.py`: binary and multi-outcome Kelly, plus fractional stake with a cap.
- `edge/engine.py`: fair probability from Pinnacle, else a consensus of other
  books (target excluded), with confidence tags, state availability and every
  filter from config.
- `edge/slips.py`: 2–6 leg pick'em optimizer. Same-game legs are excluded or
  penalised; 2+ teams and one leg per player are enforced.
- New config files: `config/edge.yaml` (thresholds, bankroll, stat models),
  `config/payouts.yaml` (PrizePicks verified, Underdog unverified, Sleeper
  variable) and `config/availability.yaml` (NY/WI).
- CLI: `betedge edges` and `betedge slips` (`--as-of` for replays).

### Changed (Phase 1, after review)
- Free-tier setup: the other 7 free SGO books are added as consensus books.
- A single combined API call covers all leagues, with a rolling 30-day object
  budget guard (`quota` config, `betedge quota`, `fetch --force`).
- Settings gain `plan` and `states`.


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
