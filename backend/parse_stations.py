"""One-time parser: convert NDBC station_table.txt into a clean JSON list of
buoys with id, name, lat, lon. Run once to generate stations.json, which the
app reads at runtime (no need to re-fetch/re-parse on every boot)."""
import re
import json

OUT = []
with open("ndbc_stations_raw.txt", encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        station_id, owner, ttype, hull, name, payload, location = parts[:7]
        # location format: "34.937 N 120.999 W (...)"
        m = re.match(r"([\d.]+)\s*([NS])\s+([\d.]+)\s*([EW])", location.strip())
        if not m:
            continue
        lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
        lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
        OUT.append({
            "id": station_id.strip().upper(),
            "name": name.strip(),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
        })

with open("stations.json", "w") as f:
    json.dump(OUT, f)

print(f"Parsed {len(OUT)} stations with valid coordinates")
