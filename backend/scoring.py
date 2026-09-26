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

# A wave big enough to count as "there's a wave" at a fetch-limited strait
# spot - deliberately low since these are wind-chop novelty waves, not
# open-coast surf. Matches the DEFAULT_PREFS min_good_height_ft used by the
# swell-scoring model, so "there's a wave" means the same thing app-wide.
GOOD_WAVE_HEIGHT_FT = 1.5


def build_historical_fetch_profile(
    rows: list[dict], facing_direction: float, window_deg: float, min_wind_mph: float,
    bucket_size_mph: float = 5.0, good_wave_height_ft: float = GOOD_WAVE_HEIGHT_FT,
) -> dict:
    """Turn a buoy's raw historical wind+wave rows into an empirical
    lookup: for wind blowing from a usable (aligned) direction, what wave
    height has that speed actually produced at this buoy historically?
    Bucketed by wind speed (5 mph bins) so a forecast hour can be compared
    against real past analogs rather than a single hand-tuned curve.

    Returns {bucket_low_mph: {"avg_wave_height_ft": float, "n": int,
    "max_wave_height_ft": float}}, plus a "max_period_s" key with the
    single highest dominant period seen in the whole window (any
    direction) - useful context for how far real swell reaches in here -
    and a "good_day_analysis" key (see _analyze_good_days) answering the
    question a raw wave-height number alone can't: of the days that had a
    wave big enough to notice, how many ALSO had wind actually blowing
    from a usable direction at a usable speed at the same time, versus
    being a wave that showed up from a misaligned/cross gust that wouldn't
    have actually been rideable here.
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
    good_day_analysis = _analyze_good_days(rows, facing_direction, window_deg, min_wind_mph, good_wave_height_ft)
    return {
        "buckets": profile, "bucket_size_mph": bucket_size_mph, "max_period_s": max_period,
        "good_day_analysis": good_day_analysis,
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


def build_storm_signature(
    local_rows: list[dict], upwind_rows: list[dict],
    big_wave_height_ft: float = 6.0, lookback_hours: int = 6,
    event_gap_hours: int = 48,
) -> dict | None:
    """Answer the user's actual question empirically: across the whole
    multi-year record, what did the storm actually look like - locally
    and upwind at Neah Bay - in the run-up to a 6ft+ swell event at
    Elwha's local buoy? Rather than asserting a wind direction is
    "good" from first principles, this clusters every historical
    6ft+ local-buoy reading into distinct storm events (a new event
    starts after an `event_gap_hours` gap with no 6ft+ reading), finds
    each event's peak, and looks at the upwind Neah Bay wind over the
    `lookback_hours` immediately preceding that peak - the sustained
    fetch-building wind is the real leading indicator, not the
    instantaneous wind at the moment the wave shows up locally.

    Returns a dict with the number of qualifying events found, the
    dominant upwind wind direction/speed pattern across them (median +
    the most common 10-degree/5-mph buckets), and the local wave
    direction/period pattern at peak - i.e. a concrete, data-backed
    "here's what a 6ft+ day at Elwha actually looks like" signature,
    used both to explain the pattern to the user and to score live
    wind direction against it."""
    if not local_rows or not upwind_rows:
        return None

    def parse_dt(iso):
        try:
            return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            return None

    big = []
    for r in local_rows:
        wave_ft = r.get("wave_height_ft")
        dt = parse_dt(r.get("observed_at"))
        if wave_ft is not None and dt is not None and wave_ft >= big_wave_height_ft:
            big.append((dt, r))
    if not big:
        return None
    big.sort(key=lambda x: x[0])

    events = []
    cur = [big[0]]
    for item in big[1:]:
        if (item[0] - cur[-1][0]) > timedelta(hours=event_gap_hours):
            events.append(cur)
            cur = [item]
        else:
            cur.append(item)
    events.append(cur)

    upwind_sorted = sorted(
        ((dt, r) for r in upwind_rows if (dt := parse_dt(r.get("observed_at"))) is not None),
        key=lambda x: x[0],
    )
    upwind_times = [x[0] for x in upwind_sorted]

    def upwind_window(start, end):
        lo = bisect.bisect_left(upwind_times, start)
        hi = bisect.bisect_right(upwind_times, end)
        return [upwind_sorted[i][1] for i in range(lo, hi)]

    speeds, dirs, peak_heights, peak_dirs, peak_periods = [], [], [], [], []
    for event in events:
        peak_dt, peak_row = max(event, key=lambda x: x[1].get("wave_height_ft") or 0.0)
        peak_heights.append(peak_row.get("wave_height_ft"))
        if peak_row.get("wave_dir_deg") is not None:
            peak_dirs.append(peak_row["wave_dir_deg"])
        if peak_row.get("dominant_period_s") is not None:
            peak_periods.append(peak_row["dominant_period_s"])
        win = upwind_window(peak_dt - timedelta(hours=lookback_hours), peak_dt)
        win_speeds = [w["wind_speed_mph"] for w in win if w.get("wind_speed_mph") is not None]
        win_dirs = [w["wind_dir_deg"] for w in win if w.get("wind_dir_deg") is not None]
        if win_speeds:
            speeds.append(sum(win_speeds) / len(win_speeds))
        if win_dirs:
            dirs.append(_circular_median_deg(win_dirs))

    if not speeds or not dirs:
        return None

    def bucket_counts(values, size):
        counts: dict[int, int] = {}
        for v in values:
            b = int(v // size) * size
            counts[b] = counts.get(b, 0) + 1
        return counts

    dir_buckets = bucket_counts(dirs, 10)
    speed_buckets = bucket_counts(speeds, 5)
    dominant_dir_bucket = max(dir_buckets, key=dir_buckets.get)
    dominant_speed_bucket = max(speed_buckets, key=speed_buckets.get)

    speeds_sorted = sorted(speeds)
    dirs_sorted = sorted(dirs)
    n = len(speeds)

    return {
        "event_count": len(events),
        "big_wave_height_ft": big_wave_height_ft,
        "lookback_hours": lookback_hours,
        "upwind_wind_speed_mph": {
            "median": round(speeds_sorted[n // 2], 1),
            "dominant_bucket_low_mph": dominant_speed_bucket,
            "dominant_bucket_pct": round(speed_buckets[dominant_speed_bucket] / n * 100, 0),
        },
        "upwind_wind_dir_deg": {
            "median": round(dirs_sorted[len(dirs_sorted) // 2], 0),
            "dominant_bucket_low_deg": dominant_dir_bucket,
            "dominant_bucket_pct": round(dir_buckets[dominant_dir_bucket] / len(dirs) * 100, 0),
        },
        "local_peak_wave_dir_deg_median": (
            round(sorted(peak_dirs)[len(peak_dirs) // 2], 0) if peak_dirs else None
        ),
        "local_peak_period_s_median": (
            round(sorted(peak_periods)[len(peak_periods) // 2], 2) if peak_periods else None
        ),
        "local_peak_height_ft_max": round(max(peak_heights), 1) if peak_heights else None,
        "summary": (
            f"Across {len(events)} distinct {big_wave_height_ft:.0f}ft+ events since the "
            f"record began, the pattern is consistent: sustained westerly wind at Neah Bay "
            f"(median ~{round(speeds_sorted[n // 2])}mph, most commonly in the "
            f"{dominant_dir_bucket}-{dominant_dir_bucket + 10}\u00b0 range) in the "
            f"{lookback_hours}h beforehand, arriving locally from "
            f"{round(sorted(peak_dirs)[len(peak_dirs) // 2]) if peak_dirs else '?'}\u00b0 "
            f"at Angeles Point."
        ),
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


def score_wind_direction_vs_storm_signature(
    wind_dir_deg: float | None, signature: dict | None, tolerance_deg: float = 35.0,
) -> dict | None:
    """Score how closely a live/forecast upwind wind direction matches
    the empirical storm signature's dominant direction (see
    build_storm_signature) - a direct, data-grounded fitness score
    (0-1) distinct from the geometric facing_direction/swell_window_deg
    fitness already used elsewhere, since this one is anchored to what
    has ACTUALLY produced 6ft+ days historically rather than a
    hand-set angle. Returns None if there's no signature or no wind
    direction to compare."""
    if not signature or wind_dir_deg is None:
        return None
    target = signature["upwind_wind_dir_deg"]["median"]
    off_angle = _angle_diff(wind_dir_deg, target)
    fitness = max(0.0, 1.0 - (off_angle / tolerance_deg))
    return {
        "wind_dir_deg": wind_dir_deg,
        "storm_signature_dir_deg": target,
        "off_angle_deg": round(off_angle, 1),
        "fitness": round(fitness, 2),
        "matches_storm_pattern": fitness >= 0.5,
    }


# ---------------------------------------------------------------------------
# Ranked quality-factor legend, per scoring model. This is deliberately
# separate from the numeric SCALE_DESCRIPTIONS bands above: those explain
# what a given SCORE means, this explains WHY - which physical inputs
# actually move the needle most, in priority order, so a user glancing at
# the detail view understands what to look for themselves, not just what
# number came out.
#
# For Elwha specifically: this is NOT a pure wind-fetch novelty wave (an
# earlier, incomplete model treated it that way). Real swell size at the
# Angeles Point buoy correlates strongly with what breaks at Elwha, so
# swell HEIGHT is the dominant factor - by a wide margin - over wind
# speed. Direction matters a lot too, but through a different mechanism
# than open-coast angle-of-attack: Elwha sits behind a large bluff, so
# one side of the point is sheltered/good and the other is shadowed out
# depending on which way the swell/wind is coming from, similar to how a
# point break works. Wind speed/fetch is real but secondary - it's what
# turns a good swell into a clean vs. chopped-up version of itself,
# not what determines whether there's a wave in the first place.
# ---------------------------------------------------------------------------
QUALITY_FACTORS = {
    "fetch_wind": [
        {
            "factor": "Swell/wave size (Angeles Point buoy)",
            "importance": "Most important",
            "detail": (
                "Real wave height at the nearby Angeles Point buoy (46267) correlates "
                "strongly with what actually breaks at Elwha - this is the dominant factor, "
                "more than wind speed. Under ~1.5ft: essentially flat. 4ft+ starts approaching "
                "'good' territory here, but only IF direction and wind also line up (see below) - "
                "size alone doesn't guarantee quality."
            ),
        },
        {
            "factor": "Swell/wind direction (bluff shadowing)",
            "importance": "Very important",
            "detail": (
                "Elwha sits behind a large bluff - one side of the point is sheltered and clean, "
                "the other gets shadowed out, depending on which way the swell/wind is coming "
                "from. This works like a point break's angle-of-attack, not simple onshore/offshore: "
                "the same swell size can be great on one side and blocked on the other."
            ),
        },
        {
            "factor": "Local wind speed/fetch",
            "importance": "Secondary",
            "detail": (
                "Sustained westerly wind down the strait adds a local wind-driven wave on top of "
                "whatever swell is already there, and determines whether conditions stay clean or "
                "get chopped up. Real, but it modulates an existing swell more than it creates "
                "surf on its own."
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


def _analyze_good_days(
    rows: list[dict], facing_direction: float, window_deg: float, min_wind_mph: float,
    good_wave_height_ft: float,
) -> dict | None:
    """Group historical hourly rows by calendar day and answer: of the
    days that had a wave big enough to be worth noticing
    (>= good_wave_height_ft at ANY hour that day), how many of those days
    ALSO had wind blowing from a usable direction at a usable speed
    during that same wave? A wave height reading alone doesn\'t tell you
    whether the wind that produced it was actually aligned - a day could
    show a "good" wave height from a stray cross-strait gust that would
    have been a mess to ride, not a clean fetch-driven wave. This is the
    overlap check: wave big enough AND wind aligned+strong enough, at the
    SAME hour, on the SAME day."""
    if not rows:
        return None
    days: dict[str, dict] = {}
    for r in rows:
        wave_ft = r.get("wave_height_ft")
        observed_at = r.get("observed_at")
        if wave_ft is None or not observed_at:
            continue
        day = observed_at[:10]
        entry = days.setdefault(day, {"had_wave": False, "had_good_wind_with_wave": False, "max_wave_ft": 0.0})
        if wave_ft > entry["max_wave_ft"]:
            entry["max_wave_ft"] = wave_ft
        if wave_ft < good_wave_height_ft:
            continue
        entry["had_wave"] = True
        wdir = r.get("wind_dir_deg")
        wspeed = r.get("wind_speed_mph")
        if wdir is not None and wspeed is not None and \
           _angle_diff(wdir, facing_direction) < window_deg and wspeed >= min_wind_mph:
            entry["had_good_wind_with_wave"] = True

    wave_days = [d for d in days.values() if d["had_wave"]]
    good_days = [d for d in wave_days if d["had_good_wind_with_wave"]]
    if not wave_days:
        return {
            "total_days": len(days), "wave_days": 0, "good_wind_days": 0, "good_wind_pct": None,
            "good_wave_height_ft": good_wave_height_ft,
        }
    return {
        "total_days": len(days),
        "wave_days": len(wave_days),
        "good_wind_days": len(good_days),
        "good_wind_pct": round(len(good_days) / len(wave_days) * 100, 0),
        "good_wave_height_ft": good_wave_height_ft,
    }


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
    historical_profile: dict | None = None, upwind_swell_hour: dict | None = None,
    storm_signature: dict | None = None, storm_upwind_hour: dict | None = None,
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
    `upwind_swell_hour` is the Neah Bay forecast hour from
    SWELL_PROPAGATION_LAG_HOURS earlier - i.e. what the swell forecast
    model predicted at the strait's mouth around the time whatever's
    arriving here now would have left. Running that through the SAME
    validated strike-signal formula used in Current Conditions
    (compute_strike_signal / forecast_strike_signal) lets the forecast
    align with today's live-buoy work instead of relying purely on
    local wind-fetch, which misses real swell events the wind-only model
    can't see coming.
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

    # --- Swell strike-signal projection: run the upwind Neah Bay swell
    # forecast (lagged by the validated ~3h propagation delay) through
    # the same strike-signal formula validated against live buoy history
    # in Current Conditions. A predicted strike nudges the score up (real
    # swell is arriving on top of/regardless of local wind); a
    # predicted signal well below threshold nudges it down slightly,
    # since local wind alone rarely holds up a good wave here without it.
    swell_bonus = 1.0
    predicted_signal = None
    predicted_is_strike = None
    fs = forecast_strike_signal(upwind_swell_hour)
    if fs:
        predicted_signal = fs["signal"]
        predicted_is_strike = fs["is_strike"]
        if predicted_is_strike:
            swell_bonus = min(1.3, 1.15 + (predicted_signal - STRIKE_THRESHOLD) / 40)
        elif predicted_signal < STRIKE_THRESHOLD * 0.5:
            swell_bonus = 0.85

    # --- Storm-signature match: does the UPWIND wind direction driving
    # this hour actually match the empirical pattern that has preceded
    # real 6ft+ days historically (see build_storm_signature)? This is
    # deliberately separate from dir_fitness above - dir_fitness grades
    # the LOCAL wind against a hand-set facing_direction/swell_window_deg,
    # while this grades the UPWIND wind (the actual storm-generating
    # wind, several hours before it arrives) against a direction learned
    # directly from years of paired buoy data. A strong match nudges the
    # score up (real historical precedent for a big day); a clear
    # mismatch (upwind wind blowing from a direction that has rarely/
    # never preceded a 6ft+ day) nudges it down, independent of how
    # aligned the local wind happens to be.
    storm_match = None
    storm_bonus = 1.0
    if storm_signature and storm_upwind_hour:
        storm_match = score_wind_direction_vs_storm_signature(
            storm_upwind_hour.get("wind_dir_deg"), storm_signature,
        )
        if storm_match:
            fitness = storm_match["fitness"]
            storm_bonus = 0.85 + 0.35 * fitness

    composite = (dir_fitness ** 1.2) * speed_fitness * fetch_bonus * historical_factor * swell_bonus * storm_bonus
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
            "swell_bonus": round(swell_bonus, 2),
            "storm_bonus": round(storm_bonus, 2),
        },
        "historical_wave_height_ft": hist_wave_height_ft,
        "historical_analog_count": hist_analog_count,
        "predicted_strike_signal": predicted_signal,
        "predicted_strike": predicted_is_strike,
        "storm_signature_match": storm_match,
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
            "This spot never gets real ocean groundswell - it's ~50mi inside "
            "the Strait of Juan de Fuca, too far for open-coast swell to survive "
            "the trip (confirmed by years of buoy history at the strait mouth "
            "vs. further in). Every number on this scale is a locally wind-built, "
            "fetch-limited wave, not a groundswell forecast."
        ),
        "bands": [
            {"label": "Epic", "range": "8-10", "wave_ft": "~4-6ft+",
             "meaning": "Strong sustained westerly wind (~22mph+) has had hours to build fetch down the whole strait, or a strike-signal swell event is layering on top. Rare - this buoy's own history puts most wind here around the 22mph bucket, not higher."},
            {"label": "Good", "range": "6.5-7.9", "wave_ft": "~2.5-4ft",
             "meaning": "Solid aligned westerly wind at/near the ~22mph ideal fetch speed, sustained for several hours upwind at Neah Bay. The most common \"actually worth going\" band for this spot."},
            {"label": "Fair", "range": "4.5-6.4", "wave_ft": "~1.5-2.5ft",
             "meaning": "Wind is aligned but on the light or short-duration side (~12-17mph), or strong but not sustained long enough to build full fetch yet. A small, textured wind-wave - ridable but unremarkable."},
            {"label": "Poor", "range": "2-4.4", "wave_ft": "<1.5ft",
             "meaning": "Wind is weak, misaligned (not blowing down-strait), or hasn't built fetch yet. Barely a ripple, if anything."},
            {"label": "Flat", "range": "0-1.9", "wave_ft": "~0ft",
             "meaning": "No usable wind-fetch at all - calm, wrong direction, or blowing up-strait (which kills the wave regardless of speed)."},
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
    storm_signature: dict | None = None,
) -> dict | None:
    """Assemble the full "Current Conditions" payload for the detail view:
    the validated strike-signal computed from the live upwind Neah Bay
    swell partition (the leading indicator), the live local buoy reading
    at Angeles Point scored as direct ground truth, a direct
    swell-to-swell correlation between the two buoys' spectral
    partitions so the UI can show whether what's arriving locally is
    actually the same train seen upwind (vs. independent local
    wind-chop), and (if a storm_signature is available) whether the
    live upwind WIND direction right now matches the empirical pattern
    that has actually preceded 6ft+ days historically - a concrete,
    data-grounded read on "does this look like the start of a real
    swell event" distinct from the swell-partition-based strike signal.
    Only meaningful for fetch_wind strait spots that have both
    reference points configured."""
    strike = compute_strike_signal(neah_bay_spec)
    correlation = compute_swell_correlation(neah_bay_spec, local_spec)
    local_live = score_live_wave_observation(spot, local_obs) if local_obs else None
    if local_live:
        local_live["label"] = label_for_score(local_live["score"])
    storm_match = None
    if storm_signature and neah_bay_obs:
        storm_match = score_wind_direction_vs_storm_signature(
            neah_bay_obs.get("wind_dir_deg"), storm_signature,
        )
    if not strike and not local_live and not correlation and not storm_match:
        return None
    return {
        "strike_signal": strike,
        "local_observation": local_live,
        "swell_correlation": correlation,
        "storm_signature_match": storm_match,
    }
