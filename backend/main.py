"""
Surf Forecast API - permanent spot catalog + live scored forecasts.

Endpoints:
  GET  /api/spots                   -> list all spots (id, name, lat, lon, current score/label)
  GET  /api/spots/{id}              -> one spot's full detail + scored hourly forecast + live buoy reading
  GET  /api/health

Run: uvicorn main:app --reload --port 8420

All forecast/observation values returned by this API are US units
(feet, mph) - see noaa_client.py and scoring.py for where the unit
normalization happens.
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import logging
import math
import os
import time
import asyncio
from datetime import datetime

from spots_data import SPOTS, SPOTS_BY_ID
from noaa_client import (
    fetch_marine_forecast, find_working_nearest_buoy, fetch_buoy_observation,
    fetch_tides_currents_wind, fetch_historical_observations, fetch_buoy_spectral,
    fetch_historical_wave_observations,
)
from scoring import (
    score_hour, score_hour_swell_transmission, label_for_score,
    score_live_wave_observation, build_current_conditions,
    scale_description_for_spot, quality_factors_for_spot, build_local_swell_benchmark,
    build_swell_transmission_model, TRANSMISSION_LAG_HOURS, STRAIT_AXIS_BEARING_DEG,
)

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

# Same idea as _LOCAL_BENCHMARK_CACHE, but for the empirical swell-
# transmission model (see build_swell_transmission_model) - the
# upwind(Neah Bay)->local(Angeles Point) height ratio / direction /
# period lookups learned from years of paired buoy history. This is the
# core calibration the new forecast model runs on, so it's worth caching
# for a long time (24h) rather than rebuilding on every request - it's a
# multi-year archive pull on a cache miss.
_TRANSMISSION_CACHE: dict[tuple, dict] = {}
_TRANSMISSION_CACHE_TTL_S = 24 * 60 * 60  # 24 hours

# Same idea as _TRANSMISSION_CACHE, but keyed on the LOCAL wave buoy (e.g.
# Angeles Point/46267 for Elwha) alone - this is the "how big has it
# actually gotten right at this spot, historically" benchmark, a
# different, simpler question than the upwind transmission calibration
# above.
_LOCAL_BENCHMARK_CACHE: dict[str, dict] = {}
_LOCAL_BENCHMARK_CACHE_TTL_S = 24 * 60 * 60  # 24 hours (same multi-year archive cost as _TRANSMISSION_CACHE)

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


async def _get_transmission_model(spot) -> dict | None:
    """Build (or return cached) the empirical swell-transmission model
    (see build_swell_transmission_model) for a fetch_wind spot that has
    both an upwind reference buoy and a local wave buoy configured - the
    learned upwind(Neah Bay)->local(Angeles Point) height-ratio/
    direction/period relationship the new forecast score is built on,
    used both to project local swell per forecast hour and to explain
    the mechanism in the UI."""
    upwind_id = spot.nearest_buoy_id
    local_id = getattr(spot, "local_wave_buoy_id", None)
    if not upwind_id or not local_id:
        return None
    key = (upwind_id, local_id)
    cached = _TRANSMISSION_CACHE.get(key)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < _TRANSMISSION_CACHE_TTL_S:
        return cached["data"]
    try:
        local_rows = await fetch_historical_wave_observations(local_id)
        upwind_rows = await fetch_historical_observations(upwind_id)
        model = build_swell_transmission_model(
            local_rows, upwind_rows, STRAIT_AXIS_BEARING_DEG,
        )
    except Exception:
        log.exception("transmission model build failed for buoys %s/%s", upwind_id, local_id)
        return None
    _TRANSMISSION_CACHE[key] = {"fetched_at": now, "data": model}
    return model


async def _get_local_swell_benchmark(spot) -> dict | None:
    """Build (or return cached) an all-time-max/good-day benchmark from
    the LOCAL wave buoy's own history (e.g. Angeles Point/46267 for
    Elwha) - a different, more directly relevant question than the
    upwind fetch-wind calibration: "how big has it actually gotten right
    here, and how often does it cross into 'good' territory", since
    local swell size is the dominant factor in whether this spot actually
    breaks well."""
    buoy_id = getattr(spot, "local_wave_buoy_id", None)
    if not buoy_id:
        return None
    cached = _LOCAL_BENCHMARK_CACHE.get(buoy_id)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < _LOCAL_BENCHMARK_CACHE_TTL_S:
        return cached["data"]
    try:
        rows = await fetch_historical_wave_observations(buoy_id)
        benchmark = build_local_swell_benchmark(rows)
    except Exception:
        log.exception("local swell benchmark fetch failed for buoy %s", buoy_id)
        return None
    _LOCAL_BENCHMARK_CACHE[buoy_id] = {"fetched_at": now, "data": benchmark}
    return benchmark



async def _get_scored_forecast(spot, days: int = 7) -> dict:
    cached = _CACHE.get(spot.id)
    now = time.time()
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_S:
        return cached["data"]

    forecast = await _fetch_with_retry(spot, days)

    if getattr(spot, "scoring_model", "swell") == "fetch_wind":
        # Strait spot: project local swell from the upwind (Neah Bay)
        # forecast via the validated swell-transmission model, lagged by
        # TRANSMISSION_LAG_HOURS (the measured propagation delay), then
        # score that projected swell the normal way - local wind at the
        # spot itself is only a grooming/onshore-blowout modifier, not a
        # wave-generation input (see scoring.py module header for why).
        upwind_forecast = None
        if spot.upwind_lat is not None and spot.upwind_lon is not None:
            try:
                upwind_forecast = await _fetch_upwind_with_retry(spot, days)
            except Exception:
                log.exception("upwind forecast fetch failed for spot %s", spot.id)

        transmission_model = await _get_transmission_model(spot)

        upwind_hours = upwind_forecast["hours"] if upwind_forecast else []
        scored_hours = []
        for i, h in enumerate(forecast["hours"]):
            # The swell that left Neah Bay TRANSMISSION_LAG_HOURS ago is
            # what's arriving here now (both forecasts are hourly and
            # share the same start time, so this is a simple index offset).
            lag_idx = i - TRANSMISSION_LAG_HOURS
            upwind_hour = upwind_hours[lag_idx] if upwind_hours and lag_idx >= 0 else None
            scored_hours.append(score_hour_swell_transmission(
                spot, h, upwind_hour=upwind_hour, transmission_model=transmission_model,
            ))
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
    """Pick the scored hour closest to right now for the map dot color.
    Forecast hours start at midnight of the request day (Open-Meteo always
    returns a full day from 00:00), so index 0 is NOT "now" - picking it
    unconditionally showed stale/wrong-time-of-day conditions (e.g. calm
    overnight wind) as the current score any time this was checked later
    in the day. Match on the hour whose local timestamp is closest to
    the current wall-clock time instead."""
    hours = scored["hours"]
    if not hours:
        return None
    now = datetime.now()
    best = hours[0]
    best_diff = None
    for h in hours:
        try:
            h_time = datetime.fromisoformat(h["time"])
        except (ValueError, TypeError):
            continue
        diff = abs((h_time - now).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = h
    return best


@app.get("/api/health")
def health():
    return {"status": "ok", "spot_count": len(SPOTS)}


# ---------------------------------------------------------------------------
# Swell window / strait overlay: a handful of points running from the
# Pacific (offshore of Neah Bay, at the strait's mouth) down the strait
# axis toward Elwha, each queried against Open-Meteo's marine forecast so
# the map can render "what does the current storm look like as it enters
# the swell window" rather than only the single upwind reference point
# already used for scoring. This is deliberately a small, fixed set of
# points (not a dense grid) - Open-Meteo's free tier only tolerates a
# handful of concurrent requests, and a handful spaced along the strait
# axis is enough to visualize a storm's shape/size/direction on approach.
_STRAIT_MOUTH = (48.493, -124.727)  # Neah Bay, at the strait's entrance


def _destination_point(lat: float, lon: float, bearing_deg: float, distance_nm: float) -> tuple[float, float]:
    """Great-circle destination point given a start, bearing, and
    distance (nautical miles) - used to lay out points along the strait
    axis without hand-typing each lat/lon."""
    r_km = 6371.0
    dist_km = distance_nm * 1.852
    lat1, lon1, brng = math.radians(lat), math.radians(lon), math.radians(bearing_deg)
    d_r = dist_km / r_km
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1), math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 4), round(math.degrees(lon2), 4)


# Distances offshore of the strait mouth (nautical miles) along the axis
# reciprocal bearing (i.e. heading OUT into the Pacific, away from the
# strait) where we sample the swell field - out to ~180nm covers where a
# typical Pacific storm's fetch sits relative to the strait's swell
# window, plus the mouth itself and partway down-strait toward Elwha.
_SWELL_WINDOW_POINTS = [
    {"label": "Elwha (down-strait)", "distance_nm": -25, "bearing_deg": (STRAIT_AXIS_BEARING_DEG + 180) % 360},
    {"label": "Strait mouth (Neah Bay)", "distance_nm": 0, "bearing_deg": STRAIT_AXIS_BEARING_DEG},
    {"label": "30nm offshore", "distance_nm": 30, "bearing_deg": STRAIT_AXIS_BEARING_DEG},
    {"label": "70nm offshore", "distance_nm": 70, "bearing_deg": STRAIT_AXIS_BEARING_DEG},
    {"label": "120nm offshore", "distance_nm": 120, "bearing_deg": STRAIT_AXIS_BEARING_DEG},
    {"label": "180nm offshore", "distance_nm": 180, "bearing_deg": STRAIT_AXIS_BEARING_DEG},
]


@app.get("/api/swell-window")
async def swell_window():
    """Current + a few hours of Open-Meteo swell forecast at a line of
    points running from offshore of the Pacific, in through the strait's
    mouth, to partway down-strait toward Elwha - the actual "swell
    window" this model's transmission logic depends on. Lets the map
    show where a storm's swell currently sits relative to the strait
    mouth, visually, instead of only a single upwind reference point."""
    points = []
    for p in _SWELL_WINDOW_POINTS:
        lat, lon = _destination_point(
            _STRAIT_MOUTH[0], _STRAIT_MOUTH[1], p["bearing_deg"], abs(p["distance_nm"]),
        )
        points.append({**p, "lat": lat, "lon": lon})

    async def fetch_one(p):
        try:
            async with _FETCH_SEMAPHORE:
                forecast = await fetch_marine_forecast(p["lat"], p["lon"], days=2)
            hour0 = forecast["hours"][0] if forecast.get("hours") else None
            return {
                "label": p["label"],
                "lat": p["lat"],
                "lon": p["lon"],
                "distance_nm": p["distance_nm"],
                "swell_height_ft": hour0.get("swell_height_ft") if hour0 else None,
                "swell_period_s": hour0.get("swell_period_s") if hour0 else None,
                "swell_dir_deg": hour0.get("swell_dir_deg") if hour0 else None,
                "time": hour0.get("time") if hour0 else None,
            }
        except Exception:
            log.exception("swell window point fetch failed for %s", p["label"])
            return {"label": p["label"], "lat": p["lat"], "lon": p["lon"], "distance_nm": p["distance_nm"], "error": True}

    results = await asyncio.gather(*(fetch_one(p) for p in points))
    return {"axis_bearing_deg": STRAIT_AXIS_BEARING_DEG, "points": results}


# ---------------------------------------------------------------------------
# Storm archive: screenshots (generated offline from the 2020-present NDBC
# archive, see static/storm_archive/storms.json) of the biggest distinct
# swell events actually recorded at the local buoy (Angeles Point/46267),
# each showing the local wave height building through the event alongside
# the upwind Neah Bay wave height/wind that produced it - concrete visual
# reference for "here's what a real 10ft+ day here has actually looked
# like on the buoys" alongside the live map.
# ---------------------------------------------------------------------------
@app.get("/api/storm-archive")
def storm_archive():
    path = os.path.join(os.path.dirname(__file__), "static", "storm_archive", "storms.json")
    if not os.path.isfile(path):
        return {"storms": []}
    with open(path, encoding="utf-8") as f:
        storms = json.load(f)
    for s in storms:
        s["image_url"] = f"/storm_archive/{s['image']}"
    return {"storms": storms}


@app.get("/api/spots")
async def list_spots():
    """Lightweight list for the map: every spot plus its current score,
    fetched concurrently and cached so this stays fast."""
    async def one(spot):
        try:
            scored = await _get_scored_forecast(spot)
            cur = _current_hour_score(scored)
            score = cur["score"] if cur else None
            label = cur["label"] if cur else "Unknown"

            # For "right now" specifically, prefer a live nearby wave buoy
            # reading over the modeled forecast score when one is
            # available - real ground truth beats a model guess for the
            # current instant, and the forecast-only path was showing
            # stale/wrong conditions (e.g. calm overnight wind carried
            # forward) while a real buoy a couple km away was reporting
            # an actual multi-foot wave.
            local_wave_id = getattr(spot, "local_wave_buoy_id", None)
            if local_wave_id:
                try:
                    obs = await fetch_buoy_observation(local_wave_id)
                    live = score_live_wave_observation(spot, obs) if obs else None
                    if live:
                        score = live["score"]
                        label = label_for_score(score)
                except Exception:
                    log.exception("live wave scoring failed for spot %s", spot.id)

            return {
                "id": spot.id,
                "name": spot.name,
                "lat": spot.lat,
                "lon": spot.lon,
                "source": spot.source,
                "current_score": score,
                "current_label": label,
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
        # The forecast array always starts at midnight of the oldest
        # requested day (Open-Meteo returns a full day from 00:00), so
        # forecast[0] is essentially always the flat overnight hour, not
        # "now" - expose the actual nearest-to-now hour explicitly so the
        # frontend has a real fallback if live buoy data is unavailable,
        # instead of defaulting to that always-flat first hour.
        result["current_hour"] = _current_hour_score(scored)
    except Exception as e:
        log.exception("forecast fetch failed for spot %s", spot_id)
        result["forecast_error"] = f"Could not load forecast right now: {e}"

    neah_bay_obs = None
    neah_bay_spec = None
    try:
        if spot.nearest_buoy_id:
            obs = await fetch_buoy_observation(spot.nearest_buoy_id)
        else:
            obs = None
        if not obs:
            _, obs = await find_working_nearest_buoy(spot.lat, spot.lon)
        if obs:
            result["buoy_observation"] = obs
            # For fetch_wind strait spots this reference buoy IS Neah Bay
            # (46087), the upwind leading-indicator buoy the strike-signal
            # formula is built on - keep the raw reading so we can reuse
            # it below without a second network call, and also pull its
            # SPECTRAL partition (swell vs local wind-chop, see
            # noaa_client.fetch_buoy_spectral) since correlating the two
            # buoys' true swell components is a cleaner signal than
            # correlating the blended wave height.
            if spot.nearest_buoy_id == "46087":
                neah_bay_obs = obs
                try:
                    neah_bay_spec = await fetch_buoy_spectral(spot.nearest_buoy_id)
                    if neah_bay_spec:
                        result["buoy_swell_partition"] = neah_bay_spec
                except Exception:
                    log.exception("Neah Bay spectral fetch failed for spot %s", spot_id)
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

    # Some spots (e.g. Elwha, Freshwater Bay) have a nearby NDBC buoy that
    # reports live wave height/period but has no wind sensor onboard, so
    # it can't serve as the wind+wave calibration reference (nearest_buoy_id)
    # but its wave reading is still the closest real observation available.
    local_wave_id = getattr(spot, "local_wave_buoy_id", None)
    local_wave_obs = None
    local_wave_spec = None
    if local_wave_id:
        try:
            local_wave_obs = await fetch_buoy_observation(local_wave_id)
            if local_wave_obs:
                result["local_wave_observation"] = local_wave_obs
                # This is real ground truth for "right now" - surface it as
                # its own scored field distinct from the modeled forecast,
                # so the detail view can show/prefer it as the current
                # condition rather than only the hourly model guess.
                live = score_live_wave_observation(spot, local_wave_obs)
                if live:
                    live["label"] = label_for_score(live["score"])
                    result["current_live_observation"] = live
            try:
                local_wave_spec = await fetch_buoy_spectral(local_wave_id)
                if local_wave_spec:
                    result["local_swell_partition"] = local_wave_spec
            except Exception:
                log.exception("local wave spectral fetch failed for spot %s", spot_id)
        except Exception:
            log.exception("local wave buoy fetch failed for spot %s", spot_id)

    # "Current Conditions": the validated strike-signal formula, computed
    # live from the upwind Neah Bay SWELL partition (leading indicator
    # down the strait axis), plus a direct swell-to-swell correlation
    # against the local Angeles Point swell partition to check whether
    # what's showing up locally is actually the same wave train (vs
    # independent local wind-chop) - this is the "what in the live
    # readings will actually make a good wave at Elwha right now" answer,
    # distinct from the hourly forecast model below.
    if neah_bay_obs or local_wave_obs:
        try:
            current_conditions = build_current_conditions(
                spot, neah_bay_obs, local_wave_obs, neah_bay_spec, local_wave_spec,
            )
            if current_conditions:
                result["current_conditions"] = current_conditions
        except Exception:
            log.exception("current conditions build failed for spot %s", spot_id)

    # For fetch_wind spots, surface a summary of the swell-transmission
    # model (see build_swell_transmission_model) so the detail view can
    # show the forecast is grounded in a real, validated upwind->local
    # relationship learned from years of paired buoy data, not a static
    # wind-speed curve.
    if getattr(spot, "scoring_model", "swell") == "fetch_wind":
        try:
            model = await _get_transmission_model(spot)
            if model:
                result["transmission_model_summary"] = {
                    "upwind_buoy_id": spot.nearest_buoy_id,
                    "local_buoy_id": getattr(spot, "local_wave_buoy_id", None),
                    "lag_hours": model.get("lag_hours"),
                    "paired_hours_used": model.get("n_pairs"),
                    "global_height_transmission_ratio": model.get("global_height_ratio"),
                    "height_ratio_buckets": len(model.get("height_ratio_lookup", {})),
                }
        except Exception:
            log.exception("transmission model summary failed for spot %s", spot_id)

    # Spot-specific explanation of what the 0-10 scale actually means here
    # (e.g. Elwha's fetch-limited wind-wave scale is NOT a groundswell
    # scale) - the UI shows this so the number isn't taken out of context.
    try:
        result["scale_description"] = scale_description_for_spot(spot)
    except Exception:
        log.exception("scale description failed for spot %s", spot_id)

    # Ranked explanation of which physical factors matter most for THIS
    # spot's model (e.g. at Elwha: swell size at the local buoy first,
    # then swell/wind direction via the bluff's shadowing effect, then
    # wind speed/fetch as a secondary modulator) - lets the UI show WHY a
    # score is what it is, not just the number.
    try:
        result["quality_factors"] = quality_factors_for_spot(spot)
    except Exception:
        log.exception("quality factors failed for spot %s", spot_id)

    # All-time benchmark from the LOCAL wave buoy's own history (distinct
    # from the upwind fetch-wind calibration above) - "how big has it
    # actually gotten here" and how many days crossed the "starting to
    # get good" swell-size threshold, since local swell size is the
    # single most informative real-world stat for this spot.
    try:
        local_benchmark = await _get_local_swell_benchmark(spot)
        if local_benchmark:
            result["local_swell_benchmark"] = local_benchmark
    except Exception:
        log.exception("local swell benchmark failed for spot %s", spot_id)

    return result


# Serve the frontend as static files from the same service.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR), name="assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str):
        # Force revalidation on every load instead of letting the browser
        # assume a cached copy of app.js/style.css/index.html is still
        # fresh - this app has shipped several fixes in quick succession
        # and a stale cached JS bundle silently serving old UI/logic
        # (with no visible error) is a much worse failure mode than the
        # extra conditional-GET round trip costs. ETag still makes this
        # a cheap 304 when nothing has actually changed.
        headers = {"Cache-Control": "no-cache"}
        candidate = os.path.join(_STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate, headers=headers)
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"), headers=headers)
