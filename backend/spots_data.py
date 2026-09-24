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
        for k, v in {**DEFAULT_PREFS, **prefs}.items():
            setattr(self, k, v)
        self.nearest_buoy_id = None


def load_spots() -> list[Spot]:
    with open(SEED_PATH) as f:
        raw = json.load(f)
    spots = []
    for i, r in enumerate(raw):
        spots.append(Spot(
            id=i + 1,
            name=r["name"],
            lat=r["lat"],
            lon=r["lon"],
            facing_direction=r["facing_direction"],
            swell_window_deg=r["swell_window_deg"],
            source=r.get("source", "unknown"),
        ))
    return spots


SPOTS = load_spots()
SPOTS_BY_ID = {s.id: s for s in SPOTS}
