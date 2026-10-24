# Decisions & assumptions

Running log of choices made while building BookerBud, newest phase last. If one
of these is wrong for you, it's meant to be easy to change.

## Phase 1 — data layer

### Data source: SportsGameOdds (verified 2026-10-24)

Everything below comes from the SGO docs (https://sportsgameodds.com/docs,
`/llms-full.txt`), the OpenAPI spec (https://sportsgameodds.com/openapi.json,
v2.0.0), the pricing page and the per-bookmaker pages. I also made one
unauthenticated call to the live API to confirm the error shape.

| Item | What the docs say |
|---|---|
| Base URL | `https://api.sportsgameodds.com/v2` |
| Auth | `x-api-key` header (or `apiKey` query param). **We use only the header** so the key never ends up in logged URLs. Missing key → `401 {"success":false,"error":"Missing API key"}` (confirmed live). |
| Main endpoint | `GET /events` with `leagueID`, `startsAfter`, `startsBefore`, `bookmakerID` (comma list), `includeAltLines`, `oddsAvailable`, `started`, `limit`, `cursor`. One event = all its markets. |
| Odds shape | `event.odds[oddID].byBookmaker[bookmakerID] = {odds, overUnder, spread, available, isMainLine, lastUpdatedAt, deeplink, altLines[]}`. `oddID = statID-statEntityID-periodID-betTypeID-sideID`. Player props have `statEntityID = playerID`. |
| Pagination | Cursor: pass `nextCursor` back unchanged until it's absent. |
| Plan filtering | Responses may carry a `notice` ("Response is missing N events and M bookmaker odds…"). We store and print it. |
| Rate limits | Amateur (free) 10 req/min, Rookie 50, Pro 300. 429 on overflow. Also 2,500 / 100k / unlimited **objects per month**, where each event returned = 1 object regardless of how many markets it has. |
| Update frequency | Amateur 10 min, Rookie 3 min, Pro sub-minute. |
| Usage | `GET /account/usage` (exposed as `betedge usage`). |

**Which of your six books each plan includes (this matters a lot):**

| Book | Amateur (free) | Rookie ($99/mo yearly, $149 monthly) | Pro ($299/mo yearly) |
|---|---|---|---|
| DraftKings | ✅ | ✅ | ✅ |
| FanDuel | ✅ | ✅ | ✅ |
| Underdog | ❌ | ✅ ("Rookie and above" per their Underdog page) | ✅ |
| Sleeper | ❌ | not stated | presumably ✅ |
| **Pinnacle** | ❌ | ❌ ("Pro and above") | ✅ |
| **PrizePicks** | ❌ | ❌ (same group as Pinnacle) | ✅ |

The free tier's 9 books are FanDuel, DraftKings, BetMGM, Caesars, ESPN BET,
Bovada, Unibet, PointsBet, William Hill. So on the free tier the core
Pinnacle-vs-pick'em comparison isn't possible. A request for an excluded book
returns an empty result, not an error, so the code still works on any plan.
See the open question in the README.

**Doc inconsistencies I worked around rather than guessed:**
- Pick'em entries: the bookmaker marketing pages say PrizePicks/Underdog entries
  have *no* `odds` field. But the real response in the OpenAPI example has
  `"odds": "+100"` on both. The parser accepts either and stores whatever is
  there (`price_american` nullable). EV for pick'em will come from payout tables,
  not from this field (Phase 2).
- Prices/lines are documented as strings (`"+145"`, `"24.5"`), but the Pinnacle
  page example uses numbers. The parser accepts both.
- The OpenAPI param is `oddID`, but some marketing pages say `oddIDs`. We don't
  filter by oddID, so it doesn't matter yet.

### Scope of a fetch
- **"Today"** means local midnight to local midnight in `timezone`
  (default `America/New_York`), converted to UTC for `startsAfter`/`startsBefore`.
  This handles DST (Nov 1 is a 25-hour day, and that's tested).
- **Pre-game only by default** (`started=false`). Use `--include-live` to also
  get games already in progress.
- **Bet types:** `ml`, `sp`, `ou`, `yn` (two-way). Others (`ml3way`, `eo`,
  custom `prop` events) are counted in the run's `skipped` field, not silently
  dropped.
- **All periods** (`game`, `1h`, `1q`, …) are stored. Phase 2 can filter.
- **Alt lines are fetched** (`includeAltLines=true`) because Phase 2 needs them
  to line up mismatched lines. They're stored with `is_alt_line=1` and hidden by
  default in `show-props` (`--alt` shows them). They add response size, not object cost.
- **Unavailable rows are kept** (`available=0`). SGO v2 keeps closed markets in
  the response, and they're useful for line history.

### Canonical record & matching
- `Prop` (see `src/bookerbud/models.py`) is one book × one side × one line.
  `Prop.market_key` = (sport, event, kind, entity, player_key, stat_type, period,
  bet_type) identifies the *market*. The line is deliberately excluded so
  Pinnacle 24.5 and PrizePicks 25.5 still land in the same group.
- `entity`: `match` for moneyline/spread (both sides form one two-way market;
  SGO itself pins `statEntityID` to the side), `home`/`away` for team totals,
  `all` for game totals, `player` for props. This keeps the team total and the
  game total from colliding.
- **Player matching:** a provider player ID that's in the event roster wins.
  Otherwise we normalise the name (ASCII-fold, lowercase, strip punctuation
  and Jr./Sr./II–V), then try an exact match, then a small nickname table
  (Josh→Joshua, Mike→Michael, …), then a conservative fuzzy match (same last
  name, similarity ≥ 0.88, and exactly one candidate). Anything else gets
  `player_not_in_roster`. If two roster players normalise to the same key, the
  row gets `ambiguous_player`. `player_key = "<sport>:<normalised_name>"`.
- **Stat matching:** `config/stat_aliases.yaml` maps provider labels to
  canonical stats per sport. Only SGO statIDs seen in their docs/examples are
  marked `# verified`: points, rebounds, assists, steals, receptions,
  passing/rushing/receiving_yards, batting_totalBases, batting_homeRuns,
  batting_strikeouts, pitching_strikeouts. Unknown labels are stored with a
  derived `stat_type` and flagged `unmapped_stat`. `betedge unmatched` lists
  them so you can add aliases. I deliberately didn't guess other SGO statIDs.
- Pick'em books are configured in `settings.yaml → bookmakers.pickem`. Their
  rows get `payout_type = pickem`.

### Storage
- SQLite via the stdlib `sqlite3` module (no ORM; the schema is small and
  append-only).
- `prop_snapshots` is **append-only**: every fetch inserts a fresh row per
  book/side/line, stamped with `fetched_at` (our clock) and `book_updated_at`
  (the book's `lastUpdatedAt`). Line movement is just the sequence of
  snapshots. `show-props --history` shows it.
- `fetch_runs` records request counts, provider notices, skipped markets and
  the raw-log paths for each run.

### Raw logging & reproducibility
- Every HTTP response, including 429/5xx retries, is written to
  `data/raw/YYYY-MM-DD/<provider>/HHMMSS_ffffff_<label>.json` with redacted
  params, status and body.
- `betedge fetch --sport X --replay <file|dir>` re-ingests logged responses
  with no API calls.

### HTTP behaviour
- Client-side throttle: minimum gap of `60 / requests_per_minute` seconds
  between requests.
- Retries 429/5xx/network errors up to `max_retries`, honouring `Retry-After`
  and otherwise backing off exponentially. 401/403 are never retried.

### Naming / tooling
- Project and package name: `bookerbud`. CLI command: `betedge` (as in your spec).
- Python pinned to 3.12 via `.python-version`/`requires-python`, managed by uv.
- `typer` brings in `rich`. I didn't add any other dependencies beyond your
  list, apart from `pyyaml` (config) and `python-dotenv` (`.env`).

### Test data
- `tests/fixtures/sgo_openapi_mlb_example.json` is a **verbatim real response**
  copied from SGO's OpenAPI examples (a finished NYY–WSH game, with DK, FD,
  Pinnacle, PrizePicks and Underdog on it).
- `tests/fixtures/sgo_nba_page{1,2}.json` are **synthetic**. I hand-built them to
  the documented schema with invented prices
  (`tests/fixtures/build_synthetic_nba.py`), to cover edge cases: alt lines,
  mismatched lines, pick'em rows with no price, an unknown stat, a player
  missing from the roster, a 3-way market, pagination and a plan notice.
  **Swap in real captures from `data/raw/` once you have a key.**

### Scraping
- Nothing in Phase 1 needed scraping, and nothing scrapes.
