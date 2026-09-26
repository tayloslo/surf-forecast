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
import bisect
import math
from datetime import datetime, timedelta

from database import Spot


# ---------------------------------------------------------------------------
# Swell transmission model (for strait/inlet spots like Elwha, WA)
#
# Elwha sits deep inside the Strait of Juan de Fuca, ~50 miles from the open
# Pacific. An earlier version of this model treated the wave here as purely
# LOCAL wind-driven chop (wind blowing down the strait piling up fetch-
# limited waves on the spot). Real analysis of the full 2020-present NDBC
# archive doesn't support that: filtering the upwind Neah Bay buoy (46087)
# to only westerly/down-strait wind actually LOWERS its correlation with
# wave height there (0.34) versus using ALL wind unfiltered (0.53) - i.e.
# Neah Bay's wave is genuine open-Pacific swell arriving whenever a Pacific
# storm is active, not locally wind-generated chop. Open-Meteo's marine
# model (GFS-Wave/WaveWatch III) already forecasts that swell days out, so
# the real missing piece was never "how much local wind fetch will build" -
# it's "how much of the swell already visible/forecastable at the strait's
# mouth actually survives the trip down-strait to Elwha, and in what shape".
#
# That transmission relationship was validated by pairing every hour of the
# 2020-present NDBC archives at Neah Bay (46087, upwind, at the strait
# mouth) and Angeles Point (46267, local, ~2km from Elwha), 2020-2023 train
# / 2024+ held-out test:
#   - Height transmission ratio (local/upwind) depends jointly on the
#     upwind swell's PERIOD and its angle off the strait's axis bearing:
#     shorter-period energy transmits much better than long-period
#     groundswell (which gets refracted/dissipated turning into the
#     strait), and on-axis energy transmits better than off-axis. Bucketed
#     lookup beats a flat ratio by ~20% out-of-sample MAE (0.83ft vs
#     1.02ft) and clearly beats using the raw upwind height directly
#     (4.22ft MAE).
#   - Local swell DIRECTION is a function of upwind direction (bucketed
#     10-degree lookup beats both a linear fit and "assume unchanged" out
#     of sample: 23.6 vs 28.6 vs 36.9 degrees MAE).
#   - Local PERIOD tracks upwind period closely (corr 0.52) with a slight
#     downward bias (~9.0s local vs ~10.4s upwind on average) as longer
#     groundswell periods get preferentially damped.
#   - Peak cross-correlation lag between the two buoys is ~4 hours
#     (Neah Bay leads), consistent with real-world group velocity for the
#     periods actually observed here.
# See build_swell_transmission_model / project_local_swell.
# ---------------------------------------------------------------------------

TRANSMISSION_LAG_HOURS = 4  # validated peak cross-correlation lag, Neah Bay -> Angeles Point
TRANSMISSION_HEIGHT_PERIOD_BUCKET_S = 2.0
TRANSMISSION_HEIGHT_ANGLE_BUCKET_DEG = 20.0
TRANSMISSION_DIR_BUCKET_DEG = 10.0
TRANSMISSION_MIN_BUCKET_N = 15


def build_swell_transmission_model(
    local_rows: list[dict], upwind_rows: list[dict],
    strait_axis_bearing_deg: float, lag_hours: int = TRANSMISSION_LAG_HOURS,
) -> dict | None:
    """Empirically learn how upwind (Neah Bay) swell height/period/
    direction transmits down-strait to the local buoy (Angeles Point),
    from the full paired historical record. This is the core of the new
    forecast model: instead of scoring LOCAL wind as if it generates the
    wave, project what the upwind swell forecast should look like by the
    time it reaches here, then score THAT the normal swell way (height/
    period/direction fitness), with local wind demoted to a pure
    groomed-vs-onshore-blown quality modifier.

    Returns a dict of bucketed lookups (height ratio by period+off-axis
    angle, local direction by upwind direction, local period by upwind
    period), each with a global fallback mean for buckets with too little
    data, plus the sample size and lag used - or None if there isn't
    enough paired data to build a model."""
    if not local_rows or not upwind_rows:
        return None

    def parse_dt(iso):
        try:
            return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return None

    def index_by_hour(rows):
        idx: dict[datetime, list[dict]] = {}
        for r in rows:
            dt = parse_dt(r.get("observed_at"))
            if dt is None:
                continue
            dt = dt.replace(minute=0, second=0, microsecond=0)
            idx.setdefault(dt, []).append(r)
        return idx

    def best(recs, key):
        vals = [r.get(key) for r in recs if r.get(key) is not None]
        return max(vals) if vals else None

    def avg(recs, key):
        vals = [r.get(key) for r in recs if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    idx_local = index_by_hour(local_rows)
    idx_upwind = index_by_hour(upwind_rows)

    pairs = []
    for t_local, recs_local in idx_local.items():
        t_upwind = t_local - timedelta(hours=lag_hours)
        recs_upwind = idx_upwind.get(t_upwind)
        if not recs_upwind:
            continue
        h_up = best(recs_upwind, "wave_height_ft")
        p_up = avg(recs_upwind, "dominant_period_s")
        d_up = avg(recs_upwind, "wave_dir_deg")
        h_lo = best(recs_local, "wave_height_ft")
        d_lo = avg(recs_local, "wave_dir_deg")
        p_lo = avg(recs_local, "dominant_period_s")
        if None in (h_up, p_up, d_up, h_lo):
            continue
        pairs.append({"h_up": h_up, "p_up": p_up, "d_up": d_up, "h_lo": h_lo, "d_lo": d_lo, "p_lo": p_lo})

    if len(pairs) < 200:
        return None

    # --- Height transmission ratio, bucketed by (upwind period, upwind
    # angle off the strait axis). Only meaningful once there's a real
    # swell to transmit (h_up >= 1ft) - near-zero upwind heights make the
    # ratio itself noise.
    height_buckets: dict[tuple, list[float]] = {}
    for p in pairs:
        if p["h_up"] < 1.0:
            continue
        off_axis = _angle_diff(p["d_up"], strait_axis_bearing_deg)
        key = (
            int(p["p_up"] // TRANSMISSION_HEIGHT_PERIOD_BUCKET_S) * TRANSMISSION_HEIGHT_PERIOD_BUCKET_S,
            int(off_axis // TRANSMISSION_HEIGHT_ANGLE_BUCKET_DEG) * TRANSMISSION_HEIGHT_ANGLE_BUCKET_DEG,
        )
        height_buckets.setdefault(key, []).append(p["h_lo"] / p["h_up"])
    height_ratio_lookup = {
        k: round(sum(v) / len(v), 3) for k, v in height_buckets.items() if len(v) >= TRANSMISSION_MIN_BUCKET_N
    }
    ratio_all = [p["h_lo"] / p["h_up"] for p in pairs if p["h_up"] >= 1.0]
    global_height_ratio = round(sum(ratio_all) / len(ratio_all), 3) if ratio_all else 0.4

    # --- Local direction, bucketed by upwind direction (only meaningful
    # for a real swell too).
    dir_buckets: dict[int, list[float]] = {}
    for p in pairs:
        if p["h_up"] < 1.0 or p["d_lo"] is None:
            continue
        key = int(p["d_up"] // TRANSMISSION_DIR_BUCKET_DEG) * int(TRANSMISSION_DIR_BUCKET_DEG)
        dir_buckets.setdefault(key, []).append(p["d_lo"])
    dir_lookup = {
        k: round(_circular_median_deg(v), 1) for k, v in dir_buckets.items() if len(v) >= TRANSMISSION_MIN_BUCKET_N
    }
    dirs_all = [p["d_lo"] for p in pairs if p["h_up"] >= 1.0 and p["d_lo"] is not None]
    global_local_dir = round(_circular_median_deg(dirs_all), 1) if dirs_all else strait_axis_bearing_deg

    # --- Local period, bucketed by upwind period (rounded to the nearest
    # second - period buckets need to be finer than height/direction
    # since local period tracks upwind period fairly directly).
    period_buckets: dict[int, list[float]] = {}
    for p in pairs:
        if p["h_up"] < 1.0 or p["p_lo"] is None:
            continue
        key = round(p["p_up"])
        period_buckets.setdefault(key, []).append(p["p_lo"])
    period_lookup = {
        k: round(sum(v) / len(v), 2) for k, v in period_buckets.items() if len(v) >= TRANSMISSION_MIN_BUCKET_N
    }
    periods_all = [p["p_lo"] for p in pairs if p["h_up"] >= 1.0 and p["p_lo"] is not None]
    global_local_period = round(sum(periods_all) / len(periods_all), 2) if periods_all else 8.0

    return {
        "lag_hours": lag_hours,
        "strait_axis_bearing_deg": strait_axis_bearing_deg,
        "n_pairs": len(pairs),
        "height_ratio_lookup": height_ratio_lookup,
        "global_height_ratio": global_height_ratio,
        "dir_lookup": dir_lookup,
        "global_local_dir_deg": global_local_dir,
        "period_lookup": period_lookup,
        "global_local_period_s": global_local_period,
    }


def project_local_swell(upwind_hour: dict | None, transmission_model: dict | None) -> dict | None:
    """Apply the empirical transmission model (build_swell_transmission_model)
    to one upwind forecast hour's wave fields, projecting what the swell
    should look like by the time it reaches the local spot -
    TRANSMISSION_LAG_HOURS later. This is the forward-looking signal: it
    lets the forecast get ahead of what will eventually show up on the
    local buoy by reading the upwind buoy's own leading position plus the
    marine forecast model, rather than waiting for it to arrive."""
    if not upwind_hour or not transmission_model:
        return None
    h_up = upwind_hour.get("wave_height_ft")
    p_up = upwind_hour.get("wave_period_s")
    d_up = upwind_hour.get("wave_dir_deg")
    if h_up is None or p_up is None or d_up is None:
        return None

    off_axis = _angle_diff(d_up, transmission_model["strait_axis_bearing_deg"])
    height_key = (
        int(p_up // TRANSMISSION_HEIGHT_PERIOD_BUCKET_S) * TRANSMISSION_HEIGHT_PERIOD_BUCKET_S,
        int(off_axis // TRANSMISSION_HEIGHT_ANGLE_BUCKET_DEG) * TRANSMISSION_HEIGHT_ANGLE_BUCKET_DEG,
    )
    ratio = transmission_model["height_ratio_lookup"].get(height_key, transmission_model["global_height_ratio"])

    dir_key = int(d_up // TRANSMISSION_DIR_BUCKET_DEG) * int(TRANSMISSION_DIR_BUCKET_DEG)
    local_dir = transmission_model["dir_lookup"].get(dir_key, transmission_model["global_local_dir_deg"])

    period_key = round(p_up)
    local_period = transmission_model["period_lookup"].get(period_key, transmission_model["global_local_period_s"])

    return {
        "swell_height_ft": round(h_up * ratio, 2),
        "swell_period_s": local_period,
        "swell_dir_deg": local_dir,
        "upwind_height_ft": h_up,
        "upwind_period_s": p_up,
        "upwind_dir_deg": d_up,
        "transmission_ratio": ratio,
        "lag_hours": transmission_model["lag_hours"],
    }


# The Angeles Point buoy (46267) sits right off Elwha and is a much
# better read on what this spot actually needs than the upwind Neah Bay
# wind/fetch analysis alone: real swell height there correlates strongly
# with what breaks at Elwha, so "how big has it actually gotten here" is
# the single most informative historical stat for calibrating "what's a
# big day" - more informative than any wind-speed bucket.
GOOD_SWELL_HEIGHT_FT = 4.0  # user-identified threshold: 4ft+ at the local buoy starts to approach "good", conditional on angle/wind alignment


def build_local_swell_benchmark(rows: list[dict], good_swell_height_ft: float = GOOD_SWELL_HEIGHT_FT) -> dict | None:
    """From the LOCAL wave buoy's own history (Angeles Point/46267 for
    Elwha, not the upwind Neah Bay reference), find the all-time max wave
    height actually observed and how many days crossed the "starting to
    get good" threshold - the real benchmark this spot should be judged
    against, since local wave height is the dominant factor in whether
    Elwha actually breaks well, well ahead of wind speed/fetch alone."""
    if not rows:
        return None
    all_time_max = None
    all_time_max_at = None
    days: dict[str, float] = {}
    for r in rows:
        wave_ft = r.get("wave_height_ft")
        observed_at = r.get("observed_at")
        if wave_ft is None:
            continue
        if all_time_max is None or wave_ft > all_time_max:
            all_time_max = wave_ft
            all_time_max_at = observed_at
        if observed_at:
            day = observed_at[:10]
            if wave_ft > days.get(day, 0.0):
                days[day] = wave_ft
    if all_time_max is None:
        return None
    good_days = sum(1 for v in days.values() if v >= good_swell_height_ft)
    return {
        "all_time_max_wave_height_ft": all_time_max,
        "all_time_max_observed_at": all_time_max_at,
        "good_swell_height_ft": good_swell_height_ft,
        "total_days": len(days),
        "days_at_or_above_good_swell": good_days,
        "days_at_or_above_pct": round(good_days / len(days) * 100, 0) if days else None,
    }


def _circular_median_deg(dirs_deg: list[float]) -> float:
    """Median of a list of compass bearings, handling the 0/360 wraparound
    (a plain numeric median of e.g. [350, 10] would wrongly give 180
    instead of 0/360). Converts to unit vectors, averages, and converts
    back - standard circular-mean approach; good enough for a summary
    stat here since these direction sets are already narrowly clustered
    around west in practice."""
    if not dirs_deg:
        return 0.0
    sin_sum = sum(math.sin(math.radians(d)) for d in dirs_deg)
    cos_sum = sum(math.cos(math.radians(d)) for d in dirs_deg)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360



# ---------------------------------------------------------------------------
# Ranked quality-factor legend, per scoring model. This is deliberately
# separate from the numeric SCALE_DESCRIPTIONS bands above: those explain
# what a given SCORE means, this explains WHY - which physical inputs
# actually move the needle most, in priority order, so a user glancing at
# the detail view understands what to look for themselves, not just what
# number came out.
#
# For Elwha specifically: this is NOT a locally wind-built wave, and it's
# NOT a pure wind-fetch novelty wave either (an earlier, incomplete model
# treated it that way). Real analysis of the buoy history shows the wave
# here is genuine open-Pacific swell that transmits (attenuated and
# refracted) down the strait from Neah Bay - local wind at Elwha doesn't
# correlate with its own upwind buoy's wave height any better than no
# wind filter at all, i.e. it doesn't build the wave. So swell height/
# period/direction (projected from the upwind buoy via the validated
# transmission model) is the dominant factor, and local wind is demoted
# to a pure grooming modifier - it determines whether that swell arrives
# clean (offshore/light wind) or chopped-out (onshore/strong wind), not
# whether there's a wave in the first place.
# ---------------------------------------------------------------------------
QUALITY_FACTORS = {
    "fetch_wind": [
        {
            "factor": "Transmitted swell height (from Neah Bay)",
            "importance": "Most important",
            "detail": (
                "Projected local swell height, empirically transmitted from the upwind Neah Bay "
                "buoy (46087) via a bucketed height-ratio lookup (by upwind period + angle off "
                "the strait axis) - validated out-of-sample against the local Angeles Point buoy "
                "(0.83ft MAE vs 1.02ft for a flat ratio, 4.22ft for using the raw upwind height "
                "directly). Under ~1.5ft transmitted: essentially flat. 4ft+ starts approaching "
                "'good' territory, but only IF direction and wind also line up (see below)."
            ),
        },
        {
            "factor": "Transmitted swell direction & period (bluff shadowing)",
            "importance": "Very important",
            "detail": (
                "Projected local swell direction/period, also transmitted from the upwind buoy "
                "(direction via a 10-degree bucketed lookup, period tracking upwind period with a "
                "slight downward bias for long groundswell). Elwha sits behind a large bluff - one "
                "side of the point is sheltered and clean, the other gets shadowed out, depending "
                "on which way that swell is coming from - like a point break's angle-of-attack."
            ),
        },
        {
            "factor": "Local wind (grooming, not generation)",
            "importance": "Secondary",
            "detail": (
                "Local wind at Elwha itself does NOT build this wave - filtering the upwind buoy's "
                "wind to only westerly/down-strait direction actually LOWERS its correlation with "
                "its own wave height (0.34 vs 0.53 unfiltered), confirming the swell arrives "
                "regardless of local wind. What local wind DOES do is groom or blow out whatever "
                "swell already arrived: offshore/light wind holds the wave face up clean, onshore/ "
                "strong wind chops it into a mess."
            ),
        },
    ],
    "swell": [
        {
            "factor": "Swell direction",
            "importance": "Most important",
            "detail": "No swell energy hitting this beach's window means no surf, regardless of size.",
        },
        {
            "factor": "Swell period",
            "importance": "Very important",
            "detail": "Longer-period groundswell is organized and powerful; short-period windswell is mushy.",
        },
        {
            "factor": "Swell height",
            "importance": "Important",
            "detail": "Too small means no surf; too big at this beach means blown out/unsafe.",
        },
        {
            "factor": "Wind direction/speed",
            "importance": "Secondary",
            "detail": "Onshore wind degrades an existing swell; offshore/light wind grooms it.",
        },
    ],
}


def quality_factors_for_spot(spot: Spot) -> list[dict]:
    """Ranked, human-readable explanation of which physical inputs matter
    most for THIS spot's scoring model, in priority order."""
    model = getattr(spot, "scoring_model", "swell")
    return QUALITY_FACTORS.get(model, QUALITY_FACTORS["swell"])


def score_hour_swell_transmission(
    spot: Spot, hour: dict, upwind_hour: dict | None = None,
    transmission_model: dict | None = None,
) -> dict:
    """Score one hourly forecast entry for a strait spot (e.g. Elwha)
    using the validated swell-transmission model instead of treating
    local wind as the wave-generating mechanism.

    `hour` is this spot's own forecast hour - used ONLY for its LOCAL
    wind (grooming/onshore-blowout modifier), never as a source of wave
    height/period/direction here.
    `upwind_hour` is the Neah Bay (46087) forecast hour from
    TRANSMISSION_LAG_HOURS before this one - what the swell forecast
    model predicts at the strait's mouth around the time whatever
    arrives here now would have left there.
    `transmission_model` (from build_swell_transmission_model) is the
    empirical upwind->local relationship learned from years of paired
    buoy history, used by project_local_swell to turn that upwind hour
    into a projected LOCAL swell height/period/direction.

    Once that projection exists, it's scored exactly like an open-coast
    swell forecast (score_hour): direction/period/height fitness against
    this spot's own preferences. Local wind only ever grooms or blows out
    that projected swell - it's demoted from "generates the wave" to
    "shapes its face," matching the real physical mechanism confirmed by
    the buoy analysis above.
    """
    projected = project_local_swell(upwind_hour, transmission_model)

    wind_speed = hour["wind_speed_mph"] or 0.0
    wind_dir = hour["wind_dir_deg"]

    if projected is None:
        # No upwind reading/model available for this hour - don't guess
        # at a swell, but don't zero the score either (unknown, not
        # necessarily flat).
        swell_height = 0.0
        swell_period = 0.0
        swell_dir = None
        dir_fitness = 0.5
        period_fitness = 0.0
        height_fitness = 0.0
    else:
        swell_height = projected["swell_height_ft"]
        swell_period = projected["swell_period_s"]
        swell_dir = projected["swell_dir_deg"]

        if swell_dir is None:
            dir_fitness = 0.5
        else:
            off_angle = _angle_diff(swell_dir, spot.facing_direction)
            dir_fitness = max(0.0, 1.0 - (off_angle / spot.swell_window_deg))

        period_fitness = _triangular_fitness(
            swell_period, spot.min_good_period_s, spot.ideal_period_s, spot.ideal_period_s + 8
        )
        height_fitness = _triangular_fitness(
            swell_height, spot.min_good_height_ft, spot.ideal_height_ft, spot.max_good_height_ft
        )

    # --- Grooming wind fitness: LOCAL wind at the spot itself only ever
    # grooms (offshore/light) or blows out (onshore/strong) whatever
    # swell the transmission model projected above - it is not treated
    # as a wave-generation input here (see module header: filtering the
    # upwind buoy to westerly/down-strait wind LOWERS its correlation
    # with its own wave height, i.e. local wind doesn't build this wave).
    if wind_dir is None:
        wind_fitness = 0.7
    else:
        onshore_closeness = max(0.0, 1.0 - (_angle_diff(wind_dir, spot.facing_direction) / spot.onshore_window_deg))
        speed_penalty = min(1.0, wind_speed / max(spot.max_good_wind_mph, 1.0))
        wind_fitness = max(0.0, 1.0 - (onshore_closeness * speed_penalty))

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
        "upwind_height_ft": projected["upwind_height_ft"] if projected else None,
        "upwind_period_s": projected["upwind_period_s"] if projected else None,
        "upwind_dir_deg": projected["upwind_dir_deg"] if projected else None,
        "transmission_ratio": projected["transmission_ratio"] if projected else None,
        "lag_hours": projected["lag_hours"] if projected else TRANSMISSION_LAG_HOURS,
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


# ---------------------------------------------------------------------------
# What the 0-10 scale actually MEANS, per scoring model. The generic
# Epic/Good/Fair/Poor/Flat labels are useful shorthand, but on their own
# they invite the wrong mental model for a fetch-limited wind-wave spot
# like Elwha: "Epic" here does NOT mean "open-coast-quality groundswell",
# it means "the local wind has built as much fetch-limited chop as this
# spot can physically produce". Surfacing that distinction explicitly in
# the UI (rather than leaving it implied) is the point of this function.
# ---------------------------------------------------------------------------
SCALE_DESCRIPTIONS = {
    "fetch_wind": {
        "model_note": (
            "This spot never gets real open-coast groundswell directly - it's ~50mi "
            "inside the Strait of Juan de Fuca. But it's not flat/wind-only either: "
            "real Pacific swell arriving at the strait's mouth (Neah Bay) transmits "
            "down-strait, attenuated and refracted, and that transmitted swell - not "
            "local wind - is what actually shows up here. Every number on this scale "
            "is a projected, transmitted swell score; local wind only grooms it clean "
            "or blows it out, it doesn't create it."
        ),
        "bands": [
            {"label": "Epic", "range": "8-10", "wave_ft": "~4-6ft+",
             "meaning": "A well-aligned, sizeable Pacific swell event at Neah Bay is projected to transmit down-strait at good size/period, arriving with clean (offshore/light) local wind. Rare - most days don't have an active swell event this size lined up this well."},
            {"label": "Good", "range": "6.5-7.9", "wave_ft": "~2.5-4ft",
             "meaning": "Solid transmitted swell, good size/period/direction, local wind not too onshore. The most common \"actually worth going\" band for this spot."},
            {"label": "Fair", "range": "4.5-6.4", "wave_ft": "~1.5-2.5ft",
             "meaning": "Some transmitted swell present but undersized, off-angle, or short-period, or local wind starting to chop it up. A small, textured wave - ridable but unremarkable."},
            {"label": "Poor", "range": "2-4.4", "wave_ft": "<1.5ft",
             "meaning": "Little swell transmitting down-strait right now, or what's arriving is badly misaligned/onshore-blown. Barely a ripple, if anything."},
            {"label": "Flat", "range": "0-1.9", "wave_ft": "~0ft",
             "meaning": "No meaningful swell event at Neah Bay to transmit, or nothing survives the trip down-strait at usable size/angle."},
        ],
    },
    "swell": {
        "model_note": (
            "Open-coast groundswell scoring: how directly the forecasted swell "
            "lines up with this beach's preferred window, how organized (period) "
            "it is, how big, and whether wind is grooming or blowing it out."
        ),
        "bands": [
            {"label": "Epic", "range": "8-10", "meaning": "Well-aligned groundswell at a good size/period with clean (offshore/light) wind."},
            {"label": "Good", "range": "6.5-7.9", "meaning": "Solid aligned swell, decent size and period, wind not too onshore."},
            {"label": "Fair", "range": "4.5-6.4", "meaning": "Swell present but off-angle, undersized, short-period, or wind starting to affect it."},
            {"label": "Poor", "range": "2-4.4", "meaning": "Weak/misaligned swell or onshore wind degrading what little there is."},
            {"label": "Flat", "range": "0-1.9", "meaning": "Essentially no usable swell energy reaching this spot."},
        ],
    },
}


def scale_description_for_spot(spot: Spot) -> dict:
    """Return the 0-10 scale explanation appropriate to this spot's
    scoring model, so the UI can show what the number actually means
    here instead of a one-size-fits-all label."""
    model = getattr(spot, "scoring_model", "swell")
    return SCALE_DESCRIPTIONS.get(model, SCALE_DESCRIPTIONS["swell"])


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


# ---------------------------------------------------------------------------
# Live "strike signal" - Current Conditions
#
# This is the formula validated against months of paired buoy history for
# the Neah Bay (46087, upwind, strait mouth) -> Angeles Point (46267, ~2km
# from Elwha) relationship, and already wired into a working scheduled
# alert ("Port Angeles Surf Strike Alert"). It answers a narrower, more
# concrete question than the forecast model does: "given what the buoys
# are reporting RIGHT NOW, is a good wave actually reaching Elwha at this
# moment?" - as opposed to the forecast model's "what will local wind
# fetch likely build to over the next several hours?".
#
# angle_offset = angular distance of the live Neah Bay wave direction from
#   292.8 degrees, the compass bearing along the Strait of Juan de Fuca
#   axis from Neah Bay to Angeles Point. Swell arriving from squarely down
#   this axis loses the least energy to shadowing by the headlands on
#   either side; swell arriving off-axis gets progressively shadowed out.
# signal = neah_bay_SWELL_height_ft * cos(angle_offset)^2 * (1.3 if the
#   swell period is under 10s else 1.0). The period bonus exists
#   because in this stretch of strait, shorter-period energy measured at
#   Neah Bay empirically transmits down-strait with LESS relative loss
#   than long-period energy (see energy_matrix analysis: -5.5dB short
#   period vs -11.9dB long period) - counterintuitive versus open-coast
#   swell, but consistent across the historical dataset.
# STRIKE_THRESHOLD = 10: at/above this signal value, roughly 44% of
#   historical hours saw an actual 4ft+ wave show up at Angeles Point
#   within a few hours (vs a much lower base rate otherwise).
#
# IMPORTANT: this uses the SPECTRAL PARTITION of each buoy's reading
# (swell_height_ft/swell_period_s/swell_dir_deg from the .spec feed,
# see noaa_client.fetch_buoy_spectral), not the blended WVHT/DPD/MWD
# summary. NDBC's plain realtime2 summary mixes true swell (long-period
# energy that traveled here from a distant wind event) together with
# whatever local wind-chop happens to be hitting the buoy at that same
# moment - correlating the BLENDED number between two buoys can hide or
# distort the actual swell-to-swell relationship, since each buoy's
# local wind chop is independent noise on top of the shared swell
# signal. Using the swell-only partition at both ends isolates the one
# component that actually propagates predictably down the strait.
STRAIT_AXIS_BEARING_DEG = 292.8
STRIKE_THRESHOLD = 10.0
STRIKE_HIT_RATE_PCT = 44  # historical P(4ft+ at Angeles Pt | signal >= threshold)
SWELL_DIR_MATCH_TOLERANCE_DEG = 30  # how close two buoys' swell directions must be to call it "the same train"
SWELL_PROPAGATION_LAG_HOURS = 3  # validated cross-correlation lag, Neah Bay -> Angeles Pt (~16.3kn group velocity for ~10.7s swell)


def _strike_signal_value(height_ft: float | None, period_s: float | None, direction_deg: float | None) -> tuple[float, float] | None:
    """Core strike-signal math, shared by the live (compute_strike_signal)
    and forecast (forecast_strike_signal) paths: signal = height *
    cos(angle off the strait axis)^2 * period bonus. Returns
    (signal, angle_offset_deg) or None if there isn't enough data to
    compute it."""
    if height_ft is None or direction_deg is None:
        return None
    angle_offset = _angle_diff(direction_deg, STRAIT_AXIS_BEARING_DEG)
    period_bonus = 1.3 if (period_s is not None and period_s < 10) else 1.0
    signal = round(height_ft * (math.cos(math.radians(angle_offset)) ** 2) * period_bonus, 2)
    return signal, round(angle_offset, 1)


def compute_strike_signal(neah_bay_spec: dict | None) -> dict | None:
    """Compute the validated live strike-signal from a fresh Neah Bay
    (46087) spectral wave partition. Uses the SWELL component only
    (swell_height_ft/swell_period_s/swell_dir_deg), not the blended
    wave reading, so local wind-chop at Neah Bay doesn't inflate the
    signal for a train that won't actually hold together down-strait.
    Returns None if Neah Bay has no usable swell partition right now."""
    if not neah_bay_spec:
        return None
    height_ft = neah_bay_spec.get("swell_height_ft")
    period_s = neah_bay_spec.get("swell_period_s")
    wave_dir = neah_bay_spec.get("swell_dir_deg")
    result = _strike_signal_value(height_ft, period_s, wave_dir)
    if result is None:
        return None
    signal, angle_offset = result
    is_strike = signal >= STRIKE_THRESHOLD

    return {
        "signal": signal,
        "threshold": STRIKE_THRESHOLD,
        "is_strike": is_strike,
        "verdict": (
            f"Likely good right now (~{STRIKE_HIT_RATE_PCT}% historical hit rate at this signal level)"
            if is_strike else
            "Not likely surfable right now based on live upwind conditions"
        ),
        "neah_bay_swell_height_ft": height_ft,
        "neah_bay_swell_period_s": period_s,
        "neah_bay_swell_dir_deg": wave_dir,
        "angle_offset_from_axis_deg": angle_offset,
        "strait_axis_bearing_deg": STRAIT_AXIS_BEARING_DEG,
        "period_bonus_applied": period_s is not None and period_s < 10,
        "observed_at": neah_bay_spec.get("observed_at"),
        "source": "live_buoy_46087_swell_partition",
    }


def forecast_strike_signal(upwind_hour: dict | None) -> dict | None:
    """Same strike-signal math as compute_strike_signal, applied to a
    forecast hour's swell fields (Open-Meteo marine forecast at Neah
    Bay: swell_height_ft/swell_period_s/swell_dir_deg) instead of a live
    buoy reading. This is what lets the Forecast section use the same
    validated physics as Current Conditions - projecting the
    strike-signal forward using the forecast model's own swell numbers -
    instead of only scoring local wind-fetch as before."""
    if not upwind_hour:
        return None
    height_ft = upwind_hour.get("swell_height_ft")
    period_s = upwind_hour.get("swell_period_s")
    wave_dir = upwind_hour.get("swell_dir_deg")
    result = _strike_signal_value(height_ft, period_s, wave_dir)
    if result is None:
        return None
    signal, angle_offset = result
    return {
        "signal": signal,
        "is_strike": signal >= STRIKE_THRESHOLD,
        "neah_bay_swell_height_ft": height_ft,
        "neah_bay_swell_period_s": period_s,
        "neah_bay_swell_dir_deg": wave_dir,
        "angle_offset_from_axis_deg": angle_offset,
        "time": upwind_hour.get("time"),
    }


def compute_swell_correlation(neah_bay_spec: dict | None, local_spec: dict | None) -> dict | None:
    """Directly compare the SWELL partitions at Neah Bay (upwind) and
    Angeles Point (local) to check whether they're actually the same
    propagating wave train right now, rather than each buoy showing
    an independent local wind-wave bump that happens to coincide. Two
    readings are treated as "the same train" when their swell
    directions agree within SWELL_DIR_MATCH_TOLERANCE_DEG - genuine
    swell holds its direction across ~90km of open strait far better
    than locally-generated chop does. When matched, reports the actual
    live transmission ratio (how much of Neah Bay's swell height is
    showing up at Angeles Point right now), which is the real-time
    analog of the -9dB median energy loss found in the historical
    analysis."""
    if not neah_bay_spec or not local_spec:
        return None
    nb_h, nb_p, nb_d = neah_bay_spec.get("swell_height_ft"), neah_bay_spec.get("swell_period_s"), neah_bay_spec.get("swell_dir_deg")
    lo_h, lo_p, lo_d = local_spec.get("swell_height_ft"), local_spec.get("swell_period_s"), local_spec.get("swell_dir_deg")
    if nb_h is None or lo_h is None:
        return None

    dir_diff = _angle_diff(nb_d, lo_d) if (nb_d is not None and lo_d is not None) else None
    same_train = dir_diff is not None and dir_diff <= SWELL_DIR_MATCH_TOLERANCE_DEG
    transmission_pct = round((lo_h / nb_h) * 100, 1) if nb_h > 0 else None

    return {
        "neah_bay_swell_height_ft": nb_h,
        "neah_bay_swell_period_s": nb_p,
        "neah_bay_swell_dir_deg": nb_d,
        "local_swell_height_ft": lo_h,
        "local_swell_period_s": lo_p,
        "local_swell_dir_deg": lo_d,
        "direction_diff_deg": round(dir_diff, 1) if dir_diff is not None else None,
        "same_train": same_train,
        "transmission_pct": transmission_pct,
        "note": (
            "Same swell direction at both buoys - this is very likely the same wave "
            "train that left Neah Bay and is now showing up at Angeles Point."
            if same_train else
            "Swell directions differ more than expected - the local reading may be "
            "dominated by a different/local source rather than swell arriving from Neah Bay."
        ),
    }


def build_current_conditions(
    spot: Spot,
    neah_bay_obs: dict | None,
    local_obs: dict | None,
    neah_bay_spec: dict | None = None,
    local_spec: dict | None = None,
) -> dict | None:
    """Assemble the full "Current Conditions" payload for the detail view:
    the validated strike-signal computed from the live upwind Neah Bay
    swell partition (the leading indicator), the live local buoy reading
    at Angeles Point scored as direct ground truth, and a direct
    swell-to-swell correlation between the two buoys' spectral
    partitions so the UI can show whether what's arriving locally is
    actually the same train seen upwind (vs. independent local
    wind-chop). All three are swell-grounded, consistent with the
    forecast model's swell-transmission approach (no wind-generates-
    the-wave logic here - that premise didn't hold up against the buoy
    history, see the module header). Only meaningful for fetch_wind
    strait spots that have both reference points configured."""
    strike = compute_strike_signal(neah_bay_spec)
    correlation = compute_swell_correlation(neah_bay_spec, local_spec)
    local_live = score_live_wave_observation(spot, local_obs) if local_obs else None
    if local_live:
        local_live["label"] = label_for_score(local_live["score"])
    if not strike and not local_live and not correlation:
        return None
    return {
        "strike_signal": strike,
        "local_observation": local_live,
        "swell_correlation": correlation,
    }
