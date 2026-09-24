"""
Surf quality scoring engine.

Turns a raw hourly forecast (swell height/period/direction + wind) into a
0-10 "how good will it be at THIS spot" score, using the spot's configured
preferences. This is a heuristic model (angle/period/height/wind fitness
multiplied together), not a physical wave-transformation model - it does
not simulate shoaling/refraction/bathymetry. That's a deliberate scope
choice for a general "any spot" tool; it's the same style of scoring used
by consumer surf-forecast sites for spots without a dedicated buoy.
"""
from database import Spot


# ---------------------------------------------------------------------------
# Fetch-limited wind wave scoring (for strait/inlet spots like Elwha, WA)
#
# Elwha sits deep inside the Strait of Juan de Fuca, ~50 miles from the open
# Pacific. Groundswell cannot survive that distance up a narrow strait -
# Open-Meteo wave model confirms this, returning near-zero swell energy
# there year-round. What actually makes this spot work is a LOCAL,
# fetch-limited wind wave: sustained strong westerly wind blowing the length
# of the strait piles up short-period chop as it travels down-strait, and
# the wave grows with both wind speed and how long/far it has had to blow
# (fetch). This is a fundamentally different mechanism than swell hitting a
# reef, so it needs its own scoring path rather than tuned swell parameters.
# ---------------------------------------------------------------------------

def score_hour_fetch_wind(spot: Spot, hour: dict, upwind_hours: list[dict] | None = None) -> dict:
    """Score one hourly forecast entry for a fetch-limited strait spot.

    `hour` is this spots own forecast hour (local wind at Elwha).
    `upwind_hours` is the corresponding slice of forecast hours from the
    upwind reference point (Neah Bay, at the straits mouth) - sustained
    westerly wind there several hours earlier is a leading indicator that
    fetch is building and about to arrive, since wind-driven chop takes
    time to propagate down-strait.
    """
    wind_speed = hour["wind_speed_kmh"] or 0.0
    wind_dir = hour["wind_dir_deg"]

    # --- Direction fitness: only wind blowing roughly DOWN the strait
    # (from the west, i.e. from Neah Bay toward Elwha) builds a usable
    # wave here. Wind from the east (down-strait, blowing the "wrong way")
    # or from land kills it, regardless of speed.
    if wind_dir is None:
        dir_fitness = 0.5
    else:
        off_angle = _angle_diff(wind_dir, spot.facing_direction)
        dir_fitness = max(0.0, 1.0 - (off_angle / spot.swell_window_deg))

    # --- Speed/fetch fitness: wave size scales with wind speed once it
    # has been blowing long enough to build fetch. Use a triangular
    # fitness the same way we would use swell height elsewhere - too light
    # and there is no wave, too strong and it is a blown-out mess. Uses
    # dedicated fetch_* thresholds (km/h) rather than the swell height/
    # period fields, since those are a different unit/mechanism entirely.
    speed_fitness = _triangular_fitness(
        wind_speed, spot.fetch_min_wind_kmh, spot.fetch_ideal_wind_kmh, spot.fetch_max_wind_kmh
    )

    # --- Sustained-fetch bonus: check whether wind at the upwind reference
    # (Neah Bay) has ALSO been blowing from a usable direction for the
    # preceding several hours. A gust that just started has not built fetch
    # yet; sustained wind over time has. This is the piece a simple
    # single-point wind score would miss entirely for this kind of spot.
    fetch_bonus = 1.0
    if upwind_hours:
        aligned = 0
        for uh in upwind_hours:
            ud = uh.get("wind_dir_deg")
            uspeed = uh.get("wind_speed_kmh") or 0.0
            if ud is not None and _angle_diff(ud, spot.facing_direction) < spot.swell_window_deg and uspeed >= spot.fetch_min_wind_kmh:
                aligned += 1
        fetch_bonus = 0.5 + 0.5 * min(1.0, aligned / max(len(upwind_hours), 1))

    composite = (dir_fitness ** 1.2) * speed_fitness * fetch_bonus
    score_10 = round(max(0.0, min(1.0, composite)) * 10, 1)

    return {
        "time": hour["time"],
        "score": score_10,
        "wind_speed_kmh": wind_speed,
        "wind_dir_deg": wind_dir,
        "components": {
            "direction_fitness": round(dir_fitness, 2),
            "speed_fitness": round(speed_fitness, 2),
            "fetch_bonus": round(fetch_bonus, 2),
        },
        "model": "fetch_wind",
    }


def _angle_diff(a: float, b: float) -> float:
    """Smallest difference between two compass bearings, 0-180."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _triangular_fitness(value: float, low: float, ideal: float, high: float) -> float:
    """1.0 at `ideal`, tapering linearly to 0.0 at/beyond `low`/`high`.
    Used for period and height fitness."""
    if value <= low or value >= high:
        return 0.0
    if value <= ideal:
        return (value - low) / (ideal - low) if ideal > low else 1.0
    return (high - value) / (high - ideal) if high > ideal else 1.0


def score_hour(spot: Spot, hour: dict) -> dict:
    """Score one hourly forecast entry against one spot's preferences.
    Uses swell (not total wave) height/period/direction, since swell is
    the organized groundswell energy that actually makes surf, whereas
    total wave height also includes local windswell chop."""
    swell_height = hour["swell_height_m"] or 0.0
    swell_period = hour["swell_period_s"] or 0.0
    swell_dir = hour["swell_dir_deg"]
    wind_speed = hour["wind_speed_kmh"] or 0.0
    wind_dir = hour["wind_dir_deg"]

    # --- Direction fitness: how close is the swell to hitting this spot's
    # preferred window, relative to the beach's facing direction?
    if swell_dir is None:
        dir_fitness = 0.5  # unknown, don't penalize/reward
    else:
        off_angle = _angle_diff(swell_dir, spot.facing_direction)
        dir_fitness = max(0.0, 1.0 - (off_angle / spot.swell_window_deg))

    # --- Period fitness: longer period groundswell is more powerful and
    # organized; short period windswell is mushy. Triangular around ideal.
    period_fitness = _triangular_fitness(
        swell_period, spot.min_good_period_s, spot.ideal_period_s, spot.ideal_period_s + 8
    )

    # --- Height fitness: too small = no surf, too big = blown out/unsafe
    # for the "ideal" configured size at this spot.
    height_fitness = _triangular_fitness(
        swell_height, spot.min_good_height_m, spot.ideal_height_m, spot.max_good_height_m
    )

    # --- Wind fitness: penalize onshore wind (blowing toward the beach,
    # i.e. from roughly the opposite of facing_direction) scaled by speed;
    # offshore/cross wind is largely ignored by this simple model.
    if wind_dir is None:
        wind_fitness = 0.7
    else:
        # Wind direction convention (from Open-Meteo) is "blowing FROM",
        # same as swell direction. Onshore wind (bad - blows straight up
        # the wave face, blowing it out) comes FROM the same general
        # direction as the swell/facing_direction. Offshore wind (good -
        # holds the wave face up, grooms it) comes from the opposite side.
        onshore_closeness = max(0.0, 1.0 - (_angle_diff(wind_dir, spot.facing_direction) / spot.onshore_window_deg))
        speed_penalty = min(1.0, wind_speed / max(spot.max_good_wind_kmh, 1.0))
        wind_fitness = 1.0 - (onshore_closeness * speed_penalty)
        wind_fitness = max(0.0, wind_fitness)

    # Direction and period matter most (no swell energy / wrong angle =
    # no surf regardless of size), height and wind modulate quality.
    composite = (dir_fitness ** 1.2) * (period_fitness ** 1.0) * \
                (0.4 + 0.6 * height_fitness) * (0.3 + 0.7 * wind_fitness)
    score_10 = round(max(0.0, min(1.0, composite)) * 10, 1)

    return {
        "time": hour["time"],
        "score": score_10,
        "swell_height_m": swell_height,
        "swell_period_s": swell_period,
        "swell_dir_deg": swell_dir,
        "wind_speed_kmh": wind_speed,
        "wind_dir_deg": wind_dir,
        "components": {
            "direction_fitness": round(dir_fitness, 2),
            "period_fitness": round(period_fitness, 2),
            "height_fitness": round(height_fitness, 2),
            "wind_fitness": round(wind_fitness, 2),
        },
    }


def label_for_score(score: float) -> str:
    if score >= 8:
        return "Epic"
    if score >= 6.5:
        return "Good"
    if score >= 4.5:
        return "Fair"
    if score >= 2:
        return "Poor"
    return "Flat"
