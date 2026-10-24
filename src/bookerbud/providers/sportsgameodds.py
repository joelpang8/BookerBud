"""SportsGameOdds (https://sportsgameodds.com) provider, API v2.

Schema verified against https://sportsgameodds.com/openapi.json (v2.0.0) and
the docs at https://sportsgameodds.com/docs (checked 2026-10-24):

* GET {base}/events, auth via `x-api-key` header.
* Event.odds is keyed by oddID = {statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}.
  statEntityID is home/away/all for game lines, a playerID for player props.
* Event.odds.<oddID>.byBookmaker.<bookmakerID> = {odds, overUnder, spread,
  available, isMainLine, lastUpdatedAt, deeplink, altLines[]}. Prices/lines are
  strings ("+145", "24.5") but we also accept numbers.
* Pick'em books (prizepicks/underdog) carry the line in `overUnder`; the
  `odds` field is either missing or a flat "+100" placeholder.
* Cursor pagination via `nextCursor`; optional `notice` when the plan filters data.
* Rate limit errors are HTTP 429.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bookerbud.config import Settings, SportsGameOddsSettings, get_secret
from bookerbud.models import EventInfo, Flag, MarketKind, RawProp, RosterPlayer, Sport
from bookerbud.providers.base import (
    EventSettlement,
    FetchResult,
    OddSettlement,
    OddsProvider,
    ProviderAuthError,
    ProviderError,
)
from bookerbud.raw_log import RawLogger

API_KEY_ENV = "SPORTSGAMEODDS_API_KEY"

# Canonical BookerBud bookmaker ID -> SGO bookmakerID. Identical today, kept
# explicit so a rename on their side is a one-line fix. The last seven are the
# remaining free (Amateur) tier books, used for consensus fair odds.
BOOKMAKER_IDS = {
    "pinnacle": "pinnacle",
    "draftkings": "draftkings",
    "fanduel": "fanduel",
    "prizepicks": "prizepicks",
    "underdog": "underdog",
    "sleeper": "sleeper",
    "betmgm": "betmgm",
    "caesars": "caesars",
    "espnbet": "espnbet",
    "bovada": "bovada",
    "unibet": "unibet",
    "pointsbet": "pointsbet",
    "williamhill": "williamhill",
}
_SGO_TO_CANON = {v: k for k, v in BOOKMAKER_IDS.items()}

GAME_ENTITIES = {"home", "away", "all"}
# Two-way markets we model. Anything else (ml3way, eo, prop, ...) is counted
# in FetchResult.skipped_markets, not silently ignored.
SUPPORTED_BET_TYPES = {"ml", "sp", "ou", "yn"}


def parse_american(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().upper()
    if s in {"EVEN", "EV"}:
        return 100
    return int(float(s))


def parse_line(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_LEAGUE_SUFFIXES = {"NBA", "MLB", "NFL"}


def _name_from_player_id(player_id: str) -> str:
    """Fallback display name: LEBRON_JAMES_1_NBA -> 'Lebron James'."""
    parts = player_id.split("_")
    while parts and (parts[-1].isdigit() or parts[-1] in _LEAGUE_SUFFIXES):
        parts.pop()
    return " ".join(p.capitalize() for p in parts)


class SportsGameOddsProvider(OddsProvider):
    name = "sportsgameodds"

    def __init__(
        self,
        api_key: str | None,
        cfg: SportsGameOddsSettings,
        raw_logger: RawLogger,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        state_path: Path | None = None,
    ):
        self.api_key = api_key
        self.cfg = cfg
        self.raw_logger = raw_logger
        self._client = client or httpx.Client(base_url=cfg.base_url, timeout=cfg.timeout_seconds)
        self._sleep = sleep
        self._clock = clock
        self._min_interval = 60.0 / max(cfg.requests_per_minute, 1)
        self._last_request: float | None = None
        self._state_path = state_path
        self.unavailable_books: set[str] = set(cfg.excluded_books)
        if state_path and state_path.exists():
            try:
                self.unavailable_books |= set(json.loads(state_path.read_text()))
            except ValueError:
                pass

    @classmethod
    def from_settings(cls, settings: Settings, raw_logger: RawLogger) -> SportsGameOddsProvider:
        return cls(get_secret(API_KEY_ENV), settings.providers.sportsgameodds, raw_logger,
                   state_path=settings.raw_dir.parent / "sgo_unavailable_books.json")

    def _books_param(self, bookmakers: list[str]) -> str:
        return ",".join(BOOKMAKER_IDS[b] for b in bookmakers if b in BOOKMAKER_IDS and b not in self.unavailable_books)

    def _learn_unavailable(self, sgo_book: str) -> None:
        self.unavailable_books.add(_SGO_TO_CANON.get(sgo_book, sgo_book))
        if self._state_path:
            learned = sorted(self.unavailable_books - set(self.cfg.excluded_books))
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(learned))

    # ------------------------------------------------------------------ HTTP

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _get(self, path: str, params: dict[str, Any], label: str) -> tuple[dict[str, Any], Any]:
        if not self.api_key:
            raise ProviderAuthError(f"{API_KEY_ENV} is not set. Copy .env.example to .env and add your key.")
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._client.get(path, params=params, headers={"x-api-key": self.api_key})
            except httpx.TransportError as exc:
                if attempt >= self.cfg.max_retries:
                    raise ProviderError(f"Network error calling {path}: {exc}") from exc
                attempt += 1
                self._sleep(2**attempt)
                continue

            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raw_path = self.raw_logger.write(
                self.name, label, url=str(resp.request.url.copy_with(query=None)), params=params,
                status=resp.status_code, body=body,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.cfg.max_retries:
                    raise ProviderError(f"HTTP {resp.status_code} from {path} after {attempt} retries")
                attempt += 1
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self._sleep(delay)
                continue
            if resp.status_code in (401, 403):
                msg = body.get("error") if isinstance(body, dict) else body
                raise ProviderAuthError(f"HTTP {resp.status_code}: {msg}")
            msg = body.get("error") if isinstance(body, dict) else body
            m = re.search(r"bookmakerID (\S+) is unavailable", str(msg or ""))
            if resp.status_code == 400 and m and params.get("bookmakerID"):
                # Plan doesn't include this book: drop it, remember it, retry.
                self._learn_unavailable(m.group(1))
                remaining = [b for b in str(params["bookmakerID"]).split(",") if b and b != m.group(1)]
                if not remaining:
                    raise ProviderError(f"None of the requested bookmakers are on your plan ({msg})")
                params = {**params, "bookmakerID": ",".join(remaining)}
                continue
            if resp.status_code >= 400 or not isinstance(body, dict) or body.get("success") is False:
                raise ProviderError(f"HTTP {resp.status_code} from {path}: {msg}")
            return body, raw_path

    # ------------------------------------------------------------ Public API

    def fetch_slate(
        self,
        leagues: dict[Sport, str],
        starts_after: datetime,
        starts_before: datetime,
        bookmakers: list[str],
        *,
        include_started: bool = False,
        max_objects: int | None = None,
    ) -> FetchResult:
        # One request for all leagues: SGO bills >= 1 object per response, so
        # separate per-league calls would waste objects on empty slates.
        league_param = ",".join(leagues.values())
        params: dict[str, Any] = {
            "leagueID": league_param,
            "startsAfter": _iso_z(starts_after),
            "startsBefore": _iso_z(starts_before),
            "bookmakerID": self._books_param(bookmakers),
            "includeAltLines": str(self.cfg.include_alt_lines).lower(),
            "oddsAvailable": "true",
        }
        if not include_started:
            params["started"] = "false"

        result = FetchResult()
        cursor: str | None = None
        page = 0
        while True:
            limit = self.cfg.page_limit
            if max_objects is not None:
                remaining = max_objects - result.objects_used
                if remaining <= 0:
                    result.truncated = True
                    result.notices.append(
                        f"Stopped paginating after {result.objects_used} objects: quota budget reached. "
                        "Some events were not fetched."
                    )
                    break
                limit = min(limit, remaining)
            page_params = dict(params, limit=limit, **({"cursor": cursor} if cursor else {}))
            body, raw_path = self._get("/events", page_params, f"events_{league_param}_p{page}")
            result.requests_made += 1
            result.objects_used += max(1, len(body.get("data") or []))
            result.raw_paths.append(raw_path)
            result.extend(self.parse_payload(body, leagues, bookmakers))
            cursor = body.get("nextCursor")
            page += 1
            if not cursor or not body.get("data"):
                break
        return result

    def fetch_schedule(
        self, leagues: dict[Sport, str], starts_after: datetime, starts_before: datetime, *, max_objects: int | None = None
    ) -> FetchResult:
        """Events in the window regardless of odds or status. Still billed one
        object per event, so we shrink the payload to a single market."""
        league_param = ",".join(leagues.values())
        result = FetchResult()
        cursor: str | None = None
        page = 0
        while True:
            limit = self.cfg.page_limit if max_objects is None else min(self.cfg.page_limit, max_objects - result.objects_used)
            if limit <= 0:
                result.truncated = True
                break
            params: dict[str, Any] = {
                "leagueID": league_param,
                "startsAfter": _iso_z(starts_after),
                "startsBefore": _iso_z(starts_before),
                "oddID": "points-home-game-ml-home",
                "bookmakerID": "draftkings",
                "limit": limit,
                **({"cursor": cursor} if cursor else {}),
            }
            body, raw_path = self._get("/events", params, f"schedule_{league_param}_p{page}")
            result.requests_made += 1
            result.objects_used += max(1, len(body.get("data") or []))
            result.raw_paths.append(raw_path)
            parsed = self.parse_payload(body, leagues, [])
            result.events += parsed.events
            cursor = body.get("nextCursor")
            page += 1
            if not cursor or not body.get("data"):
                break
        return result

    def fetch_settlement(
        self, event_ids: list[str], bookmakers: list[str], *, include_close: bool = True
    ) -> tuple[dict[str, EventSettlement], FetchResult]:
        """GET /events?eventIDs=... One object per event either way. With
        include_close, adds includeOpenCloseOdds=true: closeOdds/closeOverUnder/
        closeSpread = each book's price at event start."""
        out: dict[str, EventSettlement] = {}
        meta = FetchResult()
        books = self._books_param(bookmakers)
        for i in range(0, len(event_ids), self.cfg.page_limit):
            chunk = event_ids[i : i + self.cfg.page_limit]
            params = {"eventIDs": ",".join(chunk), "bookmakerID": books, "limit": len(chunk)}
            if include_close:
                params["includeOpenCloseOdds"] = "true"
            body, raw_path = self._get("/events", params, f"settle_{i // self.cfg.page_limit}")
            meta.requests_made += 1
            meta.objects_used += max(1, len(body.get("data") or []))
            meta.raw_paths.append(raw_path)
            for ev in body.get("data") or []:
                st = parse_settlement(ev)
                out[st.event_id] = st
                for alias in ev.get("aliases") or []:  # SGO may re-key an event; keep the id we stored
                    out.setdefault(alias, st)
        return out, meta

    def usage(self) -> dict[str, Any] | None:
        body, _ = self._get("/account/usage", {}, "account_usage")
        return body.get("data")

    def close(self) -> None:
        self._client.close()

    # --------------------------------------------------------------- Parsing

    def parse_payload(self, payload: dict[str, Any], leagues: dict[Sport, str], bookmakers: list[str]) -> FetchResult:
        wanted = {BOOKMAKER_IDS[b] for b in bookmakers if b in BOOKMAKER_IDS}
        league_to_sport = {v: k for k, v in leagues.items()}
        result = FetchResult()
        if payload.get("notice"):
            result.notices.append(str(payload["notice"]))
        for raw_event in payload.get("data") or []:
            sport = league_to_sport.get(raw_event.get("leagueID", ""))
            if sport is None:
                result.skipped_markets[f"league:{raw_event.get('leagueID')}"] += 1
                continue
            event, roster = self._parse_event(raw_event, sport)
            result.events.append(event)
            result.rosters[event.event_id] = list(roster.values())
            props, skipped = self._parse_odds(raw_event.get("odds") or {}, event, roster, wanted)
            result.props += props
            result.skipped_markets.update(skipped)
        return result

    def _parse_event(self, ev: dict[str, Any], sport: Sport) -> tuple[EventInfo, dict[str, RosterPlayer]]:
        teams = ev.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}

        def team_name(t: dict[str, Any]) -> str:
            names = t.get("names") or {}
            return names.get("long") or names.get("medium") or t.get("teamID") or "?"

        status = ev.get("status") or {}
        event = EventInfo(
            provider=self.name,
            event_id=ev["eventID"],
            sport=sport,
            league=ev.get("leagueID", sport.value.upper()),
            start_time=parse_ts(status.get("startsAt")) or datetime.now(UTC),
            home_team=team_name(home),
            away_team=team_name(away),
            home_team_id=home.get("teamID"),
            away_team_id=away.get("teamID"),
            status=status.get("displayLong"),
        )
        roster: dict[str, RosterPlayer] = {}
        for pid, p in (ev.get("players") or {}).items():
            name = p.get("name") or " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x)
            roster[pid] = RosterPlayer(
                provider_player_id=pid, name=name or _name_from_player_id(pid),
                team_id=p.get("teamID"), status=p.get("status"),
            )
        return event, roster

    def _parse_odds(
        self,
        odds: dict[str, Any],
        event: EventInfo,
        roster: dict[str, RosterPlayer],
        wanted_books: set[str],
    ) -> tuple[list[RawProp], Counter]:
        props: list[RawProp] = []
        skipped: Counter = Counter()
        for odd_id, odd in odds.items():
            bet_type = odd.get("betTypeID")
            if bet_type not in SUPPORTED_BET_TYPES:
                skipped[f"bet_type:{bet_type}"] += 1
                continue
            entity = odd.get("statEntityID", "")
            is_game = entity in GAME_ENTITIES
            player_id = None if is_game else (odd.get("playerID") or entity)
            player = roster.get(player_id) if player_id else None

            base: dict[str, Any] = dict(
                provider=self.name,
                event=event,
                market_kind=MarketKind.GAME_LINE if is_game else MarketKind.PLAYER_PROP,
                provider_market_id=odd.get("oddID", odd_id),
                stat_label=odd.get("statID", ""),
                period=odd.get("periodID", "game"),
                bet_type=bet_type,
                side=odd.get("sideID", ""),
                # SGO pins statEntityID to the side for ml/sp; both sides are one
                # head-to-head market for us.
                entity=("match" if bet_type in ("ml", "sp") else entity) if is_game else "player",
            )
            if player_id:
                base.update(
                    provider_player_id=player_id,
                    player_name=player.name if player else _name_from_player_id(player_id),
                    player_team_id=player.team_id if player else None,
                    player_status=player.status if player else None,
                )

            for sgo_book, entry in (odd.get("byBookmaker") or {}).items():
                if sgo_book not in wanted_books:
                    continue
                book = _SGO_TO_CANON[sgo_book]
                props.append(self._entry_to_prop(base, entry, book, is_alt=False))
                for alt in entry.get("altLines") or []:
                    alt_entry = {**alt, "deeplink": alt.get("deeplink") or entry.get("deeplink")}
                    props.append(self._entry_to_prop(base, alt_entry, book, is_alt=True))
        return props, skipped

    @staticmethod
    def _entry_to_prop(base: dict[str, Any], entry: dict[str, Any], book: str, *, is_alt: bool) -> RawProp:
        flags: list[Flag] = []
        bet_type = base["bet_type"]
        line_raw = entry.get("overUnder") if bet_type == "ou" else entry.get("spread") if bet_type == "sp" else None
        try:
            line = parse_line(line_raw)
        except ValueError:
            line = None
        if bet_type in ("ou", "sp") and line is None:
            flags.append(Flag.NO_LINE)
        try:
            price = parse_american(entry.get("odds"))
        except ValueError:
            price = None
            flags.append(Flag.UNPARSEABLE_PRICE)
        return RawProp(
            **base,
            bookmaker=book,
            line=line,
            price_american=price,
            available=bool(entry.get("available", True)),
            is_main_line=False if is_alt else entry.get("isMainLine"),
            is_alt_line=is_alt,
            book_updated_at=parse_ts(entry.get("lastUpdatedAt")),
            deeplink=entry.get("deeplink"),
            parse_flags=flags,
        )


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def parse_settlement(ev: dict[str, Any]) -> EventSettlement:
    status = ev.get("status") or {}
    st = EventSettlement(
        event_id=ev["eventID"], started=bool(status.get("started")), ended=bool(status.get("ended")),
        finalized=bool(status.get("finalized")), cancelled=bool(status.get("cancelled")),
        results=ev.get("results") or {},
    )
    for odd_id, o in (ev.get("odds") or {}).items():
        close: dict[str, tuple[int | None, float | None]] = {}
        for sgo_book, entry in (o.get("byBookmaker") or {}).items():
            book = _SGO_TO_CANON.get(sgo_book)
            if book is None:
                continue
            line_key = "closeOverUnder" if o.get("betTypeID") == "ou" else "closeSpread" if o.get("betTypeID") == "sp" else None
            try:
                price = parse_american(entry.get("closeOdds"))
            except ValueError:
                price = None
            line = _num(entry.get(line_key)) if line_key else None
            if price is not None or line is not None:
                close[book] = (price, line)
        st.odds[odd_id] = OddSettlement(
            odd_id=o.get("oddID", odd_id), opposing_odd_id=o.get("opposingOddID"), stat_id=o.get("statID", ""),
            stat_entity=o.get("statEntityID", ""), period=o.get("periodID", "game"), bet_type=o.get("betTypeID", ""),
            side=o.get("sideID", ""), score=_num(o.get("score")), scoring_supported=o.get("scoringSupported"),
            cancelled=bool(o.get("cancelled")), close=close,
        )
    return st
