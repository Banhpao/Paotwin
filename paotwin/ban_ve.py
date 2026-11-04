"""The drawing register (sổ bản vẽ): which drawings exist, at what level,
and who did what to them. Nothing is overwritten: every change is a new line
in the history (DEC-014).

Levels (DEC-001, DEC-005, CORE-02):
- tham_khao (Reference): the hand-made block model shipped with the demo.
- candidate: built from a drawing file, not yet checked against the machine.
- verified: a named person checked it against the machine as installed.

A drawing of the test bench uses the bench's zones (mo-hinh/ban-thu.json)
and signals (cau-hinh/tin-hieu.json). A drawing of another machine uses that
machine's own folder ban-ve/may/<mã máy>/: danh-muc.json (the zones, made with
the zoning tool), anh-xa.json (which part goes in which zone) and, once its
sensors are known, tin-hieu.json (same format as cau-hinh/tin-hieu.json). The
twin uses the signals of the machine whose drawing it shows; a machine without
tin-hieu.json cannot be chosen. Real drawings and what is
built from them stay out of git (see .gitignore): models built here go to
mo-hinh/nhap/.

Commands (run from the project folder)::

    python -m paotwin.ban_ve nhap <tệp.dxf | tệp.step> [--nguoi TÊN] [--quyen GHI_CHÚ] [--dung]
        với STEP thêm: --so-ban-ve SỐ --revision REV [--anh-xa tệp.json] [--do-min MM]
                       [--truc-dung Z|Y] (Y: tệp lấy Y làm trục đứng, như SolidWorks)
        máy khác bàn thử: --may MÃ (dùng ban-ve/may/MÃ/) hoặc --danh-muc tệp.json
    python -m paotwin.ban_ve xac-minh <mã> --nguoi TÊN --ghi-chu "đã đối chiếu gì"
    python -m paotwin.ban_ve dung <mã | tham-khao>
    python -m paotwin.ban_ve xem
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import common, dxf, step

REFERENCE = "tham-khao"
MACHINE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
IMPORTED_DIR = "mo-hinh/nhap"
LEVEL_NAMES = {"tham_khao": "Tham khảo", "candidate": "Candidate (chưa đối chiếu với máy thật)",
               "verified": "Verified (đã đối chiếu với máy thật)"}


def register_path(root: Path | None = None) -> Path:
    return (root or common.ROOT) / "ban-ve" / "so-ban-ve.json"


def load(root: Path | None = None) -> dict:
    path = register_path(root)
    if not path.exists():
        return {"dang_dung": None, "bo": [], "lich_su": []}
    return common.load_json(path)


def save(register: dict, root: Path | None = None) -> None:
    path = register_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(register, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(register: dict, entry: dict, record: dict | None = None) -> None:
    line = {"luc": _now(), **entry}
    register["lich_su"].append(line)
    if record is not None:
        record.setdefault("lich_su", []).append(line)


def machine_folder(machine: str, root: Path | None = None) -> Path:
    """ban-ve/may/<mã>/: the zone list and zone file of one machine."""
    if not MACHINE_ID.match(machine or ""):
        raise SystemExit(f"Mã máy {machine!r} không hợp lệ: chữ thường không dấu, số, - hoặc _, "
                         "tối đa 40 ký tự (vd bts7960).")
    return (root or common.ROOT) / "ban-ve" / "may" / machine


def check_catalog(catalog: dict, where: str) -> dict:
    """A zone list must name each zone once, with an id the twin can use."""
    zones = catalog.get("vung") if isinstance(catalog, dict) else None
    if not isinstance(zones, list) or not zones:
        raise SystemExit(f"{where}: danh mục chưa có vùng nào.")
    seen = set()
    for zone in zones:
        zid = zone.get("id") if isinstance(zone, dict) else None
        if not isinstance(zid, str) or not step.ZONE_ID.match(zid):
            raise SystemExit(f"{where}: mã vùng {zid!r} không hợp lệ (chữ thường không dấu, số, _).")
        if zid in seen:
            raise SystemExit(f"{where}: vùng {zid} có hai lần.")
        if not str(zone.get("ten", "")).strip():
            raise SystemExit(f"{where}: vùng {zid} chưa có tên.")
        seen.add(zid)
    return catalog


def _find(register: dict, set_id: str) -> dict:
    for record in register["bo"]:
        if record["id"] == set_id:
            return record
    raise SystemExit(f"Không có bộ bản vẽ mã {set_id!r}. Xem danh sách: python -m paotwin.ban_ve xem")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_model(model: dict, path: Path) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(model, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temp, path)


STEP_SUFFIXES = (".step", ".stp")


def import_drawing(source: Path, *, person: str, rights: str, root: Path | None = None,
                   catalog: dict | None = None, number: str | None = None,
                   revision: str | None = None, mapping: Path | None = None,
                   deflection: float | None = None,
                   catalog_source: str | None = None,
                   up: str | None = None) -> tuple[dict, dxf.Report, bool]:
    """Adds a drawing as a Candidate. Returns (record, report, already_there).

    ``catalog`` is the zone list to build against (the bench's when None);
    ``catalog_source`` says where it came from, for the record. ``up`` is the
    STEP file's vertical axis when the zone file does not say."""
    root = root or common.ROOT
    source = Path(source)
    kind = source.suffix.lower()
    if kind != ".dxf" and kind not in STEP_SUFFIXES:
        raise SystemExit("Đọc được tệp .dxf (dạng chữ) và .step / .stp.")
    if catalog is None:
        catalog = common.load_json(root / "mo-hinh" / "ban-thu.json")
    else:
        check_catalog(catalog, catalog_source or "danh mục vùng")
    register = load(root)
    digest = _sha256(source)
    names = common.load_json(Path(mapping)) if mapping else {}
    if kind == ".dxf":
        title, _zones, _frame, _ = dxf.convert(source)
    else:
        if not step.occ_available():
            raise SystemExit(f"Chưa cài {step.PACKAGE} để đọc STEP: bấm đúp CAI-DAT-STEP.cmd.")
        title = {}
    number = number or title.get("so_ban_ve") or source.stem
    revision = revision or title.get("revision") or "?"
    set_id = re.sub(r"[^0-9A-Za-z_.-]+", "-", f"{number}-{revision}").strip("-")
    # What the model is built from besides the file: same file built another
    # way (zones, zone file, vertical axis) is another model.
    how = {"danh_muc": catalog, "anh_xa": names, "truc_dung": up}
    how_digest = hashlib.sha256(json.dumps(how, ensure_ascii=False, sort_keys=True)
                                .encode("utf-8")).hexdigest()
    plain = not names and up is None and catalog_source is None
    for record in register["bo"]:
        if record["id"] == set_id:
            if record["sha256"] != digest:
                raise SystemExit(
                    f"Đã có bản vẽ {number} revision {revision} với nội dung khác. Bản vẽ sửa thì phải "
                    "đổi revision trong khung tên (REV: …), không ghi đè bản cũ.")
            same = record["cach_dung"] == how_digest if "cach_dung" in record else plain
            if same:
                # Nothing is rebuilt; what the import found is in record["bao_cao"].
                return record, dxf.Report(), True
            raise SystemExit(
                f"Đã có {set_id} từ đúng tệp này nhưng dựng theo cách khác (danh mục vùng, cách gán "
                f"hoặc trục đứng đã đổi). Sổ bản vẽ không ghi đè: nhập với revision khác "
                f"(vd {revision}2) để giữ cả hai.")
    folder = root / "ban-ve" / "tep"   # the register keeps its own copy of every file
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / source.name
    if stored.resolve() != source.resolve():
        if stored.exists() and _sha256(stored) != digest:
            stored = folder / f"{set_id}{source.suffix.lower()}"
        shutil.copyfile(source, stored)
    record = {"id": set_id, "so_ban_ve": number, "revision": revision,
              "ten": title.get("ten", ""), "loai": "dxf" if kind == ".dxf" else "step",
              "tep_nguon": stored.relative_to(root).as_posix(), "sha256": digest,
              "muc": "candidate", "quyen": rights,
              "mo_hinh": f"{IMPORTED_DIR}/{set_id}.json", "cach_dung": how_digest}
    if catalog_source:
        record["danh_muc"] = catalog_source
    if kind == ".dxf":
        model, report = dxf.build_model(stored, catalog, record)
        extra = {}
    else:
        if names:
            record["anh_xa"] = names
        model, report, extra = step.build_model(stored, catalog, record, names, deflection, up)
        record["truc_dung"] = extra["truc_dung"]
    record["bao_cao"] = {"so_khoi": report.shapes, "bo_qua": report.skipped,
                         "lop_khac": report.other_layers, "chua_ho_tro": report.unsupported,
                         "bo_qua_theo_gan": report.ignored, **extra}
    if catalog.get("ma") and catalog["ma"] != BENCH:
        model["may"] = catalog["ma"]    # which machine's signals the twin uses with it
    (root / record["mo_hinh"]).parent.mkdir(parents=True, exist_ok=True)
    _write_model(model, root / record["mo_hinh"])
    register["bo"].append(record)
    _log(register, {"viec": "nhap", "bo": set_id, "nguoi": person,
                    "ghi_chu": f"Nhập {source.name}, {report.shapes} khối, bỏ qua {len(report.skipped)}"},
         record)
    save(register, root)
    return record, report, False


def verify(set_id: str, *, person: str, note: str, root: Path | None = None) -> dict:
    root = root or common.ROOT
    if not person.strip():
        raise SystemExit("Xác minh cần tên người đã đối chiếu với máy thật (--nguoi).")
    if not note.strip():
        raise SystemExit("Xác minh cần ghi đã đối chiếu những gì (--ghi-chu).")
    register = load(root)
    record = _find(register, set_id)
    record["muc"] = "verified"
    record["xac_minh"] = {"nguoi": person, "luc": _now(), "ghi_chu": note}
    model_path = root / record["mo_hinh"]
    model = common.load_json(model_path)
    model["trang_thai_ban_ve"] = "verified"
    model["ban_ve"]["muc"] = "verified"
    model["ban_ve"]["xac_minh"] = record["xac_minh"]
    _write_model(model, model_path)
    _log(register, {"viec": "xac_minh", "bo": set_id, "nguoi": person, "ghi_chu": note}, record)
    save(register, root)
    return record


BENCH = "ban-thu"
_MACHINE_IN_PATH = re.compile(r"^ban-ve/may/([^/]+)/")


def machine_of(model: dict, record: dict | None = None) -> str | None:
    """The machine a model is of; None for the test bench."""
    machine = model.get("may")
    if not machine and record:
        found = _MACHINE_IN_PATH.match(record.get("danh_muc") or "")
        machine = found.group(1) if found else None
    return None if machine in (None, "", BENCH) else machine


def signals_for(model: dict, root: Path | None = None,
                record: dict | None = None) -> tuple[dict, str]:
    """(signal configuration, where it is) for the machine the model is of."""
    root = root or common.ROOT
    machine = machine_of(model, record)
    if machine is None:
        path = root / "cau-hinh" / "tin-hieu.json"
        config = common.load_json(path) if path.exists() else common.signals_config()
        return config, "cau-hinh/tin-hieu.json"
    path = machine_folder(machine, root) / "tin-hieu.json"
    where = path.relative_to(root).as_posix()
    if not path.exists():
        raise SystemExit(f"Máy {machine} chưa có cấu hình tín hiệu ({where}): bản sao cần biết cảm biến "
                         "nào gắn vào vùng nào của máy này (xem README, mục Tín hiệu của từng máy).")
    return common.load_json(path), where


def _zones_the_signals_need(config: dict) -> dict[str, list[str]]:
    need: dict[str, list[str]] = {}
    for signal in config.get("tin_hieu", []):
        need.setdefault(signal["vung"], []).append(signal["id"])
    for machine in config.get("may", []):
        for zone in machine.get("vung", []):
            need.setdefault(zone, []).append(f"máy {machine['id']}")
    return need


def use(set_id: str, *, person: str, root: Path | None = None) -> None:
    root = root or common.ROOT
    register = load(root)
    if set_id == REFERENCE:
        register["dang_dung"] = None
    else:
        record = _find(register, set_id)
        # The twin attaches each signal of the machine to a zone of the model;
        # a mismatch would stop it from starting. Say so now, not at the next start.
        model = common.load_json(root / record["mo_hinh"])
        config, where = signals_for(model, root, record)
        have = {z["id"] for z in model["vung"]}
        missing = sorted(z for z in _zones_the_signals_need(config) if z not in have)
        if missing:
            other = machine_of(model, record) is None and record.get("danh_muc")
            raise SystemExit(
                f"Chưa dùng được bộ {set_id} cho bản sao: tín hiệu trong {where} gắn vào vùng "
                f"{', '.join(missing)} mà bản vẽ này không có."
                + (f" Bản vẽ dựng theo danh mục {record['danh_muc']}." if other else ""))
        try:
            from .engine import Engine
            Engine(config, model)
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Chưa dùng được bộ {set_id}: {where} có chỗ sai ({exc!r}).") from None
        register["dang_dung"] = set_id
    _log(register, {"viec": "dung", "bo": set_id, "nguoi": person,
                    "ghi_chu": "Bản sao dùng bộ này từ lần chạy tới"})
    save(register, root)


def model_in_use(root: Path | None = None) -> dict:
    """The model the twin shows: the set in use, or the reference model."""
    root = root or common.ROOT
    register = load(root)
    if register.get("dang_dung"):
        record = _find(register, register["dang_dung"])
        model = common.load_json(root / record["mo_hinh"])
        machine = machine_of(model, record)
        if machine:
            model["may"] = machine      # older sets name it only in the record
        return model
    return common.load_json(root / "mo-hinh" / "ban-thu.json")


def _print_report(record: dict) -> None:
    report = record.get("bao_cao", {})
    print(f"  Số khối dựng được: {report.get('so_khoi', 0)}")
    for line in report.get("bo_qua", []):
        print(f"  Bỏ qua: {line}")
    for layer, count in report.get("lop_khac", {}).items():
        print(f"  Lớp hoặc chi tiết không thuộc vùng nào, không dùng: {layer} ({count})")
    for part, count in report.get("bo_qua_theo_gan", {}).items():
        print(f"  Không đưa vào mô hình (đã chọn bỏ qua khi khoanh vùng): {part} ({count})")
    for kind, count in report.get("chua_ho_tro", {}).items():
        print(f"  Loại đối tượng chưa đọc: {kind} ({count})")
    if "kich_thuoc_mm" in report:
        print(f"  Kích thước bao (mm): {report['kich_thuoc_mm']}, độ mịn {report['do_min_mm']} mm")
    if record.get("truc_dung"):
        print(f"  Trục đứng của tệp: {record['truc_dung']}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m paotwin.ban_ve", description="Sổ bản vẽ")
    sub = parser.add_subparsers(dest="lenh", required=True)
    p_in = sub.add_parser("nhap", help="nhập một bản vẽ DXF (thành Candidate)")
    p_in.add_argument("tep")
    p_in.add_argument("--nguoi", default=getpass.getuser())
    p_in.add_argument("--quyen", default="Chưa ghi quyền dùng bản vẽ")
    p_in.add_argument("--dung", action="store_true", help="cho bản sao dùng luôn bộ này")
    p_in.add_argument("--so-ban-ve", help="số bản vẽ (STEP không có khung tên đọc được)")
    p_in.add_argument("--revision", help="revision của bản vẽ")
    p_in.add_argument("--anh-xa", help="tệp gán vùng (.json): {tên chi tiết: mã vùng} hoặc tệp của công cụ khoanh vùng")
    p_in.add_argument("--danh-muc", help="danh mục vùng của máy (.json); bỏ trống: vùng của bàn thử")
    p_in.add_argument("--may", help="mã máy: dùng ban-ve/may/MÃ/danh-muc.json và anh-xa.json")
    p_in.add_argument("--do-min", type=float, help="độ mịn chia tam giác, mm (STEP)")
    p_in.add_argument("--truc-dung", choices=step.UP_AXES,
                      help="trục đứng của tệp STEP: Z (mặc định) hoặc Y (vd SolidWorks)")
    p_ok = sub.add_parser("xac-minh", help="ghi đã đối chiếu với máy thật (thành Verified)")
    p_ok.add_argument("ma")
    p_ok.add_argument("--nguoi", required=True)
    p_ok.add_argument("--ghi-chu", required=True)
    p_use = sub.add_parser("dung", help="chọn bộ bản vẽ bản sao sẽ dùng")
    p_use.add_argument("ma")
    p_use.add_argument("--nguoi", default=getpass.getuser())
    sub.add_parser("xem", help="liệt kê các bộ bản vẽ")
    args = parser.parse_args(argv)

    try:
        if args.lenh == "nhap":
            catalog_path, mapping_path = args.danh_muc, args.anh_xa
            if args.may:
                folder = machine_folder(args.may)
                catalog_path = catalog_path or str(folder / "danh-muc.json")
                if not mapping_path and (folder / "anh-xa.json").exists():
                    mapping_path = str(folder / "anh-xa.json")
            catalog = source = None
            if catalog_path:
                if not Path(catalog_path).exists():
                    raise SystemExit(f"Không có danh mục vùng {catalog_path}: khoanh vùng trước "
                                     "(KHOANH-VUNG.cmd).")
                catalog = common.load_json(Path(catalog_path))
                try:
                    source = Path(catalog_path).resolve().relative_to(common.ROOT).as_posix()
                except ValueError:
                    source = str(catalog_path)
            record, _report, existed = import_drawing(
                Path(args.tep), person=args.nguoi, rights=args.quyen, number=args.so_ban_ve,
                revision=args.revision, mapping=mapping_path, deflection=args.do_min,
                catalog=catalog, catalog_source=source, up=args.truc_dung)
            print(("Đã có sẵn: " if existed else "Đã nhập: ")
                  + f"{record['id']} ({LEVEL_NAMES[record['muc']]})")
            _print_report(record)
            if args.dung:
                use(record["id"], person=args.nguoi)
                print(f"Bản sao sẽ dùng {record['id']} từ lần chạy demo tới.")
        elif args.lenh == "xac-minh":
            record = verify(args.ma, person=args.nguoi, note=args.ghi_chu)
            print(f"{record['id']}: {LEVEL_NAMES['verified']} bởi {args.nguoi}.")
        elif args.lenh == "dung":
            use(args.ma, person=args.nguoi)
            print(f"Bản sao sẽ dùng {args.ma} từ lần chạy demo tới.")
        else:
            register = load()
            using = register.get("dang_dung") or REFERENCE
            print(f"Đang dùng: {using}")
            print(f"  {REFERENCE}: mô hình khối làm tay ({LEVEL_NAMES['tham_khao']})")
            for record in register["bo"]:
                print(f"  {record['id']}: {LEVEL_NAMES[record['muc']]}, tệp {record['tep_nguon']}")
    except dxf.DxfError as exc:
        print(f"Không đọc được bản vẽ: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
