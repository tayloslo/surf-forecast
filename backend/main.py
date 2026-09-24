"""
Surf Forecast API - permanent spot catalog + live scored forecasts.

Endpoints:
  GET  /api/spots                   -> list all spots (id, name, lat, lon, current score/label)
  GET  /api/spots/{id}              -> one spot's full detail + scored hourly forecast + live buoy reading
  GET  /api/health

Run: uvicorn main:app --reload --port 8420
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
import time
import asyncio

from spots_data import SPOTS, SPOTS_BY_ID
from noaa_client import (
    fetch_marine_forecast, find_working_nearest_buoy, fetch_buoy_observation,
    fetch_tides_currents_wind,
)
from scoring import score_hour, score_hour_fetch_wind, label_for_score

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("surf-api")

app = FastAPI(title="Surf Forecast API")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Simple in-memory cache: forecasts don't meaningfully change minute to
# minute, and we don't want the map view to trigger 23 outbound API calls
# every time someone loads the page. Keyed by spot id, refreshed on TTL.
_CACHE: dict[int, dict] = {}
_CACHE_TTL_S = 20 * 60  # 20 minutes

# Open-Meteo free tier rate-limits concurrent requests (429s if we fire
# ~20 at once, as happens on first map load with an empty cache). Cap
# how many forecast fetches run in parallel, and retry with backoff on
# 429 so a slow start still ends up fully populated rather than half
# the spots silently showing "Unknown".
_FETCH_SEMAPHORE = asyncio.Semaphore(4)


async def _fetch_with_retry(spot, days: int, attempts: int = 4):
    for attempt in range(attempts):
        async with _FETCH_SEMAPHORE:
            try:
                return await fetch_marine_forecast(spot.lat, spot.lon, days=days)
            except Exception as e:
                if attempt == attempts - 1:
                    raise
                is_429 = "429" in str(e)
                wait = (2 ** attempt) if is_429 else 0.5
                log.warning("forecast fetch retry %s/%s for spot %s (%s)", attempt + 1, attempts, spot.id, e)
        await asyncio.sleep(wait)


async def _fetch_upwind_with_retry(spot, days: int, attempts: int = 4):
    """Same throttled/retried fetch as _fetch_with_retry, but for a spot's
    upwind reference point rather than the spot itself (fetch_wind model)."""
    for attempt in range(attempts):
        async with _FETCH_SEMAPHORE:
            try:
                return await fetch_marine_forecast(spot.upwind_lat, spot.upwind_lon, days=days)
            except Exception as e:
                if attempt == attempts - 1:
                    raise
                is_429 = "429" in str(e)
                wait = (2 ** attempt) if is_429 else 0.5
                log.warning("upwind fetch retry %s/%s for spot %s (%s)", attempt + 1, attempts, spot.id, e)
        await asyncio.sleep(wait)


async def _get_scored_forecast(spot, days: int = 7) -> dict:
    cached = _CACHE.get(spot.id)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_S:
        return cached["data"]

    forecast = await _fetch_with_retry(spot, days)

    if getattr(spot, "scoring_model", "swell") == "fetch_wind":
        # Strait/fetch-limited spot: score local wind, using the upwind
        # reference point's wind over the preceding few hours as a
        # leading indicator of fetch building down-strait.
        upwind_forecast = None
        if spot.upwind_lat is not None and spot.upwind_lon is not None:
            try:
                upwind_forecast = await _fetch_upwind_with_retry(spot, days)
            except Exception:
                log.exception("upwind forecast fetch failed for spot %s", spot.id)

        upwind_hours = upwind_forecast["hours"] if upwind_forecast else []
        scored_hours = []
        for i, h in enumerate(forecast["hours"]):
            # Look back a few hours at the upwind point for sustained fetch.
            window = upwind_hours[max(0, i - 6):i + 1] if upwind_hours else []
            scored_hours.append(score_hour_fetch_wind(spot, h, upwind_hours=window))
    else:
        scored_hours = [score_hour(spot, h) for h in forecast["hours"]]

    for sh in scored_hours:
        sh["label"] = label_for_score(sh["score"])
    data = {
        "spot_id": spot.id,
        "fetched_at": forecast["fetched_at"],
        "hours": scored_hours,
    }
    _CACHE[spot.id] = {"fetched_at": now, "data": data}
    return data


def _current_hour_score(scored: dict) -> dict | None:
    """Pick the scored hour closest to right now for the map dot color."""
    if not scored["hours"]:
        return None
    return scored["hours"][0]


@app.get("/api/health")
def health():
    return {"status": "ok", "spot_count": len(SPOTS)}


@app.get("/api/spots")
async def list_spots():
    """Lightweight list for the map: every spot plus its current score,
    fetched concurrently and cached so this stays fast."""
    async def one(spot):
        try:
            scored = await _get_scored_forecast(spot)
            cur = _current_hour_score(scored)
            return {
                "id": spot.id,
                "name": spot.name,
                "lat": spot.lat,
                "lon": spot.lon,
                "source": spot.source,
                "current_score": cur["score"] if cur else None,
                "current_label": cur["label"] if cur else "Unknown",
            }
        except Exception:
            log.exception("failed to score spot %s", spot.id)
            return {
                "id": spot.id, "name": spot.name, "lat": spot.lat, "lon": spot.lon,
                "source": spot.source, "current_score": None, "current_label": "Unknown",
            }

    results = await asyncio.gather(*(one(s) for s in SPOTS))
    return results


@app.get("/api/spots/{spot_id}")
async def spot_detail(spot_id: int):
    spot = SPOTS_BY_ID.get(spot_id)
    if not spot:
        raise HTTPException(404, "Spot not found")

    result = {
        "id": spot.id,
        "name": spot.name,
        "lat": spot.lat,
        "lon": spot.lon,
        "facing_direction": spot.facing_direction,
        "source": spot.source,
    }

    try:
        scored = await _get_scored_forecast(spot)
        result["forecast"] = scored["hours"]
        result["forecast_fetched_at"] = scored["fetched_at"]
    except Exception as e:
        log.exception("forecast fetch failed for spot %s", spot_id)
        result["forecast_error"] = f"Could not load forecast right now: {e}"

    try:
        if spot.nearest_buoy_id:
            obs = await fetch_buoy_observation(spot.nearest_buoy_id)
        else:
            obs = None
        if not obs:
            _, obs = await find_working_nearest_buoy(spot.lat, spot.lon)
        if obs:
            result["buoy_observation"] = obs
        else:
            result["buoy_error"] = "No nearby NOAA buoy currently has live data."
    except Exception:
        log.exception("buoy fetch failed for spot %s", spot_id)
        result["buoy_error"] = "No nearby NOAA buoy currently has live data."

    # Some spots (e.g. Elwha) have a second live reference point - for
    # Elwha this is the Port Angeles NOAA tide station, the closest live
    # wind observation to the spot itself (no NDBC wave buoy sits there).
    secondary_id = getattr(spot, "secondary_buoy_id", None)
    if secondary_id:
        try:
            sec_obs = await fetch_tides_currents_wind(secondary_id)
            if sec_obs:
                result["secondary_observation"] = sec_obs
        except Exception:
            log.exception("secondary buoy fetch failed for spot %s", spot_id)

    return result


# Serve the frontend as static files from the same service.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        candidate = os.path.join(_STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
