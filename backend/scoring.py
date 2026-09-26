"""
Surf quality scoring engine.

Turns a raw hourly forecast (swell height/period/direction + wind) into a
0-10 "how good will it be at THIS spot" score, using the spot's configured
preferences. This is a heuristic model (angle/period/height/wind fitness
multiplied together), not a physical wave-transformation model - it does
not simulate shoaling/refraction/bathymetry. That's a deliberate scope
choice for a general "any spot" tool; it's the same style of scoring used
by consumer surf-forecast sites for spots without a dedicated buoy.

All height/speed inputs and thresholds in this module are US units
(feet, mph), matching what noaa_client.py returns and what US surfers
actually think in.
"""
from database import Spot


# ---------------------------------------------------------------------------
# Fetch-limited wind wave scoring (for strait/inlet spots like Elwha, WA)
#
# Elwha sits deep inside the Strait of Juan de Fuca, ~50 miles from the open
# Pacific. Groundswell cannot survive that distance up a narrow strait -
# confirmed two ways: (1) the Open-Meteo wave model returns near-zero swell
# there year-round, and (2) real buoy history backs this up - NDBC 46087
# (Neah Bay, right at the strait's mouth) has logged dominant wave periods
# up to 19s over its rolling 45-day archive, but NDBC 46088 (New Dungeness,
# just ~30mi further into the strait) tops out at 11s and 1.5m over the same
# window, with a much lower average period (~4s vs ~9s at the mouth). Ocean
# groundswell measurably dies within the first stretch of the strait. What
# actually makes this spot work is a LOCAL, fetch-limited wind wave:
# sustained strong westerly wind blowing the length of the strait piles up
# short-period chop as it travels down-strait, and the wave grows with both
# wind speed and how long/far it has had to blow (fetch). This is a
# fundamentally different mechanism than swell hitting a reef, so it needs
# its own scoring path rather than tuned swell parameters.
# ---------------------------------------------------------------------------

def build_historical_fetch_profile(
    rows: list[dict], facing_direction: float, window_deg: float, min_wind_mph: float,
    bucket_size_mph: float = 5.0,
) -> dict:
    """Turn a buoy's raw historical wind+wave rows into an empirical
    lookup: for wind blowing from a usable (aligned) direction, what wave
    height has that speed actually produced at this buoy historically?
    Bucketed by wind speed (5 mph bins) so a forecast hour can be compared
    against real past analogs rather than a single hand-tuned curve.

    Returns {bucket_low_mph: {"avg_wave_height_ft": float, "n": int,
    "max_wave_height_ft": float}}, plus a "max_period_s" key with the
    single highest dominant period seen in the whole window (any
    direction) - useful context for how far real swell reaches in here.
    """
    buckets: dict[float, list[float]] = {}
    max_period = None
    for r in rows:
        dpd = r.get("dominant_period_s")
        if dpd is not None and (max_period is None or dpd > max_period):
            max_period = dpd

        wdir = r.get("wind_dir_deg")
        wspeed = r.get("wind_speed_mph")
        wave_ft = r.get("wave_height_ft")
        if wdir is None or wspeed is None or wave_ft is None:
            continue
        if _angle_diff(wdir, facing_direction) >= window_deg:
            continue
        if wspeed < min_wind_mph * 0.5:
            # Keep some below-threshold rows too so light-wind buckets
            # exist for comparison, but skip near-zero noise.
            continue
        bucket = (wspeed // bucket_size_mph) * bucket_size_mph
        buckets.setdefault(bucket, []).append(wave_ft)

    profile = {
        bucket: {
            "avg_wave_height_ft": round(sum(vals) / len(vals), 2),
            "max_wave_height_ft": round(max(vals), 2),
            "n": len(vals),
        }
        for bucket, vals in buckets.items()
    }
    return {"buckets": profile, "bucket_size_mph": bucket_size_mph, "max_period_s": max_period}


def _historical_lookup(profile: dict | None, wind_speed_mph: float) -> dict | None:
    """Nearest-bucket lookup into a historical fetch profile for a given
    forecast wind speed. Returns None if there's no profile or no analog
    bucket with enough history to be meaningful."""
    if not profile or not profile.get("buckets"):
        return None
    bucket_size = profile["bucket_size_mph"]
    bucket = (wind_speed_mph // bucket_size) * bucket_size
    entry = profile["buckets"].get(bucket)
    if entry and entry["n"] >= 2:
        return entry
    return None


def score_hour_fetch_wind(
    spot: Spot, hour: dict, upwind_hours: list[dict] | None = None,
    historical_profile: dict | None = None,
) -> dict:
    """Score one hourly forecast entry for a fetch-limited strait spot.

    `hour` is this spot's own forecast hour (local wind at Elwha).
    `upwind_hours` is the corresponding slice of forecast hours from the
    upwind reference point (Neah Bay, at the straits mouth) - sustained
    westerly wind there several hours earlier is a leading indicator that
    fetch is building and about to arrive, since wind-driven chop takes
    time to propagate down-strait.
    `historical_profile` (from build_historical_fetch_profile) is real
    wind/wave history from the upwind buoy: it tells us what a similar
    aligned wind speed has ACTUALLY produced there before, so a forecast
    hour is graded against real analog conditions, not just a hand-tuned
    curve.
    """
    wind_speed = hour["wind_speed_mph"] or 0.0
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
    # dedicated fetch_* thresholds (mph) rather than the swell height/
    # period fields, since those are a different unit/mechanism entirely.
    speed_fitness = _triangular_fitness(
        wind_speed, spot.fetch_min_wind_mph, spot.fetch_ideal_wind_mph, spot.fetch_max_wind_mph
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
            uspeed = uh.get("wind_speed_mph") or 0.0
            if ud is not None and _angle_diff(ud, spot.facing_direction) < spot.swell_window_deg and uspeed >= spot.fetch_min_wind_mph:
                aligned += 1
        fetch_bonus = 0.5 + 0.5 * min(1.0, aligned / max(len(upwind_hours), 1))

    # --- Historical analog check: does real buoy history back up that
    # this wind speed, in this direction, actually produces a wave? If
    # the empirical bucket for this speed shows a healthy historical wave
    # height, nudge the score up slightly (real precedent); if the bucket
    # shows historically flat/small waves even at this speed (e.g. too
    # short a duration historically, or a bucket dominated by cross-strait
    # gusts that never built real fetch), nudge down. This is a modest
    # +/-15% adjustment, not a replacement for the physical model, since a
    # 45-day window is real signal but not enough data to fully trust on
    # its own.
    historical_factor = 1.0
    hist_entry = _historical_lookup(historical_profile, wind_speed)
    hist_wave_height_ft = None
    hist_analog_count = None
    if hist_entry and dir_fitness > 0.3:
        hist_wave_height_ft = hist_entry["avg_wave_height_ft"]
        hist_analog_count = hist_entry["n"]
        # Compare against this spot's own ideal fetch wave size proxy:
        # scale ideal-speed wave expectation loosely off the ideal/max
        # wind ratio so a bigger historical wave at this speed reads as
        # confirmation, a smaller one as a discount.
        if hist_wave_height_ft >= 1.5:
            historical_factor = 1.15
        elif hist_wave_height_ft < 0.7:
            historical_factor = 0.85

    composite = (dir_fitness ** 1.2) * speed_fitness * fetch_bonus * historical_factor
    score_10 = round(max(0.0, min(1.0, composite)) * 10, 1)

    return {
        "time": hour["time"],
        "score": score_10,
        "wind_speed_mph": wind_speed,
        "wind_dir_deg": wind_dir,
        "components": {
            "direction_fitness": round(dir_fitness, 2),
            "speed_fitness": round(speed_fitness, 2),
            "fetch_bonus": round(fetch_bonus, 2),
            "historical_factor": round(historical_factor, 2),
        },
        "historical_wave_height_ft": hist_wave_height_ft,
        "historical_analog_count": hist_analog_count,
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
    total wave height also includes local windswell chop. Heights are in
    feet, wind speed in mph."""
    swell_height = hour["swell_height_ft"] or 0.0
    swell_period = hour["swell_period_s"] or 0.0
    swell_dir = hour["swell_dir_deg"]
    wind_speed = hour["wind_speed_mph"] or 0.0
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
        swell_height, spot.min_good_height_ft, spot.ideal_height_ft, spot.max_good_height_ft
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
        speed_penalty = min(1.0, wind_speed / max(spot.max_good_wind_mph, 1.0))
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
        "swell_height_ft": swell_height,
        "swell_period_s": swell_period,
        "swell_dir_deg": swell_dir,
        "wind_speed_mph": wind_speed,
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


def score_live_wave_observation(spot: Spot, obs: dict) -> dict | None:
    """Score "right now" directly from a live nearby wave buoy reading,
    when one exists (local_wave_buoy_id), rather than only from the
    forecast model. This matters because the forecast-model score for
    "now" is picked by nearest-timestamp match against an hourly model
    forecast - useful for future hours, but for the CURRENT hour a real
    buoy 2-10km away reporting an actual wave height/period is strictly
    better ground truth than a modeled wind guess, and should be trusted
    over it when both exist. Reuses the same period/height triangular
    fitness curves as the open-coast swell model, since a real wave
    height/period reading is graded the same way regardless of what
    mechanism produced it."""
    wave_ft = obs.get("wave_height_ft")
    if wave_ft is None:
        return None
    period_s = obs.get("dominant_period_s")
    wave_dir = obs.get("wave_dir_deg")

    height_fitness = _triangular_fitness(
        wave_ft, spot.min_good_height_ft, spot.ideal_height_ft, spot.max_good_height_ft
    )
    if period_s is not None:
        period_fitness = _triangular_fitness(
            period_s, spot.min_good_period_s, spot.ideal_period_s, spot.ideal_period_s + 8
        )
    else:
        period_fitness = 0.7  # unknown, don't penalize/reward

    if wave_dir is not None:
        off_angle = _angle_diff(wave_dir, spot.facing_direction)
        dir_fitness = max(0.0, 1.0 - (off_angle / spot.swell_window_deg))
    else:
        dir_fitness = 0.7

    composite = (0.5 + 0.5 * dir_fitness) * (0.4 + 0.6 * period_fitness) * (0.3 + 0.7 * height_fitness)
    score_10 = round(max(0.0, min(1.0, composite)) * 10, 1)

    return {
        "score": score_10,
        "wave_height_ft": wave_ft,
        "dominant_period_s": period_s,
        "wave_dir_deg": wave_dir,
        "observed_at": obs.get("observed_at"),
        "components": {
            "height_fitness": round(height_fitness, 2),
            "period_fitness": round(period_fitness, 2),
            "direction_fitness": round(dir_fitness, 2),
        },
        "source": "live_buoy",
    }
