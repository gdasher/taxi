// SPDX-License-Identifier: BSD-3-Clause
// natgw monitor page: polls /api/stats, /api/history and /api/events.
// The DOM is built with textContent only (no HTML from data) and no inline
// styles (the Content-Security-Policy forbids them).
"use strict";

const POLL_MS = 2000;
const EVENTS_MS = 10000;
let windowSeconds = 600;
let last = null;
let history = [];

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "title") e.title = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}

function fill(el, ...kids) { el.replaceChildren(...kids.flat().filter(k => k !== null && k !== undefined)); }

function num(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (typeof v !== "number") return String(v);
  const d = digits === undefined ? (Math.abs(v) < 10 && v % 1 ? 1 : 0) : digits;
  return v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
}

function rate(v) {
  if (v === null || v === undefined) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + " G";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + " M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + " k";
  return num(v, a < 10 ? 1 : 0);
}

function pct(v) { return v === null || v === undefined ? "–" : num(v, v < 10 ? 1 : 0) + "%"; }

function dur(s) {
  if (s === null || s === undefined) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  if (d) return `${d}d ${hh}h ${m}m`;
  if (hh) return `${hh}h ${m}m`;
  if (m) return `${m}m ${ss}s`;
  return `${ss}s`;
}

function when(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString() : d.toLocaleString();
}

function ago(ts, now) { return ts ? dur(now - ts) + " ago" : "never"; }

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok && r.status !== 503) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

// ---------------------------------------------------------------- ISPs

function wanCard(w, now, mgr) {
  const state = w.state || "unknown";
  const down = state === "down";
  const cls = ["wan", state, w.stale ? "stale" : ""].join(" ");
  const t = w.traffic || {};
  const lease = w.lease;
  const link = w.link;
  const linkText = !link ? "unknown" : !link.admin_up ? "administratively down" : link.link_up ? "up" : "DOWN";

  const head = h("div", { class: "wan-head" },
    h("h3", null, w.name), h("span", { class: "if" }, w.interface || ""),
    h("span", { class: "pill " + state }, w.stale ? state + " (stale)" : state));

  const since = w.since ? now - w.since : null;
  const big = down ? `Down for ${dur(w.outage ? w.outage.duration : since)}`
    : state === "up" ? `Up for ${dur(since)}` : "Waiting for a verdict";
  const st = h("div", { class: "wan-state" },
    h("div", { class: "big" }, big),
    h("div", { class: "why" }, w.reason || ""),
    down && w.outage ? h("div", { class: "muted" }, `since ${when(w.outage.start)}`) : null);

  const facts = h("dl", { class: "facts" },
    h("dt", null, "Link"), h("dd", { class: link && !(link.admin_up && link.link_up) ? "res-bad" : "" }, linkText),
    h("dt", null, "DHCP lease"),
    h("dd", null, lease ? `${lease.address}/${lease.prefix_len} via ${lease.gateway}` +
      (w.lease_since ? ` (for ${dur(now - w.lease_since)})` : "") : "none"),
    h("dt", null, "Default route"),
    h("dd", null, w.in_route ? `yes (weight ${w.weight ?? 1})` : "no"),
    h("dt", null, "Inbound"),
    h("dd", null, `${rate(t.rx_pps)} pkt/s` +
      (t.hw_rx_pps !== null && t.hw_rx_pps !== undefined ? ` (FPGA ${rate(t.hw_rx_pps)}, VPP ${rate(t.sw_rx_pps)})` : "")),
    h("dt", null, "Outbound via VPP"), h("dd", null, `${rate(t.sw_tx_pps)} pkt/s`));

  const hist = (w.history || "").split("");
  const probes = h("div", { class: "probes", title: "probe rounds, oldest first (green: a target answered)" },
    hist.map(c => h("span", { class: c === "1" ? "" : "bad" })));
  const thr = mgr && mgr.probe ? ` · every ${mgr.probe.interval}s; down after ${mgr.probe.down_rounds} failed, ` +
    `up after ${mgr.probe.up_rounds} good` : "";

  const rows = Object.entries(w.probe_targets || {}).map(([tg, r]) => h("tr", null,
    h("td", { class: "mono" }, tg),
    h("td", { class: r.ok === true ? "res-ok" : r.ok === false ? "res-bad" : "muted" },
      r.ok === true ? "answers" : r.ok === false ? "no reply" : "not needed"),
    h("td", null, ago(r.last_ok, now)),
    h("td", null, r.last_fail ? ago(r.last_fail, now) : "–")));
  const targets = h("table", { class: "targets" },
    h("thead", null, h("tr", null, h("th", null, "Probe target"), h("th", null, "Last result"),
      h("th", null, "Last reply"), h("th", null, "Last failure"))),
    h("tbody", null, rows));

  return h("article", { class: cls, "data-wan": w.name }, head, st, facts,
    h("div", { class: "muted" }, `Probe rounds (last ${hist.length})${thr}`), probes, targets);
}

function renderWans(cur) {
  const now = cur.ts;
  const mgr = cur.wanmgr || {};
  const box = document.getElementById("wans");
  if (!cur.wans || !cur.wans.length) {
    fill(box, h("div", { class: "wan" }, h("div", { class: "big" }, "No ISP state"),
      h("div", { class: "muted" }, mgr.error || "")));
  } else {
    fill(box, cur.wans.map(w => wanCard(w, now, mgr)));
  }
  const meta = [];
  if (mgr.up) meta.push(`WAN manager reporting (updated ${dur(mgr.age)} ago)`);
  else meta.push(`WAN manager: ${mgr.error || "not reporting"} — the states shown may be out of date`);
  if (mgr.route && mgr.route.length) meta.push(`default route via ${mgr.route.map(r => r.gateway).join(", ")}`);
  else if (mgr.up) meta.push("default route: DHCP routes of all WANs (no healthy WAN)");
  document.getElementById("isp-meta").textContent = meta.join(" · ");
}

// ---------------------------------------------------------------- alerts, tiles

function renderAlerts(cur) {
  const ul = document.getElementById("alerts");
  const a = cur.alerts || [];
  if (!a.length) { fill(ul, h("li", { class: "none" }, "No alerts")); return; }
  fill(ul, a.map(x => h("li", { class: x.severity },
    h("span", { class: "pill " + x.severity }, x.severity),
    h("span", { class: "t" }, x.title),
    x.detail ? h("span", { class: "d" }, x.detail) : null,
    h("span", { class: "since" }, `since ${when(x.since)} (${dur(cur.ts - x.since)})`))));
}

function tile(k, v, s, sev) {
  return h("div", { class: "tile " + (sev || "") }, h("div", { class: "k" }, k), h("div", { class: "v" }, v),
    s ? h("div", { class: "s" }, s) : null);
}

function renderTiles(cur) {
  const t = cur.traffic || {}, d = cur.device || {}, o = cur.offload || {}, n = cur.nat || {}, v = cur.vpp || {};
  const st = d.ddr_status || {};
  let ddr = "not fitted", ddrSev = "";
  if (st.active) ddr = `${pct(d.ddr_pct)} full`;
  else if (st.present && !st.calibrated) { ddr = "no DIMM / uncalibrated"; ddrSev = "warning"; }
  else if (st.present) ddr = st.clearing ? "clearing" : "present, idle";
  const tiles = [
    tile("Offload ratio", pct(t.offload_pct), "of packets forwarded by the FPGA"),
    tile("Forwarded by FPGA", rate(t.hw_pps), "packets/s"),
    tile("Sent to VPP", rate(t.punt_pps), "packets/s"),
    tile("On-chip table", pct(d.onchip_pct), `${num(d.onchip_flows)} of ${num(d.onchip_capacity)} flows`,
      d.onchip_pct > 85 ? "warning" : ""),
    tile("DDR tier", ddr, st.active ? `${num(d.ddr_flows)} of ${num(d.ddr_capacity)} flows · hit ${pct(d.ddr_hit_pct)}` : "",
      ddrSev),
    tile("Offloaded sessions", num(o.offloaded), o.enabled === false ? "offload DISABLED" : `${num(o.pending)} pending`,
      o.enabled === false ? "warning" : ""),
    tile("NAT sessions", num(n.sessions), n.max_sessions ? `of ${num(n.max_sessions)} (${pct(n.sessions_pct)})` : ""),
    tile("VPP", v.up ? "running" : "DOWN", v.up ? `vector rate ${num(v.vector_rate, 1)}` : (v.error || ""),
      v.up ? "" : "critical"),
  ];
  fill(document.getElementById("tiles"), tiles);
}

function renderOverall(cur) {
  const el = document.getElementById("overall");
  const a = cur.alerts || [];
  const sev = a.some(x => x.severity === "critical") ? "critical" : a.length ? "warning" : "ok";
  el.className = "pill " + sev;
  el.textContent = sev === "ok" ? "all good" : `${a.length} alert${a.length > 1 ? "s" : ""}`;
  document.getElementById("updated").textContent = `updated ${new Date(cur.ts * 1000).toLocaleTimeString()}`;
  document.getElementById("host").textContent = cur.hostname || "";
  document.title = (sev === "ok" ? "" : "⚠ ") + `natgw monitor — ${cur.hostname || ""}`;
  const banner = document.getElementById("banner");
  const downs = (cur.wans || []).filter(w => w.state === "down");
  if (downs.length && cur.wanmgr && cur.wanmgr.up) {
    const up = (cur.wans || []).filter(w => w.state === "up").map(w => w.name);
    banner.textContent = downs.map(w => `${w.name} DOWN for ${dur(w.outage ? w.outage.duration : cur.ts - w.since)}`)
      .join("; ") + (up.length ? ` — traffic is using ${up.join(", ")}` : " — NO ISP IS WORKING");
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// ---------------------------------------------------------------- tables

function renderPorts(cur) {
  const body = document.querySelector("#ports tbody");
  const ports = cur.ports || [];
  if (!ports.length) { fill(body, h("tr", null, h("td", { class: "empty", colspan: 8 }, "no natgw ports"))); return; }
  fill(body, ports.map(p => {
    const r = p.rates || {}, vr = p.vpp_rates || {};
    let punt = null, drop = null;
    if (r.rx_forwarded !== null && r.rx_forwarded !== undefined) {
      punt = 0; drop = (r.punt_ingress_drop || 0) + (r.punt_bad_fcs || 0);
      for (const k of Object.keys(r)) {
        if (k.startsWith("punt_") && k !== "punt_ingress_drop" && k !== "punt_bad_fcs") punt += r[k] || 0;
      }
    }
    const l = p.link;
    return h("tr", null, h("td", null, p.name),
      h("td", { class: l && l.admin_up && !l.link_up ? "res-bad" : "" },
        !l ? "–" : !l.admin_up ? "admin down" : l.link_up ? "up" : "DOWN"),
      h("td", null, rate(r.rx_forwarded)), h("td", null, rate(punt)), h("td", null, rate(drop)),
      h("td", null, rate(vr.rx_packets)), h("td", null, rate(vr.tx_packets)), h("td", null, num((p.vpp || {}).drops)));
  }));
}

function kvTable(title, rows) {
  return h("details", null, h("summary", null, title),
    h("div", { class: "scroll" }, h("table", { class: "grid num" },
      h("thead", null, h("tr", null, h("th", null, "Counter"), h("th", null, "Value"), h("th", null, "Per second"))),
      h("tbody", null, rows.map(([k, v, r]) => h("tr", null, h("td", null, k), h("td", null, num(v)),
        h("td", null, r === undefined ? "" : rate(r))))))));
}

const openDetails = new Set();
function renderDetail(cur) {
  const box = document.getElementById("detail");
  box.querySelectorAll("details").forEach(d => {
    const k = d.querySelector("summary").textContent;
    if (d.open) openDetails.add(k); else openDetails.delete(k);
  });
  const parts = [];
  for (const p of cur.ports || []) {
    parts.push(kvTable(`Device counters: ${p.name}`,
      Object.keys(p.natgw).sort().map(k => [k, p.natgw[k], p.rates[k]])));
  }
  const o = cur.offload || {};
  if (o.counters) {
    parts.push(kvTable("Offload plugin", [["offloaded", o.offloaded], ["pending", o.pending],
      ...Object.keys(o.counters).map(k => [k, o.counters[k], o.rates[k]])]));
  }
  const n = cur.nat || {};
  parts.push(kvTable("NAT (VPP nat44-ed)", [["sessions", n.sessions], ["max sessions", n.max_sessions],
    ["fast path", null, n.fastpath_pps], ["slow path (new sessions)", null, n.slowpath_pps], ["drops", null, n.drops_pps]]));
  const v = cur.vpp || {};
  parts.push(kvTable("VPP", [["vector rate", v.vector_rate], ["input rate", v.input_rate], ["worker threads", v.workers]]));
  fill(box, parts);
  box.querySelectorAll("details").forEach(d => {
    if (openDetails.has(d.querySelector("summary").textContent)) d.open = true;
  });
}

function evidence(s) {
  if (!s) return "";
  const parts = [];
  if (s.link) parts.push(`link ${s.link.link_up ? "up" : "down"}`);
  parts.push(s.lease ? `lease ${s.lease.address} via ${s.lease.gateway}` : "no lease");
  const tg = Object.entries(s.probe_targets || {}).map(([t, r]) => `${t} ${r.ok === false ? "no reply" : r.ok ? "ok" : "–"}`);
  if (tg.length) parts.push(`probes: ${tg.join(", ")}`);
  if (s.history) parts.push(`rounds ${s.history.slice(-10)}`);
  if (s.rx_pps_before !== null && s.rx_pps_before !== undefined) parts.push(`inbound before ${rate(s.rx_pps_before)} pkt/s`);
  const others = Object.entries(s.others || {}).map(([n, st]) => `${n} ${st}`);
  if (others.length) parts.push(`others: ${others.join(", ")}`);
  return parts.join(" · ");
}

function renderOutages(cur) {
  const body = document.querySelector("#outages tbody");
  const o = cur.outages || [];
  if (!o.length) { fill(body, h("tr", null, h("td", { class: "empty", colspan: 6 }, "no outages recorded"))); return; }
  fill(body, o.map(x => h("tr", { class: x.end ? "" : "ongoing" },
    h("td", null, x.wan), h("td", null, when(x.start)),
    h("td", { class: x.end ? "" : "res-bad" }, x.end ? when(x.end) : "ongoing"),
    h("td", null, dur(x.duration)), h("td", null, x.reason || ""),
    h("td", { class: "evidence" }, evidence(x.snapshot)))));
}

function eventText(e) {
  switch (e.type) {
    case "wan":
      if (e.kind === "state") return [`${e.wan}: ${e.from} → ${e.to}`, e.detail];
      return [`${e.wan}: lease ${e.from || "none"} → ${e.to || "none"}`, e.detail];
    case "outage_start": return [`${e.wan}: outage started`, e.reason];
    case "outage_end": return [`${e.wan}: outage ended`, `after ${dur(e.duration)} (${e.reason || ""})`];
    case "alert": return [`alert ${e.state}: ${e.title}`, e.detail];
    case "vpp": return [`VPP ${e.to}`, e.detail];
    case "monitor": return ["monitor", e.detail];
    default: return [e.type, JSON.stringify(e)];
  }
}

function renderEvents(events) {
  const body = document.querySelector("#events tbody");
  const ev = events.slice().reverse().slice(0, 150);
  if (!ev.length) { fill(body, h("tr", null, h("td", { class: "empty", colspan: 3 }, "no events"))); return; }
  fill(body, ev.map(e => {
    const [w, d] = eventText(e);
    return h("tr", null, h("td", null, when(e.ts)), h("td", null, w), h("td", { class: "muted" }, d || ""));
  }));
}

// ---------------------------------------------------------------- charts

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

// series: [{label, get(sample) -> number|null, color: "--s1"}]; opts: {stacked, max, unit, shade(sample)}
function drawChart(id, samples, series, opts) {
  opts = opts || {};
  const cv = document.getElementById("c-" + id);
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const padL = 48, padR = 8, padT = 6, padB = 18;
  const pw = W - padL - padR, ph = H - padT - padB;
  const tEnd = samples.length ? samples[samples.length - 1].t : Date.now() / 1000;
  const tStart = tEnd - windowSeconds;
  const x = t => padL + (t - tStart) / windowSeconds * pw;

  // values, stacked if asked
  const vals = series.map(() => []);
  let ymax = 0;
  for (const s of samples) {
    let acc = 0;
    series.forEach((se, i) => {
      let v = se.get(s);
      if (v === null || v === undefined || Number.isNaN(v)) { vals[i].push(null); return; }
      if (opts.stacked) { acc += v; v = acc; }
      vals[i].push(v);
      ymax = Math.max(ymax, v);
    });
  }
  ymax = opts.max || niceMax(ymax * 1.05);
  const y = v => padT + ph - (v / ymax) * ph;

  g.font = "11px system-ui, sans-serif";
  g.fillStyle = cssVar("--muted");
  g.strokeStyle = cssVar("--line");
  g.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = ymax * i / 4, yy = Math.round(y(v)) + 0.5;
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(W - padR, yy); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle";
    g.fillText(opts.unit === "%" ? `${Math.round(v)}%` : v < 10 ? num(v, 2) : rate(v), padL - 6, yy);
  }
  g.textAlign = "center"; g.textBaseline = "alphabetic";
  for (let i = 0; i <= 4; i++) {
    const t = tStart + windowSeconds * i / 4;
    const ago = tEnd - t;
    g.fillText(ago < 1 ? "now" : `-${dur(ago)}`, Math.min(Math.max(x(t), padL + 14), W - padR - 14), H - 4);
  }

  // shaded spans (outages, VPP down)
  if (opts.shade) {
    g.fillStyle = cssVar("--shade");
    for (let i = 0; i < samples.length; i++) {
      if (!opts.shade(samples[i])) continue;
      const x0 = x(samples[i].t), x1 = i + 1 < samples.length ? x(samples[i + 1].t) : x0 + 2;
      g.fillRect(Math.max(x0, padL), padT, Math.max(x1 - x0, 1), ph);
    }
  }

  // stacked: fill areas from the top series down
  const order = opts.stacked ? series.map((_, i) => series.length - 1 - i) : series.map((_, i) => i);
  for (const i of order) {
    const col = cssVar(series[i].color);
    g.strokeStyle = col;
    g.lineWidth = 1.5;
    g.beginPath();
    let pen = false;
    vals[i].forEach((v, k) => {
      if (v === null) { pen = false; return; }
      const px = x(samples[k].t), py = y(Math.min(v, ymax));
      if (pen) g.lineTo(px, py); else { g.moveTo(px, py); pen = true; }
    });
    g.stroke();
    if (opts.stacked) {
      g.globalAlpha = 0.35;
      g.fillStyle = col;
      vals[i].forEach((v, k) => {
        if (v === null || k + 1 >= samples.length) return;
        const below = i > 0 ? (vals[i - 1][k] || 0) : 0;
        const x0 = x(samples[k].t), x1 = x(samples[k + 1].t);
        g.fillRect(x0, y(v), Math.max(x1 - x0, 1), y(below) - y(v));
      });
      g.globalAlpha = 1;
    }
  }

  const leg = document.getElementById("l-" + id);
  fill(leg, series.map(se => h("span", null, h("i", { class: se.color.replace("--", "") }), se.label)),
    opts.shadeLabel ? h("span", null, opts.shadeLabel) : null);
}

const REASON_LABELS = {
  miss: "no flow (new or not offloaded)", syn: "TCP SYN", finrst: "TCP FIN/RST", frag: "fragment", ttl: "TTL",
  not_ipv4: "not IPv4", mcast: "multicast/broadcast", ip_hdr: "IP options/header", proto: "other protocol",
  csum: "checksum", bypass: "bypass", vlan: "VLAN", nh: "next hop",
};
const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];

function renderCharts() {
  const s = history;
  drawChart("traffic", s, [
    { label: "forwarded by FPGA", get: x => x.hw, color: "--s3" },
    { label: "sent to VPP", get: x => x.punt, color: "--s2" },
    { label: "dropped", get: x => x.drop, color: "--s5" },
  ], { shade: x => !x.vpp, shadeLabel: "shaded: VPP down" });
  drawChart("ratio", s, [{ label: "offload ratio", get: x => x.ratio, color: "--s1" }], { max: 100, unit: "%" });

  // the reasons seen in the window, largest first; the rest summed
  const tot = {};
  for (const x of s) for (const [k, v] of Object.entries(x.reasons || {})) tot[k] = (tot[k] || 0) + v;
  const keys = Object.keys(tot).sort((a, b) => tot[b] - tot[a]);
  const top = keys.slice(0, 6), rest = keys.slice(6);
  const rs = top.map((k, i) => ({ label: REASON_LABELS[k] || k, get: x => (x.reasons || {})[k] || 0, color: COLORS[i] }));
  if (rest.length) rs.push({ label: "other", get: x => rest.reduce((a, k) => a + ((x.reasons || {})[k] || 0), 0), color: "--s8" });
  if (!rs.length) rs.push({ label: "none", get: x => (x.punt === null ? null : 0), color: "--s8" });
  drawChart("reasons", s, rs, { stacked: true });

  const wans = last && last.wans ? last.wans.map(w => w.name) : [];
  drawChart("wans", s, wans.map((n, i) => ({ label: n, get: x => (x.wans[n] || {}).rx, color: COLORS[i % 8] })),
    { shade: x => Object.values(x.wans || {}).some(w => w.up === 0), shadeLabel: "shaded: an ISP down" });
  drawChart("fill", s, [
    { label: "on-chip", get: x => x.onchip, color: "--s1" },
    { label: "DDR", get: x => x.ddr, color: "--s4" },
  ], { max: 100, unit: "%" });
  drawChart("tier", s, [
    { label: "promotions/s", get: x => x.prom, color: "--s3" },
    { label: "demotions/s", get: x => x.dem, color: "--s2" },
    { label: "DDR hit rate %", get: x => x.hit, color: "--s4" },
  ]);
}

// ---------------------------------------------------------------- polling

function render(cur) {
  last = cur;
  renderOverall(cur);
  renderWans(cur);
  renderAlerts(cur);
  renderTiles(cur);
  renderPorts(cur);
  renderOutages(cur);
  renderDetail(cur);
}

function offline(err) {
  const el = document.getElementById("overall");
  el.className = "pill critical";
  el.textContent = "monitor unreachable";
  document.getElementById("updated").textContent = String(err.message || err);
}

async function poll() {
  try {
    const [cur, hist] = await Promise.all([getJSON("api/stats"), getJSON(`api/history?seconds=${windowSeconds}`)]);
    if (!cur.error) render(cur);
    history = hist.samples || [];
    renderCharts();
  } catch (e) {
    offline(e);
  }
}

async function pollEvents() {
  try {
    renderEvents((await getJSON("api/events?limit=300")).events || []);
  } catch (e) { /* shown by poll() */ }
}

document.getElementById("window").addEventListener("click", ev => {
  const b = ev.target.closest("button");
  if (!b) return;
  windowSeconds = Number(b.dataset.s);
  document.querySelectorAll("#window button").forEach(x => x.classList.toggle("on", x === b));
  try { localStorage.setItem("natgw-window", String(windowSeconds)); } catch (e) { /* private mode */ }
  poll();
});
try {
  const w = Number(localStorage.getItem("natgw-window"));
  if ([60, 600, 3600].includes(w)) {
    windowSeconds = w;
    document.querySelectorAll("#window button").forEach(x => x.classList.toggle("on", Number(x.dataset.s) === w));
  }
} catch (e) { /* private mode */ }
window.addEventListener("resize", () => renderCharts());
poll();
pollEvents();
setInterval(poll, POLL_MS);
setInterval(pollEvents, EVENTS_MS);
