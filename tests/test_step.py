import json
import shutil
import tempfile
import unittest
from pathlib import Path

from paotwin import ban_ve, common, step
from paotwin.engine import Engine

CATALOG = common.load_json(common.ROOT / "mo-hinh" / "ban-thu.json")
RECORD = {"id": "S-1-A", "so_ban_ve": "S-1", "revision": "A", "tep_nguon": "x.step",
          "sha256": "0", "muc": "candidate"}


def cube(names, x=0.0, y=0.0, z=0.0, size=10.0):
    """A closed cube in CAD coordinates (Z up), 12 triangles."""
    s = size
    v = [(x + dx * s, y + dy * s, z + dz * s) for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, c, d in faces:
        tris += [(a, b, c), (a, c, d)]
    return step.PlainPart(names, v, tris, [[v[0], v[4]]])


def all_zone_parts():
    parts = []
    for zone in CATALOG["vung"]:
        if zone["id"] == "den_thap":
            for i, lamp in enumerate(("XANH", "VANG", "DO")):
                parts.append(cube(["BAN-THU", "", f"VUNG-DEN_THAP-{lamp}"], z=i * 10))
        else:
            parts.append(cube(["BAN-THU", "", f"VUNG-{zone['id'].upper()}-1"]))
    return parts


class NamingTest(unittest.TestCase):
    def test_names_pick_zone_frame_or_nothing(self):
        self.assertEqual(step.classify(["A", "VUNG-GPU-1"], {}), ("zone", "gpu"))
        self.assertEqual(step.classify(["VUNG-DONG_CO", "Bolt M8"], {}), ("zone", "dong_co"))
        self.assertEqual(step.classify(["KHUNG-VO-tren"], {}), ("frame", "vo"))
        self.assertIsNone(step.classify(["ASSY", "Bolt M8"], {}))
        # A supplier's own name, mapped without touching the drawing.
        self.assertEqual(step.classify(["ASSY", "Motor 3kW"], {"Motor 3kW": "DONG_CO"}),
                         ("zone", "dong_co"))

    def test_lamps(self):
        self.assertEqual(step.lamp_of(["VUNG-DEN_THAP-DO"]), "do")
        self.assertIsNone(step.lamp_of(["VUNG-DEN_THAP-1"]))

    def test_axes(self):
        # CAD Z up, Y away -> twin up = Z, z = -Y.
        self.assertEqual(step.to_twin((1.0, 2.0, 3.0)), [1.0, 3.0, -2.0])


class AssembleTest(unittest.TestCase):
    def test_complete_drawing(self):
        parts = all_zone_parts() + [cube(["BAN-THU", "KHUNG-NEN"], size=100),
                                    cube(["BAN-THU", "Bolt M8"])]
        model, report = step.assemble(parts, CATALOG, RECORD)
        self.assertEqual({z["id"] for z in model["vung"]}, {z["id"] for z in CATALOG["vung"]})
        self.assertEqual(report.other_layers, {"BAN-THU / Bolt M8": 1})
        lamps = sorted(p["den"] for z in model["vung"] if z["id"] == "den_thap" for p in z["khoi"])
        self.assertEqual(lamps, ["do", "vang", "xanh"])
        gpu = next(z for z in model["vung"] if z["id"] == "gpu")["khoi"][0]
        self.assertEqual(gpu["hinh"], "luoi")
        self.assertEqual(len(gpu["dinh"]), 8 * 3)
        self.assertEqual(len(gpu["tam_giac"]), 12 * 3)
        self.assertEqual(gpu["hop_bao"], [0.0, 0.0, -10.0, 10.0, 10.0, 0.0])
        self.assertEqual(model["khung"][0]["loai"], "nen")
        Engine(common.signals_config(), model).evaluate(0)

    def test_missing_zone_names_the_parts_left_over(self):
        parts = [p for p in all_zone_parts() if "GPU" not in p.names[-1]]
        parts.append(cube(["ASSY", "RTX card"]))
        with self.assertRaisesRegex(step.StepError, "VUNG-GPU.*RTX card"):
            step.assemble(parts, CATALOG, RECORD)
        model, _ = step.assemble(parts, CATALOG, RECORD, {"RTX card": "gpu"})
        self.assertTrue(next(z for z in model["vung"] if z["id"] == "gpu")["khoi"])

    def test_unknown_zone_is_refused(self):
        parts = all_zone_parts() + [cube(["VUNG-BOM-1"])]
        with self.assertRaisesRegex(step.StepError, "VUNG-BOM"):
            step.assemble(parts, CATALOG, RECORD)

    def test_too_many_triangles_is_refused_with_advice(self):
        old = step.MAX_TRIANGLES
        step.MAX_TRIANGLES = 50
        try:
            with self.assertRaisesRegex(step.StepError, "--do-min"):
                step.assemble(all_zone_parts(), CATALOG, RECORD)
        finally:
            step.MAX_TRIANGLES = old


@unittest.skipUnless(step.occ_available(), f"chưa cài {step.PACKAGE} (CAI-DAT-STEP.cmd)")
class OpenCascadeTest(unittest.TestCase):
    """Runs on the laptop once CAI-DAT-STEP.cmd has installed the package."""

    def test_every_name_the_reader_uses_exists(self):
        # Lists every missing OpenCASCADE name at once, not one per run.
        self.assertEqual(step.api_report(), [])

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.sample = self.folder / "BT-002.step"
        step.write_sample(self.sample, CATALOG)

    def run(self, result=None):
        # Any error from OpenCASCADE leaves a diagnosis next to the logs.
        outcome = super().run(result)
        if result is not None:
            for test, trace in result.errors + result.failures:
                if test is self:
                    out = common.ROOT / "nhat-ky" / "chan-doan-occ.txt"
                    out.parent.mkdir(exist_ok=True)
                    try:
                        raise RuntimeError(trace.strip().splitlines()[-1])
                    except RuntimeError as exc:
                        text = step.diagnose(exc)
                    out.write_text(trace + "\n\n" + text + "\n", encoding="utf-8")
        return outcome

    def test_write_then_read_back_an_assembly(self):
        parts, info = step.occ_parts(self.sample)
        names = sorted(p.names[-1] for p in parts)
        self.assertIn("VUNG-GPU-1", names)
        self.assertIn("VUNG-DEN_THAP-DO", names)
        self.assertIn("KHUNG-NEN", names)
        model, report = step.assemble(parts, CATALOG, RECORD)
        self.assertEqual(report.skipped, [])
        # The GPU box comes back where the reference model has it (twin axes).
        ref = next(z for z in CATALOG["vung"] if z["id"] == "gpu")["khoi"][0]
        got = next(z for z in model["vung"] if z["id"] == "gpu")["khoi"][0]["hop_bao"]
        (cx, cy, cz), (w, h, d) = ref["tam"], ref["kich_thuoc"]
        expected = [cx - w / 2, cy - h / 2, cz - d / 2, cx + w / 2, cy + h / 2, cz + d / 2]
        for a, b in zip(got, expected):
            self.assertAlmostEqual(a, b, delta=0.05)
        self.assertGreater(info["do_min_mm"], 0)
        # Report for the person running the check on the laptop.
        summary = {"kich_thuoc_mm": info["kich_thuoc_mm"], "do_min_mm": info["do_min_mm"],
                   "so_chi_tiet": len(parts),
                   "tam_giac": sum(len(p.triangles) for p in parts),
                   "hop_bao_gpu": got}
        out = common.ROOT / "nhat-ky" / "tu-kiem-tra-step.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    def test_import_through_the_register(self):
        root = self.folder / "du-an"
        (root / "mo-hinh").mkdir(parents=True)
        shutil.copy(common.ROOT / "mo-hinh" / "ban-thu.json", root / "mo-hinh")
        record, report, _ = ban_ve.import_drawing(self.sample, person="kiem-tra", rights="mau",
                                                  root=root, number="BT-002", revision="A")
        self.assertEqual((record["id"], record["loai"], record["muc"]), ("BT-002-A", "step", "candidate"))
        model = common.load_json(root / record["mo_hinh"])
        self.assertEqual(model["trang_thai_ban_ve"], "candidate")
        self.assertTrue(all(z["khoi"] for z in model["vung"]))


if __name__ == "__main__":
    unittest.main()
