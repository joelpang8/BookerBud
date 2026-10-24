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

## Phase 1 follow-up — your answers (2026-10-24)

You chose: **free SGO tier**, ET + pre-game only, states **NY and WI**, and
"stay under the free limit".

### Quota guard
- SGO's free allowance is 2,500 objects per **rolling 30 days from first use**
  (pricing FAQ), not per calendar month. One event = one object, and every
  response costs at least 1.
- Every live fetch records `objects_used`. Before fetching we sum the trailing
  30 days. Fetches stop at **90% (2,250)**, configurable in
  `settings.yaml → quota`. The remaining budget is passed down as a page-size
  cap, so a single fetch can't overshoot. `--force` spends up to the real
  2,500, never past it. Replays cost nothing. `betedge quota` shows the local
  count; `betedge usage` asks SGO.
- `fetch` now makes **one request for all leagues** (`leagueID=NBA,MLB,NFL`).
  This avoids paying the 1-object minimum three times on days when a league
  has no games.
- Corrected estimate: one fetch per day is roughly 300–700 objects / 30 days,
  depending on which seasons overlap. My earlier 1,000–1,400 figure assumed
  full slates in all three sports every day, which doesn't happen. That leaves
  room for about 2–3 fetches a day, which matters for Phase 5's closing-line
  snapshots.

### What the free tier means in practice
- Free-tier books: DraftKings, FanDuel, BetMGM, Caesars, ESPN BET, Bovada,
  Unibet, PointsBet, William Hill. **No Pinnacle, PrizePicks, Underdog or Sleeper.**
- So fair odds come from the **consensus** path (low confidence). The pick'em
  and slip sections stay empty until a pick'em line source exists. That code is
  built and tested on fixtures so it works the day data shows up. Getting
  pick'em lines by scraping is **not** something I'll build: the apps' terms
  prohibit it.
- The 7 non-target free books are configured as `bookmakers.consensus`. They
  are used for fair odds and never shown as bets.

### State availability (config/availability.yaml)
| | DK / FD sportsbook | PrizePicks | Underdog Pick'em | Sleeper Picks |
|---|---|---|---|---|
| NY | ✅ | ✅ Player Picks (no college) | ❌ | ❌ |
| WI | ❌ not live yet (AB 601 signed 2026-04-09, compacts pending) | ✅ | ✅ | ✅ |

Sources: PrizePicks where-can-I-play (updated 2026-10-19) and Sleeper
where-can-I-play (updated 2026-08-03), both read directly. The Underdog help
site returns 403 to automated reads, so its row comes from search snippets of
that official page; re-check it in the app. Plays are tagged with the states
where they're usable, and plays usable in none of your states are hidden
unless you pass `--all-books`.

## Phase 2 — edge engine

### Fair probability (src/bookerbud/edge/engine.py)
1. **Sharp:** Pinnacle's two-sided price closest to the target line (main
   line preferred), de-vigged, then moved to the target line if needed.
   Confidence is `high` at the same line and `medium` if moved.
2. **Consensus fallback:** every other priced book's two-sided pair, excluding
   the book being evaluated and pick'em apps. Each book is de-vigged and moved
   on its own, then the probabilities are **averaged**. Needs at least
   `min_consensus_books` (2) books. Confidence is always `low`.
- Spreads, totals and props are all reduced to "over/under a threshold" (home
  −4.5 = home margin over 4.5; away +4.5 = under 4.5), so one code path
  handles them. Moneylines and yes/no markets require exact pairing.
- **Devig default: power.** It shades longshots more than multiplicative does,
  which suits props and lopsided lines. Switch in `config/edge.yaml`.

### Mismatched lines (src/bookerbud/edge/lines.py)
- Fit a one-parameter distribution to the fair price at the source line, then
  read the probability at the target line.
- **Poisson** for low-count stats (threes, steals, HR, Ks, receptions, TDs).
- **Normal** for volume stats (σ = cv × line) and game margins/totals (fixed σ),
  with a continuity correction.
- Whole-number lines get a real push probability, and the fit uses the no-push
  conditional probability.
- Each stat has a `max_gap`; beyond it the play is skipped (`line_gap_too_large`).
  NFL spreads cap at 0.5 because key numbers (3, 7) break the normal model.
- The cv/sd values in `edge.yaml` are **starting assumptions** (typical
  game-to-game spread), to be checked against Phase 5 calibration.

### EV, pick'em, Kelly
- **Sportsbook EV** per unit = P(win)·(decimal−1) − P(lose); a push refunds the stake.
- **Pick'em leg edge** % = fair / break-even − 1, where break-even is the app's
  most generous per-leg break-even across its slip types (named in the output,
  e.g. `vs standard-2`). Pick'em ties void the leg, so the no-push probability
  is used.
- **Slips:** Poisson-binomial over leg probabilities against the payout table.
  Rules enforced: legs from at least 2 teams, one leg per player, and only one
  side per market. Same-game legs are **excluded** by default; `penalize`
  instead multiplies each same-game leg's probability by 0.97. That's a blunt
  haircut, not a correlation model.
- **Payout tables (config/payouts.yaml):**
  - PrizePicks: **verified** from its payouts page (updated 2026-10-19).
  - Underdog: **unverified** (403 on direct read, and third-party sites
    disagree), so its slips are labelled UNVERIFIED.
  - Sleeper: no fixed table (payout = entry × VS Score), so no slip EV.
  - Demons/goblins and other non-standard picks change the payout, and SGO data
    doesn't tell us which picks they are. Treat every pick'em number as
    standard-line only.
- **Kelly:** binary f* = (b·p − q)/(b·(p+q)) with push. Slips use a
  multi-outcome Kelly (bisection on the log-growth derivative). Both are scaled
  by 0.25 and capped at 2% of a $1,000 bankroll (all configurable).
- **Filters (config/edge.yaml):**
  - `min_edge_pct` 2%.
  - `max_line_age_minutes` 30, measured from **our fetch time**, because SGO's
    `lastUpdatedAt` may mean "last price change", which would wrongly flag a
    stable line as stale.
  - `max_ev_pct` 25%: anything above this is treated as bad data and skipped.
  - `require_sharp`, `min_consensus_books`, and `bet_books`.
- Alt lines are valid bet candidates. They're also where the biggest model
  error lives, so watch the `gap` column.

## Phase 3 — context filters (2026-10-24)

You confirmed the free plan is the long-term setup, so every Phase 3 source
is free and none spends SportsGameOdds quota, except one small, guarded NBA
backfill.

### Sources picked, and why
| Need | Source | Why |
|---|---|---|
| Injury status (all sports) | **SportsGameOdds `players[].status`** | Arrives inside the odds response we already pay for, from a provider licensed for betting use. Covers NFL, where no free official feed is usable. |
| MLB IL / active roster | **MLB Stats API** 40-man roster (`statsapi.mlb.com`) | MLB's own data. MLBAM's notice permits "individual, non-commercial, non-bulk use", which matches personal research at a few calls a day. |
| MLB probable pitchers, posted lineups | **MLB Stats API** schedule (`hydrate=probablePitcher,lineups`) | Same; lineups appear a few hours before first pitch. |
| MLB weather | **Open-Meteo** forecast at first pitch | Free, no key, documented, CC-BY 4.0, 10k calls/day for non-commercial use, and works for Toronto (NWS is US-only). Wind is resolved into an out/in component using MLB's venue `azimuthAngle` (bearing from home plate to CF; Wrigley = 37°, which checks out). Domes skip the call. |
| NBA rest / back-to-back | **Our own `events` table**, backfilled by one SportsGameOdds schedule call | See below. |

**Rejected: NBA.com** (official injury-report PDFs, the cdn.nba.com
schedule). NBA.com's Terms of Use say NBA data "may not be used in connection
with any gambling activity (including legal gambling activity)". By your
no-prohibited-sources rule, that's out.
**Rejected: ESPN's hidden endpoints.** They're undocumented and not offered
for this use.

### Known gaps (no compliant free source)
- **NBA and NFL lineups / late scratches:** only SGO's player status. NBA
  injury news that breaks after your last fetch won't be seen. Fetch close to
  bet time.
- **Teammate injuries** (e.g. usage bumps when a star sits) are not modelled.
  Only the player's own status matters.
- **Whether SGO actually fills `players[].status`** is unconfirmed: the docs'
  example responses don't include it. If it comes back empty in your first
  live fetch, NBA/NFL injury filtering is effectively off, and I'll tell you.

### Rules (config/context.yaml)
- Statuses `out`, `injured_list`, `suspended`, `doubtful` and
  `not_active_roster` → **drop**. `questionable` / `day_to_day` →
  **downgrade** (confidence high→medium→low). `probable` → note.
- MLB:
  - Once a team's lineup is posted, a batter not in it → drop.
  - Pitcher prop on someone other than the probable starter → drop.
  - Starter not announced yet → downgrade.
  - Rain probability ≥ 50% at first pitch → downgrade.
  - Wind ≥ 8 mph in/out → note.
- NBA back-to-back → note (switch to `downgrade` if you prefer). Without
  yesterday's schedule, rest shows as "unknown" rather than being guessed.

### Quota cost
- The NBA backfill fetches yesterday's games as events only (one market, one
  book). It costs about 1 object per game played yesterday, at most once per
  day, and is capped at 30 and by the 90% budget. Expect about 150–300 objects
  a month during the NBA season. `context --no-backfill` or
  `nba.backfill_yesterday: false` turns it off.
- MLB Stats API and Open-Meteo calls are free and don't touch the quota.
  Their responses are logged to `data/raw/` like everything else.

## Phase 4 — daily report & dashboard (2026-10-24)

- **One self-contained HTML file per day** rather than a web app. Nothing
  to host or keep running, it works offline, and old days stay viewable. The
  same report data also renders to Markdown and JSON, and Phase 5 will reuse
  it.
- **Phone access via `betedge serve`.** It's a read-only server for the
  reports folder (Python's stdlib file server, confined to that folder,
  `no-store` caching so the phone always gets the newest page). It binds to
  your LAN by default, because the phone is the point. It prints a warning
  that anyone on the same Wi-Fi can open it, and `--host 127.0.0.1` keeps it
  local. I didn't build public hosting: your bets would sit on the internet,
  and GitHub Pages on a private repo isn't free. Tailscale is the suggested
  way to view it away from home.
- **Design:** betting-slip tickets with a perforated tear line, a condensed
  scoreboard face for numbers, IBM Plex for text and odds, and cool
  green-grey neutrals with a blue accent.
  - Every color is a token with light and dark values.
  - Phones always get cards; desktop can switch to a table.
  - Filters are remembered per device (localStorage, failing safely).
  - The fair vs. needed probability chart uses one zoomed linear axis whose
    end labels are its real bounds, so a 3-point edge is actually visible.
- **The two-sentence reason** is generated from the numbers, not by an AI
  model. Sentence 1 states the price, what it needs to break even, and where
  the fair line came from (Pinnacle or which books, and any line move).
  Sentence 2 states the edge, why the confidence level is what it is, and any
  injury, lineup, weather or rest notes.
- **Warnings shown in the header:**
  - lines older than the freshness limit (their plays are hidden);
  - no snapshot or no context for a sport;
  - SGO sending no player statuses;
  - quota at or above 80% of budget;
  - unverified pick'em payout tables;
  - the free plan having no Pinnacle or pick'em lines.

  SGO's routine "upgrade your plan" notice is folded into the free-plan note
  rather than repeated.
