const API_BASE = "";

const map = L.map("map", { zoomControl: true }).setView([47.7, -123.8], 8);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 18,
}).addTo(map);

const detailPanel = document.getElementById("detail-panel");
const detailContent = document.getElementById("detail-content");
document.getElementById("close-panel-btn").addEventListener("click", () => {
  detailPanel.classList.add("hidden");
});

function scoreClass(label) {
  if (!label) return "unknown";
  const l = label.toLowerCase();
  if (l === "epic" || l === "good") return "good";
  if (l === "fair") return "fair";
  if (l === "poor" || l === "flat") return "poor";
  return "unknown";
}

function makeDotIcon(label) {
  const cls = scoreClass(label);
  return L.divIcon({
    className: "",
    html: `<div class="spot-dot-${cls}" style="width:16px;height:16px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.4);"></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

async function loadSpots() {
  const res = await fetch(`${API_BASE}/api/spots`);
  const spots = await res.json();
  spots.forEach((spot) => {
    const marker = L.marker([spot.lat, spot.lon], { icon: makeDotIcon(spot.current_label) }).addTo(map);
    marker.bindTooltip(`${spot.name} — ${spot.current_label}${spot.current_score != null ? " (" + spot.current_score + "/10)" : ""}`);
    marker.on("click", () => openDetail(spot.id));
  });
}

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtDay(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function groupByDay(hours) {
  const groups = {};
  hours.forEach((h) => {
    const day = h.time.slice(0, 10);
    if (!groups[day]) groups[day] = [];
    groups[day].push(h);
  });
  return groups;
}

async function openDetail(spotId) {
  detailPanel.classList.remove("hidden");
  detailContent.innerHTML = "Loading...";

  const res = await fetch(`${API_BASE}/api/spots/${spotId}`);
  const spot = await res.json();

  let html = "";
  const cur = spot.forecast && spot.forecast.length ? spot.forecast[0] : null;
  const curCls = cur ? scoreClass(cur.label) : "unknown";

  html += `<div class="detail-header">
    <h2>${spot.name}</h2>
    ${cur ? `<span class="score-badge badge-${curCls}">${cur.label} &middot; ${cur.score}/10 right now</span>` : ""}
  </div>`;

  if (spot.buoy_observation) {
    const b = spot.buoy_observation;
    html += `<div class="buoy-box">
      <h4>Live buoy ${b.station_id}</h4>
      <div>Wave height: ${b.wave_height_ft ?? "?"} ft &middot; Period: ${b.dominant_period_s ?? "?"} s</div>
      <div>Wind: ${b.wind_speed_mph ?? "?"} mph @ ${b.wind_dir_deg ?? "?"}&deg;</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${b.observed_at}</div>
    </div>`;
  } else if (spot.buoy_error) {
    html += `<div class="buoy-box error-msg">${spot.buoy_error}</div>`;
  }

  if (spot.secondary_observation) {
    const s = spot.secondary_observation;
    html += `<div class="buoy-box">
      <h4>Local wind ${s.station_id}</h4>
      <div>Wind: ${s.wind_speed_mph ?? "?"} mph @ ${s.wind_dir_deg ?? "?"}&deg; (gust ${s.gust_mph ?? "?"} mph)</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${s.observed_at}</div>
    </div>`;
  }

  if (spot.historical_profile_summary) {
    const hp = spot.historical_profile_summary;
    html += `<div class="buoy-box" style="opacity:0.85;">
      <h4>Historical calibration (buoy ${hp.reference_buoy_id})</h4>
      <div>Max swell period seen in last ~45 days: ${hp.max_period_s_observed ?? "?"} s
        (confirms groundswell doesn't reach this far into the strait)</div>
      <div>Forecast wind speeds are compared against ${hp.analog_buckets} real historical
        wind-speed buckets from this buoy's own wind/wave record.</div>
    </div>`;
  }

  if (spot.forecast_error) {
    html += `<p class="error-msg">${spot.forecast_error}</p>`;
  } else if (spot.forecast) {
    const groups = groupByDay(spot.forecast);
    for (const [day, hours] of Object.entries(groups)) {
      html += `<div class="day-group"><h4>${fmtDay(day)}</h4>`;
      // Show every 3rd hour to keep it scannable
      hours.filter((_, i) => i % 3 === 0).forEach((h) => {
        const cls = scoreClass(h.label);
        let detail;
        if (h.model === "fetch_wind") {
          detail = `wind ${h.wind_speed_mph?.toFixed(0) ?? "?"}mph @ ${h.wind_dir_deg?.toFixed(0) ?? "?"}\u00b0 (fetch-driven wave)`;
          if (h.historical_wave_height_ft != null) {
            detail += ` &middot; similar past wind produced ~${h.historical_wave_height_ft.toFixed(1)}ft (${h.historical_analog_count} analogs)`;
          }
        } else {
          detail = `${h.swell_height_ft?.toFixed(1) ?? "?"}ft @ ${h.swell_period_s?.toFixed(0) ?? "?"}s, wind ${h.wind_speed_mph?.toFixed(0) ?? "?"}mph`;
        }
        html += `<div class="hour-row">
          <span class="hour-time">${fmtTime(h.time)}</span>
          <span class="hour-score-dot spot-dot-${cls}"></span>
          <span class="hour-detail">${detail}</span>
          <span class="hour-label">${h.score}</span>
        </div>`;
      });
      html += `</div>`;
    }
  }

  detailContent.innerHTML = html;
}

loadSpots();
