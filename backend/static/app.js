// Surf Forecast frontend. Talks to the FastAPI backend defined in API_BASE.
// Everything the user configures (spots + their scoring preferences) is
// persisted server-side, so refreshing or reopening the page never loses
// data - it always reloads from the backend's database.

// When served by the same FastAPI app (deployed setup), leave this
// empty so requests go to relative paths on the same origin. For local
// dev with two separate servers, set window.SURF_API_BASE before this
// script loads (see index.html) to point at the backend port.
const API_BASE = window.SURF_API_BASE || "";

const el = (sel) => document.querySelector(sel);
const spotListSection = el("#spot-list-section");
const spotListEl = el("#spot-list");
const detailSection = el("#detail-section");
const detailContent = el("#detail-content");
const formSection = el("#form-section");
const form = el("#spot-form");
const formTitle = el("#form-title");
const deleteBtn = el("#delete-spot-btn");

let editingSpotId = null;

function showOnly(section) {
  [spotListSection, detailSection, formSection].forEach((s) => s.classList.add("hidden"));
  section.classList.remove("hidden");
}

async function api(path, options = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

function scoreClass(score) {
  if (score >= 8) return "score-epic";
  if (score >= 6.5) return "score-good";
  if (score >= 4.5) return "score-fair";
  if (score >= 2) return "score-poor";
  return "score-flat";
}

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", hour: "numeric" });
}

function dayKey(iso) {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

// ---------- Spot list ----------

async function loadSpotList() {
  spotListEl.innerHTML = `<p class="empty-state">Loading spots&hellip;</p>`;
  let spots;
  try {
    spots = await api("/api/spots");
  } catch (e) {
    spotListEl.innerHTML = `<div class="error-box">Could not load spots from the server. Is the backend running?<br>${e.message}</div>`;
    return;
  }

  if (spots.length === 0) {
    spotListEl.innerHTML = `<div class="empty-state">No spots yet. Click "+ Add Spot" to create your first one.</div>`;
    return;
  }

  spotListEl.innerHTML = "";
  for (const spot of spots) {
    const card = document.createElement("div");
    card.className = "spot-card";
    card.innerHTML = `
      <div>
        <div class="name">${escapeHtml(spot.name)}</div>
        <div class="coords">${spot.lat.toFixed(3)}, ${spot.lon.toFixed(3)} &middot; buoy ${spot.nearest_buoy_id || "unassigned"}</div>
      </div>
      <div class="now-score score-flat" data-spot-score="${spot.id}">&hellip;</div>
    `;
    card.addEventListener("click", () => openDetail(spot.id));
    spotListEl.appendChild(card);

    // Fire off a quick current-conditions fetch to populate the badge,
    // without blocking the rest of the list from rendering.
    api(`/api/spots/${spot.id}/forecast?days=1`).then((data) => {
      const badge = spotListEl.querySelector(`[data-spot-score="${spot.id}"]`);
      if (!badge) return;
      const nowHour = data.hours[0];
      if (!nowHour) { badge.textContent = "N/A"; return; }
      badge.textContent = nowHour.score;
      badge.className = `now-score ${scoreClass(nowHour.score)}`;
    }).catch(() => {
      const badge = spotListEl.querySelector(`[data-spot-score="${spot.id}"]`);
      if (badge) { badge.textContent = "N/A"; }
    });
  }
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

// ---------- Detail / forecast view ----------

async function openDetail(spotId) {
  showOnly(detailSection);
  detailContent.innerHTML = `<p>Loading forecast&hellip;</p>`;

  let data;
  try {
    data = await api(`/api/spots/${spotId}/forecast?days=7`);
  } catch (e) {
    detailContent.innerHTML = `<div class="error-box">Could not load forecast: ${e.message}</div>`;
    return;
  }

  const spot = data.spot;
  let html = `
    <div class="detail-header">
      <h2>${escapeHtml(spot.name)}</h2>
      <button class="edit-btn" id="edit-spot-btn">Edit Preferences</button>
    </div>
    <div class="coords">${spot.lat.toFixed(4)}, ${spot.lon.toFixed(4)}</div>
  `;

  if (data.buoy_observation) {
    const o = data.buoy_observation;
    html += `<div class="buoy-box">
      <span class="label">Live buoy ${o.station_id}</span> (observed ${new Date(o.observed_at).toLocaleString()}):
      wave height ${o.wave_height_m ?? "?"} m, dominant period ${o.dominant_period_s ?? "?"} s,
      swell dir ${o.wave_dir_deg ?? "?"}&deg;${o.wind_speed_ms != null ? `, wind ${o.wind_speed_ms} m/s` : ""}
    </div>`;
  } else if (data.buoy_error) {
    html += `<div class="error-box">${escapeHtml(data.buoy_error)} (forecast below is unaffected - it comes from the wave model, not the buoy.)</div>`;
  }

  if (data.forecast_error) {
    html += `<div class="error-box">${escapeHtml(data.forecast_error)}</div>`;
  }

  // Group hourly forecast by day
  const days = new Map();
  for (const h of data.hours) {
    const key = dayKey(h.time);
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(h);
  }

  for (const [day, hours] of days) {
    html += `<div class="day-block"><h3>${day}</h3><div class="hours-row">`;
    for (const h of hours) {
      html += `
        <div class="hour-card ${scoreClass(h.score)}">
          <div class="time">${fmtTime(h.time)}</div>
          <div class="label">${h.label}</div>
          <div class="score-num">${h.score}</div>
          <div class="meta">${h.swell_height_m ?? "?"}m @ ${h.swell_period_s ?? "?"}s<br>${Math.round(h.wind_speed_kmh ?? 0)}km/h wind</div>
        </div>
      `;
    }
    html += `</div></div>`;
  }

  detailContent.innerHTML = html;
  el("#edit-spot-btn").addEventListener("click", () => openForm(spot));
}

// ---------- Add/edit form ----------

function openForm(spot = null) {
  editingSpotId = spot ? spot.id : null;
  formTitle.textContent = spot ? `Edit ${spot.name}` : "Add a Surf Spot";
  deleteBtn.classList.toggle("hidden", !spot);

  const defaults = spot || {
    name: "", lat: "", lon: "", facing_direction: 270, swell_window_deg: 45,
    min_good_period_s: 8, ideal_period_s: 12, min_good_height_m: 0.5,
    ideal_height_m: 1.5, max_good_height_m: 3.0, max_good_wind_kmh: 15,
    onshore_window_deg: 90,
  };
  for (const [key, value] of Object.entries(defaults)) {
    const input = form.elements[key];
    if (input) input.value = value;
  }

  showOnly(formSection);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  const body = {
    name: fd.get("name"),
    lat: parseFloat(fd.get("lat")),
    lon: parseFloat(fd.get("lon")),
    facing_direction: parseFloat(fd.get("facing_direction")),
    swell_window_deg: parseFloat(fd.get("swell_window_deg")),
    min_good_period_s: parseFloat(fd.get("min_good_period_s")),
    ideal_period_s: parseFloat(fd.get("ideal_period_s")),
    min_good_height_m: parseFloat(fd.get("min_good_height_m")),
    ideal_height_m: parseFloat(fd.get("ideal_height_m")),
    max_good_height_m: parseFloat(fd.get("max_good_height_m")),
    max_good_wind_kmh: parseFloat(fd.get("max_good_wind_kmh")),
    onshore_window_deg: parseFloat(fd.get("onshore_window_deg")),
  };

  const submitBtn = form.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.textContent = "Saving...";
  try {
    if (editingSpotId) {
      await api(`/api/spots/${editingSpotId}`, { method: "PUT", body: JSON.stringify(body) });
    } else {
      await api("/api/spots", { method: "POST", body: JSON.stringify(body) });
    }
    await loadSpotList();
    showOnly(spotListSection);
  } catch (err) {
    alert(`Could not save spot: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Save Spot";
  }
});

deleteBtn.addEventListener("click", async () => {
  if (!editingSpotId) return;
  if (!confirm("Delete this spot? This cannot be undone.")) return;
  try {
    await api(`/api/spots/${editingSpotId}`, { method: "DELETE" });
    await loadSpotList();
    showOnly(spotListSection);
  } catch (err) {
    alert(`Could not delete spot: ${err.message}`);
  }
});

el("#add-spot-btn").addEventListener("click", () => openForm());
el("#cancel-form-btn").addEventListener("click", () => showOnly(spotListSection));
el("#back-btn").addEventListener("click", () => showOnly(spotListSection));

loadSpotList();
