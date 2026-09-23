"""
Live data sources:

1. NOAA NDBC realtime buoy observations - actual sensor readings (wave
   height/period/direction, wind) from the nearest offshore buoy. This is
   ground truth, not a model, but only tells you what's happening AT THE
   BUOY right now, not at the beach and not in the future.

2. Open-Meteo Marine + Weather forecast APIs - hourly wave/swell/wind
   forecasts for any lat/lon, built on NOAA's operational wave models
   (GFS-Wave / WaveWatch III) and ECMWF/GFS for wind. Free, no API key,
   several days out. This is what actually powers the forecast/scoring.
"""
import httpx
import json
import os
from datetime import datetime, timezone

STATIONS_PATH = os.path.join(os.path.dirname(__file__), "stations.json")
with open(STATIONS_PATH) as f:
    _STATIONS = json.load(f)

HTTP_TIMEOUT = 12.0


def find_nearest_buoy(lat: float, lon: float, max_results: int = 1):
    """Simple flat-earth nearest-neighbor search over the NDBC station list.
    Good enough at surf-forecast scale (stations are tens/hundreds of miles
    apart); avoids pulling in a geo library for this."""
    def dist2(s):
        return (s["lat"] - lat) ** 2 + (s["lon"] - lon) ** 2

    ranked = sorted(_STATIONS, key=dist2)
    return ranked[:max_results]


async def fetch_buoy_observation(station_id: str) -> dict | None:
    """Pull the most recent realtime observation for a station. Returns
    None if the station has no recent data (retired buoy, outage, etc.)
    rather than raising, so the app can degrade gracefully."""
    url = f"https://www.ndbc.noaa.gov/data/realtime2/{station_id.lower()}.txt"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

    lines = [l for l in resp.text.splitlines() if l and not l.startswith("#")]
    if not lines:
        return None

    # Columns: YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS PTDY TIDE
    # Walk rows newest-first and take the first one with usable wave data,
    # since gusts/wind often report before wave sensors on a given tick.
    for line in lines[:12]:
        parts = line.split()
        if len(parts) < 12:
            continue
        try:
            yr, mo, dy, hr, mn = parts[0:5]
            wdir, wspd, gst, wvht, dpd, apd, mwd = parts[5:12]

            def num(v):
                return None if v in ("MM", "999", "9999") else float(v)

            wave_height = num(wvht)
            if wave_height is None:
                continue

            return {
                "station_id": station_id.upper(),
                "observed_at": f"{yr}-{mo}-{dy}T{hr}:{mn}:00Z",
                "wind_dir_deg": num(wdir),
                "wind_speed_ms": num(wspd),
                "gust_ms": num(gst),
                "wave_height_m": wave_height,
                "dominant_period_s": num(dpd),
                "avg_period_s": num(apd),
                "wave_dir_deg": num(mwd),
            }
        except ValueError:
            continue
    return None


async def fetch_marine_forecast(lat: float, lon: float, days: int = 7) -> dict:
    """Hourly swell + wind forecast for a point, from Open-Meteo (wraps
    NOAA/ECMWF wave + atmospheric models). Returns aligned lists of hourly
    values, merged from the marine and weather endpoints."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        marine_resp, wind_resp = await client.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "wave_height,wave_direction,wave_period,"
                          "swell_wave_height,swell_wave_period,swell_wave_direction",
                "timezone": "auto",
                "forecast_days": days,
            },
        ), await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "wind_speed_unit": "kmh",
                "timezone": "auto",
                "forecast_days": days,
            },
        )
        marine_resp.raise_for_status()
        wind_resp.raise_for_status()

    marine = marine_resp.json()["hourly"]
    wind = wind_resp.json()["hourly"]

    # Both endpoints return the same hourly timestamps for the same
    # lat/lon/forecast_days/timezone params, so we can zip by index safely.
    times = marine["time"]
    n = len(times)
    hours = []
    for i in range(n):
        if i >= len(wind["time"]):
            break
        hours.append({
            "time": times[i],
            "wave_height_m": marine["wave_height"][i],
            "wave_period_s": marine["wave_period"][i],
            "wave_dir_deg": marine["wave_direction"][i],
            "swell_height_m": marine["swell_wave_height"][i],
            "swell_period_s": marine["swell_wave_period"][i],
            "swell_dir_deg": marine["swell_wave_direction"][i],
            "wind_speed_kmh": wind["wind_speed_10m"][i],
            "wind_gust_kmh": wind["wind_gusts_10m"][i],
            "wind_dir_deg": wind["wind_direction_10m"][i],
        })
    return {"lat": lat, "lon": lon, "fetched_at": datetime.now(timezone.utc).isoformat(), "hours": hours}


async def find_working_nearest_buoy(lat: float, lon: float, candidates: int = 6) -> tuple[str | None, dict | None]:
    """Some NDBC stations list nearshore wave sensors but don't publish a
    realtime2 feed (retired, seasonal, or a different data product).
    Try the nearest candidates in order and return the first one with an
    actual observation, so a spot doesn't get stuck pointing at a dead
    buoy forever."""
    for station in find_nearest_buoy(lat, lon, max_results=candidates):
        obs = await fetch_buoy_observation(station["id"])
        if obs:
            return station["id"], obs
    return None, None
