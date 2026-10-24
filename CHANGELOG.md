# Changelog

## Follow-up (2026-10-24)

### Fixed
- The first live run failed with HTTP 400 because the free plan rejects
  requests that name Pinnacle (the SGO docs said it would just return empty).
  Books not on the plan are now excluded in config, and any book the API
  rejects is dropped, retried and remembered.

### Changed
- Closing odds are off by default (`config/tracking.yaml`). `settle` fetches
  results only, and CLV comes from a snapshot taken after the play was priced
  and before the start. It is never taken from the play's own snapshot.
- SportsGameOdds player status is ignored (`use_provider_status: false`). The
  MLB roster/injured-list check stays on. The footer states the injury filter
  in effect.


## Phase 5 — Tracking (2026-10-24)

### Added
- `recommendations` table: every play the report shows, recorded once per
  day at its first-seen price and fair value.
- `bets` table and commands:
  - `betedge log` takes a recommendation # or searches the market;
    `--price`/`--line` override what you actually got.
  - `betedge bets`.
- `betedge settle`, also run first by `daily`:
  - One SportsGameOdds call per batch of finished games (`eventIDs` +
    `includeOpenCloseOdds`, about 1 object per game, quota-guarded).
  - Records each book's closing number and the closing fair probability at
    your line (same Pinnacle/consensus method as pre-game).
  - Computes CLV and beat-the-close, and grades results from the market
    `score` or the event `results`.
  - Falls back to the last pre-game snapshot when closing odds are missing.
    Retries, then marks a row `ungradable` after 6 attempts.
- `betedge performance` covers recommendations and bets:
  - hit rate, CLV, beat-close %, flat-stake ROI and real ROI;
  - breakdowns by sport, stat and book;
  - edge buckets and probability calibration.
- Dashboard **Performance** tab: summary tiles, breakdown tables, edge
  buckets and a calibration chart. `#performance` opens it directly.


## Phase 4 — Daily report & dashboard (2026-10-24)

### Added
- `betedge report` writes `data/reports/YYYY-MM-DD.html` (dashboard),
  `.md`, `.json` and `latest.html`.
- The header shows data freshness per sport, quota use, warnings and notes.
- Top plays per sport show: player and prop, book and price, fair vs. needed
  probability (drawn to scale), edge, Kelly stake, confidence, context notes
  and a two-sentence plain-English reason.
- Best 2–3 leg pick'em slips, plus a games panel (starters, lineups,
  weather, rest).
- The dashboard is responsive (cards on a phone, a cards/table toggle on
  desktop), supports light and dark, filters by sport, book, confidence and
  state, and works without JavaScript.
- `betedge serve`: a read-only server for the reports folder so your phone can
  view it on home Wi-Fi.
- `betedge daily`: fetch, then context, then report in one command.
- `pipeline.analyze()` is shared by `edges`, `slips` and `report`.
- `config/report.yaml`.


## Phase 3 — Context filters (2026-10-24)

### Added
- `context/` package:
  - MLB Stats API: probable pitchers, posted lineups, IL / active-roster
    status, venue coordinates, roof type and field orientation.
  - Open-Meteo: first-pitch weather with an out/in wind component; domes are skipped.
  - NBA rest and back-to-back flags, computed from stored events, with a
    quota-guarded one-call SportsGameOdds schedule backfill for yesterday.
  - SportsGameOdds player status, used for all sports.
- `config/context.yaml`: drop / downgrade / note rules for statuses, lineups,
  starters, rain, wind and back-to-backs.
- New tables: `context_snapshots` (append-only) and `schedule_coverage`.
- CLI:
  - `betedge context`.
  - `fetch` refreshes context automatically (`--no-context` to skip).
  - `edges`/`slips` apply context and show a `notes` column (`--no-context`
    to ignore it).
- SGO provider: `fetch_schedule()` (events only, includes finished games).


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
