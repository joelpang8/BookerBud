"""Context records attached to games and players."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Weather(BaseModel):
    temp_f: float | None = None
    precip_prob: float | None = None  # %
    wind_mph: float | None = None
    wind_from_deg: float | None = None  # meteorological: direction wind comes FROM
    wind_out_mph: float | None = None  # component blowing home plate -> CF (negative = blowing in)
    roof: str | None = None  # Open / Retractable / Dome
    source: str = "open-meteo"

    def describe(self) -> str:
        if self.roof == "Dome":
            return "dome"
        bits = []
        if self.temp_f is not None:
            bits.append(f"{self.temp_f:.0f}°F")
        if self.wind_mph is not None:
            if self.wind_out_mph is not None and abs(self.wind_out_mph) >= 5:
                bits.append(f"wind {self.wind_mph:.0f} mph ({'out' if self.wind_out_mph > 0 else 'in'} {abs(self.wind_out_mph):.0f})")
            else:
                bits.append(f"wind {self.wind_mph:.0f} mph")
        if self.precip_prob is not None:
            bits.append(f"rain {self.precip_prob:.0f}%")
        if self.roof == "Retractable":
            bits.append("retractable roof")
        return ", ".join(bits)


class GameContext(BaseModel):
    event_id: str
    sport: str
    home_team: str
    away_team: str
    # MLB
    probable_pitchers: dict[str, str | None] = Field(default_factory=dict)  # "home"/"away" -> name
    lineups_posted: dict[str, bool] = Field(default_factory=dict)  # "home"/"away"
    lineups: dict[str, list[str]] = Field(default_factory=dict)
    weather: Weather | None = None
    # NBA
    rest_days: dict[str, int | None] = Field(default_factory=dict)  # "home"/"away"; 0 = back-to-back
    notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

    @property
    def back_to_back(self) -> dict[str, bool]:
        return {side: d == 0 for side, d in self.rest_days.items() if d is not None}


class PlayerContext(BaseModel):
    player_key: str
    name: str
    team: str | None = None
    status: str | None = None  # normalised: active, questionable, doubtful, out, injured_list, not_active_roster, ...
    detail: str | None = None
    source: str
