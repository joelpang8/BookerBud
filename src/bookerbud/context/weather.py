"""Game-time weather from Open-Meteo (https://open-meteo.com): free, no key,
documented, CC-BY 4.0. Free use is non-commercial and < 10,000 calls/day."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from bookerbud.context.http import JsonClient
from bookerbud.context.models import Weather

URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = "temperature_2m,precipitation_probability,wind_speed_10m,wind_direction_10m"


def wind_out_component(speed_mph: float, wind_from_deg: float, cf_bearing_deg: float) -> float:
    """Component of the wind blowing from home plate toward center field.

    Wind direction is where the wind comes FROM, so it blows toward
    from + 180. cf_bearing is MLB's venue azimuthAngle (home plate -> CF;
    Wrigley = 37 deg)."""
    blowing_to = (wind_from_deg + 180) % 360
    return speed_mph * math.cos(math.radians(blowing_to - cf_bearing_deg))


def parse_hourly(body: dict[str, Any], at: datetime) -> dict[str, float | None]:
    """Values for the hour containing `at` (UTC timestamps, timezone=UTC)."""
    hourly = body.get("hourly") or {}
    times = hourly.get("time") or []
    target = at.astimezone(UTC).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    if target not in times:
        return {}
    i = times.index(target)
    return {k: (v[i] if i < len(v) else None) for k, v in hourly.items() if k != "time"}


class OpenMeteo:
    def __init__(self, client: JsonClient):
        self.client = client

    def at(self, lat: float, lon: float, when: datetime, *, cf_bearing: float | None, roof: str | None) -> Weather:
        if roof == "Dome":
            return Weather(roof=roof)
        day = when.astimezone(UTC).date().isoformat()
        body = self.client.get(
            URL,
            {
                "latitude": lat, "longitude": lon, "hourly": HOURLY, "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph", "timezone": "UTC", "start_date": day, "end_date": day,
            },
            f"weather_{lat:.2f}_{lon:.2f}",
        )
        h = parse_hourly(body, when)
        speed, frm = h.get("wind_speed_10m"), h.get("wind_direction_10m")
        out = wind_out_component(speed, frm, cf_bearing) if None not in (speed, frm, cf_bearing) else None
        return Weather(
            temp_f=h.get("temperature_2m"), precip_prob=h.get("precipitation_probability"),
            wind_mph=speed, wind_from_deg=frm, wind_out_mph=out, roof=roof,
        )
