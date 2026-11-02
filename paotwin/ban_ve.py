"""The drawing register (sổ bản vẽ): which drawings exist, at what level,
and who did what to them. Nothing is overwritten: every change is a new line
in the history (DEC-014).

Levels (DEC-001, DEC-005, CORE-02):
- tham_khao (Reference): the hand-made block model shipped with the demo.
- candidate: built from a drawing file, not yet checked against the machine.
- verified: a named person checked it against the machine as installed.

Commands (run from the project folder)::

    python -m paotwin.ban_ve nhap <tệp.dxf | tệp.step> [--nguoi TÊN] [--quyen GHI_CHÚ] [--dung]
        với STEP thêm: --so-ban-ve SỐ --revision REV [--anh-xa tệp.json] [--do-min MM]
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
                   deflection: float | None = None) -> tuple[dict, dxf.Report, bool]:
    """Adds a drawing as a Candidate. Returns (record, report, already_there)."""
    root = root or common.ROOT
    source = Path(source)
    kind = source.suffix.lower()
    if kind != ".dxf" and kind not in STEP_SUFFIXES:
        raise SystemExit("Đọc được tệp .dxf (dạng chữ) và .step / .stp.")
    catalog = catalog or common.load_json(root / "mo-hinh" / "ban-thu.json")
    register = load(root)
    digest = _sha256(source)
    names = common.load_json(Path(mapping)) if mapping else {}
    if kind == ".dxf":
        title, _zones, _frame, _report = dxf.convert(source)
    else:
        if not step.occ_available():
            raise SystemExit(f"Chưa cài {step.PACKAGE} để đọc STEP: bấm đúp CAI-DAT-STEP.cmd.")
        title = {}
    number = number or title.get("so_ban_ve") or source.stem
    revision = revision or title.get("revision") or "?"
    set_id = re.sub(r"[^0-9A-Za-z_.-]+", "-", f"{number}-{revision}").strip("-")
    for record in register["bo"]:
        if record["id"] == set_id:
            if record["sha256"] == digest:
                return record, _report, True
            raise SystemExit(
                f"Đã có bản vẽ {number} revision {revision} với nội dung khác. Bản vẽ sửa thì phải "
                "đổi revision trong khung tên (REV: …), không ghi đè bản cũ.")
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
              "mo_hinh": f"mo-hinh/{set_id}.json"}
    if kind == ".dxf":
        model, report = dxf.build_model(stored, catalog, record)
        extra = {}
    else:
        if names:
            record["anh_xa"] = names
        model, report, extra = step.build_model(stored, catalog, record, names, deflection)
    record["bao_cao"] = {"so_khoi": report.shapes, "bo_qua": report.skipped,
                         "lop_khac": report.other_layers, "chua_ho_tro": report.unsupported,
                         **extra}
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


def use(set_id: str, *, person: str, root: Path | None = None) -> None:
    root = root or common.ROOT
    register = load(root)
    if set_id == REFERENCE:
        register["dang_dung"] = None
    else:
        _find(register, set_id)
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
        return common.load_json(root / record["mo_hinh"])
    return common.load_json(root / "mo-hinh" / "ban-thu.json")


def _print_report(record: dict) -> None:
    report = record.get("bao_cao", {})
    print(f"  Số khối dựng được: {report.get('so_khoi', 0)}")
    for line in report.get("bo_qua", []):
        print(f"  Bỏ qua: {line}")
    for layer, count in report.get("lop_khac", {}).items():
        print(f"  Lớp hoặc chi tiết không thuộc vùng nào, không dùng: {layer} ({count})")
    for kind, count in report.get("chua_ho_tro", {}).items():
        print(f"  Loại đối tượng chưa đọc: {kind} ({count})")
    if "kich_thuoc_mm" in report:
        print(f"  Kích thước bao (mm): {report['kich_thuoc_mm']}, độ mịn {report['do_min_mm']} mm")


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
    p_in.add_argument("--anh-xa", help="tệp JSON {tên chi tiết: mã vùng}")
    p_in.add_argument("--do-min", type=float, help="độ mịn chia tam giác, mm (STEP)")
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
            record, _report, existed = import_drawing(
                Path(args.tep), person=args.nguoi, rights=args.quyen, number=args.so_ban_ve,
                revision=args.revision, mapping=args.anh_xa, deflection=args.do_min)
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
