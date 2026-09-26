"""
Washington-only surf spot catalog.

Scope: this app is for novelty wind-driven waves inside Puget Sound / the
Strait of Juan de Fuca that Surfline and other commercial forecast sites
don't cover at all (Elwha being the flagship example), plus the handful of
legitimate open-coast groundswell breaks Washington does have. No OSM
surf-spot tags exist for real WA breaks (checked - the only WA-area OSM
"surfing" tags are surf SHOPS in Westport, not break locations, and a
cluster of breaks that are actually Vancouver Island, BC, not WA), so this
is a fully hand-curated list using public geographic knowledge.

Two scoring models are used:
  - "swell": open-coast breaks that receive real Pacific groundswell.
  - "fetch_wind": spots deep in the Strait/Sound where groundswell cannot
    physically arrive: the wave instead comes from sustained wind blowing
    down a narrow channel and building local fetch. See scoring.py for
    the physical reasoning, including buoy-history evidence for why swell
    cannot reach these spots.

All height/speed preference fields are US units (feet, mph).
"""
import json

SPOTS = [
    {
        "name": "Elwha, WA", "lat": 48.1494, "lon": -123.5660,
        "facing_direction": 270, "swell_window_deg": 45,
        "scoring_model": "fetch_wind",
        "upwind_lat": 48.493, "upwind_lon": -124.727,
        "nearest_buoy_id": "46087",
        "secondary_buoy_id": "9444090",
        # Local wave reading = NDBC buoy 46267, Angeles Point, only ~5km
        # from the spot. No wind sensor onboard, so it can't replace 46087
        # for wind+wave calibration, but its live wave height/period is
        # the closest real wave observation to Elwha available.
        "local_wave_buoy_id": "46267",
        "fetch_min_wind_mph": 12.4, "fetch_ideal_wind_mph": 21.7, "fetch_max_wind_mph": 34.2,
    },
]

# Paused spots - removed from the live SPOTS list per user request but kept
# here intact (with all their original provenance comments) so they can be
# restored by moving entries back into SPOTS above, rather than re-researched
# from scratch. Not loaded by seed_spots.json while paused.
PAUSED_SPOTS = [
    # --- Open-coast groundswell breaks (real swell, same physics as any
    # Pacific beach break; these DO show up on commercial forecast sites) ---
    {
        "name": "Westport (South Jetty)", "lat": 46.8938, "lon": -124.1187,
        "facing_direction": 250, "swell_window_deg": 65,
        "nearest_buoy_id": "46029",
    },
    {
        "name": "La Push (First Beach)", "lat": 47.9084, "lon": -124.6377,
        "facing_direction": 250, "swell_window_deg": 60,
        "nearest_buoy_id": "46041",
    },
    {
        "name": "Third Beach, Olympic NP", "lat": 47.8797, "lon": -124.6297,
        "facing_direction": 250, "swell_window_deg": 55,
        "nearest_buoy_id": "46041",
    },
    {
        "name": "Ocean Shores", "lat": 46.9707, "lon": -124.1637,
        "facing_direction": 250, "swell_window_deg": 70,
        "nearest_buoy_id": "46029",
    },

    # --- Strait of Juan de Fuca / Puget Sound fetch-wind novelty spots ---
    # (the actual point of this app - not on Surfline)
    #
    # Elwha, WA - river-mouth wind wave ~50mi inside the Strait. Groundswell
    # cannot survive that distance up a narrow strait. Confirmed two ways:
    # (1) the Open-Meteo marine wave model returns near-zero swell here
    # year-round, and (2) real buoy history backs it up - NDBC 46087 at the
    # strait mouth logs dominant periods up to 19s over its 45-day rolling
    # archive, but NDBC 46088 just ~30mi further in tops out at 11s with a
    # much lower average, and 1.5m max wave height. What builds the wave at
    # Elwha instead is sustained west wind blowing the length of the strait
    # (fetch). facing_direction=270 (West) = the direction wind needs to
    # blow FROM to travel down-strait toward Elwha and build a wave.
    # Upwind reference = NDBC buoy 46087, Neah Bay, at the strait mouth -
    # sustained west wind there precedes fetch arriving at Elwha by hours,
    # and its 45-day wind/wave history is used to calibrate the model
    # against real analog conditions (see build_historical_fetch_profile).
    # Local ground truth = NOAA tide station 9444090, Port Angeles, about
    # 9km from the spot (no NDBC wave buoy sits right at Elwha itself).
    # Freshwater Bay, WA - similar mechanism to Elwha, a west-facing
    # cove a bit further up-strait (closer to Port Angeles/Neah Bay).
    # Slightly more exposed to west fetch than Elwha given its position.
    {
        "name": "Freshwater Bay, WA", "lat": 48.1650, "lon": -123.7027,
        "facing_direction": 280, "swell_window_deg": 50,
        "scoring_model": "fetch_wind",
        "upwind_lat": 48.493, "upwind_lon": -124.727,
        "nearest_buoy_id": "46087",
        "secondary_buoy_id": "9444090",
        # Same nearby wave buoy as Elwha (~11km away), still the closest
        # live wave observation available for this stretch of the strait.
        "local_wave_buoy_id": "46267",
        "fetch_min_wind_mph": 11.2, "fetch_ideal_wind_mph": 19.9, "fetch_max_wind_mph": 31.1,
    },
    # Point Wilson, Port Townsend - sits at the mouth of Admiralty Inlet,
    # where wind funneling up/down the Strait meets the entrance to Puget
    # Sound. Builds a short, punchy wind wave on strong southerly or
    # northerly blows. Upwind reference here is the strait mouth as well,
    # since a sustained westerly still drives the dominant fetch line.
    {
        "name": "Point Wilson, Port Townsend", "lat": 48.1447, "lon": -122.7597,
        "facing_direction": 300, "swell_window_deg": 50,
        "scoring_model": "fetch_wind",
        "upwind_lat": 48.493, "upwind_lon": -124.727,
        "nearest_buoy_id": "46088",
        "secondary_buoy_id": "9444090",
        "fetch_min_wind_mph": 13.7, "fetch_ideal_wind_mph": 23.6, "fetch_max_wind_mph": 34.2,
    },
]


if __name__ == "__main__":
    for s in SPOTS:
        s.setdefault("source", "curated")
    with open("seed_spots.json", "w") as f:
        json.dump(SPOTS, f, indent=2)
    print(f"Wrote {len(SPOTS)} spots")
