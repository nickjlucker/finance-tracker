// Shared helpers for every page: formatting, API access, toasts, the sidebar's
// Sync / Connect actions, and the small inline-SVG chart helpers.

// --- formatting ---------------------------------------------------------------

const _money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const _moneyWhole = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function fmtMoney(amount) {
  return _money.format(amount || 0);
}

function fmtMoneyWhole(amount) {
  return _moneyWhole.format(Math.round(amount || 0));
}

function fmtDate(iso, opts = { month: "short", day: "numeric" }) {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-US", opts);
}

function daysFromToday(iso) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((new Date(iso + "T00:00:00") - today) / 86400000);
}

function fmtRelativeDay(iso) {
  const d = daysFromToday(iso);
  if (d === 0) return "today";
  if (d === 1) return "tomorrow";
  if (d === -1) return "yesterday";
  if (d > 1 && d < 7) return `in ${d} days`;
  if (d < -1 && d > -7) return `${-d} days ago`;
  return fmtDate(iso);
}

const CATEGORY_LABELS = {
  INCOME: "Income",
  TRANSFER_IN: "Transfer in",
  TRANSFER_OUT: "Transfer out",
  LOAN_PAYMENTS: "Loan payments",
  BANK_FEES: "Bank fees",
  ENTERTAINMENT: "Entertainment",
  FOOD_AND_DRINK: "Food & drink",
  GENERAL_MERCHANDISE: "Shopping",
  HOME_IMPROVEMENT: "Home",
  MEDICAL: "Medical",
  PERSONAL_CARE: "Personal care",
  GENERAL_SERVICES: "Services",
  GOVERNMENT_AND_NON_PROFIT: "Government & nonprofit",
  TRANSPORTATION: "Transportation",
  TRAVEL: "Travel",
  RENT_AND_UTILITIES: "Rent & utilities",
  OTHER: "Other",
};

function categoryLabel(key) {
  if (!key) return "Other";
  return CATEGORY_LABELS[key] || key.charAt(0) + key.slice(1).toLowerCase().replaceAll("_", " ");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// --- API + toasts -------------------------------------------------------------

async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request to ${url} failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function showToast(message) {
  const stack = document.getElementById("toast-stack") || document.body;
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// --- sidebar: sync + connect --------------------------------------------------

// Pages set this to reload their own data after a sync or a new link.
let onDataChanged = () => {};

async function refreshSyncStatus() {
  const el = document.getElementById("sync-status");
  if (!el) return;
  try {
    const summary = await apiFetch("/api/dashboard/summary");
    const last = summary.last_synced;
    const age = last ? -daysFromToday(last) : null;
    const tone = age === null ? "" : age <= 1 ? "good" : "warn";
    const text = last ? `Synced ${fmtRelativeDay(last)}` : "Never synced";
    el.innerHTML = `<span class="dot ${tone}"></span><span>${text}</span>`;
  } catch {
    el.innerHTML = '<span class="dot"></span><span>Status unavailable</span>';
  }
}

async function runFullSync(button) {
  const label = button.textContent;
  button.disabled = true;
  button.innerHTML = '<span class="spin"></span>Syncing…';
  try {
    const r = await apiFetch("/api/sync/full", { method: "POST" });
    showToast(`Synced · ${r.added} new, ${r.modified} updated, ${r.removed} removed`);
    await Promise.all([refreshSyncStatus(), onDataChanged()]);
  } catch (err) {
    showToast(err.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function connectAccount(onSuccess) {
  const { link_token } = await apiFetch("/api/link/token", { method: "POST" });
  const handler = Plaid.create({
    token: link_token,
    onSuccess: async (public_token) => {
      try {
        const result = await apiFetch("/api/link/exchange", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ public_token }),
        });
        showToast(`Linked ${result.institution_name} (${result.accounts_linked} accounts)`);
        if (onSuccess) onSuccess();
      } catch (err) {
        showToast(err.message);
      }
    },
  });
  handler.open();
}

document.addEventListener("DOMContentLoaded", () => {
  const syncBtn = document.getElementById("sync-btn");
  if (syncBtn) syncBtn.addEventListener("click", () => runFullSync(syncBtn));
  const connectBtn = document.getElementById("connect-btn");
  if (connectBtn) {
    connectBtn.addEventListener("click", () =>
      connectAccount(() => Promise.all([refreshSyncStatus(), onDataChanged()])).catch((err) => showToast(err.message))
    );
  }
  refreshSyncStatus();
});

// --- charts -------------------------------------------------------------------
// Inline SVG, themed through CSS variables. Charts keep their aspect ratio so
// labels scale evenly instead of stretching.

const CHART_W = 640;

function fmtAxisMoney(v) {
  const abs = Math.abs(v);
  const s = abs >= 1000 ? `$${+(abs / 1000).toFixed(1)}k` : `$${Math.round(abs)}`;
  return v < 0 ? `−${s}` : s;
}

function niceTicks(min, max, count = 4) {
  const span = max - min || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  // Round outward so every value fits inside the axis range.
  const lo = Math.floor(min / step), hi = Math.ceil(max / step);
  const ticks = [];
  for (let k = lo; k <= hi; k++) ticks.push(k * step);
  return ticks;
}

function _yGrid(ticks, y, padL, width = CHART_W) {
  return ticks
    .map((t) => `
      <line x1="${padL}" x2="${width - 8}" y1="${y(t)}" y2="${y(t)}" stroke="${t === 0 ? "var(--baseline)" : "var(--gridline)"}" />
      <text x="${padL - 8}" y="${y(t) + 4}" text-anchor="end" class="axis">${fmtAxisMoney(t)}</text>`)
    .join("");
}

function svgLineChart(series, { height = 220, color = "var(--series-1)", markIndex = null, floor = null, area = true } = {}) {
  if (!series || series.length < 2) return "";
  const padL = 52, padR = 8, padT = 12, padB = 26;
  const values = series.map((p) => p.value).concat(floor != null ? [floor] : []);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(...values));
  const minY = ticks[0], maxY = ticks[ticks.length - 1];
  const x = (i) => padL + (i / (series.length - 1)) * (CHART_W - padL - padR);
  const y = (v) => padT + (1 - (v - minY) / (maxY - minY || 1)) * (height - padT - padB);

  const labelIdx = [...new Set([0, Math.round((series.length - 1) / 3), Math.round(((series.length - 1) * 2) / 3), series.length - 1])];
  const xLabels = labelIdx
    .map((i) => `<text x="${x(i)}" y="${height - 6}" text-anchor="${i === 0 ? "start" : i === series.length - 1 ? "end" : "middle"}" class="axis">${series[i].label || ""}</text>`)
    .join("");
  const path = series.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const areaPath = area ? `${path} L${x(series.length - 1).toFixed(1)},${y(Math.max(minY, 0))} L${x(0).toFixed(1)},${y(Math.max(minY, 0))} Z` : "";
  const floorLine = floor != null
    ? `<line x1="${padL}" x2="${CHART_W - padR}" y1="${y(floor)}" y2="${y(floor)}" stroke="var(--warn-strong)" stroke-dasharray="4 4" />
       <text x="${CHART_W - padR}" y="${y(floor) - 5}" text-anchor="end" class="axis">safety floor ${fmtAxisMoney(floor)}</text>`
    : "";
  let mark = "";
  if (markIndex != null && markIndex >= 0 && series[markIndex]) {
    const p = series[markIndex];
    const anchor = markIndex > series.length * 0.75 ? "end" : markIndex < series.length * 0.25 ? "start" : "middle";
    mark = `<circle cx="${x(markIndex)}" cy="${y(p.value)}" r="4.5" fill="var(--surface)" stroke="var(--bad)" stroke-width="2" />
      <text x="${x(markIndex)}" y="${y(p.value) + 18}" text-anchor="${anchor}" class="axis mark-label">Low ${fmtMoneyWhole(p.value)} · ${p.label}</text>`;
  }
  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  return `
    <svg class="chart" viewBox="0 0 ${CHART_W} ${height}" role="img">
      <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0" stop-color="${color}" stop-opacity="0.16" /><stop offset="1" stop-color="${color}" stop-opacity="0" />
      </linearGradient></defs>
      ${_yGrid(ticks, y, padL)}
      ${area ? `<path d="${areaPath}" fill="url(#${gid})" />` : ""}
      ${floorLine}
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" />
      ${mark}${xLabels}
    </svg>`;
}

function svgGroupedBars(rows, { height = 240 } = {}) {
  if (!rows || rows.length === 0) return '<div class="chart-empty">No monthly history yet.</div>';
  const padL = 52, padR = 8, padT = 12, padB = 26;
  const ticks = niceTicks(Math.min(0, ...rows.map((r) => r.savings)), Math.max(1, ...rows.flatMap((r) => [r.income, r.spend, r.savings])));
  const minY = ticks[0], maxY = ticks[ticks.length - 1];
  const y = (v) => padT + (1 - (v - minY) / (maxY - minY || 1)) * (height - padT - padB);
  const groupW = (CHART_W - padL - padR) / rows.length;
  const barW = Math.min(26, groupW / 4.2);
  const monthName = (m) => new Date(m + "-01T00:00:00").toLocaleDateString("en-US", { month: "short" });

  const bar = (bx, v, fill, title) => {
    const top = Math.min(y(v), y(0));
    const h = Math.max(1, Math.abs(y(v) - y(0)));
    return `<rect x="${bx}" y="${top}" width="${barW}" height="${h}" rx="3" fill="${fill}"><title>${title}: ${fmtMoney(v)}</title></rect>`;
  };
  const bars = rows
    .map((r, i) => {
      const cx = padL + groupW * (i + 0.5);
      const x0 = cx - barW * 1.5 - 3;
      return `
        ${bar(x0, r.income, "var(--series-1)", "Income")}
        ${bar(x0 + barW + 3, r.spend, "var(--series-spend)", "Spend")}
        ${bar(x0 + (barW + 3) * 2, r.savings, r.savings >= 0 ? "var(--good)" : "var(--bad)", r.savings >= 0 ? "Saved" : "Overspent")}
        <text x="${cx}" y="${height - 6}" text-anchor="middle" class="axis">${monthName(r.month)}${r.partial ? "*" : ""}</text>`;
    })
    .join("");

  return `
    <svg class="chart" viewBox="0 0 ${CHART_W} ${height}" role="img">${_yGrid(ticks, y, padL)}${bars}</svg>
    <div class="chart-legend">
      <span><i style="background:var(--series-1)"></i>Income</span>
      <span><i style="background:var(--series-spend)"></i>Spend</span>
      <span><i style="background:var(--good)"></i>Saved</span>
      <span><i style="background:var(--bad)"></i>Overspent</span>
      ${rows.some((r) => r.partial) ? "<span>* partial month</span>" : ""}
    </div>`;
}

// Time-scaled line chart: history (estimated points drawn lighter) plus an
// optional dashed projection. Returns markup; call wireTimeChart() after
// inserting it to enable the hover readout.
function svgTimeChart(history, projection = [], { height = 230, width = CHART_W } = {}) {
  if (!history || history.length < 2) return "";
  const padL = 56, padR = 12, padT = 14, padB = 26;
  const pts = history.map((p) => ({ t: new Date(p.date + "T00:00:00").getTime(), v: p.value, est: p.estimated, date: p.date }));
  const proj = projection.map((p) => ({ t: new Date(p.date + "T00:00:00").getTime(), v: p.value, date: p.date, proj: true }));
  const all = pts.concat(proj);
  const t0 = all[0].t, t1 = all[all.length - 1].t;
  const values = all.map((p) => p.v);
  const lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.02 || 1;
  const ticks = niceTicks(lo - pad, hi + pad);
  const minY = ticks[0], maxY = ticks[ticks.length - 1];
  const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (width - padL - padR);
  const y = (v) => padT + (1 - (v - minY) / (maxY - minY || 1)) * (height - padT - padB);
  const spanDays = (t1 - t0) / 86400000;
  const fmtTick = (t) => new Date(t).toLocaleDateString("en-US", spanDays > 400 ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" });

  const xTicks = [0, 1 / 3, 2 / 3, 1].map((f) => t0 + f * (t1 - t0));
  const xLabels = xTicks
    .map((t, i) => `<text x="${x(t)}" y="${height - 6}" text-anchor="${i === 0 ? "start" : i === 3 ? "end" : "middle"}" class="axis">${fmtTick(t)}</text>`)
    .join("");
  const path = (arr) => arr.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");

  // Split history where estimated gives way to real snapshots; share the join point.
  const firstReal = pts.findIndex((p) => !p.est);
  const estPart = firstReal === -1 ? pts : pts.slice(0, firstReal + 1);
  const realPart = firstReal === -1 ? [] : pts.slice(firstReal);
  const projPart = proj.length ? [pts[pts.length - 1], ...proj] : [];
  const area = `${path(pts)} L${x(pts[pts.length - 1].t)},${height - padB} L${x(pts[0].t)},${height - padB} Z`;

  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  const data = encodeURIComponent(JSON.stringify(all.map((p) => [p.t, p.v, p.est ? 1 : p.proj ? 2 : 0, p.date])));
  return `
    <div class="time-chart" data-points="${data}" data-t0="${t0}" data-t1="${t1}" data-miny="${minY}" data-maxy="${maxY}" data-h="${height}" data-w="${width}" data-padl="${padL}" data-padr="${padR}" data-padt="${padT}" data-padb="${padB}">
      <svg class="chart" viewBox="0 0 ${width} ${height}" role="img">
        <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stop-color="var(--series-1)" stop-opacity="0.14" /><stop offset="1" stop-color="var(--series-1)" stop-opacity="0" />
        </linearGradient></defs>
        ${_yGrid(ticks, y, padL, width)}
        <path d="${area}" fill="url(#${gid})" />
        ${estPart.length > 1 ? `<path d="${path(estPart)}" fill="none" stroke="var(--series-1)" stroke-opacity="0.55" stroke-width="2" stroke-linejoin="round" />` : ""}
        ${realPart.length > 1 ? `<path d="${path(realPart)}" fill="none" stroke="var(--series-1)" stroke-width="2.25" stroke-linejoin="round" />` : ""}
        ${projPart.length > 1 ? `<path d="${path(projPart)}" fill="none" stroke="var(--good)" stroke-width="2" stroke-dasharray="5 4" />` : ""}
        ${xLabels}
        <g class="hover" visibility="hidden">
          <line class="hover-line" y1="${padT}" y2="${height - padB}" stroke="var(--line-strong)" />
          <circle class="hover-dot" r="4" fill="var(--surface)" stroke="var(--series-1)" stroke-width="2" />
        </g>
      </svg>
      <div class="chart-tip" hidden></div>
    </div>`;
}

function wireTimeChart(container) {
  const wrap = container.querySelector(".time-chart");
  if (!wrap) return;
  const pts = JSON.parse(decodeURIComponent(wrap.dataset.points));
  const d = Object.fromEntries(Object.entries(wrap.dataset).map(([k, v]) => [k, Number(v)]));
  const svg = wrap.querySelector("svg");
  const hover = svg.querySelector(".hover");
  const tip = wrap.querySelector(".chart-tip");
  const W = d.w || CHART_W;
  const toX = (t) => d.padl + ((t - d.t0) / (d.t1 - d.t0 || 1)) * (W - d.padl - d.padr);
  const toY = (v) => d.padt + (1 - (v - d.miny) / (d.maxy - d.miny || 1)) * (d.h - d.padt - d.padb);

  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const vx = ((e.clientX - rect.left) / rect.width) * W;
    let best = pts[0];
    for (const p of pts) if (Math.abs(toX(p[0]) - vx) < Math.abs(toX(best[0]) - vx)) best = p;
    const px = toX(best[0]), py = toY(best[1]);
    hover.setAttribute("visibility", "visible");
    hover.querySelector(".hover-line").setAttribute("x1", px);
    hover.querySelector(".hover-line").setAttribute("x2", px);
    const dot = hover.querySelector(".hover-dot");
    dot.setAttribute("cx", px);
    dot.setAttribute("cy", py);
    dot.setAttribute("stroke", best[2] === 2 ? "var(--good)" : "var(--series-1)");
    const kind = best[2] === 1 ? "estimated" : best[2] === 2 ? "projected" : "synced";
    tip.innerHTML = `<strong>${fmtMoneyWhole(best[1])}</strong><span>${fmtDate(best[3], { month: "short", day: "numeric", year: "numeric" })} · ${kind}</span>`;
    tip.hidden = false;
    const left = (px / W) * rect.width;
    tip.style.left = `${Math.min(Math.max(left, 60), rect.width - 60)}px`;
    tip.style.top = `${(py / d.h) * rect.height - 8}px`;
  });
  svg.addEventListener("mouseleave", () => {
    hover.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
}

// --- daily spending by category ------------------------------------------------

const CATEGORY_SLOTS = [
  "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "TRANSPORTATION", "ENTERTAINMENT",
  "MEDICAL", "RENT_AND_UTILITIES", "GENERAL_SERVICES", "TRAVEL",
];

function categoryColor(cat) {
  const i = CATEGORY_SLOTS.indexOf(cat);
  return i === -1 ? "var(--cat-other)" : `var(--cat-${i + 1})`;
}

// Folds categories without a fixed color into "OTHER" so no hue is generated.
function slotted(categories) {
  const out = {};
  for (const [k, v] of Object.entries(categories)) {
    const key = CATEGORY_SLOTS.includes(k) ? k : "OTHER";
    out[key] = (out[key] || 0) + v;
  }
  return out;
}

const STACK_ORDER = [...CATEGORY_SLOTS, "OTHER"];

function weekly(days) {
  const weeks = [];
  for (let i = 0; i < days.length; i += 7) {
    const chunk = days.slice(i, i + 7);
    const cats = {};
    chunk.forEach((d) => Object.entries(d.categories).forEach(([k, v]) => (cats[k] = (cats[k] || 0) + v)));
    weeks.push({ date: chunk[0].date, end: chunk[chunk.length - 1].date, total: chunk.reduce((s, d) => s + d.total, 0), count: chunk.reduce((s, d) => s + d.count, 0), categories: cats });
  }
  return weeks;
}

// width = the container's pixel width, so one viewBox unit is one pixel and
// labels stay 11px at any card size (a fixed viewBox would scale text up).
function svgDailyStack(rawDays, { height = 220, width = CHART_W } = {}) {
  const grouped = rawDays.length > 92;
  const days = (grouped ? weekly(rawDays) : rawDays).map((d) => ({ ...d, slots: slotted(d.categories) }));
  const padL = 52, padR = 8, padT = 12, padB = 26;
  const ticks = niceTicks(0, Math.max(1, ...days.map((d) => d.total)));
  const maxY = ticks[ticks.length - 1];
  const y = (v) => padT + (1 - v / maxY) * (height - padT - padB);
  const slot = (width - padL - padR) / days.length;
  const barW = Math.max(2, Math.min(28, slot * 0.72));
  const GAP = 2; // surface gap between stacked segments

  let bars = "";
  let hits = "";
  days.forEach((d, i) => {
    const x = padL + slot * i + (slot - barW) / 2;
    const segs = STACK_ORDER.filter((k) => d.slots[k] > 0);
    let acc = 0;
    segs.forEach((k, si) => {
      const v = d.slots[k];
      const y0 = y(acc), y1 = y(acc + v);
      acc += v;
      const top = si === segs.length - 1;
      const h = Math.max(1, y0 - y1 - (top ? 0 : GAP));
      const yTop = y0 - h;
      const r = top ? Math.min(4, barW / 2, h) : 0;
      // Rounded data-end on the top segment only; the base sits on the axis.
      bars += r
        ? `<path d="M${x},${y0} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + barW - r} Q${x + barW},${yTop} ${x + barW},${yTop + r} V${y0} Z" fill="${categoryColor(k === "OTHER" ? "" : k)}" />`
        : `<rect x="${x}" y="${yTop}" width="${barW}" height="${h}" fill="${categoryColor(k === "OTHER" ? "" : k)}" />`;
    });
    hits += `<rect class="hit" data-i="${i}" x="${padL + slot * i}" y="${padT}" width="${slot}" height="${height - padT - padB}" fill="transparent" />
      <rect class="col-hl" x="${padL + slot * i}" y="${padT}" width="${slot}" height="${height - padT - padB}" fill="var(--text)" fill-opacity="0.05" />`;
  });

  const every = Math.ceil(days.length / Math.max(3, Math.floor(width / 110)));
  const xLabels = days
    .map((d, i) => (i % every === 0 || i === days.length - 1) && !(i !== days.length - 1 && days.length - 1 - i < every / 2)
      ? `<text x="${padL + slot * (i + 0.5)}" y="${height - 6}" text-anchor="middle" class="axis">${fmtDate(d.date)}</text>`
      : "")
    .join("");
  const avg = rawDays.reduce((s, d) => s + d.total, 0) / (rawDays.length || 1) * (grouped ? 7 : 1);
  const avgLine = avg > 0
    ? `<line x1="${padL}" x2="${width - padR}" y1="${y(avg)}" y2="${y(avg)}" stroke="var(--text-3)" stroke-dasharray="3 4" />
       <text x="${padL + 4}" y="${y(avg) - 5}" class="axis avg-label">avg ${fmtMoneyWhole(avg)}${grouped ? "/wk" : "/day"}</text>`
    : "";

  const data = encodeURIComponent(JSON.stringify(days.map((d) => ({ date: d.date, end: d.end, total: d.total, count: d.count, slots: d.slots }))));
  return `<div class="daily-chart" data-days="${data}" data-grouped="${grouped ? 1 : 0}">
    <svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Spending per ${grouped ? "week" : "day"} by category">
      ${_yGrid(ticks, y, padL, width)}${bars}${avgLine}${xLabels}${hits}
    </svg>
    <div class="chart-tip wide" hidden></div>
  </div>`;
}

function wireDailyStack(container, onPick) {
  const wrap = container.querySelector(".daily-chart");
  if (!wrap) return;
  const days = JSON.parse(decodeURIComponent(wrap.dataset.days));
  const grouped = wrap.dataset.grouped === "1";
  const tip = wrap.querySelector(".chart-tip");
  const svg = wrap.querySelector("svg");
  wrap.querySelectorAll(".hit").forEach((hit) => {
    const d = days[Number(hit.dataset.i)];
    hit.addEventListener("mouseenter", () => {
      const rows = STACK_ORDER.filter((k) => d.slots[k] > 0)
        .sort((a, b) => d.slots[b] - d.slots[a])
        .map((k) => `<div class="tip-row"><span><i style="background:${categoryColor(k === "OTHER" ? "" : k)}"></i>${k === "OTHER" ? "Other" : categoryLabel(k)}</span><span>${fmtMoney(d.slots[k])}</span></div>`)
        .join("");
      const label = grouped
        ? `${fmtDate(d.date)} – ${fmtDate(d.end)}`
        : fmtDate(d.date, { weekday: "short", month: "short", day: "numeric" });
      tip.innerHTML = `<div class="tip-head"><strong>${label}</strong><strong>${fmtMoney(d.total)}</strong></div>${rows || '<div class="tip-row"><span>No spending</span></div>'}`;
      tip.hidden = false;
      const rect = svg.getBoundingClientRect();
      const hb = hit.getBoundingClientRect();
      tip.style.left = `${Math.min(Math.max(hb.left - rect.left + hb.width / 2, 95), rect.width - 95)}px`;
      tip.style.top = `${rect.height * 0.12}px`;
      tip.style.transform = "translate(-50%, 0)";
    });
    hit.addEventListener("mouseleave", () => (tip.hidden = true));
    if (onPick) hit.addEventListener("click", () => onPick(d.date, grouped ? d.end : d.date));
  });
}

function dailySpendingHtml(data, width = CHART_W) {
  const busiest = (() => {
    const sums = Array(7).fill(0), n = Array(7).fill(0);
    data.days.forEach((d) => { const w = new Date(d.date + "T00:00:00").getDay(); sums[w] += d.total; n[w] += 1; });
    let best = -1;
    sums.forEach((s, w) => { if (n[w] && (best === -1 || s / n[w] > sums[best] / n[best])) best = w; });
    return best === -1 ? null : { day: ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"][best], avg: sums[best] / n[best] };
  })();
  const totals = {};
  data.category_totals.forEach((c) => { const k = CATEGORY_SLOTS.includes(c.name) ? c.name : "OTHER"; totals[k] = (totals[k] || 0) + c.amount; });
  const legend = Object.entries(totals)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span><i style="background:${categoryColor(k === "OTHER" ? "" : k)}"></i>${k === "OTHER" ? "Other" : categoryLabel(k)} <b>${fmtMoneyWhole(v)}</b></span>`)
    .join("");
  const stat = (label, value) => `<div><div class="kpi-label">${label}</div><div class="stat-value amount">${value}</div></div>`;
  return `
    <div class="stats" style="margin-top:0;padding-top:0;border-top:none;margin-bottom:14px">
      ${stat("Average per day", fmtMoney(data.average_per_day))}
      ${stat("Highest day", data.highest_day ? `${fmtMoneyWhole(data.highest_day.total)} <span class="muted" style="font-weight:400;font-size:12px">${fmtDate(data.highest_day.date)}</span>` : "—")}
      ${stat("No-spend days", `${data.no_spend_days} of ${data.days.length}`)}
      ${stat("Priciest weekday", busiest ? `${busiest.day} <span class="muted" style="font-weight:400;font-size:12px">~${fmtMoneyWhole(busiest.avg)}</span>` : "—")}
    </div>
    ${data.total > 0 ? svgDailyStack(data.days, { width: Math.max(280, Math.round(width)), height: width < 500 ? 180 : 230 }) : '<div class="chart-empty">No spending in this period.</div>'}
    <div class="cat-legend">${legend}</div>`;
}

function dailySpendingSub(data) {
  const scope = data.include_bills ? "All spending" : "Everyday spending";
  const aside = !data.include_bills && data.bills_excluded > 0 ? ` · ${fmtMoney(data.bills_excluded)} in fixed bills set aside` : "";
  return `${scope} · ${fmtMoney(data.total)} total${aside}`;
}

// --- Plaid update mode: grant more products on an existing connection ---------

async function grantProductAccess(itemId, product, onDone) {
  const { link_token } = await apiFetch(`/api/link/token/update/${encodeURIComponent(itemId)}?product=${product}`, { method: "POST" });
  const handler = Plaid.create({
    token: link_token,
    // Update mode keeps the existing access token; there is nothing to exchange.
    onSuccess: async () => {
      showToast("Access granted — syncing your investment history…");
      try {
        await apiFetch("/api/sync/full", { method: "POST" });
        await Promise.all([refreshSyncStatus(), onDone ? onDone() : null]);
        showToast("Investment history synced");
      } catch (err) {
        showToast(err.message);
      }
    },
    onExit: (err) => { if (err) showToast(err.display_message || err.error_message || "Plaid closed"); },
  });
  handler.open();
}

// Several lines on one time axis (e.g. money contributed vs. account value).
// series: [{ points: [{date, value}], color, label, dashed?, step? }]
function svgMultiTimeChart(series, { height = 220, width = CHART_W } = {}) {
  const all = series.flatMap((s) => s.points.map((p) => ({ t: new Date(p.date + "T00:00:00").getTime(), v: p.value })));
  if (all.length < 2) return "";
  const padL = 56, padR = 12, padT = 14, padB = 26;
  const t0 = Math.min(...all.map((p) => p.t)), t1 = Math.max(...all.map((p) => p.t));
  const ticks = niceTicks(Math.min(0, ...all.map((p) => p.v)), Math.max(...all.map((p) => p.v)));
  const minY = ticks[0], maxY = ticks[ticks.length - 1];
  const x = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * (width - padL - padR);
  const y = (v) => padT + (1 - (v - minY) / (maxY - minY || 1)) * (height - padT - padB);
  const spanDays = (t1 - t0) / 86400000;
  const fmtTick = (t) => new Date(t).toLocaleDateString("en-US", spanDays > 400 ? { month: "short", year: "numeric" } : { month: "short", day: "numeric" });
  const xLabels = [0, 0.5, 1]
    .map((f, i) => `<text x="${x(t0 + f * (t1 - t0))}" y="${height - 6}" text-anchor="${["start", "middle", "end"][i]}" class="axis">${fmtTick(t0 + f * (t1 - t0))}</text>`)
    .join("");
  const lines = series
    .map((s) => {
      const pts = s.points.map((p) => ({ t: new Date(p.date + "T00:00:00").getTime(), v: p.value }));
      if (!pts.length) return "";
      if (s.extendTo && pts[pts.length - 1].t < t1) pts.push({ t: t1, v: pts[pts.length - 1].v });
      let d = `M${x(pts[0].t)},${y(pts[0].v)}`;
      for (let i = 1; i < pts.length; i++) {
        d += s.step ? ` H${x(pts[i].t)} V${y(pts[i].v)}` : ` L${x(pts[i].t)},${y(pts[i].v)}`;
      }
      const dots = s.dots ? pts.map((p) => `<circle cx="${x(p.t)}" cy="${y(p.v)}" r="4" fill="var(--surface)" stroke="${s.color}" stroke-width="2" />`).join("") : "";
      return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" ${s.dashed ? 'stroke-dasharray="5 4"' : ""} stroke-linejoin="round" />${dots}`;
    })
    .join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img">${_yGrid(ticks, y, padL, width)}${lines}${xLabels}</svg>`;
}
