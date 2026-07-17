/*
 * UpstreamWX PDF export template logic (FR-27, PRD §17.3).
 *
 * Externalized from briefing-pdf.html so the served (?print=1 offline fallback)
 * path satisfies a strict `script-src 'self'` CSP (SA-05) — an inline <script>
 * would be blocked under the enforced policy. The server-side render
 * (sitrep/pdf.py, headless Chromium over file://) loads this sibling file too;
 * it is added to that renderer's allowed-request set.
 *
 * Reads window.__BRIEFING__ / window.__DISPLAY_CONFIG__ when injected (live tool
 * path), else fetches the sample/config JSON for the standalone worked example.
 */
/* ── Severity + icon helpers ──────────────────────────────────────────────── */
const SEV_COLOR = {
  "sev-minimal": "var(--sev-minimal)",
  "sev-elevated": "var(--sev-elevated)",
  "sev-high": "var(--sev-high)",
  "sev-extreme": "var(--sev-extreme)",
  // Heat keys must match the engine's CSS class: heat-{HeatCategory.name.lower()},
  // i.e. heat-extreme_caution / heat-extreme_danger (NOT ext-caution).
  "heat-caution": "var(--heat-caution)",
  "heat-extreme_caution": "var(--heat-ext-caution)",
  "heat-danger": "var(--heat-danger)",
  "heat-extreme_danger": "var(--heat-ext-danger)",
};
const sevColor = (cls) => SEV_COLOR[cls] || "var(--p-ink-2)";

const HAZARD_LABEL = {
  flash_flood: "Flash flood",
  lightning: "Lightning",
  heat: "Heat",
  cold_wet: "Cold / wet",
};

/* Minimal inline glyphs (1.75px stroke, 24px grid), currentColor. */
const ICON = {
  flash_flood: '<path d="M3 7c2-2 4 2 6 0s4 2 6 0 4 2 6 0M3 13c2-2 4 2 6 0s4 2 6 0 4 2 6 0M3 19c2-2 4 2 6 0s4 2 6 0 4 2 6 0"/>',
  lightning: '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>',
  heat: '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2"/>',
  cold_wet: '<path d="M12 2v14M12 2 8 6M12 2l4 4M5 9l7 4 7-4M5 15l7 4 7-4"/>',
};
function hazIcon(hz, cls = "hz-icon") {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${ICON[hz] || ""}</svg>`;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* Approachable posture language — mirrors the app exactly (data/display-config.json
   merged into TIER_LABELS, applied via displayTier/displayLeadLabel/displayLogic in
   js/app.js). Loaded in boot() before the first render so the PDF speaks the same
   language as the app: tier labels become "Low/Moderate/High/Extreme Exposure" and
   heat's NWS categories fold into the same set (Caution→Low Exposure, Extreme
   Caution→Moderate Exposure, Danger→High Exposure, Extreme Danger→Extreme Exposure).
   Per-hazard colors (sev-* / heat ramp) are unchanged, also matching the app. */
let TIER_LABELS = { Minimal: "Minimal", Elevated: "Elevated", High: "High", Extreme: "Extreme" };
async function loadLabelConfig() {
  try {
    // Server-side render injects window.__DISPLAY_CONFIG__ to avoid the file://
    // cross-origin fetch restriction Chromium enforces when loading via file: URI.
    // The standalone / offline path falls back to fetching the JSON normally.
    const cfg = window.__DISPLAY_CONFIG__ ||
      await fetch("../data/display-config.json", { cache: "no-store" }).then((r) => r.json());
    if (cfg && cfg.tier_labels) Object.assign(TIER_LABELS, cfg.tier_labels);
    if (cfg && cfg.heat_labels) Object.assign(TIER_LABELS, cfg.heat_labels);
  } catch (e) {
    /* config unavailable — keep identity defaults (raw engine labels) */
  }
}

/* Sky condition emoji → short text labels. Headless Chromium on a server may
   lack emoji fonts; these concise ASCII labels are unambiguous and equally clear
   in a tabular forecast. Applied to all hourly cell values — no-op for numbers. */
const SKY_TEXT = {
  "☀️": "Clear", "🌤️": "PClear", "⛅": "PCloudy",
  "🌥️": "Cloudy", "☁️": "Ocast",
  "🌦️": "ShwrPoss", "🌧️": "Rain", "⛈️": "Tstm", "🌩️": "Tstm",
  "🌨️": "Flurries", "❄️": "Snow", "🌬️": "Windy", "💨": "Windy",
  "🌫️": "Fog",
};
const skyLabel = (v) => SKY_TEXT[String(v).trim()] ?? v;
function displayTier(t) {
  return TIER_LABELS[t] ?? t;
}
function displayLeadLabel(s) {
  return String(s).replace(/— (.+)$/, (_, t) => `— ${displayTier(t)}`);
}
function displayLogic(s) {
  const entries = Object.entries(TIER_LABELS)
    .filter(([k, v]) => k !== v)
    .sort(([a], [b]) => b.length - a.length); // longest key first: "Extreme Caution" before "Extreme"
  if (!entries.length) return String(s);
  const re = new RegExp(
    `\\b(${entries.map(([k]) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`,
    "g"
  );
  return String(s).replace(re, (m) => TIER_LABELS[m] ?? m);
}

/* Confidence → filled pips (Low 1 / Moderate 2 / High 3). */
function confPips(label) {
  const n = { Low: 1, Moderate: 2, High: 3 }[label] ?? 0;
  const dots = [0, 1, 2].map((i) => `<span class="conf__dot ${i < n ? "on" : ""}"></span>`).join("");
  return `<span class="conf"><span class="conf__dots">${dots}</span>${esc(label)}</span>`;
}

/* "1400–2100" → "14:00–21:00". The value comes from the (possibly hostile) briefing
   JSON and every caller drops the result straight into innerHTML, so it is HTML-escaped
   here; the digit reformatting is unaffected for legitimate clock windows. */
function fmtWindow(w) {
  if (!w) return "—";
  return esc(w).replace(/\b(\d{2})(\d{2})\b/g, "$1:$2");
}
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC",
  });
}
/* Extract the local wall-clock time (HH:MM) from an ISO datetime string directly,
   without converting through Date — the string already encodes the local offset
   (e.g. 2026-06-20T08:00:00-04:00) so slicing avoids the server-timezone shift. */
function isoLocalTime(iso) {
  const m = iso && iso.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : "—";
}
function isoLocalDate(iso) {
  const m = iso && iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso || "—";
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
function fmtWindowRange(b) {
  const tz = b.mission.timezone || "UTC";
  return `${isoLocalDate(b.mission.window_start)}, ${isoLocalTime(b.mission.window_start)}–${isoLocalTime(b.mission.window_end)} ${tz}`;
}

/* ── Section renderers ────────────────────────────────────────────────────── */
function renderMasthead(b) {
  const activity = b.mission.activity === "cave" ? "Cave" : (b.mission.is_slot ? "Slot canyon" : "Canyon");
  return `<div class="masthead">
    <img class="brand__logo" src="logo-light.png" alt="UpstreamWX — Weather Briefing" />
    <div class="masthead__doc">Document<strong>Expedition Weather Briefing</strong></div>
  </div>`;
}

function renderMission(b) {
  const m = b.mission;
  /* Number() coercion: these are numbers in a legitimate briefing; a non-numeric payload
     renders as "NaN" instead of reaching innerHTML as markup. */
  const domain = b.watershed?.area_sq_mi
    ? `Upstream basin ≈ ${Number(b.watershed.area_sq_mi)} mi²` + (b.roc ? ` · RoC ${Number(b.roc.radius_mi)} mi` : "")
    : "Point conditions";
  return `<div class="mission">
    <div>
      <h1 class="mission__title">${esc(m.name)}</h1>
      <div class="mission__sub">${esc(fmtWindowRange(b))} &nbsp;·&nbsp; ${Number(m.lat).toFixed(4)}, ${Number(m.lon).toFixed(4)} &nbsp;·&nbsp; ${esc(domain)}</div>
    </div>
    <dl class="meta-grid">
      <div class="meta-row"><dt>Activity</dt><dd>${b.mission.activity === "cave" ? "Cave" : (b.mission.is_slot ? "Slot canyon" : "Canyon")}</dd></div>
      <div class="meta-row"><dt>Generated</dt><dd>${esc(fmtWhen(b.generated_at))} UTC</dd></div>
      <div class="meta-row"><dt>GEFS cycle</dt><dd class="mono">${esc(b.cache_cycle)}</dd></div>
      <div class="meta-row"><dt>UpstreamWX</dt><dd class="mono">${esc(appVersion(b))}</dd></div>
    </dl>
  </div>`;
}

function renderBluf(b) {
  const ovClass = (b.bluf.find((x) => x.label === b.overall_posture)?.severity_class)
    || severityClassFor(b.overall_posture);
  return `<div class="section">
    <div class="section__head">Bottom line up front</div>
    <div class="bluf">
      <div class="bluf__posture">
        <span class="bluf__eyebrow">Overall posture</span>
        <span class="bluf__chip" style="background:${sevColor(ovClass)}">${esc(displayTier(b.overall_posture))}</span>
        <span class="bluf__conf">Confidence: <strong>${esc(b.overall_confidence)}</strong></span>
      </div>
      <div class="bluf__divider"></div>
      <p class="bluf__summary">${blufSummary(b)}</p>
    </div>
    ${b.framed ? `<div class="framed-banner">Plain-language summary framed by an AI language model. It narrates the engine's structured result and does not change any posture, tier, or confidence.</div>` : ""}
  </div>`;
}
function severityClassFor(label) {
  return { Minimal: "sev-minimal", Elevated: "sev-elevated", High: "sev-high", Extreme: "sev-extreme" }[label] || "";
}
/* The contract's `summary` is the optional Haiku framing — null when framing is
   off (the usual production case). Fall back to a deterministic recap of the
   engine's own postures so the BLUF box is never empty. Reference-only: it
   restates the structured result and issues no recommendation. */
function blufSummary(b) {
  if (b.summary) return esc(b.summary);
  const recap = (b.bluf || [])
    .map((h) => `${HAZARD_LABEL[h.hazard] || h.hazard} ${displayTier(h.label)}`)
    .join(" · ");
  return esc(
    `Overall posture ${displayTier(b.overall_posture)}, confidence ${b.overall_confidence}. ` +
    (recap ? `By hazard: ${recap}. ` : "") +
    "Planning reference only; verify against the official NWS sources and let conditions in the field overrule this briefing."
  );
}

function renderHazardTable(b) {
  const rows = b.bluf.map((h) => `<tr>
    <td class="hz-name">${hazIcon(h.hazard)}${esc(HAZARD_LABEL[h.hazard] || h.hazard)}</td>
    <td><span class="chip" style="background:${sevColor(h.severity_class)}">${esc(displayTier(h.label))}</span></td>
    <td>${confPips(h.confidence)}</td>
    <td class="mono">${h.is_persistent ? "Through-period" : fmtWindow(h.window)}</td>
  </tr>`).join("");
  return `<div class="section section--break">
    <div class="section__head">Hazard summary</div>
    <table class="hz-table">
      <thead><tr><th>Hazard</th><th>Posture</th><th>Confidence</th><th>Window of concern</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

function renderPhases(b) {
  const cards = b.phases.map((p) => `<div class="phase">
    <div class="phase__head">
      <div><span class="phase__name">${esc(p.phase)}</span> <span class="phase__win">${fmtWindow(p.window)}</span></div>
      <div class="phase__lead">${esc(displayLeadLabel(p.lead_label))}</div>
    </div>
    <div class="phase__body">
      <div class="phase__applies">Applies: ${esc(p.applicable)}</div>
      ${p.note ? `<div class="phase__note">${esc(p.note)}</div>` : ""}
    </div>
  </div>`).join("");
  return `<div class="section section--break">
    <div class="section__head">Phase breakdown <span class="hint">approach → technical → egress</span></div>
    <div class="phases">${cards}</div>
  </div>`;
}

function renderHazardDetail(b) {
  const cards = b.hazard_detail.map((h) => `<div class="hz-detail">
    <div class="hz-detail__head">
      <span class="hz-detail__name">${hazIcon(h.hazard)}${esc(HAZARD_LABEL[h.hazard] || h.hazard)}</span>
      <span class="chip" style="background:${sevColor(h.severity_class)}">${esc(displayTier(h.label))}</span>
      ${confPips(h.confidence)}
    </div>
    <ul>${h.drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
    ${h.assumptions.map((a) => `<div class="hz-detail__assume">${esc(a)}</div>`).join("")}
    <div class="hz-detail__logic">${esc(displayLogic(h.logic))}</div>
  </div>`).join("");
  // Force this section to start a fresh page so its heading is never orphaned
  // at the bottom of page 1 with the cards pushed to page 2.
  return `<div class="section page-break-before">
    <div class="section__head">Key drivers &amp; threshold logic</div>
    ${cards}
  </div>`;
}

function renderForecast(b) {
  const fx = b.forecast_hourly;
  if (!fx) return "";
  const head = `<tr><th>Hour</th>${fx.hours.map((h) => `<th>${fmtWindow(h).slice(0, 5)}</th>`).join("")}</tr>`;
  const body = fx.rows.map((r) => `<tr>
    <th>${esc(r.label)}</th>
    ${r.values.map((v) => `<td class="mono">${esc(skyLabel(v))}</td>`).join("")}
  </tr>`).join("");
  return `<div class="section section--break">
    <div class="section__head">Hourly forecast</div>
    <div class="fx-wrap"><table class="fx-table">
      <thead>${head}</thead><tbody>${body}</tbody>
    </table></div>
  </div>`;
}

function renderSourceData(b) {
  const ri = b.risk_inputs || {};
  const ws = b.watershed || {};
  /* Number() coercion mirrors renderMission: numeric risk inputs render as numbers (or
     "NaN") even if the posted JSON carried strings; kv() escapes every value regardless. */
  const ensemble = [
    ["GEFS P(precip/thunder)", ri.gefs_p_precip != null ? Number(ri.gefs_p_precip) + "%" : "—"],
    ["GEFS P(thunderstorm)", ri.gefs_p_tstm != null ? Number(ri.gefs_p_tstm) + "%" : "—"],
    ["CAPE", ri.cape_jkg != null ? Number(ri.cape_jkg) + " J/kg" : "—"],
    ["REFS P(QPF)", ri.refs_in_range && ri.refs_p_precip != null ? Number(ri.refs_p_precip) + "%" : "n/a"],
    ["REFS P(lightning)", ri.refs_in_range && ri.refs_p_lightning != null ? Number(ri.refs_p_lightning) + "%" : "n/a"],
    ["REFS cycle", ri.refs_cycle || "—"],
  ];
  const nws = [
    ["Flash Flood Warning", ri.flash_flood_warning ? "ACTIVE" : "none"],
    ["Flash Flood Watch", ri.flash_flood_watch ? "active" : "none"],
    ["Thunderstorm Warning", ri.thunderstorm_warning ? "ACTIVE" : "none"],
    ["SPC outlook", ri.spc_category || "—"],
    ["Upstream basin", ws.area_sq_mi ? Number(ws.area_sq_mi) + " mi²" : "—"],
  ];
  const kv = (rows) => rows.map(([k, v]) =>
    `<div class="kv__row"><span class="kv__k">${esc(k)}</span><span class="kv__v">${esc(v)}</span></div>`).join("");
  return `<div class="section section--break">
    <div class="section__head">Source data <span class="hint">drill-down</span></div>
    <div class="grid-2">
      <div class="kv">${kv(nws)}</div>
      <div class="kv">${kv(ensemble)}</div>
    </div>
  </div>`;
}

/* UpstreamWX software version: the live tool seeds it from /v1/health (the
   release stamped into version.json); standalone it falls back to "dev". */
function appVersion(b) {
  return b.app_version || b.version || "dev";
}

/* §17.3 reference-only footer (FR-29/FR-40). position:fixed in print, so this
   one element repeats the persistent disclaimer on EVERY page. */
function renderPageFoot(b) {
  return `<div class="page-foot">
    <span class="page-foot__note"><strong>UpstreamWX — planning reference only.</strong>
      Not an official forecast or warning. Verify against official NWS sources.
      The go/no-go decision always rests with you and your party.</span>
    <span class="page-foot__meta">UpstreamWX ${esc(appVersion(b))}<br>${esc(fmtWhen(b.generated_at))} UTC</span>
  </div>`;
}

function render(b) {
  document.title = `UpstreamWX — ${b.mission.name} briefing`;
  const content =
    renderMasthead(b) +
    renderMission(b) +
    renderBluf(b) +
    renderHazardTable(b) +
    renderPhases(b) +
    renderForecast(b) +
    renderHazardDetail(b) +
    renderSourceData(b);
  // Layout table: <thead>/<tfoot> are spacers that repeat on every printed page,
  // giving a consistent top and bottom margin. The footer text (.page-foot) lives
  // outside the table as a standalone element: position:fixed in @media print pins
  // it to the bottom of every Chromium PDF page, while @media screen hides it
  // (screen doesn't need a repeated footer).
  document.getElementById("sheet").innerHTML =
    `<table class="layout">
       <thead><tr><td><div class="run-top"></div></td></tr></thead>
       <tbody><tr><td>${content}</td></tr></tbody>
       <tfoot><tr><td><div class="run-foot"></div></td></tr></tfoot>
     </table>` +
    renderPageFoot(b);
}

/* The app hands off the live briefing through localStorage (one-shot, read and
   cleared here) and opens this page with ?print=1 to drive Save-as-PDF. */
const PDF_HANDOFF_KEY = "uwx.pdf.briefing";
function readHandoffBriefing() {
  // Same-tab navigation, so localStorage carries the briefing across the load.
  try {
    const raw = localStorage.getItem(PDF_HANDOFF_KEY);
    if (raw) {
      localStorage.removeItem(PDF_HANDOFF_KEY);
      return JSON.parse(raw);
    }
  } catch (e) {
    /* storage blocked — fall back to the template's own data path */
  }
  return null;
}

/* Toolbar (screen only): Back returns to the app — history.back() restores the
   PWA from bfcache; otherwise navigate to the app root. */
function wireToolbar() {
  const print = document.getElementById("tb-print");
  if (print) print.addEventListener("click", () => window.print());
  const back = document.getElementById("tb-back");
  if (back) back.addEventListener("click", () => {
    if (history.length > 1) history.back();
    else window.location.assign("../");
  });
}
wireToolbar();
/* Don't print before the masthead logo has decoded, or the PDF shows an empty
   logo box. */
function whenLogoReady() {
  const im = document.querySelector(".brand__logo");
  if (!im || (im.complete && im.naturalWidth > 0)) return Promise.resolve();
  return new Promise((resolve) => {
    im.addEventListener("load", resolve, { once: true });
    im.addEventListener("error", resolve, { once: true });
    setTimeout(resolve, 3000);
  });
}

/* Data precedence: injected (the headless example renderer) → app handoff →
   the committed worked example (standalone open). */
(async function boot() {
  try {
    await loadLabelConfig();
    let b = window.__BRIEFING__ || readHandoffBriefing();
    if (!b) {
      const res = await fetch("../data/sample-briefing.json", { cache: "no-store" });
      b = await res.json();
    }
    render(b);
    if (new URLSearchParams(location.search).has("print")) {
      await whenLogoReady();
      window.focus();
      window.print();
    }
  } catch (e) {
    document.getElementById("sheet").innerHTML =
      `<p style="color:#b00">Could not load briefing data: ${esc(e.message)}</p>`;
  }
})();
