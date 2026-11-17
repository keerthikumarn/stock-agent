/* Stock desk — vanilla JS client for the stock research agent.
 *
 *  /api   -> agent API (streams run steps over Server-Sent Events)
 *  /prom  -> Prometheus query API (system health rail)
 *  /tempo -> Tempo trace API (distributed trace waterfall)
 *  /charts-> PNGs written by the Python tool
 * All paths are same-origin via the nginx reverse proxy; override in config.js.
 */
(() => {
  "use strict";

  const CFG = Object.assign(
    {
      apiBase: "/api",
      promBase: "/prom",
      tempoBase: "/tempo",
      chartsBase: "/charts",
      grafanaUrl: "http://localhost:3000",
      opsRefreshMs: 15000,
      historyTurns: 6,
    },
    window.STOCK_DESK_CONFIG || {}
  );

  const STORAGE_KEY = "stockdesk.conversations.v1";
  const MAX_CONVERSATIONS = 30;

  const TOOL_LABELS = {
    wikipedia_tool: "Reading Wikipedia",
    stock_data_tool: "Fetching price history",
    python_repl_tool: "Running Python",
  };

  const SUGGESTIONS = [
    ["Give me a short summary of Apple Inc. and its stock performance over the last 30 days.", "Wikipedia and prices"],
    ["How has Tesla's stock moved over the last 60 days?", "Price history"],
    ["Compare Microsoft and Amazon closing prices over the last 2 weeks.", "Two companies"],
    ["Plot Netflix's closing price for the last 90 days.", "Draws a chart"],
  ];

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const svgNS = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs = {}) => {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);

  function fmtMs(ms) {
    if (ms == null || Number.isNaN(ms)) return "–";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
    return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
  }
  function fmtNum(n, digits = 0) {
    if (n == null || Number.isNaN(n)) return "–";
    const a = Math.abs(n);
    if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (a >= 1e4) return `${(n / 1e3).toFixed(0)}k`;
    if (a >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
    return n.toFixed(digits);
  }
  const fmtPct = (r) => (r == null || Number.isNaN(r) ? "–" : `${(r * 100).toFixed(r > 0 && r < 0.1 ? 1 : 0)}%`);
  function fmtAgo(seconds) {
    if (seconds < 90) return `up ${Math.round(seconds)}s`;
    if (seconds < 5400) return `up ${Math.round(seconds / 60)}m`;
    if (seconds < 172800) return `up ${Math.round(seconds / 3600)}h`;
    return `up ${Math.round(seconds / 86400)}d`;
  }

  function grafanaTraceUrl(traceId) {
    const panes = {
      t: {
        datasource: "tempo",
        queries: [{ refId: "A", datasource: { type: "tempo", uid: "tempo" }, queryType: "traceql", query: traceId }],
        range: { from: "now-6h", to: "now" },
      },
    };
    return `${CFG.grafanaUrl}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`;
  }

  // ------------------------------------------------------------------ markdown (safe subset)
  function inline(s) {
    let out = esc(s);
    const codes = [];
    out = out.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
    out = out
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  }
  const isNumeric = (s) => /^-?[\d,]*\.?\d+(e[+-]?\d+)?%?$/i.test(s.trim());
  const prettyCell = (s) => (/^-?\d*\.?\d+e[+-]?\d+$/i.test(s.trim()) ? Number(s).toLocaleString("en-US", { maximumFractionDigits: 0 }) : s);

  function tableHtml(lines) {
    const head = splitRow(lines[0]);
    const body = lines.slice(2).map(splitRow);
    const numCol = head.map((_, i) => body.length > 0 && body.every((r) => !r[i] || isNumeric(r[i])));
    const th = head.map((h, i) => `<th scope="col"${numCol[i] ? ' class="num"' : ""}>${inline(h)}</th>`).join("");
    const rows = body
      .map((r) => `<tr>${head.map((_, i) => `<td${numCol[i] ? ' class="num"' : ""}>${inline(prettyCell(r[i] ?? ""))}</td>`).join("")}</tr>`)
      .join("");
    return `<div class="table-wrap"><table class="md-table"><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function md(src) {
    const lines = String(src ?? "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        html.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const buf = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) buf.push(lines[i++]);
        html.push(tableHtml(buf));
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        html.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
        i++;
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""))}</li>`);
          i++;
        }
        html.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }
      if (!line.trim()) {
        i++;
        continue;
      }
      const para = [];
      while (
        i < lines.length && lines[i].trim() && !/^```|^#{1,4}\s|^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
        !/^\s*\|.*\|\s*$/.test(lines[i])
      ) para.push(lines[i++]);
      html.push(`<p>${inline(para.join(" "))}</p>`);
    }
    return html.join("");
  }

  // ------------------------------------------------------------------ price data
  function parsePriceTable(text) {
    const lines = String(text || "").split("\n").filter((l) => /^\s*\|.*\|\s*$/.test(l));
    if (lines.length < 3) return null;
    const head = splitRow(lines[0]).map((h) => h.toLowerCase());
    const closeIdx = head.findIndex((h) => h === "close" || h === "adj close");
    if (closeIdx < 0) return null;
    const points = [];
    for (const l of lines.slice(2)) {
      const r = splitRow(l);
      const v = parseFloat((r[closeIdx] || "").replace(/,/g, ""));
      if (r[0] && !Number.isNaN(v)) points.push({ date: r[0], close: v });
    }
    points.sort((a, b) => (a.date < b.date ? -1 : 1));
    return points.length >= 2 ? points : null;
  }

  function priceSeriesFromSteps(steps) {
    const series = [];
    for (const s of steps || []) {
      if (s.type === "tool" && s.name === "stock_data_tool" && s.status === "ok" && s.result) {
        const points = parsePriceTable(s.result);
        if (points) series.push({ ticker: String(s.args?.company_ticker || "").toUpperCase(), points });
      }
    }
    return series;
  }

  function renderPriceChart(figure, { ticker, points }) {
    figure.replaceChildren();
    const first = points[0].close;
    const last = points[points.length - 1].close;
    const change = (last - first) / first;
    const dir = change >= 0 ? "up" : "down";
    const lo = Math.min(...points.map((p) => p.close));
    const hi = Math.max(...points.map((p) => p.close));

    const head = document.createElement("div");
    head.className = "price-head";
    head.innerHTML =
      `<span class="price-ticker">${esc(ticker || "Close")}</span>` +
      `<span class="price-last">${last.toFixed(2)}</span>` +
      `<span class="price-change ${dir}">${change >= 0 ? "+" : "−"}${Math.abs(last - first).toFixed(2)} (${(Math.abs(change) * 100).toFixed(2)}%)</span>` +
      `<span class="price-range">${esc(points[0].date)} to ${esc(points[points.length - 1].date)}, ${points.length} sessions</span>`;

    const W = 640, H = 190, padL = 6, padR = 46, padT = 10, padB = 22;
    const span = hi - lo || 1;
    const x = (i) => padL + (i / (points.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);

    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: `price-svg ${dir}`, role: "img",
      "aria-label": `${ticker} closing price from ${points[0].date} to ${points[points.length - 1].date}: ${first.toFixed(2)} to ${last.toFixed(2)}` });
    svg.setAttribute("preserveAspectRatio", "none");
    for (let k = 0; k <= 3; k++) {
      const v = lo + (span * k) / 3;
      svg.append(svgEl("line", { class: "grid", x1: padL, x2: W - padR, y1: y(v), y2: y(v) }));
      const t = svgEl("text", { class: "axis", x: W - padR + 6, y: y(v) + 4 });
      t.textContent = v.toFixed(2);
      svg.append(t);
    }
    const linePath = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join("");
    svg.append(svgEl("path", { class: "area", d: `${linePath}L${x(points.length - 1)},${H - padB}L${x(0)},${H - padB}Z` }));
    svg.append(svgEl("path", { class: "line", d: linePath }));
    for (const idx of [0, Math.floor((points.length - 1) / 2), points.length - 1]) {
      const t = svgEl("text", { class: "axis", x: x(idx), y: H - 4, "text-anchor": idx === 0 ? "start" : idx === points.length - 1 ? "end" : "middle" });
      t.textContent = points[idx].date;
      svg.append(t);
    }
    const cross = svgEl("line", { class: "cross", y1: padT, y2: H - padB, visibility: "hidden" });
    const dot = svgEl("circle", { class: "cross-dot", r: 4, visibility: "hidden" });
    svg.append(cross, dot);

    const tip = document.createElement("div");
    tip.className = "price-tip";
    tip.textContent = "Move across the chart to read daily closes";
    svg.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(points.length - 1, Math.round(((px - padL) / (W - padL - padR)) * (points.length - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(points[i].close)); dot.setAttribute("visibility", "visible");
      const d = (points[i].close - first) / first;
      tip.textContent = `${points[i].date}: close ${points[i].close.toFixed(2)} (${d >= 0 ? "+" : "−"}${(Math.abs(d) * 100).toFixed(2)}% from start)`;
    });
    svg.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
    });
    figure.append(head, svg, tip);
    figure.hidden = false;
  }

  function chartFilesFromSteps(steps) {
    const names = new Set();
    for (const s of steps || []) {
      if (s.type !== "tool" || s.name !== "python_repl_tool" || s.status !== "ok") continue;
      const hay = `${s.result || ""}\n${s.args?.code || ""}`;
      for (const m of hay.matchAll(/([\w.-]+\.(?:png|jpg|jpeg|svg))/gi)) names.add(m[1].split("/").pop());
    }
    return [...names];
  }

  // ------------------------------------------------------------------ storage
  const Store = {
    all() {
      try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; } catch { return []; }
    },
    save(list) {
      const trimmed = list.slice(0, MAX_CONVERSATIONS).map((c) => ({
        ...c,
        turns: c.turns.map((t) => ({ ...t, steps: (t.steps || []).map((s) => ({ ...s, result: s.result ? String(s.result).slice(0, 6000) : s.result })) })),
      }));
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed)); } catch { /* storage full: keep in memory */ }
    },
  };

  // ------------------------------------------------------------------ turn view
  class TurnView {
    constructor(turn) {
      this.turn = turn;
      this.steps = new Map();
      this.selected = null;
      this.root = $("#tpl-turn").content.firstElementChild.cloneNode(true);
      this.q = $(".question", this.root);
      this.live = $(".live-status", this.root);
      this.liveText = $(".live-text", this.root);
      this.prose = $(".prose", this.root);
      this.figure = $(".price-figure", this.root);
      this.charts = $(".charts", this.root);
      this.errorBox = $(".error-box", this.root);
      this.tape = $(".tape", this.root);
      this.tapeTotal = $(".tape-total", this.root);
      this.facts = $(".run-facts", this.root);
      this.stepsList = $(".steps", this.root);
      this.stepsPanel = $(".steps-panel", this.root);
      this.tracePanel = $(".trace-panel", this.root);
      this.tabs = [...this.root.querySelectorAll(".tab")];
      this.copyBtn = $(".copy-trace", this.root);
      this.grafana = $(".open-grafana", this.root);
      this.q.textContent = turn.q;
      this.t0 = performance.now();
      this.tabs.forEach((t) => t.addEventListener("click", () => this.showTab(t.dataset.tab)));
      this.copyBtn.addEventListener("click", () => this.copyTrace());
    }

    // -- live updates
    start(traceId) {
      this.turn.trace_id = traceId;
      this.enableTrace();
      this.tick = setInterval(() => this.drawTape(), 200);
      this.setLive("Planning the answer", "llm");
    }
    setLive(text, kind) {
      this.live.hidden = false;
      this.live.dataset.kind = kind || "llm";
      this.liveText.textContent = text;
    }
    onStep(ev) {
      const prev = this.steps.get(ev.id) || {};
      const step = { ...prev, ...ev };
      delete step.event;
      this.steps.set(ev.id, step);
      if (ev.event === "step_start") {
        this.setLive(step.type === "tool" ? `${TOOL_LABELS[step.name] || `Calling ${step.name}`}` : `Thinking with ${step.name}`, step.type);
      }
      this.renderSteps();
      this.drawTape();
      if (step.type === "tool" && step.name === "stock_data_tool" && step.status === "ok") this.renderVisuals();
    }
    finish(final) {
      clearInterval(this.tick);
      Object.assign(this.turn, {
        answer: final.answer, trace_id: final.trace_id, usage: final.usage,
        duration_ms: final.duration_ms, steps: final.steps, error: null,
      });
      this.renderComplete();
    }
    fail(error) {
      clearInterval(this.tick);
      this.turn.error = error;
      this.turn.steps = [...this.steps.values()];
      this.turn.duration_ms = performance.now() - this.t0;
      this.renderComplete();
    }

    // -- full render (finished or restored from storage)
    renderComplete() {
      const t = this.turn;
      this.live.hidden = true;
      this.steps = new Map((t.steps || []).map((s) => [s.id, s]));
      this.prose.innerHTML = t.answer ? md(t.answer) : "";
      if (t.error) {
        this.errorBox.hidden = false;
        this.errorBox.innerHTML = `<strong>${esc(t.error.title)}</strong>${esc(t.error.detail || "")}`;
      }
      this.renderVisuals();
      this.renderSteps();
      this.drawTape(t.duration_ms);
      this.renderFacts();
      if (t.trace_id) this.enableTrace();
    }

    renderVisuals() {
      const steps = [...this.steps.values()];
      const series = priceSeriesFromSteps(steps);
      if (series.length) renderPriceChart(this.figure, series[series.length - 1]);
      const files = chartFilesFromSteps(steps);
      this.charts.replaceChildren(
        ...files.map((f) => {
          const img = new Image();
          img.alt = `Chart produced by the agent: ${f}`;
          img.loading = "lazy";
          img.src = `${CFG.chartsBase}/${encodeURIComponent(f)}`;
          img.onerror = () => img.remove();
          return img;
        })
      );
    }

    renderFacts() {
      const u = this.turn.usage;
      const rows = [["Time", fmtMs(this.turn.duration_ms)]];
      if (u) {
        rows.push(["Tokens", `${fmtNum(u.input_tokens)} in, ${fmtNum(u.output_tokens)} out`]);
        rows.push(["Model calls", String(u.llm_calls)]);
        rows.push(["Tool calls", String(u.tool_calls)]);
        if (u.tool_errors) rows.push(["Tool failures", String(u.tool_errors), "bad"]);
        if (u.cost_usd) rows.push(["Cost", `$${u.cost_usd.toFixed(4)}`]);
      }
      this.facts.innerHTML = rows.map(([k, v, cls]) => `<div><dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ""}>${esc(v)}</dd></div>`).join("");
    }

    elapsedMs() { return performance.now() - this.t0; }

    drawTape(totalMs) {
      const steps = [...this.steps.values()];
      const now = this.elapsedMs();
      const end = (s) => (s.duration_ms != null ? s.start_ms + s.duration_ms : Math.max(s.start_ms, now));
      const total = Math.max(totalMs || 0, ...steps.map(end), totalMs ? 0 : now, 1);
      this.tapeTotal.textContent = fmtMs(total);
      const existing = new Map([...this.tape.children].map((n) => [n.dataset.id, n]));
      for (const s of steps) {
        let seg = existing.get(String(s.id));
        if (!seg) {
          seg = document.createElement("button");
          seg.type = "button";
          seg.dataset.id = s.id;
          seg.setAttribute("role", "listitem");
          seg.addEventListener("click", () => this.select(s.id, true));
          this.tape.append(seg);
        }
        const dur = end(s) - s.start_ms;
        const label = s.type === "tool" ? s.name.replace(/_tool$/, "") : "model";
        seg.className = `seg ${s.type}${s.status === "error" ? " error" : ""}${s.status === "running" ? " running" : ""}${this.selected === s.id ? " selected" : ""}`;
        seg.style.left = `${(s.start_ms / total) * 100}%`;
        seg.style.width = `${Math.max((dur / total) * 100, 0.4)}%`;
        seg.textContent = dur / total > 0.09 ? label : "";
        seg.title = `${s.type === "tool" ? s.name : `Model (${s.name})`}: ${fmtMs(dur)}${s.status === "error" ? ", failed" : ""}`;
        seg.setAttribute("aria-label", seg.title);
      }
    }

    renderSteps() {
      const steps = [...this.steps.values()].sort((a, b) => a.id - b.id);
      if (!this.stepsList.children.length && steps.length && this.tabs.every((t) => t.getAttribute("aria-selected") !== "true")) {
        this.showTab("steps");
      }
      const existing = new Map([...this.stepsList.children].map((n) => [n.dataset.id, n]));
      for (const s of steps) {
        let li = existing.get(String(s.id));
        if (!li) {
          li = document.createElement("li");
          li.dataset.id = s.id;
          li.innerHTML =
            `<button type="button" class="step-row" aria-expanded="false"><span class="step-kind" aria-hidden="true"></span>` +
            `<span class="step-name"></span><span class="step-meta"></span><span class="step-status"></span></button>` +
            `<div class="step-detail" hidden></div>`;
          $(".step-row", li).addEventListener("click", () => this.select(s.id, false, true));
          this.stepsList.append(li);
        }
        li.className = `step ${s.type} ${s.status}${this.selected === s.id ? " selected" : ""}`;
        const nameEl = $(".step-name", li);
        if (s.type === "tool") {
          const argText = Object.entries(s.args || {})
            .filter(([k]) => k !== "code")
            .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
          const codeLines = s.args?.code ? String(s.args.code).split("\n").length : 0;
          const summary = codeLines ? `${codeLines} line${codeLines > 1 ? "s" : ""} of code` : argText;
          nameEl.innerHTML = `${esc(s.name)}<small>${esc(summary)}</small>`;
        } else {
          const req = (s.requested_tools || []).length ? `asked for ${s.requested_tools.join(", ")}` : s.status === "ok" ? "wrote the answer" : "";
          nameEl.innerHTML = `Model<small>${esc(req)}</small>`;
        }
        const meta = [];
        if (s.type === "llm" && s.input_tokens != null) meta.push(`${fmtNum(s.input_tokens)} in / ${fmtNum(s.output_tokens)} out`);
        meta.push(s.duration_ms != null ? fmtMs(s.duration_ms) : "…");
        $(".step-meta", li).textContent = meta.join("   ");
        $(".step-status", li).textContent = s.status === "ok" ? "Done" : s.status === "error" ? "Failed" : "Running";
        const detail = $(".step-detail", li);
        if (!detail.hidden) this.fillDetail(detail, s);
      }
    }

    fillDetail(detail, s) {
      const parts = [];
      if (s.type === "tool") {
        const { code, ...rest } = s.args || {};
        if (Object.keys(rest).length) parts.push(`<div><p class="detail-label">Arguments</p><pre class="detail-pre">${esc(JSON.stringify(rest, null, 2))}</pre></div>`);
        if (code) parts.push(`<div><p class="detail-label">Code</p><pre class="detail-pre">${esc(code)}</pre></div>`);
        if (s.result != null) {
          const label = s.status === "error" ? `Error${s.error_type ? ` (${esc(s.error_type)})` : ""}` : "Result";
          const body = /\|.*\|/.test(s.result) ? `<div class="md">${md(s.result)}</div>` : `<pre class="detail-pre">${esc(s.result)}</pre>`;
          parts.push(`<div><p class="detail-label">${label}</p>${body}</div>`);
        }
      } else {
        const rows = [
          ["Model", s.name], ["Input tokens", s.input_tokens], ["Output tokens", s.output_tokens],
          ["Stopped because", s.finish_reason], ["Error", s.error_type],
        ].filter(([, v]) => v != null && v !== "");
        parts.push(`<pre class="detail-pre">${esc(rows.map(([k, v]) => `${k}: ${v}`).join("\n"))}</pre>`);
      }
      detail.innerHTML = parts.join("") || '<p class="muted">Waiting for this step to finish…</p>';
    }

    select(id, fromTape, toggle) {
      const li = this.stepsList.querySelector(`[data-id="${id}"]`);
      if (!li) return;
      const detail = $(".step-detail", li);
      const row = $(".step-row", li);
      const open = toggle ? detail.hidden : true;
      detail.hidden = !open;
      row.setAttribute("aria-expanded", String(open));
      this.selected = open ? id : null;
      if (open) this.fillDetail(detail, this.steps.get(id));
      this.showTab("steps");
      this.renderSteps();
      this.drawTape(this.turn.duration_ms);
      if (fromTape) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    showTab(name) {
      for (const t of this.tabs) t.setAttribute("aria-selected", String(t.dataset.tab === name));
      this.stepsPanel.hidden = name !== "steps";
      this.tracePanel.hidden = name !== "trace";
      if (name === "trace") this.loadTrace();
    }

    enableTrace() {
      const id = this.turn.trace_id;
      if (!id) return;
      this.tabs.find((t) => t.dataset.tab === "trace").disabled = false;
      this.copyBtn.disabled = false;
      this.grafana.href = grafanaTraceUrl(id);
      this.grafana.hidden = false;
    }

    async copyTrace() {
      try {
        await navigator.clipboard.writeText(this.turn.trace_id);
        this.copyBtn.textContent = "Copied";
      } catch {
        this.copyBtn.textContent = this.turn.trace_id;
      }
      setTimeout(() => (this.copyBtn.textContent = "Copy trace ID"), 1600);
    }

    async loadTrace(force) {
      if ((this.traceLoaded && !force) || this.traceLoading || !this.turn.trace_id) return;
      this.traceLoading = true;
      this.traceLoaded = false;
      const note = (text) => (this.tracePanel.innerHTML = `<p class="trace-note">${esc(text)}</p>`);
      note("Collecting spans from the agent and the MCP server…");
      // Spans arrive in export batches, so a trace can be partial for a few seconds.
      // Keep polling until the root agent span is present and the span count is stable.
      let lastCount = -1;
      for (let attempt = 0; attempt < 12; attempt++) {
        try {
          const r = await fetch(`${CFG.tempoBase}/api/traces/${this.turn.trace_id}`, { cache: "no-store" });
          if (r.ok) {
            const spans = flattenTrace(await r.json());
            const hasRoot = spans.some((x) => x.name.startsWith("invoke_agent"));
            const complete = hasRoot && spans.length === lastCount;
            if (spans.length) renderWaterfall(this.tracePanel, spans, false, !complete);
            if (complete) { this.traceLoaded = true; break; }
            lastCount = spans.length;
          } else if (r.status !== 404) {
            note(`The trace store answered ${r.status}. Open the trace in Grafana instead.`);
            break;
          }
        } catch {
          note("Can't reach the trace store. Open the trace in Grafana instead.");
          break;
        }
        await new Promise((res) => setTimeout(res, 2000));
      }
      if (!this.traceLoaded && lastCount <= 0) note("The trace hasn't arrived yet. Spans are exported in batches; open this tab again in a few seconds.");
      this.traceLoading = false;
    }
  }

  // ------------------------------------------------------------------ trace waterfall
  function attrValue(attrs, key) {
    const a = (attrs || []).find((x) => x.key === key);
    if (!a) return undefined;
    const v = a.value || {};
    return v.stringValue ?? v.intValue ?? v.boolValue ?? v.doubleValue;
  }
  const nsToMs = (ns) => Number(BigInt(ns) / 1000n) / 1000;

  function flattenTrace(data) {
    const batches = data.batches || data.resourceSpans || data.trace?.resourceSpans || [];
    const spans = [];
    for (const b of batches) {
      const svc = attrValue(b.resource?.attributes, "service.name") || "unknown";
      for (const ss of b.scopeSpans || b.instrumentationLibrarySpans || []) {
        for (const s of ss.spans || []) {
          const code = s.status?.code;
          spans.push({
            id: s.spanId, parent: s.parentSpanId || null, name: s.name, svc,
            start: nsToMs(s.startTimeUnixNano), end: nsToMs(s.endTimeUnixNano),
            error: code === "STATUS_CODE_ERROR" || code === 2,
            tool: attrValue(s.attributes, "gen_ai.tool.name"),
            tokens: attrValue(s.attributes, "gen_ai.usage.output_tokens"),
          });
        }
      }
    }
    return spans;
  }

  const isHttpSpan = (s) => /^(GET|POST|PUT|DELETE)(\s|$)/.test(s.name);

  function renderWaterfall(panel, spans, showHttp = false, partial = false) {
    const byId = new Map(spans.map((s) => [s.id, s]));
    // Hidden HTTP spans are skipped; their children attach to the nearest visible ancestor.
    const visible = spans.filter((s) => showHttp || !isHttpSpan(s));
    const visibleIds = new Set(visible.map((s) => s.id));
    const visibleParent = (s) => {
      let p = s.parent;
      while (p && byId.has(p) && !visibleIds.has(p)) p = byId.get(p).parent;
      return p && visibleIds.has(p) ? p : null;
    };
    const kids = new Map();
    const roots = [];
    for (const s of visible) {
      const p = visibleParent(s);
      if (p) {
        if (!kids.has(p)) kids.set(p, []);
        kids.get(p).push(s);
      } else roots.push(s);
    }
    const ordered = [];
    const walk = (s, depth) => {
      ordered.push({ ...s, depth });
      (kids.get(s.id) || []).sort((a, b) => a.start - b.start).forEach((c) => walk(c, depth + 1));
    };
    roots.sort((a, b) => a.start - b.start).forEach((r) => walk(r, 0));

    const t0 = Math.min(...spans.map((s) => s.start));
    const t1 = Math.max(...spans.map((s) => s.end));
    const total = Math.max(t1 - t0, 1);
    const services = new Set(spans.map((s) => s.svc));

    const wrap = document.createElement("div");
    wrap.className = "wf";
    const tools = document.createElement("div");
    tools.className = "wf-tools";
    const summary = document.createElement("p");
    summary.className = "trace-note";
    summary.textContent = `${spans.length} spans across ${services.size} service${services.size > 1 ? "s" : ""} (${[...services].join(" and ")}), ${fmtMs(total)} end to end.` +
      (partial ? " More spans are still arriving…" : "");
    const toggle = document.createElement("label");
    toggle.className = "wf-toggle";
    toggle.innerHTML = `<input type="checkbox"${showHttp ? " checked" : ""}> Show HTTP requests`;
    toggle.querySelector("input").addEventListener("change", (e) => renderWaterfall(panel, spans, e.target.checked, partial));
    tools.append(summary, toggle);
    wrap.append(tools);

    for (const s of ordered.slice(0, 250)) {
      const kind = s.svc === "mcp-tools" ? "mcp" : "agent";
      const row = document.createElement("div");
      row.className = "wf-row";
      const name = document.createElement("div");
      name.className = "wf-name";
      name.style.paddingLeft = `${Math.min(s.depth, 10) * 12}px`;
      name.innerHTML = `<span class="wf-svc ${kind}">${kind === "mcp" ? "MCP" : "Agent"}</span><span class="n" title="${esc(s.name)}">${esc(s.name)}</span>`;
      const track = document.createElement("div");
      track.className = "wf-track";
      const bar = document.createElement("div");
      bar.className = `wf-bar ${kind}${s.error ? " err" : ""}`;
      const left = ((s.start - t0) / total) * 100;
      const width = Math.max(((s.end - s.start) / total) * 100, 0.3);
      bar.style.left = `${left}%`;
      bar.style.width = `${width}%`;
      const dur = document.createElement("span");
      dur.className = "wf-dur";
      dur.textContent = fmtMs(s.end - s.start);
      if (width > 22) {
        dur.classList.add("inside");                       // wide bar: label inside, right-aligned
        dur.style.right = `${100 - left - width + 1}%`;
      } else if (left + width < 82) {
        dur.style.left = `${left + width + 1}%`;            // room on the right
      } else {
        dur.style.right = `${100 - left + 1}%`;             // near the end: label before the bar
      }
      track.append(bar, dur);
      row.append(name, track);
      wrap.append(row);
    }
    panel.replaceChildren(wrap);
  }

  // ------------------------------------------------------------------ app state
  const els = {
    thread: $("#thread"), welcome: $("#welcome"), suggestions: $("#suggestions"),
    form: $("#composer"), input: $("#composer-input"), send: $("#send"), stop: $("#stop"),
    historyList: $("#history-list"), historyEmpty: $("#history-empty"), title: $("#thread-title"),
    status: $("#agent-status"), model: $("#agent-model"), tools: $("#agent-tools"),
  };
  let conversations = Store.all();
  let current = null;
  let inflight = null;

  function newConversation() {
    current = { id: uid(), title: "New conversation", updated: Date.now(), turns: [] };
    renderThread();
    renderHistory();
    els.input.focus();
  }

  function openConversation(id) {
    const c = conversations.find((x) => x.id === id);
    if (!c || inflight) return;
    current = c;
    renderThread();
    renderHistory();
    closeDrawers();
  }

  function deleteConversation(id) {
    conversations = conversations.filter((c) => c.id !== id);
    Store.save(conversations);
    if (current?.id === id) newConversation();
    else renderHistory();
  }

  function persist() {
    current.updated = Date.now();
    conversations = [current, ...conversations.filter((c) => c.id !== current.id)];
    Store.save(conversations);
    renderHistory();
  }

  function renderHistory() {
    els.historyList.replaceChildren(
      ...conversations.map((c) => {
        const li = document.createElement("li");
        li.className = "history-item";
        if (current?.id === c.id) li.setAttribute("aria-current", "true");
        li.innerHTML =
          `<button type="button" class="open" title="${esc(c.title)}">${esc(c.title)}</button>` +
          `<button type="button" class="del" aria-label="Delete conversation: ${esc(c.title)}"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg></button>`;
        li.querySelector(".open").addEventListener("click", () => openConversation(c.id));
        li.querySelector(".del").addEventListener("click", () => deleteConversation(c.id));
        return li;
      })
    );
    els.historyEmpty.hidden = conversations.length > 0;
  }

  function renderThread() {
    els.thread.querySelectorAll(".turn").forEach((n) => n.remove());
    els.welcome.hidden = current.turns.length > 0;
    els.title.textContent = current.title;
    for (const t of current.turns) {
      const view = new TurnView(t);
      els.thread.append(view.root);
      view.renderComplete();
    }
    els.thread.scrollTop = els.thread.scrollHeight;
  }

  function historyForApi() {
    const out = [];
    for (const t of current.turns.slice(-CFG.historyTurns)) {
      if (!t.answer) continue;
      out.push({ role: "user", content: t.q.slice(0, 4000) }, { role: "assistant", content: t.answer.slice(0, 4000) });
    }
    return out;
  }

  function errorFor(status, code, message) {
    if (status === 503) return { title: "The agent is still starting", detail: "It is loading its tools from the MCP server. Try again in a few seconds." };
    if (status === 504) return { title: "The agent ran out of time", detail: "Try a narrower question, or check model latency on the dashboard." };
    if (status === 422 && code === "recursion_limit") return { title: "The agent took too many steps", detail: "It kept calling tools without finishing. Rephrase the question more specifically." };
    if (code === "ConnectError" || code === "ConnectTimeout") return { title: "The language model isn't reachable", detail: "The agent couldn't connect to Ollama. Check that Ollama is running." };
    return { title: "The agent couldn't finish this answer", detail: `${message || "Unexpected error"}${code ? ` (${code})` : ""}.` };
  }

  // ------------------------------------------------------------------ asking
  async function ask(query) {
    if (inflight) return;
    if (!current.turns.length) {
      current.title = query.length > 64 ? `${query.slice(0, 61)}…` : query;
      els.title.textContent = current.title;
    }
    const turn = { id: uid(), q: query, at: Date.now() };
    const history = historyForApi();
    current.turns.push(turn);
    els.welcome.hidden = true;
    const view = new TurnView(turn);
    els.thread.append(view.root);
    view.setLive("Sending your question", "llm");
    view.root.scrollIntoView({ block: "start", behavior: "smooth" });

    const controller = new AbortController();
    inflight = controller;
    setBusy(true);
    let finished = false;
    try {
      const r = await fetch(`${CFG.apiBase}/v1/query/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ query, session_id: current.id, history }),
        signal: controller.signal,
      });
      if (!r.ok || !r.body) {
        let detail = "";
        try { detail = JSON.stringify((await r.json()).detail); } catch { /* not json */ }
        view.fail(errorFor(r.status, null, `The agent API answered ${r.status} ${detail}`));
        finished = true;
      } else {
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
            if (!data) continue;
            const ev = JSON.parse(data);
            if (ev.event === "start") view.start(ev.trace_id);
            else if (ev.event === "step_start" || ev.event === "step_end") view.onStep(ev);
            else if (ev.event === "final") { view.finish(ev); finished = true; }
            else if (ev.event === "error") { view.fail(errorFor(ev.status, ev.error, ev.message)); finished = true; }
          }
        }
        if (!finished) { view.fail({ title: "The connection closed early", detail: "The agent stopped streaming before it finished. Check the agent logs." }); finished = true; }
      }
    } catch (err) {
      if (err.name === "AbortError") view.fail({ title: "Stopped", detail: "You stopped this answer before it finished." });
      else view.fail({ title: "Can't reach the agent", detail: `No response from ${CFG.apiBase}. Check that the agent container is running.` });
    } finally {
      inflight = null;
      setBusy(false);
      persist();
      refreshOps();
    }
  }

  function setBusy(busy) {
    els.thread.setAttribute("aria-busy", String(busy));
    els.send.hidden = busy;
    els.stop.hidden = !busy;
    els.input.disabled = false;
  }

  // ------------------------------------------------------------------ agent status
  async function refreshAgentInfo() {
    let delay = 10000;
    try {
      const r = await fetch(`${CFG.apiBase}/v1/info`, { cache: "no-store" });
      if (!r.ok) throw new Error(String(r.status));
      const info = await r.json();
      setAgentStatus(info.ready ? "ready" : "starting", info.ready ? "Agent ready" : "Agent is loading tools");
      els.model.textContent = info.model || "–";
      els.tools.textContent = info.tools?.length ? info.tools.map((t) => t.name.replace(/_tool$/, "")).join(", ") : "–";
      if (info.ready) delay = 60000;
    } catch {
      setAgentStatus("down", "Agent unreachable");
    }
    setTimeout(refreshAgentInfo, delay);
  }
  function setAgentStatus(state, text) {
    els.status.dataset.state = state;
    $(".agent-status-text", els.status).textContent = text;
  }

  // ------------------------------------------------------------------ system health (Prometheus)
  async function prom(query) {
    const r = await fetch(`${CFG.promBase}/api/v1/query?query=${encodeURIComponent(query)}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`Prometheus ${r.status}`);
    return (await r.json()).data.result;
  }
  const scalar = (res) => (res.length ? Number(res[0].value[1]) : null);
  const W = "[15m]";

  async function refreshOps() {
    const errBox = $("#ops-error");
    try {
      const [rpm, err, p95, tpm, tok24, calls, fails, toolP95, up, alertsRes] = await Promise.all([
        prom(`sum(rate(agent_request_duration_seconds_count${W})) * 60`),
        prom(`(sum(rate(agent_request_duration_seconds_count{error_type!=""}${W})) or vector(0)) / sum(rate(agent_request_duration_seconds_count${W}))`),
        prom(`histogram_quantile(0.95, sum by (le) (rate(agent_request_duration_seconds_bucket${W})))`),
        prom(`sum(rate(gen_ai_client_token_usage_sum${W})) * 60`),
        prom(`sum(increase(gen_ai_client_token_usage_sum[24h]))`),
        prom(`sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{mcp_method_name="tools/call"}${W})) * 60`),
        prom(`sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{mcp_method_name="tools/call",error_type!="",error_type!="tool_input_error"}${W}))
              / sum by (gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_count{mcp_method_name="tools/call"}${W}))`),
        prom(`histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate(mcp_server_operation_duration_seconds_bucket{mcp_method_name="tools/call"}${W})))`),
        prom(`max by (service_name) (process_uptime_seconds)`),
        fetch(`${CFG.promBase}/api/v1/alerts`, { cache: "no-store" }).then((r) => r.json()),
      ]);
      errBox.hidden = true;

      const errRate = scalar(err);
      setKpi("#kpi-rpm", fmtNum(scalar(rpm), 1));
      setKpi("#kpi-err", fmtPct(errRate), errRate > 0.05);
      const p95s = scalar(p95);
      setKpi("#kpi-p95", p95s == null || Number.isNaN(p95s) ? "–" : fmtMs(p95s * 1000));
      setKpi("#kpi-tpm", fmtNum(scalar(tpm)));
      const t24 = scalar(tok24);
      $("#tokens-24h").textContent = t24 ? `${fmtNum(t24)} in 24h` : "";

      const failBy = Object.fromEntries(fails.map((x) => [x.metric.gen_ai_tool_name, Number(x.value[1])]));
      const p95By = Object.fromEntries(toolP95.map((x) => [x.metric.gen_ai_tool_name, Number(x.value[1])]));
      const rows = calls.filter((x) => Number(x.value[1]) > 0).sort((a, b) => b.value[1] - a.value[1]);
      $("#tool-rows").innerHTML = rows.length
        ? rows.map((x) => {
            const n = x.metric.gen_ai_tool_name;
            const f = failBy[n] ?? 0;
            const p = p95By[n];
            return `<tr><td title="${esc(n)}">${esc(n.replace(/_tool$/, ""))}</td><td>${fmtNum(Number(x.value[1]), 1)}</td>` +
              `<td class="${f > 0.05 ? "bad" : ""}">${fmtPct(f)}</td><td>${p == null || Number.isNaN(p) ? "–" : fmtMs(p * 1000)}</td></tr>`;
          }).join("")
        : '<tr><td colspan="4" class="muted">No tool calls in the last 15 minutes</td></tr>';

      const upBy = Object.fromEntries(up.map((x) => [x.metric.service_name, Number(x.value[1])]));
      const svcs = [["stock-agent", "Agent API"], ["mcp-tools", "MCP tool server"]];
      $("#services").innerHTML = svcs.map(([key, label]) => {
        const u = upBy[key];
        return `<li><span class="dot ${u != null ? "up" : "down"}" aria-hidden="true"></span>${esc(label)}` +
          `<span class="when">${u != null ? fmtAgo(u) : "not reporting"}</span></li>`;
      }).join("");

      const active = (alertsRes.data?.alerts || []).filter((a) => a.state === "firing" || a.state === "pending");
      $("#alerts").innerHTML = active.length
        ? active.map((a) => `<li class="alert ${a.labels.severity === "critical" ? "critical" : ""}"><b>${esc(a.labels.alertname)}</b>` +
            `<span>${esc(a.state === "pending" ? "Pending: " : "")}${esc(a.annotations?.summary || "")}</span></li>`).join("")
        : '<li class="alert-ok">No alerts firing</li>';

      await drawTokenSpark();
    } catch {
      errBox.hidden = false;
      errBox.textContent = "Can't reach Prometheus, so health numbers are unavailable. Answers still work.";
    }
  }

  function setKpi(sel, text, bad) {
    const n = $(sel);
    n.textContent = text;
    n.classList.toggle("bad", Boolean(bad));
  }

  async function drawTokenSpark() {
    const end = Math.floor(Date.now() / 1000);
    const q = encodeURIComponent("sum(rate(gen_ai_client_token_usage_sum[5m])) * 60");
    const r = await fetch(`${CFG.promBase}/api/v1/query_range?query=${q}&start=${end - 3600}&end=${end}&step=60`, { cache: "no-store" });
    const svg = $("#token-spark");
    svg.replaceChildren();
    if (!r.ok) return;
    const vals = ((await r.json()).data.result[0]?.values || []).map(([t, v]) => [Number(t), Number(v) || 0]);
    if (vals.length < 2) {
      const t = svgEl("text", { x: 140, y: 36, "text-anchor": "middle", fill: "#627084", "font-size": 11 });
      t.textContent = "No token use yet";
      svg.append(t);
      return;
    }
    const max = Math.max(...vals.map((v) => v[1]), 1);
    const x = (t) => ((t - (end - 3600)) / 3600) * 280;
    const y = (v) => 60 - (v / max) * 54;
    const d = vals.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
    svg.append(svgEl("path", { class: "s-area", d: `${d}L${x(vals[vals.length - 1][0])},64L${x(vals[0][0])},64Z` }));
    svg.append(svgEl("path", { class: "s-line", d }));
  }

  // ------------------------------------------------------------------ drawers (narrow screens)
  const scrim = $("#scrim");
  function toggleDrawer(panelSel, btnSel) {
    const panel = $(panelSel);
    const open = !panel.classList.contains("open");
    closeDrawers();
    if (open) {
      panel.classList.add("open");
      $(btnSel).setAttribute("aria-expanded", "true");
      scrim.hidden = false;
    }
  }
  function closeDrawers() {
    ["#sidebar", "#ops"].forEach((s) => $(s).classList.remove("open"));
    ["#toggle-sidebar", "#toggle-ops"].forEach((s) => $(s).setAttribute("aria-expanded", "false"));
    scrim.hidden = true;
  }

  // ------------------------------------------------------------------ wiring
  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  }

  function submit() {
    const q = els.input.value.trim();
    if (!q || inflight) return;
    els.input.value = "";
    autosize();
    ask(q);
  }

  function init() {
    els.suggestions.replaceChildren(
      ...SUGGESTIONS.map(([q, tag]) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.className = "suggestion";
        b.innerHTML = `<span>${esc(q)}</span><small>${esc(tag)}</small>`;
        b.addEventListener("click", () => ask(q));
        li.append(b);
        return li;
      })
    );
    els.form.addEventListener("submit", (e) => { e.preventDefault(); submit(); });
    els.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    els.input.addEventListener("input", autosize);
    els.stop.addEventListener("click", () => inflight?.abort());
    $("#new-chat").addEventListener("click", () => { if (!inflight) { newConversation(); closeDrawers(); } });
    $("#toggle-sidebar").addEventListener("click", () => toggleDrawer("#sidebar", "#toggle-sidebar"));
    $("#toggle-ops").addEventListener("click", () => toggleDrawer("#ops", "#toggle-ops"));
    scrim.addEventListener("click", closeDrawers);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawers(); });
    $("#grafana-link").href = `${CFG.grafanaUrl}/d/agent-mcp-overview`;

    newConversation();
    refreshAgentInfo();
    refreshOps();
    setInterval(() => { if (!document.hidden) refreshOps(); }, CFG.opsRefreshMs);
  }

  init();
})();
