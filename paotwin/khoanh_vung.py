"""Zoning tool (công cụ khoanh vùng): open a STEP drawing, click its parts on
the 3D view and put each one in a zone of the machine.

    python -m paotwin.khoanh_vung <tệp.step> --may <mã máy> [--ten "Tên máy"]
        [--do-min MM] [--cong 8793] [--khong-mo-trinh-duyet]

What it writes, and nothing else: ban-ve/may/<mã>/danh-muc.json (the zones of
this machine) and ban-ve/may/<mã>/anh-xa.json (the zone file: part position ->
zone, and which axis of the file is vertical; see step.py). The copy each
save replaces is kept in ban-ve/may/<mã>/cu/.
The machine ``ban-thu`` is the test bench: its zones come from
mo-hinh/ban-thu.json and cannot be changed here, because the twin's signals
are attached to them.

The page can also import the drawing into the register (as a Candidate) with
that zone list and zone file. It never makes the twin use the drawing.

It listens on 127.0.0.1 only. A request whose Host is not this address, or a
write whose Origin is not this page, is refused: another web page open in the
same browser must not be able to read the drawing or change the zones.
"""

from __future__ import annotations

import argparse
import getpass
import gzip
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import ban_ve, common, step
from .dxf import DxfError

BENCH = "ban-thu"
DEFAULT_PORT = 8793
MAX_BODY = 2 * 1024 * 1024
MAX_ZONES = 200
PAGE = "khoanh-vung.html"
FILES = {PAGE: "text/html; charset=utf-8", "khoanh-vung.js": "text/javascript; charset=utf-8",
         "khoanh-vung.css": "text/css; charset=utf-8", "render.js": "text/javascript; charset=utf-8",
         "styles.css": "text/css; charset=utf-8"}
NUMBER = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,39}$")
REVISION = re.compile(r"^[0-9A-Za-z]{1,10}$")

log = logging.getLogger("khoanh-vung")


class Refused(Exception):
    """A request the tool will not carry out; the message is for the person."""


def part_labels(parts: list[step.PlainPart]) -> list[str]:
    """A short name for each part: its own name, numbered when several share it."""
    base = []
    for part in parts:
        named = [n.strip() for n in part.names if n and n.strip()]
        base.append(named[-1] if named else "(không tên)")
    totals, seen, out = Counter(base), Counter(), []
    for name in base:
        if totals[name] > 1:
            seen[name] += 1
            out.append(f"{name} #{seen[name]}")
        else:
            out.append(name)
    return out


def _key_order(key: str) -> list[int]:
    return [int(x) if x.isdigit() else 0 for x in key.split("/")]


def _decision(where: tuple[str, str] | None, lamp: str | None) -> dict | None:
    if where is None:
        return None
    if where[0] == "zone":
        return {"vung": where[1], **({"den": lamp} if lamp else {})}
    if where[0] == "frame":
        return {"khung": where[1]}
    return {"bo_qua": True}


def _write_keeping_old(path: Path, data: dict) -> None:
    """Writes the file whole; the copy it replaces goes to cu/ beside it."""
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.parent / "cu" / f"{path.stem}-{stamp}{path.suffix}"
        n = 1
        while backup.exists():
            backup = path.parent / "cu" / f"{path.stem}-{stamp}-{n}{path.suffix}"
            n += 1
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup)
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temp, path)


class Session:
    """One drawing opened for one machine."""

    def __init__(self, root: Path, machine: str, source: Path, parts: list[step.PlainPart],
                 info: dict, sha256: str, name: str | None = None, origin: str = ""):
        keys = [p.key for p in parts]
        if not all(keys) or len(set(keys)) != len(keys):
            raise ValueError("mỗi chi tiết phải có một vị trí riêng")
        self.root, self.machine, self.source = root, machine, Path(source)
        self.folder = ban_ve.machine_folder(machine, root)
        self.parts, self.info, self.sha256 = parts, info, sha256
        self.labels = dict(zip(keys, part_labels(parts)))
        self.zones_fixed = machine == BENCH
        self.name = name
        self.origin = origin                   # program that wrote the STEP file
        self.lock = threading.Lock()
        self._shapes: dict[str, bytes] = {}

    @property
    def catalog_path(self) -> Path:
        return self.folder / "danh-muc.json"

    @property
    def mapping_path(self) -> Path:
        return self.folder / "anh-xa.json"

    # -- what is on disk --------------------------------------------------

    def catalog(self) -> dict:
        if self.zones_fixed:
            return common.load_json(self.root / "mo-hinh" / "ban-thu.json")
        if self.catalog_path.exists():
            return common.load_json(self.catalog_path)
        return {"phien_ban": 1, "ma": self.machine, "ten": self.name or self.machine, "vung": []}

    def mapping(self) -> tuple[dict, str | None]:
        """The zone file, and a warning when it cannot be used for this drawing."""
        empty = {"phien_ban": 2, "chi_tiet": {}, "ten": {}}
        if not self.mapping_path.exists():
            return empty, None
        data = common.load_json(self.mapping_path)
        if data.get("phien_ban") != 2:      # a version 1 file: decisions by name only
            data = {"phien_ban": 2, "chi_tiet": {}, "ten": data}
        if data.get("chi_tiet") and data.get("sha256") not in (None, self.sha256):
            data = {k: v for k, v in data.items() if k != "truc_dung"}
            return dict(data, chi_tiet={}), (
                "Tệp gán vùng đang có được làm cho một tệp STEP khác (mã sha256 khác): vị trí "
                "chi tiết không còn đúng nên không dùng. Lưu lần đầu sẽ chuyển tệp cũ vào thư mục cu/.")
        return data, None

    # -- what the page shows ------------------------------------------------

    def shapes(self, up: str = "Z") -> bytes:
        """Every part's mesh with ``up`` as the vertical axis, gzip-compressed
        once per axis (the drawing does not change)."""
        if up not in step.UP_AXES:
            raise Refused(f"Trục đứng phải là {' hoặc '.join(step.UP_AXES)}.")
        if up not in self._shapes:
            items = []
            for part in self.parts:
                mesh = step.mesh_part(part, up) if part.triangles else None
                if mesh:
                    mesh.pop("nguon", None)
                    mesh.pop("khoa", None)
                items.append({"khoa": part.key, "nhan": self.labels[part.key],
                              "duong_dan": " / ".join(n for n in part.names if n and n.strip()),
                              "tam_giac": len(part.triangles), "luoi": mesh})
            body = {"tep": {"ten": self.source.name, "sha256": self.sha256, **self.info},
                    "truc_dung": up, "chi_tiet": items}
            raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._shapes[up] = gzip.compress(raw, 5)
        return self._shapes[up]

    def status(self) -> dict:
        catalog = self.catalog()
        mapping, warning = self.mapping()
        try:
            zmap = step.ZoneMap.of(mapping)
        except step.StepError as exc:
            warning = f"Tệp gán vùng có chỗ sai, chưa dùng được: {exc}"
            zmap = step.ZoneMap()
        by_name = step.ZoneMap(by_name=zmap.by_name)
        from_names = {}
        for part in self.parts:
            if part.key not in zmap.by_key:
                found = _decision(step.classify(part.names, by_name), step.lamp_of(part.names, by_name))
                if found:
                    from_names[part.key] = found
        return {
            "may": {"ma": self.machine, "ten": catalog.get("ten", self.machine),
                    "chi_doc_vung": self.zones_fixed},
            "vung": [{"id": z["id"], "ten": z.get("ten", z["id"]), "mo_ta": z.get("mo_ta", ""),
                      "co_den": any(p.get("den") for p in z.get("khoi", []))}
                     for z in catalog.get("vung", [])],
            "gan": {k: {x: y for x, y in v.items() if x != "nhan"} for k, v in zmap.by_key.items()},
            "theo_ten": from_names,
            "kiem_tra": self.check(catalog, zmap),
            "canh_bao": warning,
            "truc_dung": zmap.up or "Z",
            "truc_da_luu": zmap.up is not None,
            "phan_mem": self.origin,
            "goi_y_truc": step.suggested_up(self.origin),
            "thu_muc": self.folder.relative_to(self.root).as_posix(),
            "da_luu": self.mapping_path.exists(),
        }

    def check(self, catalog: dict, zmap: step.ZoneMap) -> dict:
        """What importing would say, without building the model."""
        ids = [z["id"] for z in catalog.get("vung", [])]
        counts = {zid: 0 for zid in ids}
        frame = skipped = unplaced = no_mesh = triangles = 0
        unknown = set()
        for part in self.parts:
            if not part.triangles:
                no_mesh += 1
                continue
            where = step.classify(part.names, zmap, part.key)
            if where is None:
                unplaced += 1
            elif where[0] == "skip":
                skipped += 1
            else:
                triangles += len(part.triangles)
                if where[0] == "frame":
                    frame += 1
                elif where[1] in counts:
                    counts[where[1]] += 1
                else:
                    unknown.add(where[1])
        problems = []
        if not ids:
            problems.append("Chưa có vùng nào: thêm vùng rồi gán chi tiết vào.")
        empty = [zid for zid in ids if counts[zid] == 0]
        if empty:
            problems.append("Vùng chưa có chi tiết nào: " + ", ".join(empty) + ".")
        if unknown:
            problems.append("Chi tiết mang tên vùng không có trong danh mục: "
                            + ", ".join(sorted(unknown)) + ".")
        if triangles > step.MAX_TRIANGLES:
            problems.append(f"Mô hình sẽ có {triangles} tam giác, quá {step.MAX_TRIANGLES}: "
                            "mở lại công cụ với --do-min lớn hơn, hoặc bỏ qua chi tiết nhỏ.")
        return {"dat": not problems, "van_de": problems, "theo_vung": counts, "khung": frame,
                "bo_qua": skipped, "chua_gan": unplaced, "khong_luoi": no_mesh,
                "tam_giac": triangles}

    # -- writes -------------------------------------------------------------

    def _new_catalog(self, old: dict, zones, name) -> dict:
        if not isinstance(zones, list) or len(zones) > MAX_ZONES:
            raise Refused(f"Danh sách vùng phải là một danh sách, tối đa {MAX_ZONES} vùng.")
        before = {z["id"]: z for z in old.get("vung", [])}
        out, seen = [], set()
        for zone in zones:
            if not isinstance(zone, dict):
                raise Refused("Mỗi vùng phải có mã và tên.")
            zid = str(zone.get("id", "")).strip()
            if not step.ZONE_ID.match(zid):
                raise Refused(f"Mã vùng {zid!r} không hợp lệ: chữ thường không dấu, số và _, "
                              "tối đa 40 ký tự.")
            if zid in seen:
                raise Refused(f"Mã vùng {zid} bị trùng.")
            seen.add(zid)
            title = str(zone.get("ten", "")).strip()
            note = str(zone.get("mo_ta") or "").strip()
            if not title or len(title) > 80:
                raise Refused(f"Vùng {zid}: tên phải có, tối đa 80 ký tự.")
            if len(note) > 400:
                raise Refused(f"Vùng {zid}: mô tả tối đa 400 ký tự.")
            entry = dict(before.get(zid, {}))     # keeps what the page does not edit
            entry.update(id=zid, ten=title, mo_ta=note)
            out.append(entry)
        title = str(name or old.get("ten") or self.machine).strip()[:120]
        return {"phien_ban": 1, "ma": self.machine, "ten": title,
                "ghi_chu": old.get("ghi_chu") or ("Danh mục vùng làm bằng công cụ khoanh vùng. "
                                                  "Chưa gắn cảm biến nào cho máy này."),
                "vung": out}

    def save(self, body) -> dict:
        if not isinstance(body, dict):
            raise Refused("Nội dung gửi lên không đúng dạng.")
        with self.lock:
            catalog = self.catalog()
            if not self.zones_fixed:
                catalog = self._new_catalog(catalog, body.get("vung"), body.get("ten_may"))
            ids = {z["id"] for z in catalog["vung"]}
            up = body.get("truc_dung", "Z")
            if up not in step.UP_AXES:
                raise Refused(f"Trục đứng phải là {' hoặc '.join(step.UP_AXES)}.")
            decisions = body.get("gan")
            if not isinstance(decisions, dict):
                raise Refused("Phần gán chi tiết không đúng dạng.")
            chosen = {}
            for key, value in decisions.items():
                if key not in self.labels:
                    raise Refused(f"Bản vẽ không có chi tiết ở vị trí {key}.")
                if value is None:
                    continue
                try:
                    decision = step.target(value, f"chi tiết {self.labels[key]}")
                except step.StepError as exc:
                    raise Refused(str(exc)) from None
                if "vung" in decision and decision["vung"] not in ids:
                    raise Refused(f"Chi tiết {self.labels[key]}: vùng {decision['vung']} "
                                  "không có trong danh mục.")
                decision["nhan"] = self.labels[key]      # for people reading the file
                chosen[key] = decision
            old, _warning = self.mapping()
            mapping = {"phien_ban": 2, "tep": self.source.name, "sha256": self.sha256,
                       "truc_dung": up,
                       "ghi_chu": ("Làm bằng công cụ khoanh vùng. Vị trí chi tiết (vd 1/3/2) và "
                                   "trục đứng chỉ đúng với tệp STEP có mã sha256 này."),
                       "chi_tiet": {k: chosen[k] for k in sorted(chosen, key=_key_order)},
                       "ten": old.get("ten", {})}
            self.folder.mkdir(parents=True, exist_ok=True)
            if not self.zones_fixed:
                _write_keeping_old(self.catalog_path, catalog)
            _write_keeping_old(self.mapping_path, mapping)
            log.info("saved %d decisions, %d zones", len(chosen), len(ids))
        return {"luu_luc": time.strftime("%H:%M:%S"), **self.status()}

    def import_drawing(self, body) -> dict:
        if not isinstance(body, dict):
            raise Refused("Nội dung gửi lên không đúng dạng.")
        number = str(body.get("so_ban_ve", "")).strip()
        revision = str(body.get("revision", "")).strip()
        rights = str(body.get("quyen", "")).strip()
        if not NUMBER.match(number):
            raise Refused("Số bản vẽ: chữ không dấu, số, . _ -, tối đa 40 ký tự.")
        if not REVISION.match(revision):
            raise Refused("Revision: chữ không dấu hoặc số, tối đa 10 ký tự (vd A).")
        if not rights or len(rights) > 300:
            raise Refused("Ghi quyền dùng bản vẽ (nguồn, được dùng vào việc gì), tối đa 300 ký tự.")
        with self.lock:
            if not self.mapping_path.exists():
                raise Refused("Chưa lưu khoanh vùng: bấm Lưu trước.")
            state = self.status()
            if state["canh_bao"]:
                raise Refused(state["canh_bao"])
            if not state["kiem_tra"]["dat"]:
                raise Refused("Chưa nhập được: " + " ".join(state["kiem_tra"]["van_de"]))
            catalog = None if self.zones_fixed else common.load_json(self.catalog_path)
            source = None if self.zones_fixed else self.catalog_path.relative_to(self.root).as_posix()
            try:
                record, _report, existed = ban_ve.import_drawing(
                    self.source, person=getpass.getuser(), rights=rights, root=self.root,
                    catalog=catalog, catalog_source=source, number=number, revision=revision,
                    mapping=self.mapping_path, deflection=self.info.get("do_min_mm"))
            except SystemExit as exc:
                raise Refused(str(exc)) from None
            except DxfError as exc:
                raise Refused(f"Không dựng được mô hình: {exc}") from None
        report = record.get("bao_cao", {})
        return {"id": record["id"], "co_san": existed, "muc": record["muc"],
                "mo_hinh": record["mo_hinh"], "so_khoi": report.get("so_khoi", 0),
                "bo_qua": report.get("bo_qua", [])}


def make_handler(session: Session, web_dir: Path, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    web_root = web_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Paotwin-khoanh-vung"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, kind: str, gzipped: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Not inside another page: a framed copy could be clicked through.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _refuse(self, status: int, why: str) -> None:
            self.close_connection = True
            self._json(status, {"loi": why})

        def do_GET(self) -> None:
            if self.headers.get("Host", "") not in hosts:
                self._refuse(HTTPStatus.FORBIDDEN, "Chỉ mở bằng địa chỉ 127.0.0.1 của máy này.")
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/hinh":
                up = (parse_qs(self.path.partition("?")[2]).get("truc_dung") or ["Z"])[0]
                try:
                    body = session.shapes(up)
                except Refused as exc:
                    self._refuse(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                if "gzip" in self.headers.get("Accept-Encoding", ""):
                    self._send(HTTPStatus.OK, body, "application/json; charset=utf-8", gzipped=True)
                else:
                    self._send(HTTPStatus.OK, gzip.decompress(body), "application/json; charset=utf-8")
            elif path == "/api/trang-thai":
                self._json(HTTPStatus.OK, session.status())
            else:
                name = PAGE if path in ("", "/") else path.lstrip("/")
                if name not in FILES:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                    return
                self._send(HTTPStatus.OK, (web_root / name).read_bytes(), FILES[name])

        def do_POST(self) -> None:
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Nội dung gửi lên quá lớn.")
                return
            # Read the body before any answer, refusals included: a socket closed
            # with unread data is reset on Windows, and the sender then gets
            # "connection aborted" instead of the answer (seen on the laptop).
            raw = self.rfile.read(length)
            if self.headers.get("Host", "") not in hosts:
                self._refuse(HTTPStatus.FORBIDDEN, "Chỉ mở bằng địa chỉ 127.0.0.1 của máy này.")
                return
            if self.headers.get("Origin") not in origins:
                self._refuse(HTTPStatus.FORBIDDEN, "Chỉ nhận lệnh từ chính trang công cụ khoanh vùng.")
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Cần gửi JSON.")
                return
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._refuse(HTTPStatus.BAD_REQUEST, "Nội dung gửi lên không phải JSON.")
                return
            path = self.path.split("?", 1)[0]
            action = {"/api/luu": session.save, "/api/nhap": session.import_drawing}.get(path)
            if action is None:
                self._refuse(HTTPStatus.NOT_FOUND, "Không có lệnh này.")
                return
            try:
                self._json(HTTPStatus.OK, action(data))
            except Refused as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"loi": str(exc)})
            except Exception:  # noqa: BLE001 - say so on the page, details in the console
                log.exception("request failed")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"loi": "Lỗi không lường trước; xem cửa sổ đang chạy công cụ."})

        def _not_allowed(self) -> None:
            self.close_connection = True
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        do_PUT = do_DELETE = do_PATCH = _not_allowed

    return Handler


def serve(session: Session, port: int, web_dir: Path | None = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                make_handler(session, web_dir or common.WEB_DIR, port))
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m paotwin.khoanh_vung",
                                     description="Khoanh vùng chi tiết của một bản vẽ STEP")
    parser.add_argument("tep", help="tệp .step / .stp")
    parser.add_argument("--may", required=True, help="mã máy, vd bts7960 (ban-thu: bàn thử)")
    parser.add_argument("--ten", help="tên máy, khi khoanh vùng lần đầu")
    parser.add_argument("--do-min", type=float, help="độ mịn chia tam giác, mm")
    parser.add_argument("--cong", type=int, default=DEFAULT_PORT, help="cổng trang web (mặc định 8793)")
    parser.add_argument("--khong-mo-trinh-duyet", action="store_true")
    args = parser.parse_args(argv)
    common.setup_logging("khoanh")

    source = Path(args.tep)
    if not source.is_file():
        print(f"Không thấy tệp {source}.")
        return 2
    if source.suffix.lower() not in ban_ve.STEP_SUFFIXES:
        print("Công cụ khoanh vùng đọc tệp .step / .stp.")
        return 2
    try:
        folder = ban_ve.machine_folder(args.may)
    except SystemExit as exc:
        print(exc)
        return 2
    if not step.occ_available():
        print(f"Chưa cài {step.PACKAGE} để đọc STEP: bấm đúp CAI-DAT-STEP.cmd.")
        return 2
    with socket.socket() as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", args.cong)) == 0:
            print(f"Cổng {args.cong} đang có chương trình khác dùng (có thể công cụ đang mở ở "
                  "cửa sổ khác). Đóng nó rồi chạy lại, hoặc dùng --cong khác.")
            return 2

    print(f"Đang đọc bản vẽ {source.name} (tệp lớn có thể mất vài phút)...", flush=True)
    started = time.monotonic()
    try:
        parts, info = step.occ_parts(source, args.do_min)
    except step.StepError as exc:
        print(f"Không đọc được bản vẽ: {exc}")
        return 1
    triangles = sum(len(p.triangles) for p in parts)
    print(f"Đọc xong: {len(parts)} chi tiết, {triangles} tam giác, kích thước bao "
          f"{info['kich_thuoc_mm']} mm, độ mịn {info['do_min_mm']} mm "
          f"({time.monotonic() - started:.0f} s).")
    origin = step.origin_of(source)
    if origin:
        hint = step.suggested_up(origin)
        print(f"Phần mềm xuất tệp: {origin}"
              + (f" (thường lấy {hint} làm trục đứng; đổi trên trang nếu hình nằm sai)." if hint else "."))
    session = Session(common.ROOT, args.may, source, parts, info, ban_ve._sha256(source), args.ten,
                      origin)
    httpd = serve(session, args.cong)
    url = f"http://127.0.0.1:{args.cong}/"
    print()
    print(f"  Công cụ khoanh vùng: {url}")
    print(f"  Lưu vào: {folder}")
    print("  Đóng cửa sổ này (hoặc Ctrl+C) để dừng.")
    print()
    if not args.khong_mo_trinh_duyet:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
