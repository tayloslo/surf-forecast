"""
Permanent surf spot catalog. No user CRUD - spots are a fixed, curated
dataset (California prototype) loaded from seed_spots.json at startup.
See seed_spots.py for provenance of each entry.

Each spot gets sane default scoring preferences (period/height/wind
tolerances) since the seed data itself only carries geometry
(lat/lon/facing_direction/swell_window_deg).
"""
import json
import os

SEED_PATH = os.path.join(os.path.dirname(__file__), "seed_spots.json")

DEFAULT_PREFS = dict(
    min_good_period_s=8.0,
    ideal_period_s=12.0,
    min_good_height_m=0.4,
    ideal_height_m=1.4,
    max_good_height_m=3.5,
    max_good_wind_kmh=15.0,
    onshore_window_deg=90.0,
    # Fetch-limited wind-wave thresholds (km/h) - only used by spots with
    # scoring_model="fetch_wind" (e.g. Elwha). Harmless defaults for every
    # other spot since the normal swell scoring path never reads them.
    fetch_min_wind_kmh=20.0,
    fetch_ideal_wind_kmh=35.0,
    fetch_max_wind_kmh=55.0,
    # "swell" scoring (default) uses the Open-Meteo marine wave model;
    # "fetch_wind" uses local + upwind wind forecasts instead, for straits
    # where groundswell cannot physically arrive.
    scoring_model="swell",
    # For fetch_wind spots: an upwind reference point (lat/lon) whose
    # sustained wind is a leading indicator of fetch building down-strait.
    upwind_lat=None,
    upwind_lon=None,
    # Optional second live buoy for the detail view (fetch_wind spots
    # benefit from showing both the upwind mouth buoy and a local reading).
    secondary_buoy_id=None,
)


class Spot:
    """Lightweight plain object standing in for what used to be the
    SQLAlchemy ORM row - scoring.py just needs attribute access."""
    def __init__(self, id, name, lat, lon, facing_direction, swell_window_deg, source, **prefs):
        self.id = id
        self.name = name
        self.lat = lat
        self.lon = lon
        self.facing_direction = facing_direction
        self.swell_window_deg = swell_window_deg
        self.source = source
        self.nearest_buoy_id = None
        for k, v in {**DEFAULT_PREFS, **prefs}.items():
            setattr(self, k, v)


def load_spots() -> list[Spot]:
    with open(SEED_PATH) as f:
        raw = json.load(f)
    spots = []
    for i, r in enumerate(raw):
        extra_prefs = {
            k: v for k, v in r.items()
            if k not in ("name", "lat", "lon", "facing_direction", "swell_window_deg", "source")
        }
        spots.append(Spot(
            id=i + 1,
            name=r["name"],
            lat=r["lat"],
            lon=r["lon"],
            facing_direction=r["facing_direction"],
            swell_window_deg=r["swell_window_deg"],
            source=r.get("source", "unknown"),
            **extra_prefs,
        ))
    return spots


SPOTS = load_spots()
SPOTS_BY_ID = {s.id: s for s in SPOTS}
