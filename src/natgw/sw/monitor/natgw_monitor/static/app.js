// SPDX-License-Identifier: BSD-3-Clause
// natgw monitor page: polls /api/stats, /api/history and /api/events and
// renders them. The DOM is built with textContent only (no HTML from data),
// and there are no inline styles: the Content-Security-Policy forbids them.
"use strict";

const STATS_MS = 2000;
const EVENTS_MS = 10000;
const HISTORY_MS = { 600: 2000, 3600: 5000, 86400: 30000, 604800: 60000, 2592000: 300000, 31536000: 600000 };
let windowSeconds = 3600;
let last = null;
let historyTimer = null;
const fmt = window.natgwFmt;

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return e;
}
const $ = id => document.getElementById(id);
function fill(e, ...kids) { e.replaceChildren(...kids.flat().filter(k => k !== null && k !== undefined && k !== false)); }

function num(v) { return v === null || v === undefined ? "–" : Number(v).toLocaleString(); }
function rate(v) { return v === null || v === undefined ? "–" : fmt(v); }
function pct(v) { return v === null || v === undefined ? "–" : fmt(v, "%"); }

function dur(s) {
  if (s === null || s === undefined) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  if (d) return `${d} d ${hh} h`;
  if (hh) return `${hh} h ${m} min`;
  if (m) return `${m} min ${ss} s`;
  return `${ss} s`;
}

function when(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  return new Date().toDateString() === d.toDateString() ? d.toLocaleTimeString()
    : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function ago(ts, now) { return ts ? `${dur(now - ts)} ago` : "never"; }

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok && r.status !== 503) throw new Error(`${url} answered ${r.status}`);
  return r.json();
}

// a state as icon + word: status color never carries meaning alone
function stateTag(state, stale) {
  const s = stale ? "stale" : state || "unknown";
  const text = { up: "✓ Up", down: "✕ Down", unknown: "Waiting for a verdict", stale: "Not current" }[s] || s;
  return h("span", { class: `state ${s}` }, text);
}

// ---------------------------------------------------------------- the verdict

function verdict(cur) {
  const wans = cur.wans || [];
  const up = wans.filter(w => w.state === "up");
  const down = wans.filter(w => w.state === "down");
  const ratio = cur.traffic && cur.traffic.offload_pct;
  if (!cur.vpp.up) return ["critical", "VPP isn't running, so new connections can't be set up."];
  if (!cur.wanmgr.up) return ["warning", "Provider state is unknown: the WAN manager isn't reporting."];
  if (wans.length && !up.length && down.length) return ["critical", "No internet provider is working."];
  if (down.length) {
    const w = down[0];
    const more = down.length > 1 ? ` and ${down.length - 1} more` : "";
    return ["warning", `${w.name}${more} down for ${dur(w.outage ? w.outage.duration : cur.ts - w.since)}. ` +
      `Traffic is using ${up.map(x => x.name).join(" and ")}.`];
  }
  const who = wans.length === 2 ? "Both providers are up" : wans.length ? `All ${wans.length} providers are up` : "No providers configured";
  const tail = ratio !== null && ratio !== undefined ? `, and the FPGA forwards ${fmt(ratio, "%")} of packets.` : ".";
  return [(cur.alerts || []).length ? "warning" : "ok", who + tail];
}

function renderHead(cur) {
  const [sev, text] = verdict(cur);
  const v = $("status");
  v.dataset.sev = sev;
  v.textContent = text;
  $("host").textContent = cur.hostname || "natgw";
  $("updated").textContent = `Updated ${new Date(cur.ts * 1000).toLocaleTimeString()}`;
  document.title = `${sev === "ok" ? "" : "⚠ "}${cur.hostname || "natgw"}`;
}

// ---------------------------------------------------------------- the signal path

function renderPath(cur) {
  const wans = cur.wans || [];
  const wanPorts = new Set(wans.map(w => (w.interface || "").split(".")[0]));
  const lanPorts = (cur.ports || []).filter(p => !wanPorts.has(p.name));
  const lanUp = lanPorts.length ? lanPorts.some(p => p.link && p.link.link_up) : null;
  const t = cur.traffic || {};

  const lan = h("div", { class: "node" }, h("div", { class: "name" }, "LAN"),
    h("div", { class: "sub" }, lanPorts.length ? lanPorts.map(p => p.name).join(", ") : ""),
    lanUp === false ? h("div", { class: "sub" }, stateTag("down")) : null);
  const lanLink = h("div", { class: `link ${lanUp === false ? "down" : lanUp === null ? "unknown" : ""}` });

  const gw = h("div", { class: "gw" },
    h("div", { class: "name" }, cur.hostname || "Gateway"),
    h("div", { class: "figure" }, t.offload_pct === null || t.offload_pct === undefined ? "–" : fmt(t.offload_pct, "%")),
    h("div", { class: "figure-label" }, "of packets forwarded by the FPGA"));

  const branches = h("div", { class: "branches" }, wans.map(w => {
    const st = w.stale ? "unknown" : w.state || "unknown";
    const tr = w.traffic || {};
    const since = w.outage ? w.outage.duration : w.since ? cur.ts - w.since : null;
    const why = st === "down" ? w.reason
      : st === "up" ? (w.lease ? `${w.lease.address}, ${rate(tr.rx_pps)} packets/s in` : "")
      : (w.reason || "");
    // where the cut is drawn says where the fault is: on the gateway's own
    // link (link, lease) or beyond the provider (probes unanswered)
    const local = /link|administratively|lease|interface/.test(w.reason || "");
    const line = st === "up" ? "link" : st === "down" ? "link down" : "link unknown";
    const seg = h("div", { class: st === "down" && local ? "break" : line });
    const tail = h("div", { class: `${st === "down" && !local ? "break" : line} tail` });
    return h("div", { class: `branch ${st}`, "data-wan": w.name },
      seg,
      h("div", { class: "isp-node" },
        h("div", { class: "name" }, w.name, stateTag(w.state, w.stale),
          since !== null ? h("span", { class: "rate" }, `for ${dur(since)}`) : null),
        h("div", { class: "why" }, why || "")),
      tail,
      h("div", { class: "internet" }, "Internet"));
  }));
  fill($("path"), lan, lanLink, gw, branches);
  // the trunk runs from the first provider's link to the last one's
  requestAnimationFrame(() => {
    const b = branches.children;
    if (b.length > 1) {
      branches.style.setProperty("--trunk-top", `${b[0].offsetHeight / 2 - 1}px`);
      branches.style.setProperty("--trunk-bottom", `${b[b.length - 1].offsetHeight / 2 - 2}px`);
    } else {
      branches.style.setProperty("--trunk-top", "50%");
      branches.style.setProperty("--trunk-bottom", "50%");
    }
  });
}

// ---------------------------------------------------------------- alerts

function renderAlerts(cur) {
  const a = cur.alerts || [];
  $("alerts-panel").hidden = !a.length;
  fill($("alerts"), a.map(x => h("li", { class: x.severity },
    h("span", { class: "sev" }, x.severity === "critical" ? "✕ Critical" : x.severity === "warning" ? "! Warning" : "Info"),
    h("span", { class: "what" }, x.title, x.detail ? h("span", { class: "detail" }, x.detail) : null),
    h("span", { class: "since" }, `${when(x.since)}, for ${dur(cur.ts - x.since)}`))));
}

// ---------------------------------------------------------------- providers

function ispDetail(w, now, mgr) {
  const st = w.stale ? "unknown" : w.state || "unknown";
  const t = w.traffic || {};
  const link = w.link;
  const linkText = !link ? "Unknown" : !link.admin_up ? "Administratively down" : link.link_up ? "Up" : "Down";
  const lease = w.lease;
  const facts = h("dl", { class: "facts" },
    st !== "up" && w.reason ? [h("dt", null, "Why"), h("dd", { class: st === "down" ? "bad" : "" }, w.reason)] : null,
    h("dt", null, "Link"), h("dd", { class: link && !(link.admin_up && link.link_up) ? "bad" : "" }, linkText),
    h("dt", null, "DHCP lease"),
    h("dd", null, lease ? `${lease.address}/${lease.prefix_len} via ${lease.gateway}` +
      (w.lease_since ? `, held for ${dur(now - w.lease_since)}` : "") : "None"),
    h("dt", null, "Default route"), h("dd", null, w.in_route ? `Yes, weight ${w.weight ?? 1}` : "No"),
    h("dt", null, "Inbound"),
    h("dd", null, `${rate(t.rx_pps)} packets/s` + (t.hw_rx_pps !== null && t.hw_rx_pps !== undefined
      ? ` (FPGA ${rate(t.hw_rx_pps)}, VPP ${rate(t.sw_rx_pps)})` : "")),
    h("dt", null, "Outbound through VPP"), h("dd", null, `${rate(t.sw_tx_pps)} packets/s`));

  const hist = (w.history || "").split("").filter(Boolean);
  const p = mgr && mgr.probe;
  const tapeLabel = h("p", { class: "tape-label" }, `Last ${hist.length} probe rounds, oldest first` +
    (p ? `. Down after ${p.down_rounds} failed rounds in a row, up after ${p.up_rounds} good ones.` : "."));
  const tape = h("div", { class: "tape", role: "img",
    "aria-label": `${hist.filter(c => c === "1").length} of ${hist.length} probe rounds answered` },
  hist.map(c => h("span", { class: c === "1" ? "" : "bad" })));

  const rows = Object.entries(w.probe_targets || {}).map(([tg, r]) => h("tr", null,
    h("td", null, tg),
    h("td", { class: r.ok === true ? "ok-text" : r.ok === false ? "bad-text" : "" },
      r.ok === true ? "✓ Answers" : r.ok === false ? "✕ No reply" : "Not needed"),
    h("td", null, ago(r.last_ok, now)), h("td", null, r.last_fail ? ago(r.last_fail, now) : "–")));
  const targets = h("table", { class: "targets" },
    h("thead", null, h("tr", null, h("th", null, "Probe target"), h("th", null, "Last result"),
      h("th", null, "Last reply"), h("th", null, "Last failure"))), h("tbody", null, rows));

  const since = w.outage ? w.outage.duration : w.since ? now - w.since : null;
  return h("article", { class: "isp", "data-wan": w.name, "data-state": st, "data-stale": w.stale || null },
    h("div", { class: "isp-head" }, h("h3", null, w.name), stateTag(w.state, w.stale),
      since !== null ? h("span", { class: "iface" }, `for ${dur(since)}`) : null,
      h("span", { class: "iface" }, w.interface || "")),
    facts, tapeLabel, tape, targets);
}

function renderIsps(cur) {
  const mgr = cur.wanmgr || {};
  const wans = cur.wans || [];
  fill($("isps"), wans.length ? wans.map(w => ispDetail(w, cur.ts, mgr))
    : h("p", null, `No provider state. ${mgr.error || ""} Check that natgw-wanmgr is running.`));
  let note;
  if (!mgr.up) note = `The WAN manager isn't reporting (${mgr.error || "no status"}), so what's shown may be out of date.`;
  else if (mgr.route && mgr.route.length) note = `The default route goes through ${mgr.route.map(r => r.gateway).join(" and ")}.`;
  else note = "No provider passes its probes, so every provider's DHCP route is in use.";
  $("isp-note").textContent = note;
}

// ---------------------------------------------------------------- figures

function figure(label, value, sub, sev) {
  return h("div", { class: sev || "" }, h("dt", null, label), h("dd", null, value, sub ? h("small", null, sub) : null));
}

function renderFigures(cur) {
  const t = cur.traffic || {}, d = cur.device || {}, o = cur.offload || {}, n = cur.nat || {}, v = cur.vpp || {};
  const st = d.ddr_status || {};
  let ddr = "Not fitted", ddrSub = "", ddrSev = "";
  if (st.active) { ddr = pct(d.ddr_pct); ddrSub = `${num(d.ddr_flows)} of ${num(d.ddr_capacity)} flows, ${pct(d.ddr_hit_pct)} hits`; }
  else if (st.present && !st.calibrated) { ddr = "No DIMM"; ddrSub = "or memory not calibrated"; ddrSev = "warning"; }
  else if (st.present) { ddr = st.clearing ? "Clearing" : "Idle"; }
  fill($("figures"),
    figure("Forwarded by the FPGA", rate(t.hw_pps), "packets/s"),
    figure("Sent to VPP", rate(t.punt_pps), "packets/s"),
    figure("Dropped", rate(t.drop_pps), "packets/s", t.drop_pps > 0 ? "warning" : ""),
    figure("On-chip flow table", pct(d.onchip_pct), `${num(d.onchip_flows)} of ${num(d.onchip_capacity)} flows`,
      d.onchip_pct > 85 ? "warning" : ""),
    figure("DDR flow table", ddr, ddrSub, ddrSev),
    figure("Sessions in hardware", num(o.offloaded), o.enabled === false ? "Offload is turned off" : `${num(o.pending)} waiting`,
      o.enabled === false ? "warning" : ""),
    figure("NAT sessions", num(n.sessions), n.max_sessions ? `of ${num(n.max_sessions)}` : ""),
    figure("VPP", v.up ? "Running" : "Stopped", v.up ? `vector rate ${fmt(v.vector_rate || 0)}` : (v.error || ""),
      v.up ? "" : "critical"));
}

// ---------------------------------------------------------------- tables

function renderPorts(cur) {
  const ports = cur.ports || [];
  fill(document.querySelector("#ports tbody"), ports.length ? ports.map(p => {
    const r = p.rates || {}, vr = p.vpp_rates || {};
    let punt = null, drop = null;
    if (r.rx_forwarded !== null && r.rx_forwarded !== undefined) {
      drop = (r.punt_ingress_drop || 0) + (r.punt_bad_fcs || 0);
      punt = Object.keys(r).filter(k => k.startsWith("punt_") && k !== "punt_ingress_drop" && k !== "punt_bad_fcs")
        .reduce((a, k) => a + (r[k] || 0), 0);
    }
    const l = p.link;
    return h("tr", null, h("td", null, p.name),
      h("td", { class: l && l.admin_up && !l.link_up ? "txt bad-text" : "txt" },
        !l ? "–" : !l.admin_up ? "Turned off" : l.link_up ? "Up" : "✕ Down"),
      h("td", null, rate(r.rx_forwarded)), h("td", null, rate(punt)), h("td", null, rate(drop)),
      h("td", null, rate(vr.rx_packets)), h("td", null, rate(vr.tx_packets)), h("td", null, num((p.vpp || {}).drops)));
  }) : h("tr", null, h("td", { class: "empty", colspan: 8 }, "No natgw ports: is VPP running with the card?")));
}

function evidence(s) {
  if (!s) return "";
  const parts = [];
  if (s.link) parts.push(`link ${s.link.link_up ? "up" : "down"}`);
  parts.push(s.lease ? `lease ${s.lease.address} via ${s.lease.gateway}` : "no lease");
  const tg = Object.entries(s.probe_targets || {}).map(([t, r]) => `${t} ${r.ok === false ? "no reply" : r.ok ? "answered" : "not tried"}`);
  if (tg.length) parts.push(`probes: ${tg.join(", ")}`);
  if (s.rx_pps_before !== null && s.rx_pps_before !== undefined) parts.push(`${fmt(s.rx_pps_before)} packets/s in just before`);
  const others = Object.entries(s.others || {}).map(([n, st]) => `${n} ${st}`);
  if (others.length) parts.push(`others: ${others.join(", ")}`);
  return parts.join("; ");
}

function renderOutages(cur) {
  const o = cur.outages || [];
  fill(document.querySelector("#outages tbody"), o.length ? o.map(x => h("tr", { "data-wan": x.wan },
    h("td", null, x.wan), h("td", { class: "when" }, when(x.start)),
    h("td", { class: x.end ? "when" : "when bad-text" }, x.end ? when(x.end) : "Ongoing"),
    h("td", { class: "n" }, dur(x.duration)), h("td", null, x.reason || ""),
    h("td", { class: "evidence" }, evidence(x.snapshot))))
    : h("tr", null, h("td", { class: "empty", colspan: 6 }, "No outages in the last year.")));
}

function eventText(e) {
  switch (e.type) {
    case "wan":
      if (e.kind === "state") return [`${e.wan} went from ${e.from} to ${e.to}`, e.detail];
      return [e.to ? `${e.wan} got lease ${e.to}` : `${e.wan} lost its lease`, e.from ? `was ${e.from}` : e.detail];
    case "outage_start": return [`${e.wan} outage started`, e.reason];
    case "outage_end": return [`${e.wan} outage ended`, `lasted ${dur(e.duration)}: ${e.reason || ""}`];
    case "alert": return [`${e.state === "raised" ? "Alert" : "Cleared"}: ${e.title}`, e.detail];
    case "vpp": return [`VPP ${e.to === "up" ? "is back" : e.to}`, e.detail];
    case "monitor": return ["Monitor started", ""];
    default: return [e.type, ""];
  }
}

function renderEvents(events) {
  const ev = events.slice().reverse().slice(0, 200);
  fill(document.querySelector("#events tbody"), ev.length ? ev.map(e => {
    const [w, d] = eventText(e);
    return h("tr", null, h("td", { class: "when" }, when(e.ts)), h("td", null, w), h("td", null, d || ""));
  }) : h("tr", null, h("td", { class: "empty", colspan: 3 }, "Nothing has happened yet.")));
}

// ---------------------------------------------------------------- every counter

const GAUGES = /(offloaded|pending|sessions|max_sessions|vector_rate|input_rate|flows|load_pct|capacity|status|last_sweep)$/;
let counterChart = null;
let counterName = null;
const openGroups = new Set();

function counterRows(prefix, values, rates) {
  return Object.keys(values).sort().map(k => {
    const name = `${prefix}${k}`;
    const b = h("button", { type: "button", "aria-pressed": String(name === counterName) }, k);
    b.addEventListener("click", () => showCounter(name));
    return h("tr", null, h("td", null, b), h("td", null, num(values[k])),
      h("td", null, rates && rates[k] !== undefined ? rate(rates[k]) : ""));
  });
}

function group(title, rows) {
  const d = h("details", { class: "counters", open: openGroups.has(title) || null },
    h("summary", null, title),
    h("div", { class: "table-wrap" }, h("table", { class: "grid num" },
      h("thead", null, h("tr", null, h("th", null, "Counter"), h("th", { class: "n" }, "Value"), h("th", { class: "n" }, "Per second"))),
      h("tbody", null, rows))));
  d.addEventListener("toggle", () => { if (d.open) openGroups.add(title); else openGroups.delete(title); });
  return d;
}

function renderDetail(cur) {
  const parts = (cur.ports || []).map(p => group(`Device counters on ${p.name}`, counterRows(`port.${p.name}.`, p.natgw, p.rates)));
  const o = cur.offload || {};
  if (o.counters) {
    parts.push(group("Offload plugin", counterRows("offload.",
      Object.assign({ offloaded: o.offloaded, pending: o.pending }, o.counters), o.rates)));
  }
  const n = cur.nat || {};
  parts.push(group("NAT", counterRows("nat.", { sessions: n.sessions, max_sessions: n.max_sessions })));
  const v = cur.vpp || {};
  parts.push(group("VPP", counterRows("vpp.", { vector_rate: v.vector_rate, input_rate: v.input_rate })));
  fill($("detail"), parts);
}

async function showCounter(name) {
  counterName = name;
  if (last) renderDetail(last);
  if (!counterChart) counterChart = new NatgwChart($("counter-chart"), { title: name, series: [] });
  await loadCounter();
}

async function loadCounter() {
  if (!counterName) return;
  const short = counterName.split(".").pop();
  const gauge = GAUGES.test(short);
  try {
    const r = await getJSON(`api/counter?name=${encodeURIComponent(counterName)}&seconds=${windowSeconds}`);
    const pts = gauge ? (r.points || []).map(([t, v]) => ({ t, v })) : (r.rates || []).map(([t, v]) => ({ t, v }));
    counterChart.root.querySelector("h3").textContent = `${counterName}${gauge ? "" : " per second"}`;
    const now = last ? last.ts : Date.now() / 1000;
    counterChart.update(pts, [], windowSeconds, now,
      [{ label: gauge ? "value" : "per second", color: "--s1", cls: "k1", get: x => x.v }]);
  } catch (e) { /* shown by the stats poll */ }
}

// ---------------------------------------------------------------- charts

const REASONS = [
  ["miss", "no flow for it (yet)", "--s1", "k1"], ["syn", "TCP SYN", "--s2", "k2"],
  ["finrst", "TCP FIN or RST", "--s3", "k3"], ["frag", "fragment", "--s4", "k4"],
  ["ttl", "TTL expiring", "--s5", "k5"], ["not_ipv4", "not IPv4", "--s6", "k6"]];
const WAN_SLOTS = [["--s1", "k1"], ["--s2", "k2"], ["--s3", "k3"]];
const charts = {};
let wanOrder = [];

function shades(samples, outages, t0, t1) {
  const out = (outages || []).map(o => ({ t0: o.start, t1: o.end || t1, kind: "outage",
    label: `${o.wan} down: ${o.reason || ""}` }));
  let start = null;
  for (const s of samples) {
    const down = s.vpp !== null && s.vpp !== undefined && s.vpp < 1;
    if (down && start === null) start = s.t;
    if (!down && start !== null) { out.push({ t0: start, t1: s.t, kind: "vpp", label: "VPP not running" }); start = null; }
  }
  if (start !== null) out.push({ t0: start, t1, kind: "vpp", label: "VPP not running" });
  return out;
}
const outageKey = { cls: "shade", label: "a provider was down", when: sp => sp.some(x => x.kind === "outage") };
const vppKey = { cls: "vpp-shade", label: "VPP wasn't running", when: sp => sp.some(x => x.kind === "vpp") };

function makeCharts() {
  const box = $("charts");
  charts.traffic = new NatgwChart(box, { title: "Packets", unit: "packets/s", shades, legendExtra: [outageKey, vppKey],
    series: [
      { label: "forwarded by the FPGA", color: "--s1", cls: "k1", get: x => x.hw, peak: x => x.max && x.max.hw },
      { label: "sent to VPP", color: "--s2", cls: "k2", get: x => x.punt, peak: x => x.max && x.max.punt },
      { label: "dropped", color: "--s3", cls: "k3", get: x => x.drop }] });
  charts.ratio = new NatgwChart(box, { title: "Share forwarded by the FPGA", unit: "%", max: 100, shades,
    legendExtra: [outageKey, vppKey], series: [{ label: "forwarded by the FPGA", color: "--s1", cls: "k1", get: x => x.ratio }] });
  charts.reasons = new NatgwChart(box, { title: "Why packets went to VPP", unit: "packets/s", stacked: true,
    series: REASONS.map(([k, label, color, cls]) => ({ label, color, cls, get: x => x.punt === null ? null : (x.reasons || {})[k] || 0 }))
      .concat([{ label: "anything else", color: "--s-other", cls: "k-other",
        get: x => x.punt === null ? null : Object.entries(x.reasons || {})
          .filter(([k]) => !REASONS.some(r => r[0] === k)).reduce((a, [, v]) => a + v, 0) }]) });
  charts.wans = new NatgwChart(box, { title: "Inbound from each provider", unit: "packets/s", shades,
    legendExtra: [outageKey], series: [] });
  charts.fill = new NatgwChart(box, { title: "Flow tables in use", unit: "%", max: 100, series: [
    { label: "on-chip", color: "--s1", cls: "k1", get: x => x.onchip },
    { label: "DDR", color: "--s2", cls: "k2", get: x => x.ddr }] });
  charts.tier = new NatgwChart(box, { title: "Flows moved between tables", unit: "per second", series: [
    { label: "to on-chip", color: "--s1", cls: "k1", get: x => x.prom },
    { label: "to DDR", color: "--s2", cls: "k2", get: x => x.dem }] });
  charts.hit = new NatgwChart(box, { title: "DDR lookups that found a flow", unit: "%", max: 100,
    series: [{ label: "hit rate", color: "--s1", cls: "k1", get: x => x.hit }] });
}

function wanSeries() {
  return wanOrder.slice(0, 3).map((n, i) => ({ label: n, color: WAN_SLOTS[i][0], cls: WAN_SLOTS[i][1],
    get: x => (x.wans[n] || {}).rx, peak: x => x.max && x.max.wans && x.max.wans[n] }));
}

function resolutionNote(samples) {
  const b = samples.length ? samples[samples.length - 1].bucket || 1 : 1;
  const span = b >= 86400 ? `${+(b / 86400).toFixed(1)} days` : b >= 3600 ? `${+(b / 3600).toFixed(1)} hours`
    : b >= 60 ? `${b / 60} minute${b === 60 ? "" : "s"}` : `${b} seconds`;
  const each = b === 1 ? "Each point is one second." : `Each point averages ${span}; the thin lines show the peak.`;
  return `${each} One-second samples are kept for 30 days, one-minute summaries for a year.`;
}

async function loadHistory() {
  const box = $("charts");
  box.classList.add("loading");
  try {
    const r = await getJSON(`api/history?seconds=${windowSeconds}`);
    const s = r.samples || [];
    const now = last ? last.ts : (s.length ? s[s.length - 1].t : Date.now() / 1000);
    for (const [k, c] of Object.entries(charts)) {
      c.update(s, r.outages, windowSeconds, now, k === "wans" ? wanSeries() : undefined);
    }
    $("resolution").textContent = resolutionNote(s);
  } catch (e) { /* the stats poll shows the monitor as unreachable */ }
  box.classList.remove("loading");
}

function scheduleHistory() {
  clearInterval(historyTimer);
  historyTimer = setInterval(loadHistory, HISTORY_MS[windowSeconds] || 5000);
}

// ---------------------------------------------------------------- polling

function renderFoot(cur) {
  const s = cur.storage;
  if (!s) return;
  const mb = (s.bytes / 1e6).toFixed(1);
  $("foot").textContent = `History is stored on this machine in ${mb} MB: one-second samples since ` +
    `${when(s.samples_from)}, one-minute summaries since ${when(s.rollup_from)}, and ${num(s.events)} events. ` +
    "Set in Atkinson Hyperlegible Next (SIL Open Font License).";
}

async function pollStats() {
  try {
    const cur = await getJSON("api/stats");
    if (cur.error) return;
    const firstWans = !last;
    last = cur;
    const names = (cur.wans || []).map(w => w.name);
    if (names.join() !== wanOrder.join()) { wanOrder = names; if (!firstWans) loadHistory(); }
    renderHead(cur);
    renderPath(cur);
    renderAlerts(cur);
    renderIsps(cur);
    renderFigures(cur);
    renderPorts(cur);
    renderOutages(cur);
    renderDetail(cur);
    renderFoot(cur);
    if (firstWans) loadHistory();
  } catch (e) {
    const v = $("status");
    v.dataset.sev = "critical";
    v.textContent = "The monitor isn't answering.";
    $("updated").textContent = String(e.message || e);
  }
}

async function pollEvents() {
  try { renderEvents((await getJSON("api/events?limit=300")).events || []); } catch (e) { /* see pollStats */ }
}

function setWindow(seconds) {
  windowSeconds = seconds;
  document.querySelectorAll("#window button").forEach(b =>
    b.setAttribute("aria-checked", String(Number(b.dataset.s) === seconds)));
  try { localStorage.setItem("natgw-window", String(seconds)); } catch (e) { /* private mode */ }
  scheduleHistory();
  loadHistory();
  loadCounter();
}

$("window").addEventListener("click", ev => {
  const b = ev.target.closest("button");
  if (b) setWindow(Number(b.dataset.s));
});
$("window").addEventListener("keydown", ev => {
  if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
  const opts = [...document.querySelectorAll("#window button")];
  const i = opts.findIndex(b => Number(b.dataset.s) === windowSeconds);
  const n = opts[Math.min(Math.max(i + (ev.key === "ArrowRight" ? 1 : -1), 0), opts.length - 1)];
  setWindow(Number(n.dataset.s));
  n.focus();
});

makeCharts();
try {
  const w = Number(localStorage.getItem("natgw-window"));
  if (HISTORY_MS[w]) windowSeconds = w;
} catch (e) { /* private mode */ }
setWindow(windowSeconds);
pollStats();
pollEvents();
setInterval(pollStats, STATS_MS);
setInterval(pollEvents, EVENTS_MS);
window.addEventListener("resize", () => Object.values(charts).forEach(c => c.draw()));
