import json
import shutil
import tempfile
import unittest
from pathlib import Path

from paotwin import ban_ve, common, dxf
from paotwin.engine import Engine

SAMPLE = common.ROOT / "ban-ve" / "mau" / "BT-001-A.dxf"


def dxf_text(*entities: str) -> str:
    body = "".join(entities)
    return f"  0\nSECTION\n  2\nENTITIES\n{body}  0\nENDSEC\n  0\nEOF\n"


def lw(layer, points, elevation=0.0, thickness=10.0, closed=True, bulge=None, normal=None):
    out = f"  0\nLWPOLYLINE\n  8\n{layer}\n 90\n{len(points)}\n 70\n{1 if closed else 0}\n"
    out += f" 38\n{elevation}\n 39\n{thickness}\n"
    for i, (x, y) in enumerate(points):
        out += f" 10\n{x}\n 20\n{y}\n"
        if bulge is not None and i == 0:
            out += f" 42\n{bulge}\n"
    if normal:
        out += f"210\n{normal[0]}\n220\n{normal[1]}\n230\n{normal[2]}\n"
    return out


def title(number="X-1", rev="A"):
    return (f"  0\nTEXT\n  8\nKHUNG-TEN\n 10\n0\n 20\n0\n 40\n5\n  1\nSO BAN VE: {number}\n"
            f"  0\nTEXT\n  8\nKHUNG-TEN\n 10\n0\n 20\n0\n 40\n5\n  1\nREV: {rev}\n")


class ReaderTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        path = folder / "t.dxf"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_sample_drawing_reads_completely(self):
        head, zones, frame, report = dxf.convert(SAMPLE)
        self.assertEqual(head, {"so_ban_ve": "BT-001", "revision": "A",
                                "ten": "Bàn thử laptop và thiết bị ảo"})
        catalog = common.load_json(common.ROOT / "mo-hinh" / "ban-thu.json")
        self.assertEqual(set(zones), {z["id"] for z in catalog["vung"]})
        self.assertEqual(report.skipped, [])
        self.assertEqual(report.other_layers, {"KICH-THUOC": 1})

    def test_elevation_and_thickness_make_a_prism(self):
        path = self.write(dxf_text(lw("VUNG-GPU", [(0, 0), (40, 0), (40, 20), (0, 20)], 5, 6)))
        _, zones, _, report = dxf.convert(path)
        part = zones["gpu"][0]
        self.assertEqual((part["hinh"], part["y0"], part["cao"]), ("lang_tru", 5.0, 6.0))
        # Plan Y away from the viewer becomes -z in the twin.
        self.assertEqual(sorted(z for _, z in part["day"]), [-20.0, -20.0, 0.0, 0.0])
        self.assertEqual(report.shapes, 1)

    def test_what_cannot_be_placed_is_reported_not_guessed(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        path = self.write(dxf_text(
            lw("VUNG-A", square, thickness=0),
            lw("VUNG-A", square, closed=False),
            lw("VUNG-A", square, bulge=0.5),
            lw("VUNG-A", square, normal=(0, 1, 0)),
            "  0\nINSERT\n  8\nVUNG-A\n  2\nKHOI\n 10\n0\n 20\n0\n",
            lw("LOP-KHAC", square)))
        _, zones, _, report = dxf.convert(path)
        self.assertEqual(zones, {})
        reasons = " | ".join(report.skipped)
        for words in ("không có độ dày", "không khép kín", "cung tròn", "nghiêng"):
            self.assertIn(words, reasons)
        self.assertEqual(report.unsupported, {"INSERT": 1})
        self.assertEqual(report.other_layers, {"LOP-KHAC": 1})

    def test_not_a_dxf(self):
        path = self.write("hello\nworld\n")
        with self.assertRaises(dxf.DxfError):
            dxf.convert(path)

    def test_zones_must_match_the_catalog(self):
        catalog = {"vung": [{"id": "gpu", "ten": "GPU", "khoi": []}]}
        record = {"id": "x", "so_ban_ve": "x", "revision": "A", "tep_nguon": "x",
                  "sha256": "0", "muc": "candidate"}
        extra = self.write(dxf_text(lw("VUNG-GPU", [(0, 0), (1, 0), (1, 1)]),
                                    lw("VUNG-LA", [(0, 0), (1, 0), (1, 1)])))
        with self.assertRaisesRegex(dxf.DxfError, "VUNG-LA"):
            dxf.build_model(extra, catalog, record)
        missing = self.write(dxf_text(lw("KHUNG-VO", [(0, 0), (1, 0), (1, 1)])))
        with self.assertRaisesRegex(dxf.DxfError, "VUNG-GPU"):
            dxf.build_model(missing, catalog, record)


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "mo-hinh").mkdir()
        shutil.copy(common.ROOT / "mo-hinh" / "ban-thu.json", self.root / "mo-hinh")

    def test_import_verify_use_and_history(self):
        record, report, existed = ban_ve.import_drawing(SAMPLE, person="An", rights="Được dùng nội bộ",
                                                        root=self.root)
        self.assertFalse(existed)
        self.assertEqual((record["id"], record["muc"]), ("BT-001-A", "candidate"))
        self.assertTrue((self.root / "ban-ve" / "tep" / "BT-001-A.dxf").exists())
        model = common.load_json(self.root / record["mo_hinh"])
        self.assertEqual(model["trang_thai_ban_ve"], "candidate")
        # The twin's rules work on the drawn model as on the hand-made one.
        engine = Engine(common.signals_config(), model)
        self.assertEqual(len(engine.evaluate(0)[1]), len(model["vung"]))
        lamps = sorted(p["den"] for z in model["vung"] if z["id"] == "den_thap" for p in z["khoi"])
        self.assertEqual(lamps, ["do", "vang", "xanh"])
        # Same file again: nothing new.
        self.assertTrue(ban_ve.import_drawing(SAMPLE, person="An", rights="", root=self.root)[2])
        # Reference until chosen.
        self.assertEqual(ban_ve.model_in_use(self.root)["trang_thai_ban_ve"], "tham_khao")
        ban_ve.use("BT-001-A", person="An", root=self.root)
        self.assertEqual(ban_ve.model_in_use(self.root)["ban_ve"]["id"], "BT-001-A")
        with self.assertRaises(SystemExit):
            ban_ve.verify("BT-001-A", person=" ", note="x", root=self.root)
        ban_ve.verify("BT-001-A", person="Bình", note="Đo lại vị trí ổ, quạt", root=self.root)
        shown = ban_ve.model_in_use(self.root)
        self.assertEqual(shown["trang_thai_ban_ve"], "verified")
        self.assertEqual(shown["ban_ve"]["xac_minh"]["nguoi"], "Bình")
        history = [line["viec"] for line in ban_ve.load(self.root)["lich_su"]]
        self.assertEqual(history, ["nhap", "dung", "xac_minh"])
        ban_ve.use(ban_ve.REFERENCE, person="An", root=self.root)
        self.assertEqual(ban_ve.model_in_use(self.root)["trang_thai_ban_ve"], "tham_khao")

    def test_a_changed_drawing_needs_a_new_revision(self):
        ban_ve.import_drawing(SAMPLE, person="An", rights="", root=self.root)
        changed = self.root / "BT-001-A-sua.dxf"
        text = SAMPLE.read_bytes()
        # Change one coordinate but keep the title block (same number, same revision).
        changed.write_bytes(text.replace(b" 10\r\n-178.0\r\n", b" 10\r\n-179.0\r\n", 1))
        self.assertNotEqual(changed.read_bytes(), text)
        with self.assertRaisesRegex(SystemExit, "revision"):
            ban_ve.import_drawing(changed, person="An", rights="", root=self.root)
        # With a new revision it goes in beside the old one, which stays.
        changed.write_bytes(changed.read_bytes().replace(b"REV: A", b"REV: B"))
        record, _, _ = ban_ve.import_drawing(changed, person="An", rights="", root=self.root)
        self.assertEqual(record["id"], "BT-001-B")
        self.assertEqual([r["id"] for r in ban_ve.load(self.root)["bo"]], ["BT-001-A", "BT-001-B"])


if __name__ == "__main__":
    unittest.main()
