// A small WebGL 1 renderer for a scene made of boxes and cylinders.
// No library: the demo installs nothing. Draws on demand, not every frame,
// so the twin barely loads the GPU it is measuring.

const VERT = `
attribute vec3 aPos;
attribute vec3 aNormal;
uniform mat4 uViewProj;
varying vec3 vNormal;
void main() {
  vNormal = aNormal;
  gl_Position = uViewProj * vec4(aPos, 1.0);
}`;

const FRAG = `
precision mediump float;
varying vec3 vNormal;
uniform vec3 uColor;
uniform float uAlpha;
uniform float uLit;
uniform float uStripe;
uniform vec3 uStripeColor;
void main() {
  vec3 color = uColor;
  if (uStripe > 0.5) {
    float s = mod(gl_FragCoord.x + gl_FragCoord.y, 14.0);
    if (s < 6.0) color = uStripeColor;
  }
  if (uLit > 0.5) {
    vec3 n = normalize(vNormal);
    float key = max(dot(n, normalize(vec3(0.45, 0.85, 0.35))), 0.0);
    float fill = max(dot(n, normalize(vec3(-0.6, 0.3, -0.5))), 0.0);
    color *= 0.52 + 0.38 * key + 0.14 * fill;
  }
  gl_FragColor = vec4(color, uAlpha);
}`;

// ---- tiny matrix helpers (column-major, as WebGL expects) ---------------

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
}

function lookAt(eye, target, up) {
  let zx = eye[0] - target[0], zy = eye[1] - target[1], zz = eye[2] - target[2];
  let l = Math.hypot(zx, zy, zz); zx /= l; zy /= l; zz /= l;
  let xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx;
  l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
  const yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
  return [xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
    -(xx * eye[0] + xy * eye[1] + xz * eye[2]),
    -(yx * eye[0] + yy * eye[1] + yz * eye[2]),
    -(zx * eye[0] + zy * eye[1] + zz * eye[2]), 1];
}

function multiply(a, b) {
  const out = new Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      out[c * 4 + r] = s;
    }
  }
  return out;
}

function rotator(deg) {
  const [rx, ry, rz] = (deg || [0, 0, 0]).map((d) => (d * Math.PI) / 180);
  const cx = Math.cos(rx), sx = Math.sin(rx), cy = Math.cos(ry), sy = Math.sin(ry);
  const cz = Math.cos(rz), sz = Math.sin(rz);
  return (v) => {
    let [x, y, z] = v;
    [y, z] = [y * cx - z * sx, y * sx + z * cx];
    [x, z] = [x * cy + z * sy, -x * sy + z * cy];
    [x, y] = [x * cz - y * sz, x * sz + y * cz];
    return [x, y, z];
  };
}

// ---- geometry: positions and normals in world space ----------------------

function boxGeometry(part) {
  const [w, h, d] = part.kich_thuoc.map((v) => v / 2);
  const rot = rotator(part.xoay), c = part.tam;
  const faces = [
    [[1, 0, 0], [[w, -h, -d], [w, h, -d], [w, h, d], [w, -h, d]]],
    [[-1, 0, 0], [[-w, -h, d], [-w, h, d], [-w, h, -d], [-w, -h, -d]]],
    [[0, 1, 0], [[-w, h, -d], [-w, h, d], [w, h, d], [w, h, -d]]],
    [[0, -1, 0], [[-w, -h, d], [-w, -h, -d], [w, -h, -d], [w, -h, d]]],
    [[0, 0, 1], [[-w, -h, d], [w, -h, d], [w, h, d], [-w, h, d]]],
    [[0, 0, -1], [[w, -h, -d], [-w, -h, -d], [-w, h, -d], [w, h, -d]]],
  ];
  const pos = [], nor = [], idx = [];
  const place = (v) => { const r = rot(v); return [r[0] + c[0], r[1] + c[1], r[2] + c[2]]; };
  faces.forEach(([n, quad], f) => {
    const rn = rot(n);
    quad.forEach((v) => { pos.push(...place(v)); nor.push(...rn); });
    idx.push(f * 4, f * 4 + 1, f * 4 + 2, f * 4, f * 4 + 2, f * 4 + 3);
  });
  const corners = [];
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
    corners.push(place([sx * w, sy * h, sz * d]));
  }
  const edges = [];
  const pairs = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  pairs.forEach(([a, b]) => edges.push(...corners[a], ...corners[b]));
  return { pos, nor, idx, edges, corners };
}

function cylinderGeometry(part) {
  const seg = 36, r = part.ban_kinh, half = part.dai / 2, c = part.tam;
  // Built along y, then turned to the requested axis.
  const toAxis = { y: (v) => v, x: (v) => [v[1], -v[0], v[2]], z: (v) => [v[0], v[2], -v[1]] }[part.truc || "y"];
  const rot = rotator(part.xoay);
  const place = (v) => { const p = rot(toAxis(v)); return [p[0] + c[0], p[1] + c[1], p[2] + c[2]]; };
  const turn = (v) => rot(toAxis(v));
  const pos = [], nor = [], idx = [], edges = [], corners = [];
  for (let i = 0; i <= seg; i++) {
    const a = (i / seg) * Math.PI * 2, x = Math.cos(a), z = Math.sin(a);
    pos.push(...place([r * x, -half, r * z]), ...place([r * x, half, r * z]));
    nor.push(...turn([x, 0, z]), ...turn([x, 0, z]));
  }
  for (let i = 0; i < seg; i++) {
    const k = i * 2;
    idx.push(k, k + 1, k + 3, k, k + 3, k + 2);
  }
  for (const [y, ny] of [[half, 1], [-half, -1]]) {
    const centre = pos.length / 3;
    pos.push(...place([0, y, 0])); nor.push(...turn([0, ny, 0]));
    for (let i = 0; i <= seg; i++) {
      const a = (i / seg) * Math.PI * 2;
      pos.push(...place([r * Math.cos(a), y, r * Math.sin(a)])); nor.push(...turn([0, ny, 0]));
    }
    for (let i = 0; i < seg; i++) {
      if (ny > 0) idx.push(centre, centre + i + 2, centre + i + 1);
      else idx.push(centre, centre + i + 1, centre + i + 2);
    }
    for (let i = 0; i < seg; i++) {
      const a0 = (i / seg) * Math.PI * 2, a1 = ((i + 1) / seg) * Math.PI * 2;
      edges.push(...place([r * Math.cos(a0), y, r * Math.sin(a0)]),
        ...place([r * Math.cos(a1), y, r * Math.sin(a1)]));
    }
  }
  for (const a of [0, Math.PI / 2, Math.PI, Math.PI * 1.5]) {
    edges.push(...place([r * Math.cos(a), -half, r * Math.sin(a)]),
      ...place([r * Math.cos(a), half, r * Math.sin(a)]));
  }
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
    corners.push(place([sx * r, sy * half, sz * r]));
  }
  return { pos, nor, idx, edges, corners };
}

// Ear clipping for a simple polygon given counter-clockwise in (x, z).
function triangulate(pts) {
  const idx = pts.map((_, i) => i);
  const tris = [];
  const cross = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const inside = (p, a, b, c) => cross(a, b, p) >= 0 && cross(b, c, p) >= 0 && cross(c, a, p) >= 0;
  let guard = 0;
  while (idx.length > 3 && guard++ < 10000) {
    let cut = false;
    for (let i = 0; i < idx.length; i++) {
      const ia = idx[(i + idx.length - 1) % idx.length], ib = idx[i], ic = idx[(i + 1) % idx.length];
      const a = pts[ia], b = pts[ib], c = pts[ic];
      if (cross(a, b, c) <= 0) continue;
      if (idx.some((j) => j !== ia && j !== ib && j !== ic && inside(pts[j], a, b, c))) continue;
      tris.push([ia, ib, ic]);
      idx.splice(i, 1);
      cut = true;
      break;
    }
    if (!cut) break;   // not simple: stop rather than draw something wrong
  }
  if (idx.length === 3) tris.push([idx[0], idx[1], idx[2]]);
  return tris;
}

// A prism from a drawing: footprint [[x, z], ...] from height y0 up by cao.
function prismGeometry(part) {
  // Work in plan coordinates (x, Y = -z), counter-clockwise, whatever order
  // the file gave: ear clipping and outward normals both rely on it.
  let pts = part.day.map(([x, z]) => [x, -z]);
  let area = 0;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    area += a[0] * b[1] - b[0] * a[1];
  }
  if (area < 0) pts = pts.slice().reverse();
  const y0 = part.y0, y1 = part.y0 + part.cao;
  const P = (p, y) => [p[0], y, -p[1]];
  const pos = [], nor = [], idx = [], edges = [], corners = [];
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const a = pts[i], b = pts[(i + 1) % n];
    const dx = b[0] - a[0], dY = b[1] - a[1];
    const len = Math.hypot(dx, dY) || 1;
    // Outward in plan is (dY, -dx); plan Y is -z in the scene.
    const normal = [dY / len, 0, dx / len];
    const k = pos.length / 3;
    pos.push(...P(a, y0), ...P(b, y0), ...P(b, y1), ...P(a, y1));
    for (let j = 0; j < 4; j++) nor.push(...normal);
    idx.push(k, k + 1, k + 2, k, k + 2, k + 3);
    edges.push(...P(a, y0), ...P(b, y0), ...P(a, y1), ...P(b, y1), ...P(a, y0), ...P(a, y1));
    corners.push(P(a, y0), P(a, y1));
  }
  for (const [y, ny] of [[y1, 1], [y0, -1]]) {
    const k = pos.length / 3;
    for (const p of pts) { pos.push(...P(p, y)); nor.push(0, ny, 0); }
    for (const [a, b, c] of triangulate(pts)) idx.push(k + a, k + b, k + c);
  }
  return { pos, nor, idx, edges, corners };
}

// A triangle mesh from a 3D drawing (STEP), flat-shaded; edges are the CAD
// edges the importer sent, not every triangle side.
function meshGeometry(part) {
  const v = part.dinh, t = part.tam_giac;
  const pos = [], nor = [], idx = [];
  for (let i = 0; i < t.length; i += 3) {
    const a = t[i] * 3, b = t[i + 1] * 3, c = t[i + 2] * 3;
    const ux = v[b] - v[a], uy = v[b + 1] - v[a + 1], uz = v[b + 2] - v[a + 2];
    const wx = v[c] - v[a], wy = v[c + 1] - v[a + 1], wz = v[c + 2] - v[a + 2];
    let nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    const k = pos.length / 3;
    pos.push(v[a], v[a + 1], v[a + 2], v[b], v[b + 1], v[b + 2], v[c], v[c + 1], v[c + 2]);
    nor.push(nx, ny, nz, nx, ny, nz, nx, ny, nz);
    idx.push(k, k + 1, k + 2);
  }
  const [x0, y0, z0, x1, y1, z1] = part.hop_bao;
  const corners = [];
  for (const x of [x0, x1]) for (const y of [y0, y1]) for (const z of [z0, z1]) corners.push([x, y, z]);
  return { pos, nor, idx, edges: part.canh || [], corners };
}

export function geometry(part) {
  if (part.hinh === "luoi") return meshGeometry(part);
  if (part.hinh === "lang_tru") return prismGeometry(part);
  return part.hinh === "tru" ? cylinderGeometry(part) : boxGeometry(part);
}

// ---- the renderer --------------------------------------------------------

export class Scene {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext("webgl", { antialias: true, alpha: false, preserveDrawingBuffer: false });
    if (!gl) throw new Error("Trình duyệt này không bật được WebGL.");
    this.gl = gl;
    // Large meshes from STEP drawings need 32-bit indices (WebGL 1 extension,
    // available on practically every GPU).
    this.uint32 = gl.getExtension("OES_element_index_uint");
    this.program = this._program(VERT, FRAG);
    this.loc = {
      pos: gl.getAttribLocation(this.program, "aPos"),
      nor: gl.getAttribLocation(this.program, "aNormal"),
      viewProj: gl.getUniformLocation(this.program, "uViewProj"),
      color: gl.getUniformLocation(this.program, "uColor"),
      alpha: gl.getUniformLocation(this.program, "uAlpha"),
      lit: gl.getUniformLocation(this.program, "uLit"),
      stripe: gl.getUniformLocation(this.program, "uStripe"),
      stripeColor: gl.getUniformLocation(this.program, "uStripeColor"),
    };
    this.meshes = [];
    this.camera = { yaw: -0.62, pitch: 0.55, dist: 1500, target: [0, 60, -20] };
    this.home = { ...this.camera, target: [...this.camera.target] };
    this.viewProj = null;
    this.background = [0.91, 0.93, 0.95];
    this.edgeColor = [0.12, 0.2, 0.28];
    this._pick = null;
    this._queued = false;
    this.onDraw = null;
  }

  _program(vs, fs) {
    const gl = this.gl;
    const compile = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, compile(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  _buffer(data, target = this.gl.ARRAY_BUFFER, Type = Float32Array) {
    const gl = this.gl, b = gl.createBuffer();
    gl.bindBuffer(target, b);
    gl.bufferData(target, new Type(data), gl.STATIC_DRAW);
    return b;
  }

  // part: model part; style(): {color, alpha, stripe, stripeColor, edges, edgeAlpha, layer}
  add(part, meta, style) {
    const g = geometry(part);
    const big = g.pos.length / 3 > 65535;
    if (big && !this.uint32) throw new Error("Trình duyệt không vẽ được chi tiết lớn hơn 65 535 đỉnh.");
    const mesh = {
      meta, style, corners: g.corners,
      pos: this._buffer(g.pos), nor: this._buffer(g.nor),
      idx: this._buffer(g.idx, this.gl.ELEMENT_ARRAY_BUFFER, big ? Uint32Array : Uint16Array),
      idxType: big ? this.gl.UNSIGNED_INT : this.gl.UNSIGNED_SHORT, count: g.idx.length,
      edgePos: this._buffer(g.edges), edgeCount: g.edges.length / 3,
      edgeNor: this._buffer(new Array(g.edges.length).fill(0)),
      pickId: this.meshes.length + 1,
    };
    this.meshes.push(mesh);
    return mesh;
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(this.canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(this.canvas.clientHeight * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w; this.canvas.height = h;
      this._pick = null;
    }
    this.request();
  }

  eye() {
    const c = this.camera, cp = Math.cos(c.pitch);
    return [c.target[0] + c.dist * cp * Math.sin(c.yaw), c.target[1] + c.dist * Math.sin(c.pitch),
      c.target[2] + c.dist * cp * Math.cos(c.yaw)];
  }

  _matrices() {
    const aspect = this.canvas.width / this.canvas.height;
    // A narrow screen (a phone held upright) needs a wider vertical angle
    // to keep the whole bench in view.
    const fov = aspect < 1.25 ? 2 * Math.atan(Math.tan((19 * Math.PI) / 180) * (1.25 / aspect)) : (38 * Math.PI) / 180;
    const proj = perspective(Math.min(fov, (80 * Math.PI) / 180), aspect, 20, 12000);
    this.viewProj = multiply(proj, lookAt(this.eye(), this.camera.target, [0, 1, 0]));
  }

  project(p) {
    if (!this.viewProj) this._matrices();
    const m = this.viewProj;
    const x = m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12];
    const y = m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13];
    const w = m[3] * p[0] + m[7] * p[1] + m[11] * p[2] + m[15];
    if (w <= 0) return null;
    return [((x / w + 1) / 2) * this.canvas.clientWidth, ((1 - y / w) / 2) * this.canvas.clientHeight];
  }

  request() {
    if (this._queued) return;
    this._queued = true;
    requestAnimationFrame(() => { this._queued = false; this.draw(); });
  }

  _attrs(posBuf, norBuf) {
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.enableVertexAttribArray(this.loc.pos);
    gl.vertexAttribPointer(this.loc.pos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, norBuf);
    gl.enableVertexAttribArray(this.loc.nor);
    gl.vertexAttribPointer(this.loc.nor, 3, gl.FLOAT, false, 0, 0);
  }

  _solid(mesh, color, alpha, lit, stripe, stripeColor) {
    const gl = this.gl;
    this._attrs(mesh.pos, mesh.nor);
    gl.uniform3fv(this.loc.color, color);
    gl.uniform1f(this.loc.alpha, alpha);
    gl.uniform1f(this.loc.lit, lit ? 1 : 0);
    gl.uniform1f(this.loc.stripe, stripe ? 1 : 0);
    gl.uniform3fv(this.loc.stripeColor, stripeColor || color);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.idx);
    gl.drawElements(gl.TRIANGLES, mesh.count, mesh.idxType, 0);
  }

  _edges(mesh, color, alpha) {
    const gl = this.gl;
    this._attrs(mesh.edgePos, mesh.edgeNor);
    gl.uniform3fv(this.loc.color, color);
    gl.uniform1f(this.loc.alpha, alpha);
    gl.uniform1f(this.loc.lit, 0);
    gl.uniform1f(this.loc.stripe, 0);
    gl.drawArrays(gl.LINES, 0, mesh.edgeCount);
  }

  _depthFromEye(mesh, eye) {
    let s = 0;
    for (const c of mesh.corners) s += Math.hypot(c[0] - eye[0], c[1] - eye[1], c[2] - eye[2]);
    return s / mesh.corners.length;
  }

  draw() {
    const gl = this.gl;
    this._matrices();
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(...this.background, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.loc.viewProj, false, this.viewProj);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    const styled = this.meshes.map((m) => ({ m, s: m.style(m) }));
    const opaque = styled.filter((x) => x.s.alpha >= 1);
    const clear = styled.filter((x) => x.s.alpha < 1);

    gl.enable(gl.POLYGON_OFFSET_FILL);
    gl.polygonOffset(1, 1);
    gl.depthMask(true);
    for (const { m, s } of opaque) this._solid(m, s.color, 1, true, s.stripe, s.stripeColor);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    for (const { m, s } of opaque) if (s.edges !== false) this._edges(m, s.edgeColor || this.edgeColor, s.edgeAlpha ?? 0.85);

    const eye = this.eye();
    clear.sort((a, b) => this._depthFromEye(b.m, eye) - this._depthFromEye(a.m, eye));
    gl.depthMask(false);
    for (const { m, s } of clear) {
      this._solid(m, s.color, s.alpha, true, false);
      if (s.edges !== false) this._edges(m, s.edgeColor || this.edgeColor, s.edgeAlpha ?? 0.5);
    }
    gl.depthMask(true);

    // A part in trouble that sits behind another still shows its outline
    // through what covers it, so it can be found without turning the model.
    const ghosts = styled.filter((x) => x.s.ghost);
    if (ghosts.length) {
      gl.depthFunc(gl.ALWAYS);
      gl.depthMask(false);
      for (const { m, s } of ghosts) this._edges(m, s.ghost, 0.55);
      gl.depthMask(true);
      gl.depthFunc(gl.LEQUAL);
    }
    if (this.onDraw) this.onDraw();
  }

  // Which mesh is under a point (CSS pixels), by drawing ids off-screen.
  pick(x, y) {
    const gl = this.gl, w = this.canvas.width, h = this.canvas.height;
    if (!this._pick) {
      const fb = gl.createFramebuffer(), tex = gl.createTexture(), depth = gl.createRenderbuffer();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.bindRenderbuffer(gl.RENDERBUFFER, depth);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, w, h);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, depth);
      this._pick = fb;
    }
    this._matrices();
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._pick);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.disable(gl.BLEND);
    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.loc.viewProj, false, this.viewProj);
    for (const m of this.meshes) {
      if (!m.meta.pickable) continue;   // see-through shell does not block a pick
      const id = m.pickId;
      this._solid(m, [(id & 255) / 255, ((id >> 8) & 255) / 255, 0], 1, false, false);
    }
    const dpr = w / this.canvas.clientWidth;
    const px = new Uint8Array(4);
    gl.readPixels(Math.round(x * dpr), Math.round(h - y * dpr), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.enable(gl.BLEND);
    const id = px[0] + (px[1] << 8);
    this.request();
    return id ? this.meshes[id - 1] : null;
  }

  // Orbit with a pointer, zoom with the wheel or two fingers.
  bindControls(onClick) {
    const el = this.canvas, pointers = new Map();
    let moved = 0, pinch = null;
    el.addEventListener("pointerdown", (e) => {
      el.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      moved = 0;
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), dist: this.camera.dist };
      }
    });
    el.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId)) return;
      const [px, py] = pointers.get(e.pointerId);
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      if (pointers.size === 2 && pinch) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        this.zoomTo(pinch.dist * (pinch.d / Math.max(d, 1)));
        moved += 10;
        return;
      }
      const dx = e.clientX - px, dy = e.clientY - py;
      moved += Math.abs(dx) + Math.abs(dy);
      this.camera.yaw -= dx * 0.008;
      this.camera.pitch = Math.max(0.05, Math.min(1.45, this.camera.pitch + dy * 0.006));
      this.request();
    });
    const end = (e) => {
      if (pointers.has(e.pointerId) && pointers.size === 1 && moved < 6) {
        const r = el.getBoundingClientRect();
        onClick(this.pick(e.clientX - r.left, e.clientY - r.top));
      }
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", (e) => { pointers.delete(e.pointerId); pinch = null; });
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoomTo(this.camera.dist * Math.exp(e.deltaY * 0.0012));
    }, { passive: false });
  }

  zoomTo(dist) {
    this.camera.dist = Math.max(250, Math.min(4000, dist));
    this.request();
  }

  reset() {
    this.camera = { ...this.home, target: [...this.home.target] };
    this.request();
  }

  focus(point, dist) {
    this.camera.target = [...point];
    if (dist) this.camera.dist = dist;
    this.request();
  }
}
