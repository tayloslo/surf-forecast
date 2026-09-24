const API_BASE = "";

const map = L.map("map", { zoomControl: true }).setView([36.5, -121.0], 7);
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
      <div>Wave height: ${b.wave_height_m ?? "?"} m &middot; Period: ${b.dominant_period_s ?? "?"} s</div>
      <div>Wind: ${b.wind_speed_ms ?? "?"} m/s @ ${b.wind_dir_deg ?? "?"}&deg;</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${b.observed_at}</div>
    </div>`;
  } else if (spot.buoy_error) {
    html += `<div class="buoy-box error-msg">${spot.buoy_error}</div>`;
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
        html += `<div class="hour-row">
          <span class="hour-time">${fmtTime(h.time)}</span>
          <span class="hour-score-dot spot-dot-${cls}"></span>
          <span class="hour-detail">${h.swell_height_m?.toFixed(1) ?? "?"}m @ ${h.swell_period_s?.toFixed(0) ?? "?"}s, wind ${h.wind_speed_kmh?.toFixed(0) ?? "?"}km/h</span>
          <span class="hour-label">${h.score}</span>
        </div>`;
      });
      html += `</div>`;
    }
  }

  detailContent.innerHTML = html;
}

loadSpots();
