"""
Global surf spot catalog.
Sources:
 - OSM Overpass API worldwide (leisure=pitch + sport=surfing) - real,
   community-verified break locations.
 - Hand-curated list of world-famous public breaks (name/coords are public
   geographic knowledge, not scraped from any proprietary source).
   facing_direction/swell_window are estimated from known coastline
   orientation + general public surf knowledge for each break.
"""
import json

OSM_ALL = json.load(open("osm_spots_global.json"))
_INLAND_FALSE_POSITIVES = {"blackforestwave", "Radical Kite Center"}
OSM_BREAKS = [
    e for e in OSM_ALL
    if e.get("tags", {}).get("leisure") == "pitch"
    and e.get("tags", {}).get("name")
    and e["tags"]["name"] not in _INLAND_FALSE_POSITIVES
]

# facing_direction = compass bearing the swell needs to come FROM to hit
# the break well (i.e. the seaward-facing normal of the coastline/reef).
CURATED_BREAKS = [
    # California (existing)
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
    # Hawaii
    {"name": "Banzai Pipeline", "lat": 21.6650, "lon": -158.0533, "facing_direction": 340, "swell_window_deg": 45},
    {"name": "Waimea Bay", "lat": 21.6392, "lon": -158.0656, "facing_direction": 350, "swell_window_deg": 50},
    {"name": "Sunset Beach, Oahu", "lat": 21.6764, "lon": -158.0406, "facing_direction": 20, "swell_window_deg": 55},
    {"name": "Honolua Bay, Maui", "lat": 21.0100, "lon": -156.6394, "facing_direction": 300, "swell_window_deg": 50},
    {"name": "Jaws (Pe'ahi), Maui", "lat": 20.9339, "lon": -156.2986, "facing_direction": 10, "swell_window_deg": 45},
    # Mexico / Central America
    {"name": "Puerto Escondido (Zicatela)", "lat": 15.8397, "lon": -97.0656, "facing_direction": 200, "swell_window_deg": 60},
    {"name": "Pascuales", "lat": 18.9333, "lon": -103.9167, "facing_direction": 210, "swell_window_deg": 55},
    {"name": "Salina Cruz", "lat": 16.1667, "lon": -95.2, "facing_direction": 190, "swell_window_deg": 55},
    {"name": "Pavones, Costa Rica", "lat": 8.3833, "lon": -83.0833, "facing_direction": 220, "swell_window_deg": 55},
    {"name": "Witch's Rock, Costa Rica", "lat": 10.6167, "lon": -85.7167, "facing_direction": 240, "swell_window_deg": 55},
    # South America
    {"name": "Chicama, Peru", "lat": -7.6961, "lon": -79.4394, "facing_direction": 220, "swell_window_deg": 50},
    {"name": "Punta de Lobos, Chile", "lat": -34.4167, "lon": -72.0, "facing_direction": 260, "swell_window_deg": 55},
    {"name": "Pipa, Brazil", "lat": -6.2333, "lon": -35.05, "facing_direction": 90, "swell_window_deg": 60},
    # Australia
    {"name": "Bells Beach", "lat": -38.3667, "lon": 144.2833, "facing_direction": 200, "swell_window_deg": 55},
    {"name": "Snapper Rocks", "lat": -28.1608, "lon": 153.5486, "facing_direction": 110, "swell_window_deg": 55},
    {"name": "Byron Bay (The Pass)", "lat": -28.6394, "lon": 153.6172, "facing_direction": 100, "swell_window_deg": 60},
    {"name": "Margaret River (Main Break)", "lat": -33.9581, "lon": 114.9989, "facing_direction": 260, "swell_window_deg": 55},
    {"name": "Cronulla", "lat": -34.0517, "lon": 151.1544, "facing_direction": 110, "swell_window_deg": 60},
    {"name": "Manly Beach", "lat": -33.7969, "lon": 151.2881, "facing_direction": 100, "swell_window_deg": 60},
    # Indonesia
    {"name": "Uluwatu, Bali", "lat": -8.8153, "lon": 115.0864, "facing_direction": 220, "swell_window_deg": 55},
    {"name": "Padang Padang, Bali", "lat": -8.8117, "lon": 115.1069, "facing_direction": 220, "swell_window_deg": 50},
    {"name": "Desert Point, Lombok", "lat": -8.8422, "lon": 115.9394, "facing_direction": 230, "swell_window_deg": 50},
    {"name": "G-Land, Java", "lat": -8.6167, "lon": 114.3833, "facing_direction": 190, "swell_window_deg": 50},
    {"name": "Mentawai (Macaronis)", "lat": -2.0833, "lon": 99.6833, "facing_direction": 220, "swell_window_deg": 55},
    # Philippines
    {"name": "Cloud 9, Siargao", "lat": 9.8014, "lon": 126.1653, "facing_direction": 90, "swell_window_deg": 50},
    # French Polynesia
    {"name": "Teahupo'o, Tahiti", "lat": -17.8461, "lon": -149.2664, "facing_direction": 190, "swell_window_deg": 45},
    # Fiji
    {"name": "Cloudbreak, Fiji", "lat": -17.8667, "lon": 177.2, "facing_direction": 210, "swell_window_deg": 45},
    # South Africa
    {"name": "Jeffreys Bay (Supertubes)", "lat": -34.0489, "lon": 24.9231, "facing_direction": 190, "swell_window_deg": 50},
    {"name": "Muizenberg", "lat": -34.1083, "lon": 18.4708, "facing_direction": 160, "swell_window_deg": 60},
    {"name": "Dungeons, Cape Town", "lat": -34.1897, "lon": 18.3406, "facing_direction": 220, "swell_window_deg": 45},
    # Morocco
    {"name": "Anchor Point, Taghazout", "lat": 30.5450, "lon": -9.7147, "facing_direction": 280, "swell_window_deg": 55},
    # Portugal / Spain / France
    {"name": "Nazaré (Praia do Norte)", "lat": 39.6033, "lon": -9.0847, "facing_direction": 280, "swell_window_deg": 50},
    {"name": "Ericeira (Ribeira d'Ilhas)", "lat": 38.9744, "lon": -9.4189, "facing_direction": 280, "swell_window_deg": 55},
    {"name": "Mundaka", "lat": 43.4083, "lon": -2.6981, "facing_direction": 340, "swell_window_deg": 45},
    {"name": "Hossegor (La Graviere)", "lat": 43.6667, "lon": -1.4333, "facing_direction": 270, "swell_window_deg": 60},
    # UK / Ireland
    {"name": "Fistral Beach, Newquay", "lat": 50.4167, "lon": -5.1, "facing_direction": 280, "swell_window_deg": 65},
    {"name": "Thurso East, Scotland", "lat": 58.5967, "lon": -3.5175, "facing_direction": 20, "swell_window_deg": 50},
    {"name": "Bundoran, Ireland", "lat": 54.4794, "lon": -8.2861, "facing_direction": 290, "swell_window_deg": 55},
    # Japan
    {"name": "Shonan (Kamakura)", "lat": 35.3025, "lon": 139.5325, "facing_direction": 170, "swell_window_deg": 60},
]


def build_all():
    spots = []
    for e in OSM_BREAKS:
        spots.append({
            "name": e["tags"]["name"],
            "lat": e["lat"],
            "lon": e["lon"],
            "facing_direction": 200,  # unknown per-spot orientation; reasonable generic default
            "swell_window_deg": 70,   # wider tolerance since we don't know true orientation
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
