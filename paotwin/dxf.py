"""Read an ASCII DXF drawing and turn it into the twin's block model.

The 2D-to-block path of the plan ("2D CAD: dựng khối đơn giản theo kích
thước trên bản vẽ"), using what DXF itself already means:

- A closed POLYLINE or LWPOLYLINE drawn in plan (X right, Y away from the
  viewer) with an elevation (group 38, or the Z of a POLYLINE) and a
  thickness (group 39) is a prism from that elevation up by that thickness.
- A CIRCLE with a thickness is an upright cylinder.
- Layer ``VUNG-<id>`` puts the shape in zone <id>; ``KHUNG-NEN``,
  ``KHUNG-VO`` and ``KHUNG-TRONG`` are the table, the shell and inner parts
  that belong to no zone. Any other layer is left out and reported.
- In a zone whose catalog entry has lamps (the stack light), the DXF colour
  of a shape says which lamp it is: 1 red, 2 yellow, 3 green (AutoCAD
  colour index).
- TEXT/MTEXT on layer ``KHUNG-TEN`` (the title block) carry
  ``SO BAN VE: …``, ``REV: …`` and ``TEN: …``.

Anything the reader cannot place faithfully (a shape with no thickness, an
arc segment, a tilted extrusion, a block reference) is not guessed: it is
skipped and listed in the report, so a person sees what is missing.
Units are millimetres. The twin's axes: x = X, up = Z, z = -Y.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

ZONE_PREFIX = "VUNG-"
FRAME_LAYERS = {"KHUNG-NEN": "nen", "KHUNG-VO": "vo", "KHUNG-TRONG": "trong"}
TITLE_LAYER = "KHUNG-TEN"
TITLE_KEYS = {"SO BAN VE": "so_ban_ve", "REV": "revision", "TEN": "ten"}
LAMP_BY_ACI = {1: "do", 2: "vang", 3: "xanh"}


class DxfError(Exception):
    """The file is not a DXF this reader can use."""


@dataclass
class Report:
    shapes: int = 0
    skipped: list[str] = field(default_factory=list)
    other_layers: dict[str, int] = field(default_factory=dict)
    unsupported: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped.append(why)


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    # AutoCAD writes non-ASCII as \U+XXXX in older files.
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)


def pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    if len(lines) % 2:
        lines = lines[:-1] if not lines[-1].strip() else lines
    if len(lines) % 2:
        raise DxfError("số dòng lẻ: tệp bị cắt hoặc không phải DXF dạng chữ")
    out = []
    for i in range(0, len(lines), 2):
        try:
            code = int(lines[i].strip())
        except ValueError as exc:
            raise DxfError(f"dòng {i + 1} không phải mã nhóm DXF") from exc
        out.append((code, lines[i + 1].strip()))
    return out


def entities(items: list[tuple[int, str]]) -> list[tuple[str, list[tuple[int, str]]]]:
    """(type, its group pairs) for every entity in the ENTITIES section."""
    out, inside, current = [], False, None
    for i, (code, value) in enumerate(items):
        if code == 0 and value == "SECTION" and i + 1 < len(items) and items[i + 1] == (2, "ENTITIES"):
            inside = True
            continue
        if not inside:
            continue
        if code == 0:
            if current is not None:
                out.append(current)
            if value == "ENDSEC":
                inside, current = False, None
                continue
            current = (value, [])
        elif current is not None:
            current[1].append((code, value))
    return out


def _f(groups: list[tuple[int, str]], code: int, default: float | None = None) -> float | None:
    for c, v in groups:
        if c == code:
            try:
                return float(v)
            except ValueError as exc:
                raise DxfError(f"mã nhóm {code} không phải số: {v!r}") from exc
    return default


def _s(groups: list[tuple[int, str]], code: int, default: str = "") -> str:
    for c, v in groups:
        if c == code:
            return v
    return default


def _upright(groups) -> bool:
    nx, ny, nz = _f(groups, 210, 0.0), _f(groups, 220, 0.0), _f(groups, 230, 1.0)
    return abs(nx) < 1e-9 and abs(ny) < 1e-9 and nz > 0


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))


def _clean(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for p in points:
        if not out or math.dist(p, out[-1]) > 1e-6:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-6:
        out.pop()
    return out


def _prism(points, elevation, thickness, label) -> dict:
    """Plan polygon (X, Y) -> the twin's prism: footprint in (x, z), base y, height."""
    if thickness < 0:
        elevation, thickness = elevation + thickness, -thickness
    footprint = [[round(x, 3), round(-y, 3)] for x, y in points]
    if _polygon_area([(x, z) for x, z in footprint]) < 0:
        footprint.reverse()
    return {"hinh": "lang_tru", "day": footprint, "y0": round(elevation, 3),
            "cao": round(thickness, 3), "nguon": label}


def convert(path: Path) -> tuple[dict, dict[str, list[dict]], list[dict], Report]:
    """Returns (title, {zone id: parts}, frame parts, report)."""
    raw = Path(path).read_bytes()
    items = pairs(_decode(raw))
    if not any(c == 0 and v == "SECTION" for c, v in items):
        raise DxfError("không thấy SECTION nào: không phải tệp DXF")
    found = entities(items)
    if not found:
        raise DxfError("không thấy mục ENTITIES nào trong bản vẽ")
    report = Report()
    title: dict[str, str] = {}
    zones: dict[str, list[dict]] = {}
    frame: list[dict] = []

    def place(layer: str, part: dict, groups) -> None:
        report.shapes += 1
        colour = _f(groups, 62)
        if colour is not None:
            part["mau_aci"] = int(colour)
        if layer.upper().startswith(ZONE_PREFIX):
            zones.setdefault(layer[len(ZONE_PREFIX):].lower(), []).append(part)
        else:
            part["loai"] = FRAME_LAYERS[layer.upper()]
            part["ten"] = layer
            frame.append(part)

    index = 0
    while index < len(found):
        kind, groups = found[index]
        index += 1
        layer = _s(groups, 8, "0")
        upper = layer.upper()
        if kind in ("TEXT", "MTEXT"):
            if upper == TITLE_LAYER:
                text = _s(groups, 1)
                text = re.sub(r"\\[A-Za-z][^;]*;", "", text).replace("\\P", " ")
                key, sep, value = text.partition(":")
                if sep and key.strip().upper() in TITLE_KEYS:
                    title[TITLE_KEYS[key.strip().upper()]] = value.strip()
            continue
        if kind == "POLYLINE":
            vertices = []
            while index < len(found) and found[index][0] == "VERTEX":
                vertices.append(found[index][1])
                index += 1
            if index < len(found) and found[index][0] == "SEQEND":
                index += 1
            groups = groups + [(0, "")]
        wanted = upper.startswith(ZONE_PREFIX) or upper in FRAME_LAYERS
        if not wanted:
            if kind not in ("SEQEND", "VERTEX"):
                report.other_layers[layer] = report.other_layers.get(layer, 0) + 1
            continue
        label = f"{kind} trên lớp {layer}"
        if kind in ("LWPOLYLINE", "POLYLINE"):
            if not _upright(groups):
                report.skip(f"{label}: mặt phẳng bị nghiêng, chưa đọc được")
                continue
            flags = int(_f(groups, 70, 0) or 0)
            if kind == "POLYLINE":
                if flags & (8 | 16 | 64):
                    report.skip(f"{label}: đa tuyến 3D hoặc lưới, chưa đọc được")
                    continue
                points = [(_f(v, 10, 0.0), _f(v, 20, 0.0)) for v in vertices]
                bulges = [_f(v, 42, 0.0) for v in vertices]
                elevation = _f(groups, 30, 0.0)
            else:
                xs = [float(v) for c, v in groups if c == 10]
                ys = [float(v) for c, v in groups if c == 20]
                points = list(zip(xs, ys))
                bulges = [float(v) for c, v in groups if c == 42]
                elevation = _f(groups, 38, 0.0)
            thickness = _f(groups, 39, 0.0)
            points = _clean(points)
            if not flags & 1:
                report.skip(f"{label}: đường bao không khép kín")
                continue
            if len(points) < 3 or abs(_polygon_area(points)) < 1e-6:
                report.skip(f"{label}: đường bao không có diện tích")
                continue
            if any(abs(b) > 1e-9 for b in bulges):
                report.skip(f"{label}: có cung tròn trong đường bao, chưa đọc được")
                continue
            if abs(thickness) < 1e-9:
                report.skip(f"{label}: không có độ dày (chỉ là hình 2D)")
                continue
            place(layer, _prism(points, elevation, thickness, label), groups)
        elif kind == "CIRCLE":
            if not _upright(groups):
                report.skip(f"{label}: mặt phẳng bị nghiêng, chưa đọc được")
                continue
            thickness = _f(groups, 39, 0.0)
            radius = _f(groups, 40, 0.0)
            if abs(thickness) < 1e-9 or radius <= 0:
                report.skip(f"{label}: không có độ dày hoặc bán kính (chỉ là hình 2D)")
                continue
            x, y, z = _f(groups, 10, 0.0), _f(groups, 20, 0.0), _f(groups, 30, 0.0)
            base, height = (z + thickness, -thickness) if thickness < 0 else (z, thickness)
            place(layer, {"hinh": "tru", "truc": "y", "tam": [round(x, 3), round(base + height / 2, 3),
                                                             round(-y, 3)],
                          "ban_kinh": round(radius, 3), "dai": round(height, 3), "nguon": label},
                  groups)
        else:
            report.unsupported[kind] = report.unsupported.get(kind, 0) + 1
    return title, zones, frame, report


def build_model(path: Path, catalog: dict, record: dict) -> tuple[dict, Report]:
    """The twin's model: geometry from the drawing, zone names and checks
    from the zone catalog (they belong to the machine, not to the drawing).
    """
    title, zones, frame, report = convert(path)
    known = {z["id"]: z for z in catalog["vung"]}
    extra = sorted(set(zones) - set(known))
    missing = sorted(set(known) - set(zones))
    if extra:
        raise DxfError("bản vẽ có lớp vùng không có trong danh mục vùng: "
                       + ", ".join(ZONE_PREFIX + z.upper() for z in extra))
    if missing:
        raise DxfError("danh mục có vùng mà bản vẽ không vẽ: "
                       + ", ".join(ZONE_PREFIX + z.upper() for z in missing))
    out_zones = []
    for zone in catalog["vung"]:
        parts = zones[zone["id"]]
        if any(p.get("den") for p in zone.get("khoi", [])):
            for part in parts:
                lamp = LAMP_BY_ACI.get(part.get("mau_aci"))
                if lamp:
                    part["den"] = lamp
            if not any(p.get("den") for p in parts):
                report.skip(f"vùng {zone['id']}: không khối nào có màu đèn (1 đỏ, 2 vàng, 3 xanh)")
        entry = {k: v for k, v in zone.items() if k != "khoi"}
        entry["khoi"] = parts
        out_zones.append(entry)
    model = {
        "phien_ban": 1,
        "ten": title.get("ten") or catalog.get("ten", ""),
        "trang_thai_ban_ve": record["muc"],
        "ban_ve": dict({k: record[k] for k in ("id", "so_ban_ve", "revision", "tep_nguon", "sha256", "muc")},
                       loai="dxf"),
        "ghi_chu": ("Dựng từ bản vẽ DXF: mỗi đường bao khép kín có cao độ và độ dày thành một khối. "
                    "Phần không đọc được được liệt kê trong sổ bản vẽ, không đoán."),
        "ghi_chu_kiem_tra": catalog.get("ghi_chu_kiem_tra", ""),
        "vung": out_zones,
        "khung": frame,
    }
    return model, report
