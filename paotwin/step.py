"""Read a STEP drawing (3D CAD) into the twin's model.

Two layers:

- ``occ_parts`` talks to OpenCASCADE through the optional package
  ``cadquery-ocp-novtk`` (installed by CAI-DAT-STEP.cmd into ``.venv``). It
  walks the assembly tree with names, meshes every part and returns plain
  lists: no OpenCASCADE object leaves this function.
- ``assemble`` turns those plain parts into the twin's model. It is pure
  Python and fully tested without OpenCASCADE.

Naming rule, the same as the DXF layers: a part or sub-assembly whose name is
``VUNG-<ID>`` or ``VUNG-<ID>-<anything>`` belongs to zone <ID> (and everything
under it, unless renamed below); ``KHUNG-NEN``, ``KHUNG-VO``, ``KHUNG-TRONG``
are the frame. In the stack-light zone a name ending in ``-XANH``,
``-VANG`` or ``-DO`` says which lamp it is. When a supplier's drawing uses
its own names, a mapping file ``{"part name in the STEP": "zone id"}`` maps
them without editing the drawing.

Units: OpenCASCADE converts the file to millimetres. Axes: CAD Z is up; the
twin's x = X, up = Z, z = -Y.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .dxf import FRAME_LAYERS, ZONE_PREFIX, DxfError, Report

LAMP_WORDS = {"XANH": "xanh", "VANG": "vang", "DO": "do"}
MAX_TRIANGLES = 400_000
PACKAGE = "cadquery-ocp-novtk"


class StepError(DxfError):
    """The STEP file cannot be used (reported like a DXF problem)."""


def occ_available() -> bool:
    try:
        import OCP  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class PlainPart:
    """One meshed part, in CAD coordinates (mm, Z up)."""
    names: list[str]                       # from the root down to this part
    vertices: list[tuple[float, float, float]]
    triangles: list[tuple[int, int, int]]
    edges: list[list[tuple[float, float, float]]] = field(default_factory=list)


def classify(names: list[str], mapping: dict[str, str]) -> tuple[str, str] | None:
    """('zone', id) or ('frame', kind) from the nearest named level, or None."""
    for name in reversed(names):
        if not name:
            continue
        if name in mapping:
            return "zone", mapping[name].lower()
        upper = name.strip().upper()
        for layer, kind in FRAME_LAYERS.items():
            if upper == layer or upper.startswith(layer + "-"):
                return "frame", kind
        if upper.startswith(ZONE_PREFIX):
            rest = upper[len(ZONE_PREFIX):]
            zone = rest.split("-", 1)[0]
            if zone:
                return "zone", zone.lower()
    return None


def lamp_of(names: list[str]) -> str | None:
    for name in reversed(names):
        tail = name.strip().upper().rsplit("-", 1)[-1]
        if tail in LAMP_WORDS:
            return LAMP_WORDS[tail]
    return None


def to_twin(p: tuple[float, float, float]) -> list[float]:
    x, y, z = p
    return [round(x, 2), round(z, 2), round(-y, 2)]


def mesh_part(part: PlainPart) -> dict:
    verts = [to_twin(v) for v in part.vertices]
    flat = [c for v in verts for c in v]
    tris = [i for t in part.triangles for i in t]
    edges = []
    for line in part.edges:
        pts = [to_twin(p) for p in line]
        for a, b in zip(pts, pts[1:]):
            edges.extend(a + b)
    xs, ys, zs = flat[0::3], flat[1::3], flat[2::3]
    return {"hinh": "luoi", "dinh": flat, "tam_giac": tris, "canh": edges,
            "hop_bao": [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)],
            "nguon": " / ".join(n for n in part.names if n)}


def assemble(parts: list[PlainPart], catalog: dict, record: dict,
             mapping: dict[str, str] | None = None) -> tuple[dict, Report]:
    mapping = mapping or {}
    report = Report()
    zones: dict[str, list[dict]] = {}
    frame: list[dict] = []
    unnamed: dict[str, int] = {}
    triangles = 0
    for part in parts:
        if not part.triangles:
            report.skip(f"{' / '.join(part.names) or 'chi tiết không tên'}: không chia được thành tam giác")
            continue
        where = classify(part.names, mapping)
        if where is None:
            key = " / ".join(n for n in part.names if n) or "(không tên)"
            unnamed[key] = unnamed.get(key, 0) + 1
            continue
        triangles += len(part.triangles)
        piece = mesh_part(part)
        report.shapes += 1
        if where[0] == "zone":
            lamp = lamp_of(part.names)
            if lamp:
                piece["den"] = lamp
            zones.setdefault(where[1], []).append(piece)
        else:
            piece["loai"] = where[1]
            piece["ten"] = part.names[-1] if part.names else where[1]
            frame.append(piece)
    report.other_layers = unnamed
    if triangles > MAX_TRIANGLES:
        raise StepError(f"bản vẽ chia ra {triangles} tam giác, quá {MAX_TRIANGLES}: "
                        "nhập lại với độ mịn thô hơn (--do-min lớn hơn)")
    known = {z["id"]: z for z in catalog["vung"]}
    extra = sorted(set(zones) - set(known))
    missing = sorted(set(known) - set(zones))
    if extra:
        raise StepError("bản vẽ có chi tiết mang tên vùng không có trong danh mục: "
                        + ", ".join(ZONE_PREFIX + z.upper() for z in extra))
    if missing:
        hint = ""
        if unnamed:
            names = ", ".join(sorted(unnamed)[:12])
            hint = (f". Các tên chi tiết chưa gán vùng: {names}"
                    + (" …" if len(unnamed) > 12 else "")
                    + ". Có thể gán bằng tệp ánh xạ (--anh-xa).")
        raise StepError("danh mục có vùng mà bản vẽ không có chi tiết nào: "
                        + ", ".join(ZONE_PREFIX + z.upper() for z in missing) + hint)
    out_zones = []
    for zone in catalog["vung"]:
        entry = {k: v for k, v in zone.items() if k != "khoi"}
        entry["khoi"] = zones[zone["id"]]
        if any(p.get("den") for p in zone.get("khoi", [])) and \
                not any(p.get("den") for p in entry["khoi"]):
            report.skip(f"vùng {zone['id']}: không chi tiết nào có tên đèn (-XANH, -VANG, -DO)")
        out_zones.append(entry)
    model = {
        "phien_ban": 1,
        "ten": record.get("ten") or catalog.get("ten", ""),
        "trang_thai_ban_ve": record["muc"],
        "ban_ve": dict({k: record[k] for k in ("id", "so_ban_ve", "revision", "tep_nguon", "sha256", "muc")},
                       loai="step"),
        "ghi_chu": (f"Dựng từ bản vẽ STEP bằng OpenCASCADE: {triangles} tam giác. "
                    "Chi tiết không gán được vùng được liệt kê trong sổ bản vẽ, không đoán."),
        "ghi_chu_kiem_tra": catalog.get("ghi_chu_kiem_tra", ""),
        "vung": out_zones,
        "khung": frame,
    }
    return model, report


# ---------------------------------------------------------------------------
# OpenCASCADE layer (runs only where cadquery-ocp-novtk is installed)
# ---------------------------------------------------------------------------

def occ_parts(path, deflection: float | None = None) -> tuple[list[PlainPart], dict]:
    """Reads the STEP assembly with names; meshes each leaf part.

    Returns (parts, info) where info has the bounding box size and the
    deflection used. ``deflection`` (mm) defaults to 0.2 % of the model's
    diagonal, at least 0.1 mm.
    """
    if not occ_available():
        raise StepError(f"chưa cài {PACKAGE}: bấm đúp CAI-DAT-STEP.cmd")
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Bnd import Bnd_Box
    from OCP.GCPnts import GCPnts_TangentialDeflection
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    # Only real classes, no collection typedefs (TDF_LabelSequence,
    # TopTools_IndexedMapOfShape...): OpenCASCADE 8 dropped many of those.
    from OCP.TDF import TDF_ChildIterator, TDF_Label
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

    def cast(kind: str):
        """TopoDS.Face_s in OCP 7; OCP 8 may name it differently."""
        for name in (kind + "_s", kind):
            found = getattr(TopoDS, name, None)
            if found is not None:
                return found
        raise StepError(f"bản OCP này không có hàm đổi kiểu TopoDS.{kind}: "
                        "xem nhat-ky/chan-doan-occ.txt")

    as_face, as_edge, as_vertex = cast("Face"), cast("Edge"), cast("Vertex")

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    status = reader.ReadFile(str(path))
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepError(f"OpenCASCADE không đọc được tệp STEP (mã {status})")
    if not reader.Transfer(doc):
        raise StepError("OpenCASCADE đọc được tệp nhưng không dựng được hình")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def name_of(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return attr.Get().ToExtString()
        return ""

    leaves: list[tuple[list[str], object]] = []

    def walk(label, loc, names, depth=0):
        if depth > 64:
            raise StepError("cây lắp ráp sâu quá 64 cấp")
        own = name_of(label)
        if XCAFDoc_ShapeTool.IsReference_s(label):
            ref = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(label, ref)
            inner = loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(label))
            # The instance name and the product name can differ; keep both.
            walk(ref, inner, names + [own], depth + 1)
        elif XCAFDoc_ShapeTool.IsAssembly_s(label):
            # The components of an assembly are its child labels.
            child = TDF_ChildIterator(label, False)
            while child.More():
                comp = child.Value()
                if XCAFDoc_ShapeTool.IsComponent_s(comp):
                    walk(comp, loc, names + [own], depth + 1)
                child.Next()
        else:
            shape = XCAFDoc_ShapeTool.GetShape_s(label)
            if not shape.IsNull():
                leaves.append((names + [own], shape.Moved(loc)))

    # Free shapes are the top-level shape labels no assembly uses.
    top = TDF_ChildIterator(shape_tool.Label(), False)
    while top.More():
        label = top.Value()
        if XCAFDoc_ShapeTool.IsShape_s(label) and XCAFDoc_ShapeTool.IsFree_s(label):
            walk(label, TopLoc_Location(), [])
        top.Next()
    if not leaves:
        raise StepError("tệp STEP không có chi tiết hình học nào")

    box = Bnd_Box()
    for _names, shape in leaves:
        BRepBndLib.Add_s(shape, box, True)
    try:
        # Bnd_Box.Get() returns a struct OCP 8 cannot convert; the corners work.
        low, high = box.CornerMin(), box.CornerMax()
        xmin, ymin, zmin, xmax, ymax, zmax = low.X(), low.Y(), low.Z(), high.X(), high.Y(), high.Z()
    except (TypeError, AttributeError):
        points = []
        for _names, shape in leaves:
            vertex = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
            while vertex.More():
                p = BRep_Tool.Pnt_s(as_vertex(vertex.Current()))
                points.append((p.X(), p.Y(), p.Z()))
                vertex.Next()
        if not points:
            raise StepError("không tính được kích thước bao của bản vẽ")
        xs, ys, zs = zip(*points)
        xmin, ymin, zmin, xmax, ymax, zmax = min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)
    diagonal = math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
    if deflection is None:
        deflection = max(0.1, diagonal * 0.002)

    parts = []
    for names, shape in leaves:
        BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
        vertices, triangles = [], []
        explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
        while explorer.More():
            face = as_face(explorer.Current())
            location = TopLoc_Location()
            poly = BRep_Tool.Triangulation_s(face, location)
            if poly is not None:
                trsf = location.Transformation()
                offset = len(vertices)
                for k in range(1, poly.NbNodes() + 1):
                    p = poly.Node(k).Transformed(trsf)
                    vertices.append((p.X(), p.Y(), p.Z()))
                reverse = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
                for k in range(1, poly.NbTriangles() + 1):
                    t = poly.Triangle(k)
                    a, b, c = t.Value(1) - 1 + offset, t.Value(2) - 1 + offset, t.Value(3) - 1 + offset
                    triangles.append((a, c, b) if reverse else (a, b, c))
            explorer.Next()
        edges, seen = [], set()
        edge_explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
        while edge_explorer.More():
            edge = as_edge(edge_explorer.Current())
            edge_explorer.Next()
            try:
                key = hash(edge)       # an edge shared by two faces is drawn once
            except TypeError:
                key = None
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            if BRep_Tool.Degenerated_s(edge):
                continue
            try:
                curve = BRepAdaptor_Curve(edge)
                points = GCPnts_TangentialDeflection(curve, 0.3, deflection)
                line = []
                for j in range(1, points.NbPoints() + 1):
                    p = points.Value(j)
                    line.append((p.X(), p.Y(), p.Z()))
                if len(line) >= 2:
                    edges.append(line)
            except Exception:  # an edge without a 3D curve: the mesh is still drawn
                continue
        parts.append(PlainPart([n for n in names if n is not None], vertices, triangles, edges))
    return parts, {"kich_thuoc_mm": [round(xmax - xmin, 1), round(ymax - ymin, 1), round(zmax - zmin, 1)],
                   "do_min_mm": round(deflection, 3)}


# Every OpenCASCADE name the reader relies on, as (module, class, attribute).
API_USED = [
    ("BRep", "BRep_Tool", "Triangulation_s"), ("BRep", "BRep_Tool", "Degenerated_s"),
    ("BRep", "BRep_Tool", "Pnt_s"), ("BRepAdaptor", "BRepAdaptor_Curve", None),
    ("BRepBndLib", "BRepBndLib", "Add_s"), ("BRepMesh", "BRepMesh_IncrementalMesh", None),
    ("Bnd", "Bnd_Box", "CornerMin"), ("Bnd", "Bnd_Box", "CornerMax"),
    ("GCPnts", "GCPnts_TangentialDeflection", "NbPoints"),
    ("GCPnts", "GCPnts_TangentialDeflection", "Value"),
    ("STEPCAFControl", "STEPCAFControl_Reader", "SetNameMode"),
    ("STEPCAFControl", "STEPCAFControl_Reader", "ReadFile"),
    ("STEPCAFControl", "STEPCAFControl_Reader", "Transfer"),
    ("TDF", "TDF_ChildIterator", "Value"), ("TDF", "TDF_Label", "FindAttribute"),
    ("TDataStd", "TDataStd_Name", "GetID_s"), ("TDataStd", "TDataStd_Name", "Get"),
    ("TDocStd", "TDocStd_Document", "Main"),
    ("TopExp", "TopExp_Explorer", "Current"), ("TopLoc", "TopLoc_Location", "Multiplied"),
    ("TopLoc", "TopLoc_Location", "Transformation"),
    # OCP 7 names the casts Face_s...; OCP 8.0.1 names them Face... (seen on the laptop).
    ("TopoDS", "TopoDS", ("Face_s", "Face")), ("TopoDS", "TopoDS", ("Edge_s", "Edge")),
    ("TopoDS", "TopoDS", ("Vertex_s", "Vertex")),
    ("XCAFDoc", "XCAFDoc_DocumentTool", "ShapeTool_s"),
    ("XCAFDoc", "XCAFDoc_ShapeTool", "IsReference_s"), ("XCAFDoc", "XCAFDoc_ShapeTool", "GetReferredShape_s"),
    ("XCAFDoc", "XCAFDoc_ShapeTool", "GetLocation_s"), ("XCAFDoc", "XCAFDoc_ShapeTool", "IsAssembly_s"),
    ("XCAFDoc", "XCAFDoc_ShapeTool", "IsComponent_s"), ("XCAFDoc", "XCAFDoc_ShapeTool", "GetShape_s"),
    ("XCAFDoc", "XCAFDoc_ShapeTool", "IsShape_s"), ("XCAFDoc", "XCAFDoc_ShapeTool", "IsFree_s"),
    ("XCAFDoc", "XCAFDoc_ShapeTool", "Label"),
    ("Poly", "Poly_Triangulation", "NbNodes"), ("Poly", "Poly_Triangulation", "Node"),
    ("Poly", "Poly_Triangulation", "NbTriangles"), ("Poly", "Poly_Triangulation", "Triangle"),
    ("Poly", "Poly_Triangle", "Value"), ("gp", "gp_Pnt", "Transformed"),
]


def api_report() -> list[str]:
    """Each missing name, with the names that do exist next to it."""
    import importlib

    missing = []
    for module, cls, attr in API_USED:
        try:
            owner = getattr(importlib.import_module("OCP." + module), cls)
        except (ImportError, AttributeError) as exc:
            missing.append(f"OCP.{module}.{cls}: KHÔNG CÓ ({exc.__class__.__name__})")
            continue
        choices = attr if isinstance(attr, tuple) else (attr,)
        if attr and not any(hasattr(owner, a) for a in choices):
            attr = choices[0]
            stem = attr[:-2] if attr.endswith("_s") else attr
            near = sorted(n for n in dir(owner) if stem.lower() in n.lower())
            missing.append(f"OCP.{module}.{cls}.{attr}: KHÔNG CÓ; tên gần giống: "
                           + (", ".join(near) or "(không có)")
                           + f"; mọi tên của {cls}: "
                           + ", ".join(n for n in dir(owner) if not n.startswith("__")))
    return missing


def diagnose(error: BaseException) -> str:
    """Lists what the installed OCP really offers around a failing name, so a
    fix is based on the package itself and not on a guess."""
    import importlib
    import pkgutil
    import traceback

    lines = ["Lỗi: " + "".join(traceback.format_exception_only(type(error), error)).strip()]
    try:
        import OCP
        lines.append(f"OCP: {getattr(OCP, '__version__', '?')} tại {getattr(OCP, '__file__', '?')}")
        missing = api_report()
        lines.append(f"Kiểm tra {len(API_USED)} tên OpenCASCADE mã dùng: thiếu {len(missing)}")
        lines += ["  " + m for m in missing]
        words = ("LabelSequence", "ChildIterator", "IndexedMapOfShape", "FreeShapes",
                 "Triangulation", "TangentialDeflection")
        for module in ("TDF", "TopTools", "XCAFDoc", "BRep", "GCPnts", "Poly", "TopExp"):
            try:
                mod = importlib.import_module("OCP." + module)
            except ImportError as exc:
                lines.append(f"OCP.{module}: không nạp được ({exc})")
                continue
            names = sorted(n for n in dir(mod) if not n.startswith("_"))
            lines.append(f"OCP.{module}: {len(names)} tên; " + ", ".join(names[:400]))
        for word in words:
            hits = []
            for info in pkgutil.iter_modules(OCP.__path__):
                try:
                    mod = importlib.import_module("OCP." + info.name)
                except Exception:
                    continue
                hits += [f"{info.name}.{n}" for n in dir(mod) if word in n]
            lines.append(f"có chứa '{word}': " + (", ".join(hits[:40]) or "không có"))
    except Exception as exc:  # the diagnosis itself must never hide the error
        lines.append(f"Không chẩn đoán được: {exc!r}")
    return "\n".join(lines)


def build_model(path, catalog: dict, record: dict, mapping: dict[str, str] | None = None,
                deflection: float | None = None) -> tuple[dict, Report, dict]:
    parts, info = occ_parts(path, deflection)
    model, report = assemble(parts, catalog, record, mapping)
    return model, report, info


def write_sample(path, model: dict) -> None:
    """Writes a STEP assembly of the reference model, named by the rule above.

    Used by the self-check on the laptop: OpenCASCADE writes the file and the
    reader above reads it back, so both directions are exercised.
    """
    if not occ_available():
        raise StepError(f"chưa cài {PACKAGE}")
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_StepModelType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    def cad(p):  # twin (x, up, z) -> CAD (X, Y, Z up)
        x, y, z = p
        return x, -z, y

    def solid(part):
        if part["hinh"] == "hop":
            w, h, d = part["kich_thuoc"]       # twin: x, up, z
            shape = BRepPrimAPI_MakeBox(gp_Pnt(-w / 2, -d / 2, -h / 2), w, d, h).Shape()
            rx = (part.get("xoay") or [0, 0, 0])[0]
            if rx:
                rot = gp_Trsf()
                # twin rotation about x by rx; in CAD the same axis is X, and
                # the twin's z is -Y, so the angle keeps its sign.
                rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.radians(rx))
                shape = BRepBuilderAPI_Transform(shape, rot, True).Shape()
        else:
            r, length = part["ban_kinh"], part["dai"]
            axis = {"y": gp_Dir(0, 0, 1), "x": gp_Dir(1, 0, 0), "z": gp_Dir(0, -1, 0)}[part.get("truc", "y")]
            start = gp_Pnt(0, 0, 0).Translated(gp_Vec(axis.X(), axis.Y(), axis.Z()).Multiplied(-length / 2))
            shape = BRepPrimAPI_MakeCylinder(gp_Ax2(start, axis), r, length).Shape()
        move = gp_Trsf()
        move.SetTranslation(gp_Vec(*cad(part["tam"])))
        return shape, TopLoc_Location(move)

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    top = tool.NewShape()
    TDataStd_Name.Set_s(top, TCollection_ExtendedString("BAN-THU"))

    def add(name, part):
        shape, loc = solid(part)
        label = tool.AddShape(shape, False)
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        tool.AddComponent(top, label, loc)

    for part in model["khung"]:
        add({"nen": "KHUNG-NEN", "vo": "KHUNG-VO", "trong": "KHUNG-TRONG"}[part["loai"]], part)
    for zone in model["vung"]:
        for i, part in enumerate(zone["khoi"]):
            suffix = {"xanh": "-XANH", "vang": "-VANG", "do": "-DO"}.get(part.get("den"), f"-{i + 1}")
            add("VUNG-" + zone["id"].upper() + suffix, part)
    tool.UpdateAssemblies()
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    if not writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs):
        raise StepError("OpenCASCADE không chuyển được mô hình mẫu sang STEP")
    if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepError("OpenCASCADE không ghi được tệp STEP mẫu")
