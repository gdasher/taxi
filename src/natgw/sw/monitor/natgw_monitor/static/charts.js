// SPDX-License-Identifier: BSD-3-Clause
// A small canvas time-series chart for the natgw monitor: 2px lines (or a
// stacked area with 2px surface gaps), recessive hairline grid, one y axis,
// shaded spans (ISP outages, VPP down), thin peak lines, a crosshair with a
// tooltip listing every series (also by keyboard: arrow keys), and a table
// view. Colors come from CSS custom properties (--s1 ...), so light and dark
// are both the validated steps. Text is set with textContent only.
"use strict";

(function () {
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function niceStep(max, n) {
    const raw = max / n;
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * p) return m * p;
    return 10 * p;
  }

  function fmt(v, unit) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    if (unit === "%") return `${v < 10 && v % 1 ? v.toFixed(1) : Math.round(v)}%`;
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "G";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(0) + "k";
    if (a >= 1000) return Math.round(v).toLocaleString();
    if (a >= 10) return Math.round(v).toString();
    return v.toFixed(a && a < 1 ? 2 : 1).replace(/\.0+$/, "");
  }

  function clock(t, span) {
    const d = new Date(t * 1000);
    if (span > 2 * 86400) return d.toLocaleDateString(undefined, { weekday: "short" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: span <= 900 ? "2-digit" : undefined });
  }

  class Chart {
    // opts: {title, unit, series: [{label, color, get, peak?}], stacked, max,
    //        shades: (samples, outages) -> [{t0, t1, kind}], legendExtra: [{cls, label}]}
    constructor(parent, opts) {
      this.o = opts;
      this.root = el("figure", "chart");
      const head = el("div", "chart-head");
      const h = el("h3", null, opts.title);
      if (opts.unit && opts.unit !== "%") h.append(el("span", "unit", opts.unit));
      this.tableBtn = el("button", null, "Show as table");
      this.tableBtn.setAttribute("aria-expanded", "false");
      this.tableBtn.addEventListener("click", () => this.toggleTable());
      head.append(h, this.tableBtn);
      this.plot = el("div", "plot");
      this.cv = el("canvas");
      this.cv.tabIndex = 0;
      this.cv.setAttribute("role", "img");
      this.cv.setAttribute("aria-label", `${opts.title}. Use the left and right arrow keys to read values.`);
      this.tip = el("div", "tip");
      this.tip.hidden = true;
      this.plot.append(this.cv, this.tip);
      this.legend = el("div", "legend");
      this.table = el("div", "chart-table");
      this.table.hidden = true;
      this.root.append(head, this.plot, this.legend, this.table);
      parent.append(this.root);
      this.samples = [];
      this.idx = null;
      this.cv.addEventListener("pointermove", e => this.pointer(e));
      this.cv.addEventListener("pointerleave", () => { this.idx = null; this.draw(); });
      this.cv.addEventListener("keydown", e => this.key(e));
      this.cv.addEventListener("blur", () => { this.idx = null; this.draw(); });
    }

    update(samples, outages, windowSeconds, tEnd, series) {
      if (series) this.o.series = series;
      this.samples = samples;
      this.outages = outages || [];
      this.win = windowSeconds;
      this.tEnd = tEnd;
      this.renderLegend();
      this.draw();
      if (!this.table.hidden) this.renderTable();
    }

    renderLegend() {
      const items = [];
      const s = this.o.series;
      if (s.length > 1 || this.o.stacked) {
        for (const se of s) {
          const i = el("span");
          i.append(el("span", `key ${this.o.stacked ? "" : "line"} ${se.cls}`), se.label);
          items.push(i);
        }
      }
      if (s.some(se => se.peak) && this.samples.some(x => (x.bucket || 1) > 1)) {
        const i = el("span");
        i.append(el("span", `key thin ${s[0].cls}`), "thin lines: the peak within each point");
        items.push(i);
      }
      for (const x of this.o.legendExtra || []) {
        if (x.when && !x.when(this.shadeSpans())) continue;
        const i = el("span");
        i.append(el("span", `key ${x.cls}`), x.label);
        items.push(i);
      }
      this.legend.replaceChildren(...items);
    }

    shadeSpans() {
      return this.o.shades ? this.o.shades(this.samples, this.outages, this.tEnd - this.win, this.tEnd) : [];
    }

    geometry() {
      const W = this.cv.clientWidth, H = this.cv.clientHeight;
      const padL = 52, padR = 10, padT = 8, padB = 22;
      return { W, H, padL, padR, padT, padB, pw: W - padL - padR, ph: H - padT - padB,
        t0: this.tEnd - this.win, t1: this.tEnd };
    }

    values() {
      // per series, per sample: the (stacked) value, null for gaps
      const s = this.o.series, out = s.map(() => []);
      let max = 0;
      for (const x of this.samples) {
        let acc = 0;
        s.forEach((se, i) => {
          let v = se.get(x);
          if (v === null || v === undefined || Number.isNaN(v)) { out[i].push(null); return; }
          if (this.o.stacked) { acc += v; v = acc; }
          out[i].push(v);
          max = Math.max(max, v, se.peak ? (se.peak(x) || 0) : 0);
        });
      }
      return { v: out, max };
    }

    draw() {
      const g0 = this.geometry();
      const { W, H, padL, padT, pw, ph, t0, t1 } = g0;
      if (!W || !H || !this.win) return;
      const dpr = window.devicePixelRatio || 1;
      this.cv.width = Math.round(W * dpr);
      this.cv.height = Math.round(H * dpr);
      const g = this.cv.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, W, H);
      const font = css("--font");
      const { v, max } = this.values();
      let ymax = this.o.max || 0;
      let step;
      if (!ymax) {
        step = niceStep(Math.max(max, 1e-9) * 1.05, 4);
        ymax = Math.max(step * Math.ceil(Math.max(max, 1e-9) * 1.05 / step), step);
      } else {
        step = ymax / 4;
      }
      const x = t => padL + (t - t0) / (t1 - t0) * pw;
      const y = val => padT + ph - Math.min(val, ymax) / ymax * ph;
      this.x = x;

      // shaded spans first, under everything
      for (const sp of this.shadeSpans()) {
        const a = Math.max(x(sp.t0), padL), b = Math.min(x(sp.t1), padL + pw);
        if (b <= a) continue;
        if (sp.kind === "outage") {
          g.fillStyle = css("--crit-wash");
          g.fillRect(a, padT, Math.max(b - a, 2), ph);
          g.fillStyle = css("--crit");
          g.fillRect(a, padT, Math.max(b - a, 2), 2);
        } else {
          g.fillStyle = css("--wash");
          g.fillRect(a, padT, Math.max(b - a, 2), ph);
        }
      }

      // grid and y ticks: hairlines, solid, recessive
      g.font = `12px ${font}`;
      g.lineWidth = 1;
      g.textAlign = "right";
      g.textBaseline = "middle";
      for (let val = 0; val <= ymax + 1e-9; val += step) {
        const yy = Math.round(y(val)) + 0.5;
        g.strokeStyle = val === 0 ? css("--axis") : css("--rule");
        g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + pw, yy); g.stroke();
        g.fillStyle = css("--muted");
        g.fillText(fmt(val, this.o.unit), padL - 8, yy);
      }
      // x ticks
      g.textAlign = "center";
      g.textBaseline = "alphabetic";
      for (let i = 0; i <= 4; i++) {
        const t = t0 + (t1 - t0) * i / 4;
        const label = i === 4 ? "now" : clock(t, this.win);
        const xx = Math.min(Math.max(x(t), padL + 24), padL + pw - 18);
        g.fillText(label, xx, H - 5);
      }

      const s = this.o.series;
      const S = this.samples;
      if (this.o.stacked) {
        // areas from the top layer down, each separated by a 2px surface gap
        for (let i = s.length - 1; i >= 0; i--) {
          g.fillStyle = css(s[i].color);
          g.beginPath();
          let open = false;
          for (let k = 0; k < S.length; k++) {
            const val = v[i][k];
            if (val === null) continue;
            const px = x(S[k].t), py = y(val);
            if (!open) { g.moveTo(px, y(0)); open = true; }
            g.lineTo(px, py);
          }
          if (open) {
            for (let k = S.length - 1; k >= 0; k--) {
              if (v[i][k] === null) continue;
              g.lineTo(x(S[k].t), y(0));
              break;
            }
            g.closePath();
            g.fill();
          }
        }
        g.strokeStyle = css("--surface");
        g.lineWidth = 2;
        for (let i = 0; i < s.length - 1; i++) {
          this.path(g, S, v[i], x, y);
          g.stroke();
        }
      } else {
        for (let i = 0; i < s.length; i++) {
          const col = css(s[i].color);
          if (s[i].peak && S.some(q => (q.bucket || 1) > 1)) {
            g.strokeStyle = col;
            g.globalAlpha = 0.45;
            g.lineWidth = 1;
            this.path(g, S, S.map(q => s[i].peak(q)), x, y);
            g.stroke();
            g.globalAlpha = 1;
          }
          g.strokeStyle = col;
          g.lineWidth = 2;
          g.lineJoin = "round";
          g.lineCap = "round";
          this.path(g, S, v[i], x, y);
          g.stroke();
        }
      }

      // crosshair and tooltip
      if (this.idx !== null && S[this.idx]) {
        const q = S[this.idx];
        const xx = Math.round(x(q.t)) + 0.5;
        g.strokeStyle = css("--muted");
        g.lineWidth = 1;
        g.beginPath(); g.moveTo(xx, padT); g.lineTo(xx, padT + ph); g.stroke();
        for (let i = 0; i < s.length; i++) {
          const val = v[i][this.idx];
          if (val === null) continue;
          g.fillStyle = css("--surface");
          g.beginPath(); g.arc(xx, y(val), 6, 0, 2 * Math.PI); g.fill();
          g.fillStyle = css(s[i].color);
          g.beginPath(); g.arc(xx, y(val), 4, 0, 2 * Math.PI); g.fill();
        }
        this.showTip(q, xx, g0);
      } else {
        this.tip.hidden = true;
      }
    }

    path(g, S, vals, x, y) {
      g.beginPath();
      let pen = false;
      for (let k = 0; k < S.length; k++) {
        const val = vals[k];
        if (val === null || val === undefined) { pen = false; continue; }
        const px = x(S[k].t), py = y(val);
        if (pen) g.lineTo(px, py); else { g.moveTo(px, py); pen = true; }
      }
    }

    showTip(q, xx, geo) {
      const rows = [el("div", "when", new Date(q.t * 1000).toLocaleString() +
        ((q.bucket || 1) > 1 ? ` (average of ${q.bucket >= 60 ? q.bucket / 60 + " min" : q.bucket + " s"})` : ""))];
      const s = this.o.series;
      for (let i = s.length - 1; i >= 0; i--) {
        const se = s[i];
        const r = el("div", "row");
        const k = el("span", `k ${se.cls}`);
        let text = fmt(se.get(q), this.o.unit);
        if (se.peak && (q.bucket || 1) > 1) text += `, peak ${fmt(se.peak(q), this.o.unit)}`;
        r.append(k, el("b", null, text), el("span", null, se.label));
        rows.push(r);
      }
      for (const sp of this.shadeSpans()) {
        if (q.t >= sp.t0 && q.t <= sp.t1 && sp.label) rows.push(el("div", "when", sp.label));
      }
      this.tip.replaceChildren(...rows);
      this.tip.hidden = false;
      const tw = this.tip.offsetWidth;
      let left = xx + 12;
      if (left + tw > geo.W) left = xx - tw - 12;
      this.tip.style.left = `${Math.max(left, 0)}px`;
      this.tip.style.top = `${geo.padT}px`;
    }

    nearest(px) {
      const S = this.samples;
      if (!S.length || !this.x) return null;
      let best = null, bd = Infinity;
      for (let k = 0; k < S.length; k++) {
        const d = Math.abs(this.x(S[k].t) - px);
        if (d < bd) { bd = d; best = k; }
      }
      return best;
    }

    pointer(e) {
      const r = this.cv.getBoundingClientRect();
      this.idx = this.nearest(e.clientX - r.left);
      this.draw();
    }

    key(e) {
      const n = this.samples.length;
      if (!n) return;
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        const d = e.key === "ArrowLeft" ? -1 : 1;
        this.idx = this.idx === null ? n - 1 : Math.min(Math.max(this.idx + d * (e.shiftKey ? 10 : 1), 0), n - 1);
        this.draw();
        e.preventDefault();
      } else if (e.key === "Escape") {
        this.idx = null;
        this.draw();
      }
    }

    toggleTable() {
      this.table.hidden = !this.table.hidden;
      this.tableBtn.textContent = this.table.hidden ? "Show as table" : "Hide table";
      this.tableBtn.setAttribute("aria-expanded", String(!this.table.hidden));
      if (!this.table.hidden) this.renderTable();
    }

    renderTable() {
      const s = this.o.series;
      const t = el("table", "grid num");
      const hr = el("tr");
      hr.append(el("th", null, "Time"));
      for (const se of s) hr.append(el("th", "n", se.label));
      const thead = el("thead");
      thead.append(hr);
      const tb = el("tbody");
      for (const q of this.samples.slice(-120).reverse()) {
        const tr = el("tr");
        tr.append(el("td", null, new Date(q.t * 1000).toLocaleString()));
        for (const se of s) tr.append(el("td", null, fmt(se.get(q), this.o.unit)));
        tb.append(tr);
      }
      t.append(thead, tb);
      this.table.replaceChildren(t);
    }
  }

  window.NatgwChart = Chart;
  window.natgwFmt = fmt;
})();
