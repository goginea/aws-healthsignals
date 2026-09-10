/* HealthSignals static dashboard sections (carried over from the demo AS-IS).
 * 'How the Influenza Signal Spreads' + 'Geographic Zoom' with baked static data.
 * Injected into the dashboard by app.js -> renderHealthSignalsStatic().
 * Generated from the deployed demo; do not hand-edit — regenerate if the demo changes.
 */
window.renderHealthSignalsStatic = function (container) {
  if (!container || container.__hsStaticRendered) return;
  container.__hsStaticRendered = true;
  container.innerHTML = STATIC_HTML;
  // Run the original Chart.js / Texas-map / zoom init logic verbatim.
  try { STATIC_INIT(); } catch (e) { console.error("static section init failed", e); }
};

var STATIC_HTML = "<section id=\"spread\">\n  <h2 class=\"text-2xl font-bold text-cyan-900 border-b-2 border-cyan-200 pb-2\">How the Influenza Signal Spreads</h2>\n  <p class=\"mt-3 text-slate-600 max-w-3xl\">HealthSignals models a <strong>metro-leads-rural</strong> pattern: sentinel metros cross the ED-visit threshold first, then calibration predicts when affinity-linked rural counties follow. Below is the <strong>real signal data</strong> from the Delphi fetch (weeks 29\u201334, 2026) and the affinity map used to predict downstream spread. <span class=\"text-slate-400\">(Metro/county positions are true geographic locations; values are the actual fetched signal.)</span></p>\n\n  <div class=\"grid md:grid-cols-2 gap-6 mt-6\">\n    <div class=\"bg-white rounded-lg shadow p-5\">\n      <h3 class=\"font-bold text-cyan-900\">Metro flu signal over time</h3>\n      <p class=\"text-sm text-slate-600 mb-3\">% of ED visits for influenza per sentinel metro. The dashed line is the detection threshold; San Antonio crosses highest at W34, becoming the season leader.</p>\n      <canvas id=\"tsChart\" height=\"220\"></canvas>\n    </div>\n    <div class=\"bg-white rounded-lg shadow p-5\">\n      <h3 class=\"font-bold text-cyan-900\">Texas spread map</h3>\n      <p class=\"text-sm text-slate-600 mb-3\">Circles = sentinel metros (size &amp; color by that week's flu %). Squares = subscribing rural counties. Lines = metro\u2192county affinity. Scrub the week to watch the signal build; the alert path lights up when the leader crosses threshold.</p>\n      <div class=\"flex items-center gap-3 mb-3\">\n        <span class=\"text-xs font-semibold text-slate-500\">Week</span>\n        <input id=\"weekSlider\" type=\"range\" min=\"0\" max=\"5\" value=\"5\" step=\"1\" class=\"flex-1 accent-rose-600\"/>\n        <span id=\"weekLabel\" class=\"text-sm font-bold text-cyan-800 w-14 text-right\">W34</span>\n        <button id=\"playBtn\" class=\"text-xs bg-cyan-700 hover:bg-cyan-800 text-white px-3 py-1 rounded\">\u25b6 Play</button>\n      </div>\n      <div id=\"mapStatus\" class=\"text-xs text-slate-500 mb-2 h-4\"></div>\n      <div id=\"txmap\"></div>\n      <div class=\"flex flex-wrap gap-3 mt-3 text-xs text-slate-600\">\n        <span class=\"flex items-center gap-1\"><span class=\"inline-block w-3 h-3 rounded-full bg-rose-500\"></span>metro (higher signal)</span>\n        <span class=\"flex items-center gap-1\"><span class=\"inline-block w-3 h-3 rounded-full bg-amber-400\"></span>metro (lower)</span>\n        <span class=\"flex items-center gap-1\"><span class=\"inline-block w-3 h-3 bg-slate-500\"></span>rural county</span>\n        <span class=\"flex items-center gap-1\"><span class=\"inline-block w-4 h-0.5 bg-rose-500\"></span>alert path</span>\n      </div>\n    </div>\n  </div>\n</section>\n<section id=\"zoom\">\n  <h2 class=\"text-2xl font-bold text-cyan-900 border-b-2 border-cyan-200 pb-2\">Geographic Zoom \u2014 County \u00b7 State \u00b7 National</h2>\n  <p class=\"mt-3 text-slate-600 max-w-3xl\">The same CDC NSSP surveillance, viewed at three granularities. <strong>Zoom out</strong> to national or state context, or <strong>zoom in</strong> to a single county's own measured ED-visit rate and trend. All values are CDC-published (national &amp; county from the NSSP trajectories dataset <span class=\"font-mono text-xs\">rdmq-nq56</span>; state from <span class=\"font-mono text-xs\">vutn-jzwm</span>) and are baked from the live pipeline's S3 output.</p>\n\n  <div class=\"bg-white rounded-lg shadow p-5 mt-6\">\n    <div class=\"flex flex-wrap items-center gap-4 mb-4\">\n      <div class=\"flex items-center gap-2\">\n        <span class=\"text-xs font-semibold text-slate-500 uppercase\">Level</span>\n        <div id=\"zoomLevels\" class=\"inline-flex rounded-md overflow-hidden border border-cyan-200\">\n          <button data-level=\"national\" class=\"zoom-lvl px-3 py-1 text-sm\">National</button>\n          <button data-level=\"state\" class=\"zoom-lvl px-3 py-1 text-sm border-l border-cyan-200\">State</button>\n          <button data-level=\"county\" class=\"zoom-lvl px-3 py-1 text-sm border-l border-cyan-200\">County</button>\n        </div>\n      </div>\n      <div class=\"flex items-center gap-2\">\n        <span class=\"text-xs font-semibold text-slate-500 uppercase\">Disease</span>\n        <select id=\"zoomDisease\" class=\"text-sm border border-slate-300 rounded px-2 py-1\"></select>\n      </div>\n      <div id=\"zoomCountyWrap\" class=\"flex items-center gap-2 hidden\">\n        <span class=\"text-xs font-semibold text-slate-500 uppercase\">County</span>\n        <select id=\"zoomCounty\" class=\"text-sm border border-slate-300 rounded px-2 py-1\"></select>\n      </div>\n      <div class=\"text-xs text-slate-400 ml-auto\" id=\"zoomWeek\"></div>\n    </div>\n\n    <div id=\"zoomCards\" class=\"grid sm:grid-cols-2 lg:grid-cols-3 gap-3\"></div>\n    <p class=\"text-xs text-slate-400 mt-4\" id=\"zoomNote\"></p>\n  </div>\n</section>";

function STATIC_INIT() {

const GEO = {"generated_at":"2026-09-10T02:09:51.301697Z","diseases":["covid","influenza","rsv"],"national":{"covid":{"value":0.62,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.19,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"rising","week_end":"2026-09-05"}},"state":{"texas":{"name":"Texas","covid":{"value":1.7,"trend":"rising","week_end":"2026-08-29"},"influenza":{"value":0.3,"trend":"rising","week_end":"2026-08-29"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-08-29"}}},"county":{"48325":{"name":"Medina","state_key":"texas","covid":{"value":2.37,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.23,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48201":{"name":"Harris","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48493":{"name":"Wilson","state_key":"texas","covid":{"value":2.37,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.23,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48187":{"name":"Guadalupe","state_key":"texas","covid":{"value":2.37,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.23,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48257":{"name":"Kaufman","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48113":{"name":"Dallas","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48021":{"name":"Bastrop","state_key":"texas","covid":{"value":0.94,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.3,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.02,"trend":"stable","week_end":"2026-09-05"}},"48143":{"name":"Erath County","state_key":"texas","covid":{"value":1.65,"trend":"stable","week_end":"2026-09-05"},"influenza":{"value":0.0,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48397":{"name":"Rockwall","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48339":{"name":"Montgomery","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48231":{"name":"Hunt","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48071":{"name":"Chambers","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48139":{"name":"Ellis","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48093":{"name":"Comanche County","state_key":"texas","covid":{"value":1.65,"trend":"stable","week_end":"2026-09-05"},"influenza":{"value":0.0,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48251":{"name":"Johnson","state_key":"texas","covid":{"value":1.71,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.06,"trend":"stable","week_end":"2026-09-05"}},"48085":{"name":"Collin","state_key":"texas","covid":{"value":1.05,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48439":{"name":"Tarrant","state_key":"texas","covid":{"value":1.71,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.06,"trend":"stable","week_end":"2026-09-05"}},"48291":{"name":"Liberty","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48157":{"name":"Fort Bend","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48209":{"name":"Hays","state_key":"texas","covid":{"value":2.43,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.56,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48121":{"name":"Denton","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.16,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48029":{"name":"Bexar","state_key":"texas","covid":{"value":2.37,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.23,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48091":{"name":"Comal","state_key":"texas","covid":{"value":1.62,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.19,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48167":{"name":"Galveston","state_key":"texas","covid":{"value":1.3,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.18,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48491":{"name":"Williamson","state_key":"texas","covid":{"value":0.94,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.3,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.02,"trend":"stable","week_end":"2026-09-05"}},"48055":{"name":"Caldwell","state_key":"texas","covid":{"value":2.43,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.56,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.03,"trend":"stable","week_end":"2026-09-05"}},"48453":{"name":"Travis","state_key":"texas","covid":{"value":0.94,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.3,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.02,"trend":"stable","week_end":"2026-09-05"}},"48039":{"name":"Brazoria","state_key":"texas","covid":{"value":1.83,"trend":"stable","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"stable","week_end":"2026-09-05"},"rsv":{"value":0.0,"trend":"stable","week_end":"2026-09-05"}},"48367":{"name":"Parker","state_key":"texas","covid":{"value":1.71,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.26,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.06,"trend":"stable","week_end":"2026-09-05"}},"48259":{"name":"Kendall","state_key":"texas","covid":{"value":2.37,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.23,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}},"48473":{"name":"Waller","state_key":"texas","covid":{"value":1.12,"trend":"rising","week_end":"2026-09-05"},"influenza":{"value":0.31,"trend":"rising","week_end":"2026-09-05"},"rsv":{"value":0.04,"trend":"stable","week_end":"2026-09-05"}}}};
(function() {
  const TREND_STYLE = {
    rising:    {label: "\u2191 Rising",    cls: "text-rose-600",  bg: "bg-rose-50 border-rose-200"},
    declining: {label: "\u2193 Declining", cls: "text-emerald-600", bg: "bg-emerald-50 border-emerald-200"},
    stable:    {label: "\u2192 Stable",    cls: "text-slate-600", bg: "bg-slate-50 border-slate-200"},
    unknown:   {label: "\u2014 Unknown",   cls: "text-slate-400", bg: "bg-slate-50 border-slate-200"}
  };
  const DISEASE_LABEL = {covid: "COVID-19", influenza: "Influenza", rsv: "RSV"};

  let level = "national";
  let disease = GEO.diseases.includes("influenza") ? "influenza" : GEO.diseases[0];

  // Populate disease selector
  const dsel = document.getElementById("zoomDisease");
  GEO.diseases.forEach(d => {
    const o = document.createElement("option");
    o.value = d; o.textContent = DISEASE_LABEL[d] || d;
    if (d === disease) o.selected = true;
    dsel.appendChild(o);
  });

  // Populate county selector (sorted by name)
  const csel = document.getElementById("zoomCounty");
  const counties = Object.entries(GEO.county)
    .map(([fips, o]) => ({fips, name: o.name}))
    .sort((a, b) => a.name.localeCompare(b.name));
  counties.forEach(c => {
    const o = document.createElement("option");
    o.value = c.fips; o.textContent = c.name;
    csel.appendChild(o);
  });
  let county = counties.length ? counties[0].fips : null;

  function card(title, sub, sig) {
    const t = TREND_STYLE[(sig && sig.trend) || "unknown"];
    const val = (sig && sig.value != null) ? sig.value.toFixed(2) + "%" : "n/a";
    return '<div class="rounded-lg border ' + t.bg + ' p-4">' +
      '<div class="text-xs font-semibold text-slate-500 uppercase">' + title + '</div>' +
      (sub ? '<div class="text-xs text-slate-400">' + sub + '</div>' : '') +
      '<div class="mt-2 text-3xl font-extrabold text-cyan-900">' + val + '</div>' +
      '<div class="text-sm text-slate-500">ED visits</div>' +
      '<div class="mt-2 font-semibold ' + t.cls + '">' + t.label + '</div>' +
    '</div>';
  }

  function highlightLevel() {
    document.querySelectorAll(".zoom-lvl").forEach(b => {
      const on = b.dataset.level === level;
      b.className = "zoom-lvl px-3 py-1 text-sm" +
        (b.dataset.level !== "national" ? " border-l border-cyan-200" : "") +
        (on ? " bg-cyan-700 text-white" : " bg-white text-cyan-800 hover:bg-cyan-50");
    });
  }

  function render() {
    highlightLevel();
    document.getElementById("zoomCountyWrap").classList.toggle("hidden", level !== "county");
    const cards = document.getElementById("zoomCards");
    const dlabel = DISEASE_LABEL[disease] || disease;
    let html = "", week = "", note = "";

    if (level === "national") {
      const sig = GEO.national[disease];
      html = card("United States", dlabel, sig);
      week = sig ? "Week ending " + sig.week_end : "";
      note = "National ED-visit rate across all reporting facilities — the widest zoom-out.";
    } else if (level === "state") {
      const states = Object.entries(GEO.state);
      html = states.map(([sk, o]) => card(o.name || sk, dlabel, o[disease])).join("");
      const any = states.length ? states[0][1][disease] : null;
      week = any ? "Week ending " + any.week_end : "";
      note = "State-level ED-visit rate (CDC-published, vutn-jzwm). Add more states to the pipeline config to populate this view.";
    } else {
      const c = GEO.county[county];
      const sig = c ? c[disease] : null;
      // Show the selected county alongside its national + state context for comparison
      html =
        card((c ? c.name : "County"), "County · " + dlabel, sig) +
        card("Texas", "State context", (GEO.state.texas || {})[disease]) +
        card("United States", "National context", GEO.national[disease]);
      week = sig ? "Week ending " + sig.week_end : "";
      note = "Zoom-in to one county's OWN measured ED visits (rdmq-nq56), shown next to its state and national context.";
    }

    cards.innerHTML = html || '<div class="text-slate-400 text-sm">No data at this level.</div>';
    document.getElementById("zoomWeek").textContent = week;
    document.getElementById("zoomNote").textContent = note;
  }

  document.querySelectorAll(".zoom-lvl").forEach(b =>
    b.addEventListener("click", () => { level = b.dataset.level; render(); }));
  dsel.addEventListener("change", e => { disease = e.target.value; render(); });
  csel.addEventListener("change", e => { county = e.target.value; render(); });
  render();
})();


const VIZ = {"weeks": ["W29", "W30", "W31", "W32", "W33", "W34"], "metros": {"Houston": {"lat": 29.76, "lon": -95.37, "series": [0.11, 0.11, 0.14, 0.13, 0.19, 0.27], "msa": "26420"}, "DFW": {"lat": 32.78, "lon": -96.8, "series": [0.11, 0.09, 0.15, 0.15, 0.12, 0.25], "msa": "19100"}, "Austin": {"lat": 30.27, "lon": -97.74, "series": [0.1, 0.12, 0.13, 0.13, 0.13, 0.31], "msa": "12420"}, "San Antonio": {"lat": 29.42, "lon": -98.49, "series": [0.12, 0.11, 0.07, 0.08, 0.14, 0.34], "msa": "41700"}}, "counties": {"Erath County": {"lat": 32.22, "lon": -98.22, "fips": "48143", "affinity": {"19100": 0.75, "26420": 0.25}, "primary": "19100"}, "Brown County": {"lat": 31.77, "lon": -98.99, "fips": "48049", "affinity": {"19100": 0.6, "41700": 0.4}, "primary": "19100"}, "Comanche County": {"lat": 31.9, "lon": -98.6, "fips": "48093", "affinity": {"19100": 0.65, "12420": 0.35}, "primary": "19100"}}, "threshold": 0.2, "leader": "San Antonio", "alerted_county": "Brown County"};

// ---- Time-series line chart ----
(function() {
  const metros = VIZ.metros;
  const colors = { "Houston":"#0891b2", "DFW":"#7c3aed", "Austin":"#059669", "San Antonio":"#e11d48" };
  const datasets = Object.keys(metros).map(name => ({
    label: name,
    data: metros[name].series,
    borderColor: colors[name] || "#334155",
    backgroundColor: colors[name] || "#334155",
    borderWidth: name === VIZ.leader ? 3 : 2,
    tension: 0.3,
    pointRadius: 3
  }));
  // threshold line
  datasets.push({
    label: "Threshold ("+VIZ.threshold+"%)",
    data: VIZ.weeks.map(() => VIZ.threshold),
    borderColor: "#94a3b8", borderDash: [6,4], borderWidth: 1.5, pointRadius: 0, fill: false
  });
  new Chart(document.getElementById("tsChart"), {
    type: "line",
    data: { labels: VIZ.weeks, datasets },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: { y: { title: { display: true, text: "% ED visits (influenza)" }, beginAtZero: true } }
    }
  });
})();

// ---- Texas SVG spread map (week-aware) ----
(function() {
  const W = 520, H = 420;
  const LON_MIN = -106.7, LON_MAX = -93.3, LAT_MIN = 25.6, LAT_MAX = 36.6;
  function proj(lat, lon) {
    const x = (lon - LON_MIN) / (LON_MAX - LON_MIN) * (W - 40) + 20;
    const y = (LAT_MAX - lat) / (LAT_MAX - LAT_MIN) * (H - 40) + 20;
    return [x, y];
  }
  const TX = [[-106.6,32.0],[-103.0,32.0],[-103.0,36.5],[-100.0,36.5],[-100.0,34.6],[-99.6,34.2],[-96.9,33.9],[-94.5,33.6],[-94.0,33.3],[-94.1,31.9],[-93.5,31.0],[-93.7,29.7],[-95.1,29.1],[-96.5,28.2],[-97.4,27.3],[-97.1,25.9],[-99.1,26.4],[-100.8,29.3],[-102.3,29.9],[-103.3,29.0],[-104.9,30.6],[-106.5,31.8],[-106.6,32.0]];
  const outline = TX.map(p => proj(p[1], p[0]).join(",")).join(" ");
  const metros = VIZ.metros, counties = VIZ.counties;
  // global max across ALL weeks so circle scaling is stable while scrubbing
  const allVals = Object.keys(metros).flatMap(n => metros[n].series);
  const maxV = Math.max(...allVals);
  function metroColor(v) { return v >= VIZ.threshold ? "#e11d48" : "#f59e0b"; }
  function metroR(v) { return 6 + (v / maxV) * 15; }
  const msaToName = {}; Object.keys(metros).forEach(n => msaToName[metros[n].msa] = n);

  function render(wk) {
    // leader for this week = highest metro that is above threshold (else none)
    let leaderName = null, leaderVal = -1;
    Object.keys(metros).forEach(n => {
      const v = metros[n].series[wk];
      if (v >= VIZ.threshold && v > leaderVal) { leaderVal = v; leaderName = n; }
    });
    const alertActive = leaderName === VIZ.leader; // alert path shown only when San Antonio leads

    let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" style="max-width:520px">';
    svg += '<polygon points="'+outline+'" fill="#f1f5f9" stroke="#cbd5e1" stroke-width="1.5"/>';
    // affinity lines
    Object.keys(counties).forEach(cn => {
      const c = counties[cn]; const [cx,cy] = proj(c.lat, c.lon);
      Object.keys(c.affinity).forEach(msa => {
        const mn = msaToName[msa]; if(!mn) return;
        const [mx,my] = proj(metros[mn].lat, metros[mn].lon);
        const isAlertPath = alertActive && (cn === VIZ.alerted_county && mn === VIZ.leader);
        const w = c.affinity[msa];
        svg += '<line x1="'+mx+'" y1="'+my+'" x2="'+cx+'" y2="'+cy+'" stroke="'+(isAlertPath?"#e11d48":"#cbd5e1")+'" stroke-width="'+(isAlertPath?3:(0.8+w*1.5))+'" stroke-dasharray="'+(isAlertPath?"":"3,3")+'" opacity="'+(isAlertPath?0.9:0.5)+'"/>';
      });
    });
    // counties
    Object.keys(counties).forEach(cn => {
      const c = counties[cn]; const [x,y] = proj(c.lat, c.lon);
      const alerted = alertActive && cn === VIZ.alerted_county;
      svg += '<rect x="'+(x-5)+'" y="'+(y-5)+'" width="10" height="10" fill="'+(alerted?"#0f172a":"#64748b")+'" stroke="#fff" stroke-width="1"/>';
      svg += '<text x="'+(x+8)+'" y="'+(y+3)+'" font-size="9" fill="#334155">'+cn.replace(" County","")+(alerted?" \u2b50":"")+'</text>';
    });
    // metros
    Object.keys(metros).forEach(mn => {
      const v = metros[mn].series[wk]; const [x,y] = proj(metros[mn].lat, metros[mn].lon);
      const isLeader = mn === leaderName;
      svg += '<circle cx="'+x+'" cy="'+y+'" r="'+metroR(v)+'" fill="'+metroColor(v)+'" fill-opacity="0.75" stroke="'+(isLeader?"#0f172a":"#fff")+'" stroke-width="'+(isLeader?2.5:1)+'"/>';
      svg += '<text x="'+x+'" y="'+(y-metroR(v)-3)+'" font-size="10" font-weight="bold" text-anchor="middle" fill="#0f172a">'+mn+' '+v.toFixed(2)+'%'+(isLeader?" (leader)":"")+'</text>';
    });
    svg += '</svg>';
    document.getElementById("txmap").innerHTML = svg;
    document.getElementById("weekLabel").textContent = VIZ.weeks[wk];
    const st = document.getElementById("mapStatus");
    if (leaderName) st.innerHTML = '<span class="text-rose-600 font-semibold">'+VIZ.weeks[wk]+': '+leaderName+' crossed threshold ('+leaderVal.toFixed(2)+'%)'+(alertActive?' \u2192 alert dispatched to '+VIZ.alerted_county:'')+'</span>';
    else st.textContent = VIZ.weeks[wk]+': no metro above threshold — monitoring';
  }

  const slider = document.getElementById("weekSlider");
  slider.addEventListener("input", e => render(parseInt(e.target.value)));
  render(VIZ.weeks.length - 1);

  // Play button: animate through the weeks
  let timer = null;
  document.getElementById("playBtn").addEventListener("click", function() {
    if (timer) { clearInterval(timer); timer = null; this.textContent = "▶ Play"; return; }
    this.textContent = "⏸ Pause";
    let wk = 0; slider.value = 0; render(0);
    const btn = this;
    timer = setInterval(() => {
      wk++;
      if (wk >= VIZ.weeks.length) { clearInterval(timer); timer = null; btn.textContent = "▶ Play"; return; }
      slider.value = wk; render(wk);
    }, 900);
  });
})();

}
