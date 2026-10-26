"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = { get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };
let KEY = store.get("copilot_key");
let cfg = { tile_url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: "© OpenStreetMap contributors", center: [24.4539, 54.3773], zoom: 11 };
let gmap = null, gLay = [], mapReady = false;
let facilities = [], regions = [], lmap = null, facLayer = null, gazLayer = null, candLayer = null, lastPin = "";
let callId = null, ws = null, S = null, wallWs = null, gaz = [], lastCueCount = 0;

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(KEY ? { "X-API-Key": KEY } : {}), ...(opts.headers || {}) } });
  if (!r.ok) { const t = await r.text(); throw new Error(r.status + " " + t); }
  return r.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
const wsUrl = (p) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${p}${KEY ? "?token=" + encodeURIComponent(KEY) : ""}`;

// ------------------------------------------------------------------ tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== name));
  if (name === "taker" && lmap) setTimeout(() => lmap.invalidateSize(), 50);
  if (name === "taker" && gmap) setTimeout(() => google.maps.event.trigger(gmap, "resize"), 50);
  if (name === "wall") openWall();
  if (name === "admin") loadAdmin();
}

// -------------------------------------------------------------- call control
async function initAI() {
  try { const st = await api("/api/agent/status"); if (st.call_data_shared) { $("#aiDraft").hidden = false;
    $("#aiDraft").onclick = async () => { if (!callId) return; try { await post(`/api/calls/${callId}/ai-summary`); } catch (e) { alert(e.message); } }; } } catch {}
}
async function init() {
  initAI();
  const sc = await api("/api/scenarios");
  $("#scenario").innerHTML = sc.map((s) => `<option value="${esc(s.id)}">${esc(s.title)}</option>`).join("");
  gaz = await api("/api/gazetteer");
  try { cfg = await api("/api/config"); [facilities, regions] = await Promise.all([api("/api/facilities"), api("/api/regions")]); } catch {}
  await initGoogle(); mapReady = true; if (S) drawMap();
  const q = new URLSearchParams(location.search).get("call");
  if (q) connect(q);
}
$("#startBtn").onclick = async () => {
  const r = await post("/api/calls", { scenario: $("#scenario").value, speed: parseFloat($("#speed").value) || 3 });
  connect(r.call_id);
};
$("#endBtn").onclick = async () => { if (callId) { S = await post(`/api/calls/${callId}/end`); render(); } };
function connect(id) {
  if (ws) ws.close();
  callId = id; lastCueCount = 0; $("#callId").textContent = id;
  ws = new WebSocket(wsUrl("/ws/calls/" + id));
  ws.onmessage = (m) => { const e = JSON.parse(m.data); if (e.type === "state") { S = e.state; render(); } };
}
async function ensureCall() {
  if (callId && S && S.status === "active") return callId;
  const r = await post("/api/calls", {}); connect(r.call_id);
  await new Promise((res) => setTimeout(res, 300));
  return r.call_id;
}
$("#typeForm").onsubmit = async (e) => {
  e.preventDefault();
  const t = $("#typeText").value.trim(); if (!t) return;
  const id = await ensureCall();
  await post(`/api/calls/${id}/segments`, { speaker: "caller", text: t, final: true, confidence: 0.95 });
  $("#typeText").value = "";
};

// ----------------------------------------- live mic through browser ASR (Chrome)
let rec = null;
$("#micBtn").onclick = async () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert("This browser has no Web Speech API. Use Chrome, or POST segments from your own ASR."); return; }
  if (rec) { rec.stop(); return; }
  const id = await ensureCall();
  rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.lang = $("#micLang").value;
  rec.onresult = (ev) => {
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      post(`/api/calls/${id}/segments`, { speaker: "caller", text: r[0].transcript, final: r.isFinal, confidence: r[0].confidence || 0.8 }).catch(() => {});
    }
  };
  rec.onend = () => { rec = null; $("#micBtn").textContent = "Mic (browser ASR)"; };
  rec.start(); $("#micBtn").textContent = "Stop mic";
};

// ------------------------------------------------------------------ render
function hl(text, evid) {
  const ranges = [];
  for (const e of (evid || []).filter(Boolean)) { const i = text.indexOf(e); if (i >= 0) ranges.push([i, i + e.length]); }
  ranges.sort((a, b) => a[0] - b[0]);
  let out = "", pos = 0;
  for (const [s, e] of ranges) { if (s < pos) continue; out += esc(text.slice(pos, s)) + "<mark>" + esc(text.slice(s, e)) + "</mark>"; pos = e; }
  return out + esc(text.slice(pos));
}
function render() {
  if (!S) return;
  const shadow = S.mode === "shadow";
  $("#modeBadge").textContent = shadow ? "SHADOW MODE: AI hidden" : "ASSIST MODE";
  $("#modeBadge").className = "badge " + (shadow ? "shadow" : "");
  $("#langInfo").textContent = S.lang !== "unknown" ? `· ${S.lang}${S.dialect ? " / " + S.dialect : ""}${S.code_switched ? " · code-switched" : ""}` : "";
  const evid = Object.values(S.cues).map((c) => c.evidence);
  const rows = [...S.segments, ...Object.values(S.partials)];
  const t = $("#transcript"), atBottom = t.scrollTop + t.clientHeight >= t.scrollHeight - 30;
  t.innerHTML = rows.map((m) => `<div class="msg ${m.speaker} ${m.final ? "" : "partial"} ${m.confidence < 0.7 ? "lowq" : ""}" dir="auto">
      <small>${m.speaker}${m.confidence < 0.7 ? " · low audio quality, verify" : ""}</small>${hl(m.text, m.speaker === "calltaker" ? [] : evid)}</div>`).join("");
  if (atBottom) t.scrollTop = t.scrollHeight;

  const cues = Object.values(S.cues);
  $("#cues").innerHTML = cues.map((c) => `<div class="cue ${c.level} ${c.acked ? "acked" : "new"}"><b>⚠ ${esc(c.label)}</b><span class="muted">“${esc(c.evidence)}”</span>${c.acked ? "" : `<button data-cue="${esc(c.id)}">Acknowledge</button>`}</div>`).join("");
  document.querySelectorAll("[data-cue]").forEach((b) => (b.onclick = () => post(`/api/calls/${callId}/cues/${b.dataset.cue}/ack`)));
  if (cues.length > lastCueCount) beep();
  lastCueCount = cues.length;

  const sv = S.severity;
  $("#sevBadge").textContent = sv.level; $("#sevBadge").className = "sev " + sv.level;
  $("#sevReasons").innerHTML = sv.reasons.length ? `Score ${sv.score}: ` + sv.reasons.map((r) => `${esc(r.label)} +${r.points}`).join(" · ") : esc(sv.name);
  $("#summary").textContent = S.summary || (shadow ? "AI output hidden in shadow mode." : "Waiting for call…");

  const order = ["incident_type", "priority", "location", "address_detail", "persons", "ages", "injuries", "weapons", "vehicles", "plates", "hazards", "caller_number", "language", "narrative"];
  const fields = Object.values(S.fields).sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  $("#fields").innerHTML = fields.map((f) => {
    const pct = Math.round(f.confidence * 100), low = f.confidence < 0.5;
    const alt = f.meta && f.meta.ai_alt != null ? `<div class="alt">AI now suggests: ${esc(f.meta.ai_alt)}</div>` : "";
    return `<div class="field ${f.status}" title="${esc(f.explanation)}"><div class="lbl">${esc(f.label)}</div>
      <div><div class="val" dir="auto">${esc(f.value)}</div>${low ? '<span class="alt">low confidence, verify</span>' : ""}${alt}
      <div class="conf ${low ? "low" : ""}"><i style="width:${pct}%"></i></div></div>
      <div class="acts">${S.status === "active" ? `<button data-a="accept" data-k="${f.key}">✓</button><button data-a="edit" data-k="${f.key}">✎</button><button data-a="reject" data-k="${f.key}">✕</button>` : ""}</div></div>`;
  }).join("");
  document.querySelectorAll("#fields [data-a]").forEach((b) => (b.onclick = async () => {
    let value = null;
    if (b.dataset.a === "edit") { value = prompt("New value", S.fields[b.dataset.k].value); if (value === null) return; }
    try { await post(`/api/calls/${callId}/fields/${b.dataset.k}`, { action: b.dataset.a, value }); } catch (e) { alert(e.message); }
  }));

  const q = S.next_question;
  $("#nextQ").innerHTML = q ? `<div dir="auto"><b>${esc(q[q.primary])}</b></div><div class="muted" dir="auto">${esc(q[q.primary === "ar" ? "en" : "ar"])}</div>` : (S.status === "active" ? "All protocol questions answered." : "–");
  $("#checklist").innerHTML = S.checklist.map((i) => `<li class="${i.done ? "done" : ""}">${esc(i.question.en)}</li>`).join("");
  $("#instr").innerHTML = S.instructions.map((i) => `<div class="instr"><b>${esc(i.cue.replace("_", " "))}</b><div>${esc(i.en)}</div><div dir="rtl">${esc(i.ar)}</div><div class="muted">${esc(i.note)}</div></div>`).join("");
  drawCase();
  drawMap();
  const tm = S.timings_ms || {};
  $("#timings").textContent = `Processing ms: total ${tm.total ?? "–"} · extract ${tm.extract ?? "–"} · location ${tm.location ?? "–"} · reason ${tm.reason ?? "–"} · sop ${tm.sop ?? "–"}   (in-process compute only)`;
}
function beep() {
  try { const a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(); o.frequency.value = 880; o.connect(a.destination); o.start(); o.stop(a.currentTime + 0.15); } catch {}
}
// ---- Google Maps provider (key from /api/config; falls back to Leaflet / OpenStreetMap when absent or rejected)
function loadGoogle(key) {
  return new Promise((res, rej) => { window.__gmReady = res; window.gm_authFailure = () => rej(new Error("key rejected"));
    const sc = document.createElement("script"); sc.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&loading=async&libraries=marker&callback=__gmReady`;
    sc.async = true; sc.onerror = () => rej(new Error("load failed")); document.head.appendChild(sc); setTimeout(() => rej(new Error("timeout")), 10000); });
}
const gEl = (html, css) => { const d = document.createElement("div"); d.style.transform = "translateY(50%)"; const i = document.createElement("div"); i.style.cssText = css; d.appendChild(i); if (html) i.textContent = html; return d; };
async function initGoogle() {
  if (!cfg.google_maps_key) return;
  try {
    await loadGoogle(cfg.google_maps_key);
    gmap = new google.maps.Map($("#leaflet"), { center: { lat: cfg.center[0], lng: cfg.center[1] }, zoom: cfg.zoom, mapId: "DEMO_MAP_ID", gestureHandling: "greedy" });
    const dot = (lat, lon, col, size, title) => new google.maps.marker.AdvancedMarkerElement({ map: gmap, position: { lat, lng: lon }, title, content: gEl("", `width:${size}px;height:${size}px;border-radius:50%;background:${col};border:2px solid #fff`) });
    gaz.forEach((g) => dot(g.lat, g.lon, "#64748b", 9, `${g.name} · ${g.name_ar}`));
    const FC = { hospital: "#22c55e", police: "#3b82f6", civil_defence: "#f97316" };
    facilities.forEach((f) => dot(f.lat, f.lon, FC[f.kind], 12, `${f.name} (${f.kind.replace("_", " ")})`));
  } catch (e) { gmap = null; }
}
function drawGoogle(cands) {
  gLay.forEach((o) => (o.setMap ? o.setMap(null) : (o.map = null))); gLay = [];
  cands.forEach((c, i) => { const col = i === 0 ? "#ef4444" : "#f59e0b", sz = i === 0 ? 20 : 14;
    gLay.push(new google.maps.marker.AdvancedMarkerElement({ map: gmap, position: { lat: c.lat, lng: c.lon }, title: `${c.name} ${Math.round(c.confidence * 100)}%`, zIndex: 10 - i, content: gEl("", `width:${sz}px;height:${sz}px;border-radius:50%;background:${col}66;border:3px solid ${col}`) })); });
  ((S.case && S.case.facilities) || []).forEach((f) => gLay.push(new google.maps.Polyline({ map: gmap, path: [{ lat: S.case.location.lat, lng: S.case.location.lon }, { lat: f.lat, lng: f.lon }], strokeColor: "#64748b", strokeOpacity: .8, strokeWeight: 2 })));
  if (S.aml) gLay.push(new google.maps.Circle({ map: gmap, center: { lat: S.aml.lat, lng: S.aml.lon }, radius: S.aml.accuracy_m || 100, strokeColor: "#3b82f6", strokeWeight: 1, fillColor: "#3b82f6", fillOpacity: .12 }));
  const key = cands[0] ? `${S.call_id}:${cands[0].name}` : "";
  if (key && key !== lastPin) { lastPin = key; gmap.setCenter({ lat: cands[0].lat, lng: cands[0].lon }); gmap.setZoom(14); }
}
function drawMap() {
  const cands = S.candidates || [];
  $("#cands").innerHTML = cands.length ? cands.map((c, i) => `${i === 0 ? "<b>" : ""}${esc(c.name)} ${Math.round(c.confidence * 100)}%${i === 0 ? "</b>" : ""}${c.note ? " · " + esc(c.note) : ""}`).join("<br>") : "No location yet.";
  if (!mapReady) return;
  if (gmap) return drawGoogle(cands);
  if (!window.L) { $("#leaflet").classList.add("hidden"); $("#map").classList.remove("hidden"); return drawSchematic(cands); }
  if (!lmap) {
    lmap = L.map("leaflet", { zoomControl: true }).setView(cfg.center, cfg.zoom);
    L.tileLayer(cfg.tile_url, { maxZoom: 19, attribution: cfg.attribution }).addTo(lmap);
    gazLayer = L.layerGroup().addTo(lmap); facLayer = L.layerGroup().addTo(lmap); candLayer = L.layerGroup().addTo(lmap);
    regions.forEach((r) => L.circleMarker([r.lat, r.lon], { radius: 1, opacity: 0, fillOpacity: 0, interactive: false }).bindTooltip(`${r.name} · ${r.name_ar}`, { permanent: true, direction: "top", className: "regionlbl" }).addTo(lmap));
    const FC = { hospital: "#22c55e", police: "#3b82f6", civil_defence: "#f97316" };
    facilities.forEach((f) => L.circleMarker([f.lat, f.lon], { radius: 5, color: FC[f.kind], weight: 2, fillColor: FC[f.kind], fillOpacity: 0.8 }).bindTooltip(`${f.name} (${f.kind.replace("_", " ")})`).addTo(facLayer));
    gaz.forEach((g) => L.circleMarker([g.lat, g.lon], { radius: 4, color: "#64748b", weight: 1, fillOpacity: 0.7 }).bindTooltip(`${g.name} · ${g.name_ar}`).addTo(gazLayer));
  }
  candLayer.clearLayers();
  cands.forEach((c, i) => {
    const col = i === 0 ? "#ef4444" : "#f59e0b";
    L.circleMarker([c.lat, c.lon], { radius: i === 0 ? 10 : 7, color: col, weight: 3, fillColor: col, fillOpacity: 0.35 })
      .bindTooltip(`${c.name} ${Math.round(c.confidence * 100)}%`, { permanent: i === 0 }).addTo(candLayer);
  });
  ((S.case && S.case.facilities) || []).forEach((f) => L.polyline([[S.case.location.lat, S.case.location.lon], [f.lat, f.lon]], { color: "#64748b", weight: 2, dashArray: "4 4" }).addTo(candLayer));
  if (S.aml) L.circle([S.aml.lat, S.aml.lon], { radius: S.aml.accuracy_m || 100, color: "#3b82f6", weight: 1, fillOpacity: 0.12 }).addTo(candLayer);
  const key = cands[0] ? `${S.call_id}:${cands[0].name}` : "";
  if (key && key !== lastPin) { lastPin = key; lmap.setView([cands[0].lat, cands[0].lon], 14); }
}
function drawCase() {
  const c = S.case; if (!c) return;
  const loc = c.location, row = (k, v) => (v ? `<dt>${k}</dt><dd dir="auto">${v}</dd>` : "");
  let h = `<div class="cn">${esc(c.case_number)} <span class="chip">${esc(c.status)}</span></div><dl>`;
  if (loc) {
    h += row("Region", `${esc(loc.region)} <span class="muted">${esc(loc.region_ar || "")}</span>`);
    h += row("Area", esc(loc.area));
    h += row("Location", `${esc(loc.name)}${loc.detail ? ", " + esc(loc.detail) : ""} <span class="muted">(${Math.round(loc.confidence * 100)}%, ${esc(loc.status)})</span>`);
    h += row("Coordinates", `${loc.lat.toFixed(4)}, ${loc.lon.toFixed(4)}`);
  }
  if (c.incident) h += row("Incident", `${esc(c.incident.label)} <span class="muted">${esc(c.incident.code || "")}</span>`);
  if (c.priority) h += row("Priority", esc(c.priority.value));
  const p = c.people || {};
  if (p.persons || p.ages || p.injuries) h += row("People", [p.persons && p.persons + " involved", p.ages && "age " + p.ages, p.injuries].filter(Boolean).map(esc).join("; "));
  if (c.agencies && c.agencies.length) h += row("Agencies", c.agencies.map((a) => `<span class="chip">${esc(a.name)} ${esc(a.hotline || "")}</span>`).join(""));
  (c.facilities || []).forEach((f) => (h += row("Nearest " + f.kind.replace("_", " "), `${esc(f.name)} <span class="muted">${f.distance_km} km straight-line</span>`)));
  h += row("Caller", [c.caller.number, c.caller.lang !== "unknown" ? c.caller.lang + (c.caller.dialect ? "/" + c.caller.dialect : "") : ""].filter(Boolean).map(esc).join(" · "));
  h += "</dl>" + (c.flags || []).map((f) => `<span class="chip warn">${esc(f)}</span>`).join("");
  h += `<ul class="tl">${c.timeline.map((e) => `<li><b>+${e.t_s}s</b> ${esc(e.event)}</li>`).join("")}</ul>`;
  $("#caseCard").innerHTML = h;
}
function drawSchematic(cands) {
  const W = 300, H = 220, pad = 16, lats = gaz.map((g) => g.lat), lons = gaz.map((g) => g.lon);
  const [la0, la1, lo0, lo1] = [Math.min(...lats), Math.max(...lats), Math.min(...lons), Math.max(...lons)];
  const px = (lo) => pad + ((lo - lo0) / (lo1 - lo0 || 1)) * (W - 2 * pad), py = (la) => H - pad - ((la - la0) / (la1 - la0 || 1)) * (H - 2 * pad);
  $("#map").innerHTML = gaz.map((g) => `<circle cx="${px(g.lon)}" cy="${py(g.lat)}" r="3" fill="#8b98a5"><title>${esc(g.name)}</title></circle>`).join("")
    + cands.map((c, i) => `<circle cx="${px(c.lon)}" cy="${py(c.lat)}" r="${i ? 6 : 8}" fill="${i ? "#f59e0b" : "#ef4444"}" fill-opacity=".4"><title>${esc(c.name)}</title></circle>`).join("");
}

// -------------------------------------------------------------- supervisor wall
function openWall() {
  if (wallWs) return;
  wallWs = new WebSocket(wsUrl("/ws/wall"));
  wallWs.onmessage = (m) => { const e = JSON.parse(m.data); if (e.type === "wall") drawWall(e.calls); };
  wallWs.onclose = () => (wallWs = null);
}
function drawWall(calls) {
  $("#wallTable tbody").innerHTML = calls.map((c) => `<tr class="row" data-id="${esc(c.call_id)}">
    <td>${esc(c.case_number)}</td><td>${esc(c.region ?? "–")}</td><td>${c.severity ? `<span class="sev ${c.severity.level}" style="font-size:13px;padding:2px 8px">${c.severity.level}</span>` : "–"}</td>
    <td>${esc(c.incident ?? "–")}</td><td>${esc(c.location ?? "–")}</td><td>${esc(c.lang)}${c.dialect ? " / " + esc(c.dialect) : ""}</td>
    <td>${c.cues.map((x) => `<span class="alt">${esc(x.label)}</span>`).join(" ")}</td>
    <td>${c.age_s}s${c.stuck ? ' <span class="alt">silent ' + c.silent_s + "s</span>" : ""}</td><td>${esc(c.status)}</td></tr>`).join("") || '<tr><td colspan="8" class="muted">No calls yet</td></tr>';
  document.querySelectorAll("#wallTable tr.row").forEach((r) => (r.onclick = () => { connect(r.dataset.id); showTab("taker"); }));
}

// ------------------------------------------------------------------- admin
async function loadAdmin() {
  $("#apiKey").value = KEY;
  const f = await api("/api/admin/features");
  $("#modeCtl").innerHTML = `Mode: <select id="modeSel"><option value="assist">assist</option><option value="shadow">shadow</option></select>`;
  $("#modeSel").value = f.mode;
  $("#modeSel").onchange = async (e) => { await post("/api/admin/mode", { mode: e.target.value }); loadAdmin(); };
  $("#featCtl").innerHTML = Object.entries(f.features).map(([k, v]) => `<label style="display:block"><input type="checkbox" data-f="${k}" ${v ? "checked" : ""}> ${k}</label>`).join("");
  document.querySelectorAll("[data-f]").forEach((c) => (c.onchange = () => post(`/api/admin/features/${c.dataset.f}`, { enabled: c.checked })));
  refreshMetrics(); refreshShadow(); refreshAudit();
}
async function refreshMetrics() { $("#metrics").textContent = JSON.stringify(await api("/api/metrics"), null, 2); }
async function refreshShadow() { $("#shadowRep").textContent = JSON.stringify(await api("/api/shadow/report"), null, 2); }
async function refreshAudit() {
  const [v, rows] = await Promise.all([api("/api/audit/verify"), api("/api/audit?limit=150")]);
  $("#auditOk").innerHTML = v.ok ? `<span class="muted">chain OK · ${v.records} records</span>` : `<span class="alt">CHAIN BROKEN at #${v.broken_at_seq}</span>`;
  $("#auditTable tbody").innerHTML = rows.reverse().map((r) => `<tr><td>${r.seq}</td><td>${esc(r.call_id ?? "")}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td><td>${esc(JSON.stringify(r.data)).slice(0, 120)}</td></tr>`).join("");
}
$("#refreshMetrics").onclick = refreshMetrics; $("#refreshShadow").onclick = refreshShadow; $("#refreshAudit").onclick = refreshAudit;
$("#apiKey").onchange = (e) => { KEY = e.target.value; store.set("copilot_key", KEY); };
// ------------------------------------------------------------- support agent
const agent = { hist: [] };
const SUG = ["What is shadow mode?", "How is priority calculated?", "What do the alerts mean?", "How do I switch a feature off?"];
function agentAdd(cls, text, note) { const d = document.createElement("div"); d.className = "am " + cls; d.dir = "auto"; d.textContent = text; if (note) { const n = document.createElement("small"); n.textContent = note; d.appendChild(n); } $("#agentLog").appendChild(d); $("#agentLog").scrollTop = 1e6; }
async function agentOpen() {
  $("#agentPanel").hidden = false; $("#agentBtn").hidden = true; $("#agentIn").focus();
  try { const st = await api("/api/agent/status"); const ai = st.mode !== "off";
    $("#agentMode").textContent = ai ? `AI · ${st.provider}${st.local ? " (local)" : ""}${st.reachable === false ? " · not reachable" : ""}` : "keyword help (no AI configured)"; $("#agentMode").className = "agentmode " + (ai ? "ai" : "");
    $("#agentNote").textContent = "Read-only: it cannot dispatch or change records." + (st.call_data_shared ? (st.local ? " Call data goes to the local model on this network." : " Call data is shared with the AI provider.") : ai ? " Call content is not shared with the AI." : ""); } catch { $("#agentMode").textContent = "offline"; }
  if (!$("#agentLog").children.length) agentAdd("a", "Hi, I can explain how the Co-Pilot works, protocol questions and system status. I cannot dispatch units, edit records or give medical advice.");
  $("#agentSug").innerHTML = SUG.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  document.querySelectorAll("#agentSug button").forEach((b) => (b.onclick = () => agentAsk(b.textContent)));
}
async function agentAsk(q) {
  agentAdd("u", q); agent.hist.push({ role: "user", content: q }); $("#agentIn").value = "";
  const wait = document.createElement("div"); wait.className = "am a"; wait.textContent = "…"; $("#agentLog").appendChild(wait);
  try { const r = await post("/api/agent/chat", { messages: agent.hist }); wait.remove(); agentAdd("a", r.reply, r.tools.length ? "looked up: " + r.tools.map((t) => t.name).join(", ") : r.mode === "faq" ? "keyword help" : ""); agent.hist.push({ role: "assistant", content: r.reply }); }
  catch (e) { wait.remove(); agentAdd("a", "Support is unavailable: " + e.message); }
}
$("#agentBtn").onclick = agentOpen; $("#agentClose").onclick = () => { $("#agentPanel").hidden = true; $("#agentBtn").hidden = false; };
$("#agentForm").onsubmit = (e) => { e.preventDefault(); const q = $("#agentIn").value.trim(); if (q) agentAsk(q); };
init().catch((e) => console.error(e));
