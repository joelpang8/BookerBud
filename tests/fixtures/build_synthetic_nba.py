"""Build a SYNTHETIC NBA /v2/events response following the documented SGO schema."""
import json, sys

T = "2026-10-24T21:5{}:00.000Z"

def bk(odds=None, ou=None, sp=None, avail=True, main=None, ts=0, alts=None, link=None):
    d = {}
    if odds is not None: d["odds"] = odds
    if ou is not None: d["overUnder"] = ou
    if sp is not None: d["spread"] = sp
    d["available"] = avail
    if main is not None: d["isMainLine"] = main
    d["lastUpdatedAt"] = T.format(ts)
    if link: d["deeplink"] = link
    if alts: d["altLines"] = alts
    return d

def odd(stat, ent, bet, side, opp_side, books, period="game", player=None, name=None):
    oid = f"{stat}-{ent}-{period}-{bet}-{side}"
    opp_ent = ent
    if bet in ("ml", "sp"):
        opp_ent = {"home": "away", "away": "home"}[ent]
    o = {"oddID": oid, "opposingOddID": f"{stat}-{opp_ent}-{period}-{bet}-{opp_side}", "marketName": name or oid,
         "statID": stat, "statEntityID": ent, "periodID": period, "betTypeID": bet, "sideID": side,
         "started": False, "ended": False, "cancelled": False, "bookOddsAvailable": True, "fairOddsAvailable": True,
         "byBookmaker": books}
    if player: o["playerID"] = player
    return oid, o

def ou_pair(stat, pid, label, over_books, under_books):
    a = odd(stat, pid, "ou", "over", "under", over_books, player=pid, name=f"{label} Over/Under")
    b = odd(stat, pid, "ou", "under", "over", under_books, player=pid, name=f"{label} Over/Under")
    return [a, b]

def team(tid, long, medium, short):
    return {"teamID": tid, "names": {"long": long, "medium": medium, "short": short}}

def event1():
    odds = []
    # Game lines
    odds += [odd("points", "home", "ml", "home", "away", {"pinnacle": bk("-185", ts=1), "draftkings": bk("-190", ts=2), "fanduel": bk("-178", ts=3)}, name="Moneyline"),
             odd("points", "away", "ml", "away", "home", {"pinnacle": bk("+165", ts=1), "draftkings": bk("+160", ts=2), "fanduel": bk("+150", ts=3)}, name="Moneyline"),
             odd("points", "home", "sp", "home", "away", {"pinnacle": bk("-104", sp="-4.5", ts=1), "draftkings": bk("-110", sp="-4.5", ts=2), "fanduel": bk("-108", sp="-4", ts=3)}, name="Spread"),
             odd("points", "away", "sp", "away", "home", {"pinnacle": bk("-106", sp="+4.5", ts=1), "draftkings": bk("-110", sp="+4.5", ts=2), "fanduel": bk("-112", sp="+4", ts=3)}, name="Spread"),
             odd("points", "all", "ou", "over", "under", {"pinnacle": bk("-103", ou="229.5", ts=1), "draftkings": bk("-110", ou="229.5", ts=2), "fanduel": bk("-105", ou="230", ts=3)}, name="Over/Under"),
             odd("points", "all", "ou", "under", "over", {"pinnacle": bk("-107", ou="229.5", ts=1), "draftkings": bk("-110", ou="229.5", ts=2), "fanduel": bk("-115", ou="230", ts=3)}, name="Over/Under"),
             # Team total (home) - must not collide with game total
             odd("points", "home", "ou", "over", "under", {"draftkings": bk("-115", ou="117.5", ts=2)}, name="Home Team Total"),
             odd("points", "home", "ou", "under", "over", {"draftkings": bk("-105", ou="117.5", ts=2)}, name="Home Team Total"),
             ]
    # 3-way market: out of scope, should be counted as skipped
    odds.append(odd("points", "home", "ml3way", "home", "away+draw", {"draftkings": bk("-200", ts=2)}, period="reg", name="3-Way"))
    # LeBron points: Pinnacle 24.5, DK 24.5, FD 25.5, PrizePicks 25.5, Underdog 24.5, Sleeper 25.5
    L = "LEBRON_JAMES_1_NBA"
    odds += ou_pair("points", L, "LeBron James Points",
        {"pinnacle": bk("-140", ou="24.5", ts=1, alts=[bk("+130", ou="26.5", ts=1)]), "betmgm": bk("-135", ou="24.5", ts=5), "caesars": bk("-130", ou="24.5", ts=5),
         "draftkings": bk("-115", ou="24.5", ts=2, main=True,
             alts=[bk("+120", ou="25.5", main=False, ts=2), bk("-160", ou="23.5", main=False, ts=2)]),
         "fanduel": bk("+102", ou="25.5", ts=3), "prizepicks": bk(ou="25.5", ts=4, link="https://app.prizepicks.com/?projections=1-o-25.5"),
         "underdog": bk("+100", ou="24.5", ts=4), "sleeper": bk(ou="25.5", ts=4)},
        {"pinnacle": bk("+120", ou="24.5", ts=1, alts=[bk("-165", ou="26.5", ts=1)]), "betmgm": bk("+105", ou="24.5", ts=5), "caesars": bk("+100", ou="24.5", ts=5),
         "draftkings": bk("-105", ou="24.5", ts=2, main=True),
         "fanduel": bk("-130", ou="25.5", ts=3), "prizepicks": bk(ou="25.5", ts=4), "underdog": bk("+100", ou="24.5", ts=4), "sleeper": bk(ou="25.5", ts=4)})
    odds += ou_pair("rebounds", L, "LeBron James Rebounds",
        {"pinnacle": bk("+105", ou="7.5", ts=1), "draftkings": bk("+100", ou="7.5", ts=2), "prizepicks": bk(ou="7.5", ts=4)},
        {"pinnacle": bk("-125", ou="7.5", ts=1), "draftkings": bk("-120", ou="7.5", ts=2), "prizepicks": bk(ou="7.5", ts=4)})
    J = "NIKOLA_JOKIC_1_NBA"
    odds += ou_pair("points+rebounds+assists", J, "Nikola Jokic Pts+Reb+Ast",
        {"pinnacle": bk("-110", ou="50.5", ts=1), "fanduel": bk("-114", ou="50.5", ts=3), "underdog": bk(ou="49.5", ts=4), "sleeper": bk(ou="50.5", ts=4)},
        {"pinnacle": bk("-110", ou="50.5", ts=1), "fanduel": bk("-106", ou="50.5", ts=3), "underdog": bk(ou="49.5", ts=4), "sleeper": bk(ou="50.5", ts=4)})
    odds += ou_pair("assists", J, "Nikola Jokic Assists",
        {"draftkings": bk("-130", ou="9.5", ts=2), "fanduel": bk("-125", ou="9.5", ts=3)},
        {"draftkings": bk("+105", ou="9.5", ts=2), "fanduel": bk("+100", ou="9.5", ts=3)})
    # Unknown stat label for our alias table -> flagged unmapped_stat
    M = "JAMAL_MURRAY_1_NBA"
    odds += ou_pair("doubleDoubles", M, "Jamal Murray Double Double",
        {"draftkings": bk("+400", ou="0.5", ts=2)}, {"draftkings": bk("-600", ou="0.5", ts=2)})
    # Player missing from event.players -> flagged player_not_in_roster
    X = "BRONNY_JAMES_1_NBA"
    odds += ou_pair("points", X, "Bronny James Points",
        {"prizepicks": bk(ou="3.5", ts=4)}, {"prizepicks": bk(ou="3.5", ts=4)})
    players = {
        L: {"playerID": L, "teamID": "LOS_ANGELES_LAKERS_NBA", "firstName": "LeBron", "lastName": "James", "name": "LeBron James", "status": "probable"},
        J: {"playerID": J, "teamID": "DENVER_NUGGETS_NBA", "firstName": "Nikola", "lastName": "Jokić", "name": "Nikola Jokić", "status": "active"},
        M: {"playerID": M, "teamID": "DENVER_NUGGETS_NBA", "firstName": "Jamal", "lastName": "Murray", "name": "Jamal Murray", "status": "questionable", "statusDetails": "Hamstring"},
    }
    return {"eventID": "SYNTH_NBA_LAL_DEN", "sportID": "BASKETBALL", "leagueID": "NBA", "type": "match",
            "teams": {"home": team("DENVER_NUGGETS_NBA", "Denver Nuggets", "Nuggets", "DEN"),
                      "away": team("LOS_ANGELES_LAKERS_NBA", "Los Angeles Lakers", "Lakers", "LAL")},
            "status": {"started": False, "ended": False, "live": False, "displayLong": "Upcoming", "oddsPresent": True,
                       "oddsAvailable": True, "finalized": False, "startsAt": "2026-10-25T01:00:00.000Z"},
            "players": players, "odds": dict(odds)}

def event2():
    """Free-tier shaped game: no Pinnacle, only DK/FD + the other free books."""
    odds = []
    odds += [odd("points", "home", "ml", "home", "away", {"draftkings": bk("-150", ts=2), "fanduel": bk("-135", ts=3), "betmgm": bk("-155", ts=5), "caesars": bk("-150", ts=5), "espnbet": bk("-152", ts=5)}, name="Moneyline"),
             odd("points", "away", "ml", "away", "home", {"draftkings": bk("+128", ts=2), "fanduel": bk("+114", ts=3), "betmgm": bk("+130", ts=5), "caesars": bk("+126", ts=5), "espnbet": bk("+128", ts=5)}, name="Moneyline"),
             odd("points", "all", "ou", "over", "under", {"draftkings": bk("-110", ou="226.5", ts=2), "fanduel": bk("-110", ou="226.5", ts=3), "betmgm": bk("-110", ou="227.5", ts=5), "bovada": bk("-115", ou="227", ts=5)}, name="Over/Under"),
             odd("points", "all", "ou", "under", "over", {"draftkings": bk("-110", ou="226.5", ts=2), "fanduel": bk("-110", ou="226.5", ts=3), "betmgm": bk("-110", ou="227.5", ts=5), "bovada": bk("-105", ou="227", ts=5)}, name="Over/Under")]
    C = "STEPHEN_CURRY_1_NBA"
    odds += ou_pair("threePointersMade", C, "Stephen Curry Threes",
        {"draftkings": bk("-125", ou="4.5", ts=2), "fanduel": bk("+105", ou="4.5", ts=3), "betmgm": bk("-130", ou="4.5", ts=5), "caesars": bk("-128", ou="4.5", ts=5), "williamhill": bk("-125", ou="4.5", ts=5),
         "prizepicks": bk(ou="4.5", ts=4), "underdog": bk(ou="4.5", ts=4)},
        {"draftkings": bk("-105", ou="4.5", ts=2), "fanduel": bk("-135", ou="4.5", ts=3), "betmgm": bk("+100", ou="4.5", ts=5), "caesars": bk("-102", ou="4.5", ts=5), "williamhill": bk("-105", ou="4.5", ts=5),
         "prizepicks": bk(ou="4.5", ts=4), "underdog": bk(ou="4.5", ts=4)})
    B = "DEVIN_BOOKER_1_NBA"
    odds += ou_pair("points", B, "Devin Booker Points",
        {"fanduel": bk("-110", ou="26.5", ts=3, avail=False), "draftkings": bk("-120", ou="27.5", ts=2), "betmgm": bk("-115", ou="27.5", ts=5), "espnbet": bk("-118", ou="27.5", ts=5), "sleeper": bk(ou="26.5", ts=4), "prizepicks": bk(ou="26.5", ts=4)},
        {"fanduel": bk("-110", ou="26.5", ts=3, avail=False), "draftkings": bk("-110", ou="27.5", ts=2), "betmgm": bk("-115", ou="27.5", ts=5), "espnbet": bk("-112", ou="27.5", ts=5), "sleeper": bk(ou="26.5", ts=4), "prizepicks": bk(ou="26.5", ts=4)})
    players = {C: {"playerID": C, "teamID": "GOLDEN_STATE_WARRIORS_NBA", "name": "Stephen Curry"},
               B: {"playerID": B, "teamID": "PHOENIX_SUNS_NBA", "name": "Devin Booker"}}
    return {"eventID": "SYNTH_NBA_GSW_PHX", "sportID": "BASKETBALL", "leagueID": "NBA", "type": "match",
            "teams": {"home": team("PHOENIX_SUNS_NBA", "Phoenix Suns", "Suns", "PHX"),
                      "away": team("GOLDEN_STATE_WARRIORS_NBA", "Golden State Warriors", "Warriors", "GSW")},
            "status": {"started": False, "ended": False, "displayLong": "Upcoming", "startsAt": "2026-10-25T02:30:00.000Z"},
            "players": players, "odds": dict(odds)}

note = ("SYNTHETIC test data, hand-built to the documented SportsGameOdds v2 /events schema "
        "(openapi.json + bookmaker docs). Prices are invented. Split into two pages to exercise cursor pagination.")
out = sys.argv[1]
json.dump({"_comment": note, "success": True, "data": [event1()], "nextCursor": "n.SYNTH.page2",
           "notice": "Response is missing 0 events and 3 bookmaker odds. Upgrade your API key to access all data from this query."},
          open(f"{out}/sgo_nba_page1.json", "w"), indent=1, ensure_ascii=False)
json.dump({"_comment": note, "success": True, "data": [event2()]}, open(f"{out}/sgo_nba_page2.json", "w"), indent=1, ensure_ascii=False)
