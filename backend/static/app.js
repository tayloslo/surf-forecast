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

// Continuous 0-10 color scale (poor-red -> fair-yellow -> good-green), so
// two "Fair" hours that are actually 4.6 vs 6.4 don't render visually
// identical - a quick glance at hue/shade should track the real number,
// not just which of 4 coarse buckets it falls in. Interpolates through
// the same brand colors used by the discrete badges so the two systems
// read as one consistent scale, just finer-grained.
const SCALE_STOPS = [
  { at: 0, rgb: [192, 73, 44] },   // poor/flat red
  { at: 5, rgb: [212, 160, 23] },  // fair yellow
  { at: 10, rgb: [30, 158, 90] },  // good green
];
function scoreColor(score) {
  if (score == null) return "#8a94a3"; // --unknown
  const s = Math.max(0, Math.min(10, score));
  let lo = SCALE_STOPS[0], hi = SCALE_STOPS[SCALE_STOPS.length - 1];
  for (let i = 0; i < SCALE_STOPS.length - 1; i++) {
    if (s >= SCALE_STOPS[i].at && s <= SCALE_STOPS[i + 1].at) {
      lo = SCALE_STOPS[i]; hi = SCALE_STOPS[i + 1]; break;
    }
  }
  const span = hi.at - lo.at || 1;
  const t = (s - lo.at) / span;
  const rgb = lo.rgb.map((c, i) => Math.round(c + (hi.rgb[i] - c) * t));
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
}

function makeDotIcon(label, score) {
  const color = score != null ? scoreColor(score) : null;
  const cls = scoreClass(label);
  const style = color
    ? `width:16px;height:16px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.4);background:${color};`
    : `width:16px;height:16px;border-radius:50%;border:2px solid white;box-shadow:0 1px 4px rgba(0,0,0,0.4);`;
  return L.divIcon({
    className: "",
    html: `<div class="spot-dot-${cls}" style="${style}"></div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

async function loadSpots() {
  const res = await fetch(`${API_BASE}/api/spots`);
  const spots = await res.json();
  spots.forEach((spot) => {
    const marker = L.marker([spot.lat, spot.lon], { icon: makeDotIcon(spot.current_label, spot.current_score) }).addTo(map);
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

  // Header badge: prefer real live-buoy ground truth for "right now"
  // (matches what the map dot color uses) over the forecast model's
  // guess for the current hour, since a real reading a couple km away
  // beats a modeled wind guess for the current instant.
  const cc = spot.current_conditions;
  const liveLocal = cc && cc.local_observation ? cc.local_observation : null;
  // spot.forecast[0] is always midnight of the oldest requested day
  // (almost always the flat overnight hour) - use the server's actual
  // nearest-to-now pick as the fallback instead, so the header never
  // shows a stale/flat score just because the live buoy fetch hiccuped.
  const fallback = spot.current_hour || (spot.forecast && spot.forecast.length ? spot.forecast[0] : null);
  const headline = liveLocal || fallback;
  const headlineCls = headline ? scoreClass(headline.label) : "unknown";

  html += `<div class="detail-header">
    <h2>${spot.name}</h2>
    ${headline ? `<span class="score-badge badge-${headlineCls}">${headline.label} &middot; ${headline.score}/10 right now</span>` : ""}
  </div>`;

  // Color-scale legend + spot-specific explanation of what the 0-10
  // number actually means here. Kept together right under the header so
  // it's the first thing read before diving into either section below -
  // a "7/10" means something very different at a wind-fetch novelty spot
  // like Elwha than at an open-coast swell break, and that shouldn't be
  // left implicit.
  html += `<div class="scale-legend">
    <div class="scale-gradient-bar"></div>
    <div class="scale-gradient-labels"><span>0 Flat</span><span>5 Fair</span><span>10 Epic</span></div>
  </div>`;

  if (spot.scale_description) {
    const sd = spot.scale_description;
    html += `<details class="scale-details">
      <summary>What does this spot's 0-10 scale mean?</summary>
      <div class="scale-model-note">${sd.model_note}</div>
      ${sd.bands.map((b) => `<div class="scale-band-row">
        <span class="scale-band-label badge-${scoreClass(b.label)}">${b.label} &middot; ${b.range}</span>
        <span class="scale-band-meaning">${b.wave_ft ? `<strong>${b.wave_ft}</strong> &mdash; ` : ""}${b.meaning}</span>
      </div>`).join("")}
    </details>`;
  }

  // ---------------------------------------------------------------
  // SECTION 1: Current Conditions - what the live buoys are reporting
  // RIGHT NOW, and whether that combination actually predicts a good
  // wave at this spot at this moment. This is ground truth, not a
  // model of the future.
  // ---------------------------------------------------------------
  html += `<div class="section-heading">Current Conditions</div>
    <div class="section-sub">What the live buoy readings say is happening right now</div>`;

  if (cc && cc.strike_signal) {
    const s = cc.strike_signal;
    const strikeCls = s.is_strike ? "good" : "poor";
    html += `<div class="buoy-box strike-box">
      <h4>Strike signal (upwind swell @ Neah Bay)</h4>
      <div class="strike-verdict badge-${strikeCls}">${s.verdict}</div>
      <div>Signal: <strong>${s.signal}</strong> (strike threshold: ${s.threshold})</div>
      <div>Neah Bay swell right now: ${s.neah_bay_swell_height_ft} ft @ ${s.neah_bay_swell_period_s ?? "?"}s,
        from ${s.neah_bay_swell_dir_deg}&deg; (${s.angle_offset_from_axis_deg}&deg; off the ${s.strait_axis_bearing_deg}&deg; strait axis)</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Swell-only, separated from local wind-chop &middot; observed ${s.observed_at}</div>
    </div>`;
  }

  if (cc && cc.swell_correlation) {
    const sc = cc.swell_correlation;
    const matchCls = sc.same_train ? "good" : "poor";
    html += `<div class="buoy-box">
      <h4>Swell correlation: Neah Bay &rarr; Angeles Point</h4>
      <div class="strike-verdict badge-${matchCls}" style="font-size:0.78rem;">${sc.same_train ? "Same swell train confirmed" : "Directions don't match"}</div>
      <div>Neah Bay swell: ${sc.neah_bay_swell_height_ft} ft @ ${sc.neah_bay_swell_period_s ?? "?"}s from ${sc.neah_bay_swell_dir_deg ?? "?"}&deg;</div>
      <div>Angeles Pt swell: ${sc.local_swell_height_ft} ft @ ${sc.local_swell_period_s ?? "?"}s from ${sc.local_swell_dir_deg ?? "?"}&deg;
        (${sc.direction_diff_deg ?? "?"}&deg; direction difference)</div>
      ${sc.transmission_pct != null ? `<div>Live transmission: <strong>${sc.transmission_pct}%</strong> of Neah Bay's swell height is showing up here right now</div>` : ""}
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">${sc.note}</div>
    </div>`;
  }

  if (liveLocal) {
    const w = liveLocal;
    const wCls = scoreClass(w.label);
    const lp = spot.local_swell_partition;
    html += `<div class="buoy-box">
      <h4>Local wave buoy (Angeles Point) &middot; <span class="badge-${wCls}" style="padding:2px 8px;border-radius:10px;color:white;font-size:0.75em;">${w.label}</span></h4>
      <div>Blended reading: ${w.wave_height_ft ?? "?"} ft @ ${w.dominant_period_s ?? "?"} s &middot; Dir: ${w.wave_dir_deg ?? "?"}&deg;</div>
      ${lp ? `<div style="opacity:0.8;">&rarr; split: swell ${lp.swell_height_ft ?? "?"}ft@${lp.swell_period_s ?? "?"}s from ${lp.swell_dir_deg ?? "?"}&deg;,
        wind-wave ${lp.windwave_height_ft ?? "?"}ft@${lp.windwave_period_s ?? "?"}s from ${lp.windwave_dir_deg ?? "?"}&deg;</div>` : ""}
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${w.observed_at}</div>
    </div>`;
  }

  if (spot.buoy_observation) {
    const b = spot.buoy_observation;
    html += `<div class="buoy-box">
      <h4>Upwind reference buoy ${b.station_id} (Neah Bay)</h4>
      <div>Wind: ${b.wind_speed_mph ?? "?"} mph @ ${b.wind_dir_deg ?? "?"}&deg;</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${b.observed_at}</div>
    </div>`;
  } else if (spot.buoy_error) {
    html += `<div class="buoy-box error-msg">${spot.buoy_error}</div>`;
  }

  if (spot.secondary_observation) {
    const s = spot.secondary_observation;
    html += `<div class="buoy-box">
      <h4>Local wind ${s.station_id} (Port Angeles)</h4>
      <div>Wind: ${s.wind_speed_mph ?? "?"} mph @ ${s.wind_dir_deg ?? "?"}&deg; (gust ${s.gust_mph ?? "?"} mph)</div>
      <div style="opacity:0.6;font-size:0.8em;margin-top:4px;">Observed ${s.observed_at}</div>
    </div>`;
  }

  if (!cc && !spot.buoy_observation && !spot.secondary_observation) {
    html += `<div class="buoy-box error-msg">No live buoy data available right now.</div>`;
  }

  // ---------------------------------------------------------------
  // SECTION 2: Forecast - the fetch/wind model built from historical
  // buoy-relationship analysis, projecting the next several days.
  // ---------------------------------------------------------------
  html += `<div class="section-heading">Forecast</div>
    <div class="section-sub">Local wind-fetch + upwind swell strike-signal (lagged ~3h) + historical buoy analogs</div>`;

  if (spot.historical_profile_summary) {
    const hp = spot.historical_profile_summary;
    const gd = hp.good_day_analysis;
    html += `<div class="buoy-box" style="opacity:0.85;">
      <h4>Historical calibration (buoy ${hp.reference_buoy_id})</h4>
      <div>Max swell period seen in last ~45 days: ${hp.max_period_s_observed ?? "?"} s
        (confirms groundswell doesn't reach this far into the strait)</div>
      <div>Forecast wind speeds are compared against ${hp.analog_buckets} real historical
        wind-speed buckets from this buoy's own wind/wave record.</div>
      ${gd && gd.wave_days ? `<div style="margin-top:6px;">Of the last ${gd.total_days} days,
        <strong>${gd.wave_days}</strong> had a wave &ge; ${gd.good_wave_height_ft}ft at some point &mdash;
        but wind was only actually aligned+strong enough to have produced it on
        <strong>${gd.good_wind_days} of those (${gd.good_wind_pct}%)</strong>. The rest likely came from a
        misaligned or short-lived gust, not a clean fetch-driven wave.</div>` : ""}
      ${gd && !gd.wave_days ? `<div style="margin-top:6px;">No day in the last ${gd.total_days} had a wave
        &ge; ${gd.good_wave_height_ft}ft at this buoy.</div>` : ""}
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
        let detail;
        if (h.model === "fetch_wind") {
          detail = `wind ${h.wind_speed_mph?.toFixed(0) ?? "?"}mph @ ${h.wind_dir_deg?.toFixed(0) ?? "?"}\u00b0 (fetch-driven wave)`;
          if (h.historical_wave_height_ft != null) {
            detail += ` &middot; similar past wind produced ~${h.historical_wave_height_ft.toFixed(1)}ft (${h.historical_analog_count} analogs)`;
          }
          if (h.predicted_strike_signal != null) {
            detail += h.predicted_strike ?
              ` &middot; <strong>swell strike signal ${h.predicted_strike_signal}</strong> (upwind swell projected to arrive)` :
              ` &middot; swell signal ${h.predicted_strike_signal} (below strike threshold)`;
          }
        } else {
          detail = `${h.swell_height_ft?.toFixed(1) ?? "?"}ft @ ${h.swell_period_s?.toFixed(0) ?? "?"}s, wind ${h.wind_speed_mph?.toFixed(0) ?? "?"}mph`;
        }
        html += `<div class="hour-row">
          <span class="hour-time">${fmtTime(h.time)}</span>
          <span class="hour-score-dot" style="background:${scoreColor(h.score)}"></span>
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
