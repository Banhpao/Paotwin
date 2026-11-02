import { Scene } from "./render.js";

const SEVERITY = { loi: 4, mat_tin_hieu: 3, canh_bao: 2, binh_thuong: 1, chua_co: 0 };
const LABEL = { loi: "Lỗi", mat_tin_hieu: "Mất tín hiệu", canh_bao: "Cảnh báo",
  binh_thuong: "Bình thường", chua_co: "Chưa có cảm biến" };
const GLYPH = { loi: "!", mat_tin_hieu: "?", canh_bao: "▲", binh_thuong: "✓", chua_co: "–" };
const STALE_PAGE_MS = 5000;   // no update for this long: the page itself is cut off
const PULSE_MS = 90;

const $ = (id) => document.getElementById(id);
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let model = null, config = null, snapshot = null, lastUpdate = 0;
let selected = null, showAllNames = false, xray = true;
let scene = null;
const zoneMeshes = new Map();   // zone id -> meshes
const zoneAnchor = new Map();   // zone id -> [x, y, z] where its tag sits
const tags = new Map();

// ---- colours read from CSS so the 3D view and the page agree ------------

function cssColor(name) {
  const probe = document.createElement("span");
  probe.style.color = `var(${name})`;
  document.body.appendChild(probe);
  const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  probe.remove();
  return rgb.map((v) => v / 255);
}

let palette = {};
function readPalette() {
  palette = {
    loi: cssColor("--loi"), canh_bao: cssColor("--canh_bao"), mat_tin_hieu: cssColor("--mat"),
    mat2: cssColor("--mat-2"), binh_thuong: cssColor("--binh_thuong"), chua_co: cssColor("--chua_co"),
    sheet: cssColor("--sheet"), ink: cssColor("--ink"), rule: cssColor("--rule"),
    goc: cssColor("--mau-goc"),
  };
  if (scene) { scene.background = palette.sheet; scene.edgeColor = palette.ink; scene.request(); }
}

const LAMP = { xanh: [0.16, 0.72, 0.36], vang: [0.98, 0.76, 0.1], do: [0.9, 0.16, 0.12] };
const dim = (c, k) => c.map((v) => v * k);

// ---- what the page shows for a zone -------------------------------------

function pageCutOff() {
  return !snapshot || Date.now() - lastUpdate > STALE_PAGE_MS;
}

function zoneState(id) {
  if (pageCutOff()) {
    const spec = model.vung.find((z) => z.id === id);
    const sids = signalsOf(id);
    const measured = sids.some((s) => config.tin_hieu.find((t) => t.id === s).kieu !== "chua_co");
    return measured ? { trang_thai: "mat_tin_hieu", ly_do: "Trang này không nhận được cập nhật từ máy chủ bản sao" }
      : { trang_thai: "chua_co", ly_do: spec?.mo_ta || "" };
  }
  return snapshot.vung[id];
}

function signalsOf(zoneId) {
  return config.tin_hieu.filter((s) => s.vung === zoneId).map((s) => s.id);
}

let pulse = 1;
function zoneStyle(mesh) {
  const meta = mesh.meta;
  if (meta.kind === "frame") {
    if (meta.loai === "nen") {
      const table = palette.sheet.map((v, i) => v * 0.7 + palette.rule[i] * 0.3);
      return { color: table, alpha: 1, edges: true, edgeAlpha: 0.35 };
    }
    if (meta.loai === "vo") {
      return xray ? { color: palette.sheet, alpha: 0.13, edgeAlpha: 0.55 }
        : { color: dim(palette.sheet, 0.97), alpha: 1, edgeAlpha: 0.9 };
    }
    return { color: dim(palette.rule, 0.95), alpha: 1, edgeAlpha: 0.5 };
  }
  const state = zoneState(meta.zone).trang_thai;
  if (meta.lamp) {
    const lit = !pageCutOff() && lampLit(meta.lamp);
    return { color: lit ? LAMP[meta.lamp] : dim(LAMP[meta.lamp], 0.32), alpha: 1,
      edgeColor: state === "loi" ? palette.loi : undefined, edgeAlpha: 0.9 };
  }
  // "Bình thường" keeps the part's own colour: only trouble is painted.
  // "Chưa có cảm biến" is the same colour, see-through, outline only.
  if (state === "binh_thuong" || state === "chua_co") {
    const style = state === "binh_thuong"
      ? { color: palette.goc, alpha: 1, edgeAlpha: meta.zone === selected ? 1 : 0.8 }
      : { color: palette.goc, alpha: 0.32, edgeAlpha: 0.7 };
    if (meta.zone === selected) style.edgeColor = [0.02, 0.45, 0.85];
    return style;
  }
  let color = palette[state];
  if (state === "loi") color = color.map((v) => Math.min(1, v * pulse));
  const style = { color, alpha: 1, edgeAlpha: meta.zone === selected ? 1 : 0.8, ghost: palette[state] };
  if (state === "mat_tin_hieu") { style.stripe = true; style.stripeColor = palette.mat2; }
  if (meta.zone === selected) style.edgeColor = [0.02, 0.45, 0.85];
  return style;
}

function lampLit(lamp) {
  const value = snapshot?.tin_hieu["den_thap/trang_thai"];
  if (!value || value.trang_thai === "mat_tin_hieu") return false;
  return { xanh: "chay", vang: "cho", do: "loi" }[lamp] === value.gia_tri;
}

// ---- scene ------------------------------------------------------------------

function buildScene() {
  scene = new Scene($("view"));
  readPalette();
  for (const part of model.khung) {
    scene.add(part, { kind: "frame", loai: part.loai, pickable: false }, zoneStyle);
  }
  for (const zone of model.vung) {
    const meshes = [];
    let top = null;
    for (const part of zone.khoi) {
      const mesh = scene.add(part, { kind: "zone", zone: zone.id, lamp: part.den, pickable: true }, zoneStyle);
      meshes.push(mesh);
      for (const c of mesh.corners) {
        if (!top || c[1] > top[1]) top = [...c];
      }
    }
    const xs = meshes.flatMap((m) => m.corners.map((c) => c[0]));
    const zs = meshes.flatMap((m) => m.corners.map((c) => c[2]));
    // Tag over the first part, so a zone spread out (Wi-Fi) points at its main piece.
    const first = meshes[0].corners;
    const cx = first.reduce((s, c) => s + c[0], 0) / first.length;
    const cz = first.reduce((s, c) => s + c[2], 0) / first.length;
    const fy = Math.max(...first.map((c) => c[1]));
    zoneAnchor.set(zone.id, [cx, fy, cz]);
    zoneMeshes.set(zone.id, { meshes, centre: [(Math.min(...xs) + Math.max(...xs)) / 2, top[1] / 2,
      (Math.min(...zs) + Math.max(...zs)) / 2] });
  }
  scene.onDraw = placeTags;
  scene.bindControls((mesh) => select(mesh && mesh.meta.kind === "zone" ? mesh.meta.zone : null, false));
  new ResizeObserver(() => scene.resize()).observe($("view"));
  scene.resize();
}

function placeTags() {
  const holder = $("labels");
  const shown = [];
  for (const zone of model.vung) {
    const state = zoneState(zone.id).trang_thai;
    const show = showAllNames || zone.id === selected || ["loi", "canh_bao", "mat_tin_hieu"].includes(state);
    let tag = tags.get(zone.id);
    if (!show) { if (tag) tag.hidden = true; continue; }
    if (!tag) {
      tag = document.createElement("button");
      tag.type = "button";
      tag.addEventListener("click", () => select(zone.id, false));
      holder.appendChild(tag);
      tags.set(zone.id, tag);
    }
    const p = scene.project(zoneAnchor.get(zone.id));
    if (!p) { tag.hidden = true; continue; }
    tag.hidden = false;
    tag.className = `tag chip chip-${state}${zone.id === selected ? " selected" : ""}`;
    const text = `${shortName(zone)}: ${LABEL[state]}`;
    if (tag.dataset.text !== text) {
      tag.innerHTML = `<span class="glyph" aria-hidden="true">${GLYPH[state]}</span>${escapeHtml(shortName(zone))}`;
      tag.setAttribute("aria-label", text);
      tag.dataset.text = text;
    }
    shown.push({ tag, p, rank: SEVERITY[state] + (zone.id === selected ? 10 : 0) });
  }
  // Worst first keeps its place; a tag that would cover one already placed
  // moves up, with its leader line stretched down to the part.
  shown.sort((a, b) => b.rank - a.rank);
  const placed = [];
  for (const item of shown) {
    const w = item.tag.offsetWidth, h = item.tag.offsetHeight;
    let lift = 0;
    for (let tries = 0; tries < 12; tries++) {
      const box = { l: item.p[0] - w / 2, r: item.p[0] + w / 2, b: item.p[1] - 10 - lift, t: item.p[1] - 10 - lift - h };
      const hit = placed.find((o) => box.l < o.r + 4 && box.r > o.l - 4 && box.t < o.b + 3 && box.b > o.t - 3);
      if (!hit) { placed.push(box); break; }
      lift += hit.b - box.t + 3;
    }
    item.tag.style.left = `${item.p[0]}px`;
    item.tag.style.top = `${item.p[1] - lift}px`;
    item.tag.style.setProperty("--lead", `${9 + lift}px`);
  }
}

function shortName(zone) {
  return zone.ten.replace(/\s*\(.*\)\s*$/, "");
}

// ---- side panels -------------------------------------------------------------

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function chip(state, text) {
  return `<span class="chip chip-${state}"><span class="glyph" aria-hidden="true">${GLYPH[state]}</span>${escapeHtml(text ?? LABEL[state])}</span>`;
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString("vi-VN", { hour12: false });
}

function since(zoneId) {
  const event = snapshot?.su_kien.find((e) => e.vung === zoneId);
  if (!event) return "";
  return `Từ ${clock(event.t)}`;
}

function renderTop() {
  const zones = model.vung.map((z) => ({ zone: z, s: zoneState(z.id) }));
  const worst = zones.reduce((a, b) => (SEVERITY[b.s.trang_thai] > SEVERITY[a.s.trang_thai] ? b : a));
  const count = (st) => zones.filter((x) => x.s.trang_thai === st).length;
  const overall = worst.s.trang_thai;
  let text;
  if (pageCutOff()) text = "Mất kết nối máy chủ bản sao";
  else if (overall === "loi") text = `${count("loi")} vùng đang lỗi`;
  else if (overall === "mat_tin_hieu") text = `${count("mat_tin_hieu")} vùng mất tín hiệu`;
  else if (overall === "canh_bao") text = `${count("canh_bao")} vùng cần để ý`;
  else text = "Mọi vùng có cảm biến đều bình thường";
  $("overall").innerHTML = chip(overall === "chua_co" ? "binh_thuong" : overall, text);

  $("counts").innerHTML = ["loi", "mat_tin_hieu", "canh_bao", "binh_thuong", "chua_co"].map((st) => {
    const n = count(st);
    return `<li class="${n ? "" : "zero"}">${chip(st, `${LABEL[st]} ${n}`)}</li>`;
  }).join("");

  if (pageCutOff()) {
    $("links").innerHTML = `<span class="bad">Không nhận được cập nhật từ máy chủ</span>`;
    return;
  }
  const g = snapshot.gateway;
  const gw = g.online ? "Gateway đang kết nối" : g.online === false ? "Gateway mất kết nối" : "Chưa thấy gateway";
  $("links").innerHTML = [
    `<span class="${snapshot.broker ? "" : "bad"}">${snapshot.broker ? "Broker MQTT đang kết nối" : "Mất kết nối broker MQTT"}</span>`,
    `<span class="${g.online ? "" : "bad"}">${gw}</span>`,
  ].join("");
}

function renderAttention() {
  const rows = model.vung
    .map((z) => ({ zone: z, s: zoneState(z.id) }))
    .filter((x) => ["loi", "mat_tin_hieu", "canh_bao"].includes(x.s.trang_thai))
    .sort((a, b) => SEVERITY[b.s.trang_thai] - SEVERITY[a.s.trang_thai]);
  if (!rows.length) {
    $("attention").innerHTML = `<li><p class="all-clear">Không có vùng nào lỗi, cảnh báo hay mất tín hiệu.</p></li>`;
    return;
  }
  $("attention").innerHTML = rows.map(({ zone, s }) => `
    <li><button type="button" class="row-${s.trang_thai}" data-zone="${zone.id}">
      <span aria-hidden="true">${chip(s.trang_thai, LABEL[s.trang_thai])}</span>
      <span class="zone-name">${escapeHtml(zone.ten)}</span>
      <span class="why">${escapeHtml(s.ly_do)}</span>
      ${pageCutOff() ? "" : `<span class="since">${since(zone.id)}</span>`}
    </button></li>`).join("");
  for (const b of $("attention").querySelectorAll("button")) {
    b.setAttribute("aria-label", `${LABEL[zoneState(b.dataset.zone).trang_thai]}: ${b.textContent.trim()}`);
    b.addEventListener("click", () => select(b.dataset.zone, true));
  }
}

function thresholdText(spec, rule) {
  if (!rule) return "";
  const unit = spec.don_vi || "";
  const sign = rule.huong === "tren" ? "≥" : "≤";
  const n = (v) => String(v).replace(".", ",");
  const parts = [];
  if (rule.canh_bao != null) parts.push(`Cảnh báo ${sign} ${n(rule.canh_bao)} ${unit}`);
  if (rule.loi != null) parts.push(`Lỗi ${sign} ${n(rule.loi)} ${unit}`);
  return parts.join(", ");
}

function ageText(age) {
  if (age == null) return "";
  if (age < 1.5) return "vừa cập nhật";
  return `${Math.round(age)} s trước`;
}

function renderDetail() {
  if (!selected) {
    $("detail-title").textContent = "Chi tiết";
    $("detail").innerHTML = `<p class="empty">Chạm vào một bộ phận trên mô hình, hoặc một dòng trong danh sách trên, để xem từng tín hiệu.</p>`;
    return;
  }
  const zone = model.vung.find((z) => z.id === selected);
  const zs = zoneState(zone.id);
  $("detail-title").textContent = zone.ten;
  const items = signalsOf(zone.id).map((sid) => {
    const spec = config.tin_hieu.find((t) => t.id === sid);
    const s = pageCutOff() ? (spec.kieu === "chua_co"
      ? { trang_thai: "chua_co", ly_do: spec.ly_do }
      : { trang_thai: "mat_tin_hieu", ly_do: "Trang này không nhận được cập nhật" }) : snapshot.tin_hieu[sid];
    const rule = s.nguong || (spec.nguong && spec.nguong.nguon === "demo" ? spec.nguong : null);
    const meta = [];
    if (rule) {
      meta.push(thresholdText(spec, rule));
      meta.push(rule.nguon === "demo"
        ? `<span class="tag-demo" title="Số tạm cho bản demo, chưa được duyệt">DEMO, chưa duyệt</span>`
        : `<span class="tag-device" title="${escapeHtml(rule.giai_thich || "")}">Do thiết bị báo</span>`);
    }
    if (s.tuoi_s != null && s.trang_thai !== "chua_co") meta.push(ageText(s.tuoi_s));
    const value = s.chu || (s.trang_thai === "mat_tin_hieu" ? "Không có số" : "");
    return `<li>
      <div class="top"><span class="name">${escapeHtml(spec.ten)}</span><span class="value">${escapeHtml(value)}</span></div>
      <div class="meta">${chip(s.trang_thai)}${meta.map((m) => `<span>${m}</span>`).join("")}</div>
      ${s.ly_do ? `<div class="reason">${escapeHtml(s.ly_do)}</div>` : ""}
      ${rule && rule.giai_thich ? `<div class="meta">${escapeHtml(rule.giai_thich)}</div>` : ""}
    </li>`;
  }).join("");
  const checks = (zone.kiem_tra || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("");
  $("detail").innerHTML = `
    <div class="detail-head">${chip(zs.trang_thai)}</div>
    <p class="detail-desc">${escapeHtml(zone.mo_ta)}</p>
    ${checks ? `<div class="checks"><h3>Việc cần kiểm tra</h3><ol>${checks}</ol>
      <p class="note">${escapeHtml(model.ghi_chu_kiem_tra || "")}</p></div>` : ""}
    <ul class="signals">${items}</ul>`;
}

// ---- 30-minute history of the selected zone's numbers ------------------------

const seriesCache = new Map();  // signal id -> [[t, v], ...]
let historyZone = null, historyTimer = null;

function numericSignals(zoneId) {
  return config.tin_hieu.filter((s) => s.vung === zoneId && (s.kieu === "so" || s.kieu === "lech_pha"));
}

async function loadHistory() {
  const zoneId = selected;
  if (!zoneId) return;
  await Promise.all(numericSignals(zoneId).map(async (spec) => {
    try {
      const res = await fetch(`api/lich-su?tin_hieu=${encodeURIComponent(spec.id)}&phut=30`, { cache: "no-store" });
      if (res.ok) seriesCache.set(spec.id, (await res.json()).diem);
    } catch (err) { /* the chart keeps its last data; the banner reports the outage */ }
  }));
  if (zoneId === selected) renderCharts();
}

function followHistory() {
  if (historyZone === selected) return;
  historyZone = selected;
  clearInterval(historyTimer);
  seriesCache.clear();
  renderCharts();
  if (selected) {
    loadHistory();
    historyTimer = setInterval(loadHistory, 10000);
  }
}

function chartSvg(spec, points, rule) {
  const W = 320, H = 96, L = 40, R = 8, T = 8, B = 18;
  const now = Date.now() / 1000;
  const recent = points.filter((p) => p[0] >= now - 1800);
  if (!recent.length) return `<p class="empty">Chưa có số nào trong 30 phút qua.</p>`;
  // The axis starts at the first reading (at most 30 minutes back, at least
  // 2 minutes), so a fresh start is not squeezed against the right edge.
  const t0 = Math.min(now - 120, Math.max(now - 1800, recent[0][0]));
  const shown = recent.filter((p) => p[0] >= t0);
  const limits = [rule?.canh_bao, rule?.loi].filter((v) => v != null);
  let lo = Math.min(...shown.map((p) => p[1]), ...limits);
  let hi = Math.max(...shown.map((p) => p[1]), ...limits);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const x = (t) => L + ((t - t0) / (now - t0)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  // A gap longer than the lost-signal rule breaks the line: no invented values.
  const gap = Number(config.mat_tin_hieu_sau_s) || 6;
  let d = "";
  shown.forEach((p, i) => {
    const jump = i === 0 || p[0] - shown[i - 1][0] > gap;
    d += `${jump ? "M" : "L"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`;
  });
  const unit = spec.don_vi || "";
  const n = (v) => fmt(Math.round(v * 100) / 100);
  const lines = [];
  if (rule?.canh_bao != null) lines.push(["canh_bao", rule.canh_bao, "Cảnh báo"]);
  if (rule?.loi != null) lines.push(["loi", rule.loi, "Lỗi"]);
  const last = shown[shown.length - 1];
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="${escapeHtml(spec.ten)} từ ${clock(t0)}, gần nhất ${n(last[1])} ${unit}"
      data-signal="${spec.id}">
    <line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"/>
    <text class="tick" x="${L - 4}" y="${T + 4}" text-anchor="end">${n(hi - pad)}</text>
    <text class="tick" x="${L - 4}" y="${H - B}" text-anchor="end">${n(lo + pad)}</text>
    <text class="tick" x="${L}" y="${H - 4}">${clock(t0).slice(0, 5)}</text>
    <text class="tick" x="${W - R}" y="${H - 4}" text-anchor="end">bây giờ</text>
    ${lines.map(([cls, v, label]) => {
      // Fault label above its line, warning label below its line: they never
      // overprint each other however close the two levels are.
      const above = (cls === "loi") === (rule.huong === "tren");
      const ty = above ? y(v) - 3 : y(v) + 10;
      return `<line class="limit limit-${cls}" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>
      <text class="tick" x="${W - R - 2}" y="${ty.toFixed(1)}" text-anchor="end">${label} ${n(v)}</text>`;
    }).join("")}
    <path class="series" d="${d}"/>
    <circle class="last" cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3.5"/>
    <line class="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>
  </svg>`;
}

function renderCharts() {
  const holder = $("charts");
  if (!selected || pageCutOff() && !seriesCache.size) { holder.innerHTML = ""; return; }
  const specs = numericSignals(selected);
  if (!specs.length) { holder.innerHTML = ""; return; }
  holder.innerHTML = `<h3>Diễn biến gần đây (tối đa 30 phút)</h3>` + specs.map((spec) => {
    const live = snapshot?.tin_hieu[spec.id];
    const rule = live?.nguong || null;
    return `<figure><figcaption>${escapeHtml(spec.ten)}${spec.don_vi ? ` (${escapeHtml(spec.don_vi)})` : ""}</figcaption>
      ${chartSvg(spec, seriesCache.get(spec.id) || [], rule)}<div class="tip" hidden></div></figure>`;
  }).join("") + `<p class="note">Mỗi phút cũng được lưu (nhỏ nhất, trung bình, lớn nhất) vào thư mục nhat-ky.</p>`;
  for (const svg of holder.querySelectorAll("svg.chart")) bindTooltip(svg);
}

function bindTooltip(svg) {
  const spec = config.tin_hieu.find((s) => s.id === svg.dataset.signal);
  const points = (seriesCache.get(spec.id) || []).filter((p) => p[0] >= Date.now() / 1000 - 1800);
  const tip = svg.parentElement.querySelector(".tip");
  const cross = svg.querySelector(".cross");
  const box = () => svg.getBoundingClientRect();
  const move = (clientX) => {
    if (!points.length) return;
    const r = box(), W = 320, L = 40, R = 8;
    const vx = ((clientX - r.left) / r.width) * W;
    const now = Date.now() / 1000;
    const t0 = Math.min(now - 120, Math.max(now - 1800, points[0][0]));
    const t = t0 + ((vx - L) / (W - L - R)) * (now - t0);
    let best = points[0];
    for (const p of points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
    const px = L + ((best[0] - t0) / (now - t0)) * (W - L - R);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${clock(best[0])}: ${fmt(Math.round(best[1] * 100) / 100)} ${spec.don_vi || ""}`;
  };
  svg.addEventListener("pointermove", (e) => move(e.clientX));
  svg.addEventListener("pointerdown", (e) => move(e.clientX));
  svg.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
}

// ---- machine-level state (the M64 words) -------------------------------------

const MACHINE_CHIP = { loi: "loi", khong_ro: "mat_tin_hieu", gia_cong: "may", cho: "may", tat: "may" };
const MACHINE_GLYPH = { loi: "!", khong_ro: "?", gia_cong: "▶", cho: "Ⅱ", tat: "■" };

function minutes(sec) {
  if (sec < 60) return `${Math.round(sec)} giây`;
  return `${Math.round(sec / 60)} phút`;
}

function renderMachines() {
  const machines = snapshot?.may || [];
  if (!machines.length) { $("machine-panel").hidden = true; return; }
  $("machine-panel").hidden = false;
  $("machines").innerHTML = machines.map((m) => {
    const state = pageCutOff() ? "khong_ro" : m.trang_thai;
    const label = pageCutOff() ? "Không rõ: tắt máy hoặc mất tín hiệu" : m.nhan;
    const spent = Object.entries(m.thoi_gian_s).filter(([, s]) => s >= 1)
      .map(([k, s]) => `${{ gia_cong: "Gia công", cho: "Chờ", tat: "Tắt", loi: "Lỗi", khong_ro: "Không rõ" }[k]} ${minutes(s)}`);
    return `<div class="machine">
      <div class="machine-head"><span class="machine-name">${escapeHtml(m.ten)}</span>
        <span class="chip chip-${MACHINE_CHIP[state]}"><span class="glyph" aria-hidden="true">${MACHINE_GLYPH[state]}</span>${escapeHtml(label)}</span></div>
      ${m.tu_luc && !pageCutOff() ? `<p class="note">Từ ${clock(m.tu_luc)}</p>` : ""}
      ${m.ghi_chu ? `<p class="reason">${escapeHtml(m.ghi_chu)}</p>` : ""}
      ${spent.length ? `<p class="note">Từ lúc mở máy chủ: ${spent.join(", ")}.</p>` : ""}
    </div>`;
  }).join("");
}

function renderEvents() {
  const events = snapshot?.su_kien || [];
  if (!events.length) {
    $("events").innerHTML = `<li><span></span><span class="empty">Chưa có thay đổi trạng thái nào từ lúc mở máy chủ.</span></li>`;
    return;
  }
  $("events").innerHTML = events.slice(0, 12).map((e) => `
    <li><time datetime="${new Date(e.t * 1000).toISOString()}">${clock(e.t)}</time>
      <span class="what">${escapeHtml(e.ten_vung)}: ${e.may ? escapeHtml(e.ly_do) : `${LABEL[e.tu]} thành ${LABEL[e.den]}`}</span>
      ${e.ly_do && !e.may ? `<span class="why">${escapeHtml(e.ly_do)}</span>` : ""}</li>`).join("");
}

function renderMeasure() {
  if (!snapshot) { $("measure").textContent = ""; return; }
  const d = snapshot.do_tre;
  const part = d.so_mau
    ? `Độ trễ đo được từ lúc gateway đọc tới lúc máy chủ nhận: trung vị ${fmt(d.p50_ms)} ms, 95% dưới ${fmt(d.p95_ms)} ms, lớn nhất ${fmt(d.max_ms)} ms (${d.so_mau} mẫu gần nhất).`
    : "Chưa có mẫu độ trễ.";
  $("measure").textContent = `${part} Mất tín hiệu khi quá ${config.mat_tin_hieu_sau_s} s không có dữ liệu mới (${config.ghi_chu_mat_tin_hieu})`;
}

const fmt = (v) => String(v).replace(".", ",");

function renderAll() {
  renderTop();
  renderMachines();
  renderAttention();
  renderDetail();
  renderEvents();
  renderMeasure();
  $("banner").hidden = !pageCutOff() || !model;
  if (!$("banner").hidden) {
    $("banner").textContent = "Trang này không nhận được cập nhật từ máy chủ bản sao. Mọi vùng có cảm biến được hiện là Mất tín hiệu cho tới khi kết nối lại.";
  }
  startOrStopPulse();
  scene?.request();
}

// ---- selection and pulse -----------------------------------------------------

function select(zoneId, moveCamera) {
  selected = zoneId;
  if (zoneId && moveCamera) {
    const z = zoneMeshes.get(zoneId);
    scene.focus(z.centre, 950);
  }
  renderDetail();
  followHistory();
  scene.request();
  if (zoneId && window.matchMedia("(max-width: 900px)").matches && moveCamera === false) {
    $("detail-panel").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "nearest" });
  }
}

let pulseTimer = null;
function startOrStopPulse() {
  const anyFault = model && model.vung.some((z) => zoneState(z.id).trang_thai === "loi");
  if (anyFault && !reduceMotion && !pulseTimer) {
    const start = performance.now();
    pulseTimer = setInterval(() => {
      pulse = 0.78 + 0.22 * (0.5 + 0.5 * Math.sin((performance.now() - start) / 260));
      scene.request();
    }, PULSE_MS);
  } else if ((!anyFault || reduceMotion) && pulseTimer) {
    clearInterval(pulseTimer);
    pulseTimer = null;
    pulse = 1;
  }
}

// ---- title block: which drawing this is, and how far it can be trusted -------

function renderTitleBlock() {
  const drawing = model.ban_ve;
  $("tb-name").textContent = model.ten;
  const level = {
    tham_khao: "Tham khảo (chưa xác minh)",
    candidate: "Candidate: bản vẽ chưa đối chiếu với máy thật",
    verified: "Verified: đã đối chiếu với máy thật",
  }[model.trang_thai_ban_ve] || model.trang_thai_ban_ve;
  $("tb-level").textContent = level;
  $("title-block").classList.toggle("unverified", model.trang_thai_ban_ve !== "verified");
  const rows = [];
  if (drawing) {
    rows.push(["Số bản vẽ", `${drawing.so_ban_ve}, revision ${drawing.revision}`]);
    rows.push(["Tệp gốc", drawing.tep_nguon]);
    if (drawing.xac_minh) rows.push(["Xác minh", `${drawing.xac_minh.nguoi}, ${drawing.xac_minh.luc}`]);
    $("tb-note").textContent = drawing.loai === "step"
      ? "Hình dựng từ bản vẽ 3D STEP (OpenCASCADE chia tam giác)."
      : "Khối dựng từ bản vẽ DXF (cao độ, độ dày).";
  } else {
    $("tb-note").textContent = "Khối minh hoạ làm tay; vị trí linh kiện là ước lượng.";
  }
  const list = $("title-block").querySelector("dl");
  list.querySelectorAll(".tb-extra").forEach((el) => el.remove());
  for (const [k, v] of rows) {
    const row = document.createElement("div");
    row.className = "tb-extra";
    row.innerHTML = `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`;
    list.insertBefore(row, list.lastElementChild);
  }
}

// ---- data ----------------------------------------------------------------------

let knownFaults = null;
let lastTouch = 0;
const IDLE_BEFORE_TURNING_MS = 15000;

function turnToNewFault() {
  const faults = new Set(Object.entries(snapshot.vung)
    .filter(([, z]) => z.trang_thai === "loi").map(([id]) => id));
  const fresh = knownFaults ? [...faults].filter((id) => !knownFaults.has(id)) : [];
  knownFaults = faults;
  // Turn the model only when nobody is using it, so it never fights a hand.
  if (fresh.length && scene && Date.now() - lastTouch > IDLE_BEFORE_TURNING_MS) {
    select(fresh[0], true);
  }
}

function connect() {
  const source = new EventSource("api/luong");
  source.onmessage = (e) => {
    snapshot = JSON.parse(e.data);
    lastUpdate = Date.now();
    turnToNewFault();
    renderAll();
  };
  // EventSource reconnects on its own; the watchdog below shows the gap.
}

async function start() {
  let body;
  try {
    const res = await fetch("api/mo-hinh", { cache: "no-store" });
    body = await res.json();
    model = body.mo_hinh;
    config = body.tin_hieu;
  } catch (err) {
    $("banner").hidden = false;
    $("banner").textContent = "Không tải được mô hình từ máy chủ bản sao. Kiểm tra cửa sổ đang chạy demo rồi tải lại trang.";
    return;
  }
  if (body.cong_mo_phong) {
    $("sim-link").href = `${location.protocol}//${location.hostname}:${body.cong_mo_phong}/`;
    $("sim-link").hidden = false;
  }
  renderTitleBlock();
  $("title-block").title = model.ghi_chu;
  try {
    buildScene();
  } catch (err) {
    $("banner").hidden = false;
    $("banner").textContent = `Không vẽ được mô hình 3D: ${err.message} Danh sách bên cạnh vẫn đúng.`;
  }

  $("xray").addEventListener("click", (e) => {
    xray = !xray;
    e.currentTarget.setAttribute("aria-pressed", String(xray));
    scene?.request();
  });
  $("names").addEventListener("click", (e) => {
    showAllNames = !showAllNames;
    e.currentTarget.setAttribute("aria-pressed", String(showAllNames));
    scene?.request();
  });
  $("home").addEventListener("click", () => { scene?.reset(); select(null, false); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", readPalette);
  for (const kind of ["pointerdown", "wheel", "keydown"]) {
    document.addEventListener(kind, () => { lastTouch = Date.now(); }, { passive: true, capture: true });
  }

  renderAll();
  connect();
  setInterval(() => { if (pageCutOff()) renderAll(); }, 1000);
}

start();
