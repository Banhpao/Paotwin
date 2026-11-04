import gzip
import http.client
import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from paotwin import ban_ve, common, khoanh_vung, step

from tests.test_step import cube


def part(names, key, **kw):
    p = cube(names, **kw)
    p.key = key
    return p


def board():
    """A small module like the BTS7960: one board, two identical ICs, screws."""
    top = "MODULE"
    return [part([top, "NAUO1", "PCB"], "1/1", size=60),
            part([top, "NAUO2", "HEATSINK"], "1/2", z=10),
            part([top, "NAUO4", "IC"], "1/3", x=5),
            part([top, "NAUO5", "IC"], "1/4", x=30),
            part([top, "NAUO6", "Screw"], "1/5"),
            part([top, "NAUO7", "Screw"], "1/6")]


ZONES = [{"id": "ic_trai", "ten": "IC trái"}, {"id": "ic_phai", "ten": "IC phải"},
         {"id": "tan_nhiet", "ten": "Tản nhiệt"}]
CHOICES = {"1/1": {"khung": "nen"}, "1/2": {"vung": "tan_nhiet"}, "1/3": {"vung": "ic_trai"},
           "1/4": {"vung": "ic_phai"}, "1/5": {"bo_qua": True}, "1/6": {"bo_qua": True}}


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "mo-hinh").mkdir()
        shutil.copy(common.ROOT / "mo-hinh" / "ban-thu.json", self.root / "mo-hinh")
        self.source = self.root / "module.step"
        self.source.write_bytes(b"ISO-10303-21; not read in these tests")
        self.session = khoanh_vung.Session(self.root, "module", self.source, board(),
                                           {"kich_thuoc_mm": [60, 60, 20], "do_min_mm": 0.1},
                                           "abc123", "Module thử")

    def test_identical_parts_are_numbered(self):
        self.assertEqual(khoanh_vung.part_labels(board()),
                         ["PCB", "HEATSINK", "IC #1", "IC #2", "Screw #1", "Screw #2"])

    def test_starts_empty_and_says_what_is_missing(self):
        status = self.session.status()
        self.assertEqual(status["vung"], [])
        self.assertFalse(status["kiem_tra"]["dat"])
        self.assertEqual(status["kiem_tra"]["chua_gan"], 6)
        self.assertEqual(status["thu_muc"], "ban-ve/may/module")

    def test_save_writes_the_zone_list_and_zone_file(self):
        status = self.session.save({"ten_may": "Module thử", "vung": ZONES, "gan": CHOICES})
        self.assertTrue(status["kiem_tra"]["dat"], status["kiem_tra"])
        self.assertEqual(status["kiem_tra"]["theo_vung"], {"ic_trai": 1, "ic_phai": 1, "tan_nhiet": 1})
        catalog = common.load_json(self.session.catalog_path)
        self.assertEqual([z["id"] for z in catalog["vung"]], ["ic_trai", "ic_phai", "tan_nhiet"])
        mapping = common.load_json(self.session.mapping_path)
        self.assertEqual(mapping["sha256"], "abc123")
        self.assertEqual(mapping["chi_tiet"]["1/3"], {"vung": "ic_trai", "nhan": "IC #1"})
        self.assertEqual(list(mapping["chi_tiet"]), ["1/1", "1/2", "1/3", "1/4", "1/5", "1/6"])
        # The file the tool writes is one import accepts.
        model, report = step.assemble(board(), catalog, {"sha256": "abc123", "muc": "candidate",
                                                         "id": "x", "so_ban_ve": "x", "revision": "A",
                                                         "tep_nguon": "x"}, mapping)
        self.assertEqual(report.ignored, {"MODULE / NAUO6 / Screw": 1, "MODULE / NAUO7 / Screw": 1})
        self.assertEqual([f["loai"] for f in model["khung"]], ["nen"])

    def test_each_save_keeps_the_copy_it_replaces(self):
        self.session.save({"vung": ZONES, "gan": CHOICES})
        self.session.save({"vung": ZONES, "gan": CHOICES})          # same content: no copy
        old = self.session.folder / "cu"
        self.assertFalse(old.exists())
        self.session.save({"vung": ZONES, "gan": dict(CHOICES, **{"1/5": None})})
        self.assertEqual(sorted(p.name.split("-")[0] for p in old.iterdir()), ["anh"])
        # What the zone file's other part says (decisions by name) survives a save.
        data = common.load_json(self.session.mapping_path)
        data["ten"] = {"Screw": {"bo_qua": True}}
        self.session.mapping_path.write_text(json.dumps(data), encoding="utf-8")
        self.session.save({"vung": ZONES, "gan": CHOICES})
        self.assertEqual(common.load_json(self.session.mapping_path)["ten"], {"Screw": {"bo_qua": True}})

    def test_what_is_refused(self):
        cases = [
            ({"vung": ZONES, "gan": {"9/9": {"bo_qua": True}}}, "9/9"),
            ({"vung": ZONES, "gan": {"1/3": {"vung": "khong_co"}}}, "khong_co"),
            ({"vung": ZONES, "gan": {"1/3": {"vung": "ic_trai", "den": "tim"}}}, "đèn"),
            ({"vung": [{"id": "IC", "ten": "x"}], "gan": {}}, "Mã vùng"),
            ({"vung": [{"id": "a", "ten": "x"}, {"id": "a", "ten": "y"}], "gan": {}}, "trùng"),
            ({"vung": [{"id": "a", "ten": " "}], "gan": {}}, "tên"),
            ({"vung": "x", "gan": {}}, "danh sách"),
            ({"vung": ZONES, "gan": []}, "gán"),
            ([], "dạng"),
        ]
        for body, words in cases:
            with self.subTest(body=body), self.assertRaisesRegex(khoanh_vung.Refused, words):
                self.session.save(body)
        self.assertFalse(self.session.folder.exists())

    def test_the_bench_keeps_its_zones(self):
        bench = khoanh_vung.Session(self.root, "ban-thu", self.source, board(), {}, "abc123")
        status = bench.status()
        self.assertTrue(status["may"]["chi_doc_vung"])
        self.assertIn("den_thap", [z["id"] for z in status["vung"]])
        self.assertTrue(next(z for z in status["vung"] if z["id"] == "den_thap")["co_den"])
        bench.save({"vung": [{"id": "khac", "ten": "Khác"}], "gan": {"1/3": {"vung": "gpu"}}})
        self.assertFalse(bench.catalog_path.exists())
        self.assertEqual(common.load_json(bench.mapping_path)["chi_tiet"]["1/3"]["vung"], "gpu")

    def test_a_zone_file_of_another_drawing_is_not_applied(self):
        self.session.save({"vung": ZONES, "gan": CHOICES})
        other = khoanh_vung.Session(self.root, "module", self.source, board(), {}, "khac")
        status = other.status()
        self.assertIn("sha256", status["canh_bao"])
        self.assertEqual(status["gan"], {})

    def test_names_already_in_the_drawing_are_shown(self):
        parts = board() + [part(["MODULE", "VUNG-TAN_NHIET-2"], "1/7")]
        session = khoanh_vung.Session(self.root, "module", self.source, parts, {}, "abc123")
        self.assertEqual(session.status()["theo_ten"], {"1/7": {"vung": "tan_nhiet"}})

    def test_the_vertical_axis_is_saved_with_the_zone_file(self):
        status = self.session.status()
        self.assertEqual((status["truc_dung"], status["truc_da_luu"]), ("Z", False))
        self.session.save({"vung": ZONES, "gan": CHOICES, "truc_dung": "Y"})
        self.assertEqual(common.load_json(self.session.mapping_path)["truc_dung"], "Y")
        self.assertEqual(self.session.status()["truc_dung"], "Y")
        with self.assertRaisesRegex(khoanh_vung.Refused, "Trục đứng"):
            self.session.save({"vung": ZONES, "gan": CHOICES, "truc_dung": "X"})
        # Another file: neither its positions nor its axis apply.
        other = khoanh_vung.Session(self.root, "module", self.source, board(), {}, "khac")
        self.assertEqual(other.status()["truc_dung"], "Z")

    def test_the_program_that_wrote_the_file_suggests_the_axis(self):
        session = khoanh_vung.Session(self.root, "module", self.source, board(), {}, "abc123",
                                      origin="SolidWorks 2017")
        self.assertEqual(session.status()["goi_y_truc"], "Y")
        self.assertIsNone(self.session.status()["goi_y_truc"])

    def test_parts_need_their_own_position(self):
        parts = board()
        parts[1].key = "1/1"
        with self.assertRaises(ValueError):
            khoanh_vung.Session(self.root, "module", self.source, parts, {}, "abc123")

    def test_import_into_the_register(self):
        def build(path, catalog, record, mapping=None, deflection=None, up=None):
            model, report = step.assemble(board(), catalog, record, mapping, up)
            return model, report, {"kich_thuoc_mm": [60, 60, 20], "do_min_mm": deflection,
                                   "truc_dung": model["ban_ve"]["truc_dung"]}

        with self.assertRaisesRegex(khoanh_vung.Refused, "Lưu"):
            self.session.import_drawing({"so_ban_ve": "M-1", "revision": "A", "quyen": "thử"})
        self.session.save({"vung": ZONES, "gan": CHOICES})
        for bad, words in (({"so_ban_ve": "M 1", "revision": "A", "quyen": "x"}, "Số bản vẽ"),
                           ({"so_ban_ve": "M-1", "revision": "", "quyen": "x"}, "Revision"),
                           ({"so_ban_ve": "M-1", "revision": "A", "quyen": ""}, "quyền")):
            with self.subTest(body=bad), self.assertRaisesRegex(khoanh_vung.Refused, words):
                self.session.import_drawing(bad)
        sha = ban_ve._sha256(self.source)
        self.session.sha256 = sha        # the real digest, as main() gives it
        self.session.save({"vung": ZONES, "gan": CHOICES})
        with mock.patch.object(step, "occ_available", return_value=True), \
                mock.patch.object(step, "build_model", side_effect=build):
            result = self.session.import_drawing({"so_ban_ve": "M-1", "revision": "A",
                                                  "quyen": "Tệp thử, chỉ dùng nội bộ"})
        self.assertEqual((result["id"], result["muc"], result["so_khoi"]), ("M-1-A", "candidate", 4))
        # The same file and the same zoning again: the set that is already there.
        with mock.patch.object(step, "occ_available", return_value=True), \
                mock.patch.object(step, "build_model", side_effect=build):
            again = self.session.import_drawing({"so_ban_ve": "M-1", "revision": "A", "quyen": "x"})
            self.assertTrue(again["co_san"])
            # Built another way (here: Y up): not handed the old model, asked for a new revision.
            self.session.save({"vung": ZONES, "gan": CHOICES, "truc_dung": "Y"})
            with self.assertRaisesRegex(khoanh_vung.Refused, "A2"):
                self.session.import_drawing({"so_ban_ve": "M-1", "revision": "A", "quyen": "x"})
            second = self.session.import_drawing({"so_ban_ve": "M-1", "revision": "A2", "quyen": "x"})
        self.assertEqual(ban_ve.load(self.root)["bo"][1]["truc_dung"], "Y")
        self.assertEqual(second["id"], "M-1-A2")
        record = ban_ve.load(self.root)["bo"][0]
        self.assertEqual(record["danh_muc"], "ban-ve/may/module/danh-muc.json")
        self.assertEqual(record["anh_xa"]["chi_tiet"]["1/4"]["vung"], "ic_phai")
        self.assertTrue((self.root / "mo-hinh" / "nhap" / "M-1-A.json").exists())
        # Another machine's drawing is not for the bench twin.
        with self.assertRaisesRegex(SystemExit, "tín hiệu"):
            ban_ve.use("M-1-A", person="An", root=self.root)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTest(unittest.TestCase):
    """The page and its two writes, over HTTP, with the checks on Host and Origin."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "mo-hinh").mkdir()
        shutil.copy(common.ROOT / "mo-hinh" / "ban-thu.json", self.root / "mo-hinh")
        source = self.root / "module.step"
        source.write_bytes(b"x")
        self.session = khoanh_vung.Session(self.root, "module", source, board(), {}, "abc123")
        self.port = free_port()
        self.httpd = khoanh_vung.serve(self.session, self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def post(self, path, data, **headers):
        base = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        base.update(headers)
        return self.request("POST", path, json.dumps(data).encode("utf-8"),
                            {k: v for k, v in base.items() if v is not None})

    def test_page_and_shapes(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"khoanh-vung.js", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        status, headers, body = self.request("GET", "/api/hinh", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        shapes = json.loads(gzip.decompress(body))
        self.assertEqual([p["khoa"] for p in shapes["chi_tiet"]], ["1/1", "1/2", "1/3", "1/4", "1/5", "1/6"])
        self.assertEqual(shapes["chi_tiet"][2]["nhan"], "IC #1")
        self.assertEqual(shapes["chi_tiet"][2]["luoi"]["hinh"], "luoi")
        status, _, body = self.request("GET", "/api/hinh")
        self.assertEqual(json.loads(body)["tep"]["sha256"], "abc123")
        # The same parts with Y up: CAD (x, y, z) is shown as is.
        z_up = json.loads(body)["chi_tiet"][0]["luoi"]["hop_bao"]
        status, _, body = self.request("GET", "/api/hinh?truc_dung=Y")
        shapes = json.loads(body)
        self.assertEqual(shapes["truc_dung"], "Y")
        self.assertNotEqual(shapes["chi_tiet"][0]["luoi"]["hop_bao"], z_up)
        self.assertEqual(shapes["chi_tiet"][0]["luoi"]["hop_bao"], [0.0, 0.0, 0.0, 60.0, 60.0, 60.0])
        self.assertEqual(self.request("GET", "/api/hinh?truc_dung=X")[0], 400)

    def test_only_the_tools_own_files(self):
        for path in ("/app.js", "/../paotwin/khoanh_vung.py", "/index.html", "/%2e%2e/README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_another_host_name_is_refused(self):
        # A name pointed at 127.0.0.1 by someone else's page (DNS rebinding).
        status, _, _ = self.request("GET", "/api/hinh", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_writes_only_from_the_tools_page(self):
        data = {"vung": ZONES, "gan": CHOICES}
        self.assertEqual(self.post("/api/luu", data, Origin=None)[0], 403)
        self.assertEqual(self.post("/api/luu", data, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/luu", data, **{"Content-Type": "text/plain"})[0], 415)
        self.assertFalse(self.session.folder.exists())
        status, _, body = self.post("/api/luu", data)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["kiem_tra"]["dat"])
        self.assertTrue(self.session.mapping_path.exists())

    def test_refusals_come_back_as_words(self):
        status, _, body = self.post("/api/luu", {"vung": ZONES, "gan": {"1/3": {"vung": "khong_co"}}})
        self.assertEqual(status, 400)
        self.assertIn("khong_co", json.loads(body)["loi"])
        status, _, body = self.request("POST", "/api/luu", b"{not json",
                                       {"Content-Type": "application/json",
                                        "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 400)
        self.assertEqual(self.post("/api/khac", {})[0], 404)
        self.assertEqual(self.request("DELETE", "/api/luu")[0], 405)


if __name__ == "__main__":
    unittest.main()
