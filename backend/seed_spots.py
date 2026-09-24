"""
Seed dataset of California surf breaks.
Sources:
 - OSM Overpass API (leisure=pitch + sport=surfing) around Santa Cruz
 - Hand-curated list of well-known public CA breaks (name/coords are public
   geographic knowledge; facing_direction/swell_window are inferred from coastline
   orientation + general public surf knowledge, not scraped from any proprietary source)
"""
import json

OSM_BREAKS = json.load(open("osm_spots_ca.json"))
OSM_BREAKS = [e for e in OSM_BREAKS if e.get("tags", {}).get("leisure") == "pitch" and e.get("tags", {}).get("name")]

# facing_direction = compass direction the wave face points TOWARD (i.e. spot "faces" this way,
# meaning it wants swell arriving FROM this direction... we'll define precisely in scoring)
# swell_window_deg = how many degrees off facing_direction is still workable
CURATED_BREAKS = [
    {"name": "Malibu (Surfrider Beach)", "lat": 34.0367, "lon": -118.6786, "facing_direction": 200, "swell_window_deg": 60},
    {"name": "Huntington Beach Pier", "lat": 33.6553, "lon": -118.0053, "facing_direction": 210, "swell_window_deg": 70},
    {"name": "Trestles (Lower)", "lat": 33.3825, "lon": -117.5931, "facing_direction": 225, "swell_window_deg": 60},
    {"name": "Mavericks", "lat": 37.4914, "lon": -122.5006, "facing_direction": 280, "swell_window_deg": 50},
    {"name": "Rincon", "lat": 34.3728, "lon": -119.4761, "facing_direction": 190, "swell_window_deg": 55},
    {"name": "Steamer Lane", "lat": 36.9513, "lon": -122.0247, "facing_direction": 200, "swell_window_deg": 60},
    {"name": "Ocean Beach, SF", "lat": 37.7594, "lon": -122.5107, "facing_direction": 260, "swell_window_deg": 70},
    {"name": "Swami's, Encinitas", "lat": 33.0303, "lon": -117.2946, "facing_direction": 220, "swell_window_deg": 60},
    {"name": "Windansea", "lat": 32.8236, "lon": -117.2789, "facing_direction": 230, "swell_window_deg": 60},
    {"name": "Blacks Beach", "lat": 32.8894, "lon": -117.2531, "facing_direction": 230, "swell_window_deg": 65},
]

def build_all():
    spots = []
    for e in OSM_BREAKS:
        spots.append({
            "name": e["tags"]["name"],
            "lat": e["lat"],
            "lon": e["lon"],
            "facing_direction": 200,   # Santa Cruz breaks generally face S/SW
            "swell_window_deg": 60,
            "source": "osm",
        })
    for c in CURATED_BREAKS:
        c2 = dict(c)
        c2["source"] = "curated"
        spots.append(c2)
    return spots

if __name__ == "__main__":
    spots = build_all()
    with open("seed_spots.json", "w") as f:
        json.dump(spots, f, indent=2)
    print(f"Wrote {len(spots)} spots")
