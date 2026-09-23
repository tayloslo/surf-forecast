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
