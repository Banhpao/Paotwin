// Zoning tool page: pick parts on the 3D view, put them in the machine's
// zones, save. The server (paotwin/khoanh_vung.py) checks and writes; this
// page only keeps the person's unsaved choices.

import { Scene } from "./render.js";

// Zone identity colours, in a fixed order (a zone keeps its colour while
// others are added). No red: red is the twin's fault colour.
const PALETTE = ["#2f6db5", "#d98a1c", "#2f9a6e", "#8f5bb5", "#c2577a",
  "#3b9fb8", "#8a6a2e", "#5a6fd1", "#9a9a2c"];
const OVERFLOW = "#7d8b99";
const UNASSIGNED = "#c9d1d9";
const FRAME = "#9aa7b3";
const SKIPPED = "#b0b8c0";
const FOCUS = "#1468b3";
const FRAME_NAMES = { nen: "Khung: nền, bệ", vo: "Khung: vỏ", trong: "Khung: phần trong" };
const LAMP_NAMES = { xanh: "đèn xanh", vang: "đèn vàng", do: "đèn đỏ" };
const ZONE_ID = /^[a-z0-9_]{1,40}$/;
const LIST_LIMIT = 400;

const $ = (id) => document.getElementById(id);
const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);

const state = {
  parts: [],              // {khoa, nhan, duong_dan, tam_giac, luoi, mesh}
  byKey: new Map(),
  machine: {},
  zones: [],              // [{id, ten, mo_ta}]
  fixed: false,           // bench: zones come from the twin and cannot change
  gan: {},                // key -> decision chosen here (saved or not)
  fromNames: {},          // key -> decision the drawing's names already give
  selected: new Set(),
  target: null,           // {vung: id[, den]} | {khung: kind} | {bo_qua: true}
  through: true,
  up: "Z",                // the STEP file's vertical axis: "Z" or "Y"
  axis: {},               // what the server says about it (saved, suggested, program)
  dirty: false,
  serverCheck: null,
};

let scene = null;

// ---- small helpers ----------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function zoneIndex(id) { return state.zones.findIndex((z) => z.id === id); }

function zoneColor(id) {
  const i = zoneIndex(id);
  return i >= 0 && i < PALETTE.length ? PALETTE[i] : OVERFLOW;
}

function zoneName(id) {
  const z = state.zones.find((x) => x.id === id);
  return z ? z.ten : `${id} (không có trong danh mục)`;
}

function decisionOf(key) {
  return key in state.gan ? state.gan[key] : (state.fromNames[key] || null);
}

function describe(d) {
  if (!d) return "Chưa gán";
  if (d.vung) return zoneName(d.vung) + (d.den ? `, ${LAMP_NAMES[d.den]}` : "");
  if (d.khung) return FRAME_NAMES[d.khung];
  return "Bỏ qua, không đưa vào mô hình";
}

function swatchClass(d) {
  if (!d) return "kv-sw kv-sw-chua_gan";
  if (d.khung) return "kv-sw kv-sw-khung";
  if (d.bo_qua) return "kv-sw kv-sw-bo_qua";
  return "kv-sw";
}

function swatch(d) {
  const node = el("span", { class: swatchClass(d), "aria-hidden": "true" });
  if (d && d.vung) node.style.background = zoneColor(d.vung);
  return node;
}

function sameTarget(a, b) {
  if (!a || !b) return false;
  return (a.vung && a.vung === b.vung) || (a.khung && a.khung === b.khung) || (a.bo_qua && b.bo_qua);
}

function setDirty(dirty) {
  state.dirty = dirty;
  const s = $("save-state");
  s.classList.toggle("dirty", dirty);
  if (dirty) s.textContent = "Có thay đổi chưa lưu";
  $("save").disabled = !dirty;
  $("import").disabled = dirty;
}

function banner(text) {
  const b = $("banner");
  b.textContent = text || "";
  b.hidden = !text;
}

async function call(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* answered with no JSON */ }
  if (!res.ok) throw new Error(data.loi || `Máy chủ trả lỗi ${res.status}`);
  return data;
}

// ---- 3D -----------------------------------------------------------------------

function style(mesh) {
  const key = mesh.meta.key;
  const d = decisionOf(key);
  const picked = state.selected.has(key);
  let color = UNASSIGNED, alpha = 1, edgeAlpha = 0.55;
  if (d && d.vung) color = zoneColor(d.vung);
  else if (d && d.khung) { color = FRAME; alpha = 0.3; edgeAlpha = 0.3; }
  else if (d && d.bo_qua) { color = SKIPPED; alpha = 0.1; edgeAlpha = 0.12; }
  if (picked) {
    return { color: rgb(color), alpha: Math.max(alpha, 0.85), stripe: true, stripeColor: rgb(FOCUS),
      edgeColor: rgb(FOCUS), edgeAlpha: 1, ghost: rgb(FOCUS) };
  }
  return { color: rgb(color), alpha, edgeAlpha };
}

function refreshPickable() {
  for (const p of state.parts) {
    if (!p.mesh) continue;
    const d = decisionOf(p.khoa);
    p.mesh.meta.pickable = !(state.through && d && (d.khung || d.bo_qua));
  }
}

// Puts the parts on the scene (again, after the vertical axis changes) and
// frames them.
function loadMeshes(parts) {
  scene.clear();
  let box = null;
  for (const p of parts) {
    p.mesh = null;
    if (!p.luoi) continue;
    p.mesh = scene.add(p.luoi, { key: p.khoa, pickable: true }, style);
    const b = p.luoi.hop_bao;
    box = box ? [Math.min(box[0], b[0]), Math.min(box[1], b[1]), Math.min(box[2], b[2]),
      Math.max(box[3], b[3]), Math.max(box[4], b[4]), Math.max(box[5], b[5])] : [...b];
  }
  if (box) scene.fit(box);
}

function buildScene(parts) {
  const canvas = $("view");
  scene = new Scene(canvas);
  loadMeshes(parts);
  scene.bindControls((mesh) => {
    if (!mesh) { state.selected.clear(); }
    else if (state.selected.has(mesh.meta.key)) state.selected.delete(mesh.meta.key);
    else state.selected.add(mesh.meta.key);
    showPicked(mesh ? mesh.meta.key : null);
    renderAll();
  });
  new ResizeObserver(() => scene.resize()).observe(canvas);
  scene.resize();
}

function showPicked(key) {
  const out = $("picked");
  out.textContent = "";
  if (!key) return;
  const p = state.byKey.get(key);
  out.append(el("span", { text: `${p.nhan}: ${describe(decisionOf(key))}. Vị trí ${p.khoa}, ${p.duong_dan}` }));
}

function lookAtSelection() {
  let box = null;
  for (const key of state.selected) {
    const p = state.byKey.get(key);
    if (!p || !p.luoi) continue;
    const b = p.luoi.hop_bao;
    box = box ? [Math.min(box[0], b[0]), Math.min(box[1], b[1]), Math.min(box[2], b[2]),
      Math.max(box[3], b[3]), Math.max(box[4], b[4]), Math.max(box[5], b[5])] : [...b];
  }
  if (!box) return;
  const size = Math.max(Math.hypot(box[3] - box[0], box[4] - box[1], box[5] - box[2]), 1);
  scene.focus([(box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2], size * 2.4);
}

// ---- side column ------------------------------------------------------------

function counts() {
  const byZone = {}, other = { khung: 0, bo_qua: 0, chua_gan: 0 };
  for (const z of state.zones) byZone[z.id] = 0;
  for (const p of state.parts) {
    if (!p.luoi) continue;
    const d = decisionOf(p.khoa);
    if (!d) other.chua_gan += 1;
    else if (d.vung) byZone[d.vung] = (byZone[d.vung] || 0) + 1;
    else if (d.khung) other.khung += 1;
    else other.bo_qua += 1;
  }
  return { byZone, other };
}

// An empty zone stands out (import needs every zone); empty frame or
// "leave out" is normal.
function targetButton(target, label, sub, count, d, flagEmpty = false) {
  const pressed = sameTarget(state.target, target);
  return el("li", {}, el("button", {
    type: "button", "aria-pressed": pressed ? "true" : "false",
    onclick: () => { state.target = pressed ? null : target; renderAll(); },
  }, swatch(d), el("span", { class: "name", text: label }, sub ? el("span", { class: "id", text: sub }) : null),
  el("span", { class: flagEmpty && count === 0 ? "count none" : "count", text: `${count} chi tiết` })));
}

function renderTargets() {
  const { byZone, other } = counts();
  const zones = $("zones");
  zones.textContent = "";
  if (!state.zones.length) {
    zones.append(el("li", { class: "empty", text: "Chưa có vùng nào. Thêm vùng ở dưới, rồi chọn chi tiết và gán vào." }));
  }
  for (const z of state.zones) {
    zones.append(targetButton({ vung: z.id }, z.ten, z.id, byZone[z.id] || 0, { vung: z.id }, true));
  }
  const specials = $("specials");
  specials.textContent = "";
  for (const kind of ["nen", "vo", "trong"]) {
    const n = state.parts.filter((p) => p.luoi && decisionOf(p.khoa)?.khung === kind).length;
    specials.append(targetButton({ khung: kind }, FRAME_NAMES[kind], "vẽ mờ, không thuộc vùng nào", n, { khung: kind }));
  }
  specials.append(targetButton({ bo_qua: true }, "Bỏ qua", "ốc vít, chi tiết nhỏ: không đưa vào mô hình", other.bo_qua, { bo_qua: true }));
  renderZoneEdit(byZone);
}

function renderZoneEdit(byZone) {
  const box = $("zone-edit");
  box.textContent = "";
  const t = state.target;
  const zone = t && t.vung ? state.zones.find((z) => z.id === t.vung) : null;
  if (!zone) { box.hidden = true; return; }
  box.hidden = false;
  const lampZone = state.zones.find((z) => z.id === zone.id)?.co_den;
  if (lampZone) {
    const pick = el("select", { id: "lamp", onchange: (e) => { state.target = { vung: zone.id, ...(e.target.value ? { den: e.target.value } : {}) }; } },
      el("option", { value: "", text: "Không phải đèn" }),
      ...Object.entries(LAMP_NAMES).map(([v, n]) => el("option", { value: v, text: n, selected: t.den === v })));
    box.append(el("label", { for: "lamp", text: "Chi tiết gán vào là đèn nào" }), pick);
  }
  if (state.fixed) {
    box.append(el("p", { class: "kv-note", text: zone.mo_ta || "" }));
    return;
  }
  const name = el("input", { id: "zone-name", type: "text", maxlength: "80", value: zone.ten });
  const note = el("textarea", { id: "zone-note", rows: "2", maxlength: "400" });
  note.value = zone.mo_ta || "";
  const err = el("p", { class: "kv-error", role: "alert" });
  const used = byZone[zone.id] || 0;
  box.append(
    el("label", { for: "zone-name", text: `Tên vùng ${zone.id}` }), name,
    el("label", { for: "zone-note", text: "Mô tả" }), note, err,
    el("div", { class: "row" },
      el("button", { type: "button", onclick: () => {
        const ten = name.value.trim();
        if (!ten) { err.textContent = "Vùng phải có tên."; return; }
        zone.ten = ten; zone.mo_ta = note.value.trim();
        setDirty(true); renderAll();
      } }, "Đổi tên, mô tả"),
      el("button", { type: "button", disabled: used > 0, title: used > 0 ? "Bỏ gán các chi tiết của vùng này trước" : null, onclick: () => {
        state.zones = state.zones.filter((z) => z.id !== zone.id);
        state.target = null;
        setDirty(true); renderAll();
      } }, used > 0 ? `Xoá vùng (còn ${used} chi tiết)` : "Xoá vùng")));
}

function filteredParts() {
  const q = $("filter").value.trim().toLowerCase();
  const onlyOpen = $("only-open").checked;
  return state.parts.filter((p) => {
    if (onlyOpen && decisionOf(p.khoa)) return false;
    if (!q) return true;
    return p.nhan.toLowerCase().includes(q) || p.duong_dan.toLowerCase().includes(q) || p.khoa === q;
  });
}

function renderParts() {
  const list = $("parts");
  list.textContent = "";
  const shown = filteredParts();
  for (const p of shown.slice(0, LIST_LIMIT)) {
    const d = decisionOf(p.khoa);
    const viaName = !(p.khoa in state.gan) && state.fromNames[p.khoa];
    const where = !p.luoi ? "Không chia được thành tam giác, không vẽ được"
      : describe(d) + (viaName ? " (theo tên trong bản vẽ)" : "");
    list.append(el("li", {}, el("button", {
      type: "button", "aria-pressed": state.selected.has(p.khoa) ? "true" : "false",
      onclick: () => {
        if (state.selected.has(p.khoa)) state.selected.delete(p.khoa); else state.selected.add(p.khoa);
        showPicked(p.khoa);
        renderAll();
      },
    }, swatch(d), el("span", { class: "label", text: p.nhan }),
    el("span", { class: d ? "where" : "where open", text: `${where} · ${p.khoa}` }))));
  }
  const note = $("parts-note");
  note.textContent = shown.length > LIST_LIMIT
    ? `Đang hiện ${LIST_LIMIT} trong ${shown.length} chi tiết; lọc theo tên để thu hẹp.`
    : `${shown.length} trong ${state.parts.length} chi tiết.`;
}

function renderActions() {
  const n = state.selected.size;
  const assign = $("assign");
  assign.disabled = !n || !state.target;
  assign.textContent = !state.target ? "Chọn một vùng ở trên để gán"
    : `Gán ${n || ""} chi tiết vào ${describe(state.target)}`.replace("  ", " ");
  $("unassign").disabled = !n || ![...state.selected].some((k) => k in state.gan);
  $("same-name").disabled = !n;
  $("clear").disabled = !n;
  $("look").disabled = !n;
}

function renderCheck() {
  const box = $("check");
  box.textContent = "";
  const { byZone, other } = counts();
  const problems = [];
  if (!state.zones.length) problems.push("Chưa có vùng nào.");
  const empty = state.zones.filter((z) => !byZone[z.id]).map((z) => z.ten);
  if (empty.length) problems.push(`Vùng chưa có chi tiết nào: ${empty.join(", ")}.`);
  if (state.serverCheck && !state.dirty) {
    for (const p of state.serverCheck.van_de) if (!problems.includes(p)) problems.push(p);
  }
  if (problems.length) {
    box.append(el("ol", { class: "kv-check" }, ...problems.map((p) => el("li", { text: p }))));
  } else {
    box.append(el("p", { class: "kv-ok", text: "Mọi vùng đã có chi tiết: nhập được." }));
  }
  box.append(el("p", { class: "kv-counts",
    text: `Chưa gán ${other.chua_gan} · khung ${other.khung} · bỏ qua ${other.bo_qua}. `
      + "Chi tiết chưa gán sẽ không vào mô hình; nhập vẫn được, sổ bản vẽ liệt kê chúng." }));
}

function renderAll() {
  refreshPickable();
  renderTargets();
  renderParts();
  renderActions();
  renderCheck();
  if (scene) scene.request();
}

// ---- actions ------------------------------------------------------------------

function assignSelected() {
  if (!state.target || !state.selected.size) return;
  const n = state.selected.size;
  for (const key of state.selected) state.gan[key] = { ...state.target };
  state.selected.clear();
  $("picked").textContent = "";
  setDirty(true);
  renderAll();
  $("save-state").textContent = `Đã gán ${n} chi tiết vào ${describe(state.target)}; chưa lưu`;
}

function unassignSelected() {
  for (const key of state.selected) delete state.gan[key];
  state.selected.clear();
  setDirty(true);
  renderAll();
}

function selectSameName() {
  const names = new Set([...state.selected].map((k) => state.byKey.get(k).nhan.replace(/ #\d+$/, "")));
  for (const p of state.parts) if (names.has(p.nhan.replace(/ #\d+$/, ""))) state.selected.add(p.khoa);
  renderAll();
}

function addZone(event) {
  event.preventDefault();
  const id = $("new-id").value.trim(), ten = $("new-name").value.trim(), mo_ta = $("new-note").value.trim();
  const err = $("add-error");
  if (!ZONE_ID.test(id)) { err.textContent = "Mã vùng: chữ thường không dấu, số và dấu gạch dưới, tối đa 40 ký tự."; return; }
  if (state.zones.some((z) => z.id === id)) { err.textContent = `Đã có vùng ${id}.`; return; }
  if (!ten) { err.textContent = "Vùng phải có tên."; return; }
  err.textContent = "";
  state.zones.push({ id, ten, mo_ta });
  state.target = { vung: id };
  $("new-id").value = ""; $("new-name").value = ""; $("new-note").value = "";
  setDirty(true);
  renderAll();
}

function applyStatus(s) {
  state.machine = s.may;
  state.fixed = s.may.chi_doc_vung;
  state.zones = s.vung.map((z) => ({ ...z }));
  state.gan = { ...s.gan };
  state.fromNames = s.theo_ten;
  state.serverCheck = s.kiem_tra;
  $("machine-name").textContent = `Khoanh vùng: ${s.may.ten}`;
  document.title = `Khoanh vùng ${s.may.ma}`;
  $("machine-edit").hidden = state.fixed;
  $("machine-title").value = s.may.ten;
  $("add-zone").hidden = state.fixed;
  $("zones-note").textContent = state.fixed
    ? "Bàn thử: các vùng lấy từ mô hình của bản sao và đã gắn cảm biến, nên không thêm, bớt ở đây."
    : `Vùng của máy ${s.may.ma}. Lưu vào ${s.thu_muc}/.`;
  banner(s.canh_bao || "");
  if (state.target && state.target.vung && zoneIndex(state.target.vung) < 0) state.target = null;
  state.axis = { savedUp: s.truc_da_luu ? s.truc_dung : null, suggested: s.goi_y_truc, program: s.phan_mem };
  state.up = s.truc_dung;
  renderAxis();
}

function renderAxis() {
  $("up-axis").textContent = `Trục đứng: ${state.up}`;
  const note = $("axis-note");
  const { savedUp, suggested, program } = state.axis;
  const status = savedUp === state.up ? "đã lưu" : (!savedUp && state.up === "Z" ? "mặc định, chưa lưu" : "chưa lưu");
  let text = `Trục đứng của tệp: ${state.up} (${status}).`;
  if (program) text += ` Phần mềm xuất tệp: ${program}.`;
  const hint = suggested && suggested !== state.up && !savedUp;
  if (hint) text += ` Phần mềm này thường lấy ${suggested} làm trục đứng: nếu máy hiện nằm nghiêng hay dựng đứng, bấm "Trục đứng" trên hình để đổi.`;
  note.textContent = text;
  note.classList.toggle("suggest", Boolean(hint));
}

async function fetchShapes(up) {
  const r = await fetch(`/api/hinh?truc_dung=${encodeURIComponent(up)}`);
  if (!r.ok) throw new Error(`hình: ${r.status}`);
  return r.json();
}

async function switchAxis() {
  const next = state.up === "Z" ? "Y" : "Z";
  const button = $("up-axis");
  button.disabled = true;
  try {
    const shapes = await fetchShapes(next);
    for (const fresh of shapes.chi_tiet) {
      const p = state.byKey.get(fresh.khoa);
      if (p) p.luoi = fresh.luoi;
    }
    state.up = next;
    loadMeshes(state.parts);
    setDirty(true);
    renderAxis();
    renderAll();
  } catch (e) {
    banner(`Không đổi được trục đứng: ${e.message}`);
  } finally {
    button.disabled = false;
  }
}

async function save() {
  $("save").disabled = true;
  $("save-state").textContent = "Đang lưu…";
  try {
    const s = await call("/api/luu", {
      ten_may: $("machine-title").value.trim(),
      vung: state.zones.map(({ id, ten, mo_ta }) => ({ id, ten, mo_ta })),
      gan: state.gan,
      truc_dung: state.up,
    });
    applyStatus(s);
    setDirty(false);
    $("save-state").textContent = `Đã lưu lúc ${s.luu_luc}`;
    renderAll();
  } catch (e) {
    setDirty(true);
    $("save-state").textContent = `Chưa lưu được: ${e.message}`;
  }
}

async function importDrawing(event) {
  event.preventDefault();
  const out = $("import-result");
  out.textContent = "Đang dựng mô hình từ bản vẽ (tệp lớn có thể mất vài phút)…";
  $("import").disabled = true;
  try {
    const r = await call("/api/nhap", {
      so_ban_ve: $("number").value.trim(), revision: $("revision").value.trim(), quyen: $("rights").value.trim(),
    });
    out.textContent = (r.co_san ? `Bản vẽ này đã có trong sổ: ${r.id}.` : `Đã nhập ${r.id}, mức Candidate, ${r.so_khoi} khối.`)
      + ` Mô hình: ${r.mo_hinh}.`
      + (state.fixed ? " Chọn cho bản sao dùng bằng SO-BAN-VE.cmd." : " Bản sao chưa dùng được bản vẽ này cho tới khi cấu hình tín hiệu của máy.")
      + (r.bo_qua.length ? ` Lưu ý: ${r.bo_qua.join("; ")}` : "");
  } catch (e) {
    out.textContent = "";
    out.append(el("span", { class: "bad", text: e.message }));
  } finally {
    $("import").disabled = state.dirty;
  }
}

// ---- start --------------------------------------------------------------------

async function start() {
  try {
    // The axis comes with the saved zone file; the shapes are drawn for it.
    const status = await fetch("/api/trang-thai").then((r) => { if (!r.ok) throw new Error(`trạng thái: ${r.status}`); return r.json(); });
    const shapes = await fetchShapes(status.truc_dung);
    state.parts = shapes.chi_tiet;
    for (const p of state.parts) state.byKey.set(p.khoa, p);
    const t = shapes.tep;
    const triangles = state.parts.reduce((s, p) => s + p.tam_giac, 0);
    $("file-info").textContent = `${t.ten} · ${state.parts.length} chi tiết, ${triangles.toLocaleString("vi-VN")} tam giác`
      + (t.kich_thuoc_mm ? ` · bao ${t.kich_thuoc_mm.join(" × ")} mm` : "")
      + ` · sha256 ${t.sha256.slice(0, 12)}…`;
    applyStatus(status);
    buildScene(state.parts);
    setDirty(false);
    $("save-state").textContent = status.da_luu ? "Đã có bản lưu" : "Chưa lưu lần nào";
    renderAll();
  } catch (e) {
    banner(`Không tải được bản vẽ từ công cụ: ${e.message}. Xem cửa sổ đang chạy công cụ.`);
  }
}

$("save").addEventListener("click", save);
$("assign").addEventListener("click", assignSelected);
$("unassign").addEventListener("click", unassignSelected);
$("same-name").addEventListener("click", selectSameName);
$("clear").addEventListener("click", () => { state.selected.clear(); $("picked").textContent = ""; renderAll(); });
$("look").addEventListener("click", lookAtSelection);
$("up-axis").addEventListener("click", switchAxis);
$("home").addEventListener("click", () => scene && scene.reset());
$("through").addEventListener("click", (e) => {
  state.through = !state.through;
  e.currentTarget.setAttribute("aria-pressed", state.through ? "true" : "false");
  renderAll();
});
$("filter").addEventListener("input", renderParts);
$("only-open").addEventListener("change", renderParts);
$("add-form").addEventListener("submit", addZone);
$("import-form").addEventListener("submit", importDrawing);
$("machine-title").addEventListener("input", () => setDirty(true));
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

start();
