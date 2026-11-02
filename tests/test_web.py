import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from paotwin import common, server, sim
from tests.test_modbus_sim import free_port


def get(port, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, res.getheader("Content-Type"), data


class TwinPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No broker on this port: the page must still load and show "lost".
        cls.twin = server.Twin(common.signals_config(), common.model(),
                               ("127.0.0.1", free_port()))
        cls.twin.start()
        cls.httpd = server.serve("127.0.0.1", 0, cls.twin, common.WEB_DIR, sim_port=8792)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.twin.stop()

    def test_page_and_scripts_are_served(self):
        status, kind, body = get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", kind)
        self.assertIn(b'id="sim-link"', body)
        for name in ("app.js", "render.js"):
            status, kind, _ = get(self.port, "/" + name)
            self.assertEqual((status, kind.split(";")[0]), (200, "text/javascript"))

    def test_model_tells_where_the_simulation_panel_is(self):
        status, _, body = get(self.port, "/api/mo-hinh")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["cong_mo_phong"], 8792)
        self.assertEqual(data["mo_hinh"]["trang_thai_ban_ve"], "tham_khao")

    def test_without_a_broker_nothing_is_shown_as_normal(self):
        _, _, body = get(self.port, "/api/trang-thai")
        data = json.loads(body)
        self.assertFalse(data["broker"])
        states = {z["trang_thai"] for z in data["vung"].values()}
        self.assertEqual(states, {"mat_tin_hieu", "chua_co"})

    def test_history_api(self):
        status, _, body = get(self.port, "/api/lich-su?tin_hieu=laptop/cpu_tai&phut=5")
        data = json.loads(body)
        self.assertEqual((status, data["phut"], data["diem"]), (200, 5.0, []))
        status, _, _ = get(self.port, "/api/lich-su?tin_hieu=khong_co")
        self.assertEqual(status, 404)

    def test_minutes_are_written_as_min_mean_max(self):
        import tempfile, time as _t
        from pathlib import Path
        from paotwin.engine import Engine
        with tempfile.TemporaryDirectory() as folder:
            twin = server.Twin(common.signals_config(), common.model(), ("127.0.0.1", 1),
                               minute_dir=Path(folder))
            minute = int(_t.time() // 60) - 1
            twin._minute_done = minute
            for i, v in enumerate((10.0, 20.0, 30.0)):
                twin.engine._remember("laptop/cpu_tai", minute * 60 + 5 + i, v)
            self.assertEqual(twin.write_minutes(_t.time()), 1)
            files = list(Path(folder).glob("so-do-*.csv"))
            lines = files[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "phut,tin_hieu,so_mau,nho_nhat,trung_binh,lon_nhat")
            self.assertTrue(lines[1].endswith("laptop/cpu_tai,3,10.0,20.0,30.0"))
            self.assertEqual(twin.write_minutes(_t.time()), 0)   # nothing twice

    def test_the_twin_is_read_only(self):
        for method in ("POST", "PUT", "DELETE"):
            status, _, _ = get(self.port, "/api/trang-thai", method=method, body=b"{}")
            self.assertEqual(status, 405, method)

    def test_files_outside_the_web_folder_are_not_served(self):
        for path in ("/../cau-hinh/mang.json", "/%2e%2e/README.md", "/..%2fREADME.md"):
            status, _, _ = get(self.port, path)
            self.assertEqual(status, 404, path)

    def test_the_stream_sends_a_snapshot_at_once(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/luong")
        res = conn.getresponse()
        self.assertIn("text/event-stream", res.getheader("Content-Type"))
        seen = b""
        while b"\ndata: " not in b"\n" + seen or not seen.rstrip().endswith(b"}"):
            seen += res.fp.readline()
        line = [l for l in seen.splitlines() if l.startswith(b"data: ")][0]
        self.assertIn("vung", json.loads(line[6:]))
        conn.close()


class SimulationPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        net = {"modbus": {"host": "127.0.0.1", "port": free_port(),
                          "don_vi_dong_co": 1, "don_vi_den_thap": 2},
               "duong_mang_ao": {"host": "127.0.0.1", "port": free_port()},
               "broker": {"host": "127.0.0.1", "port": free_port()},
               "web": {"host": "127.0.0.1", "port": 8791}}
        cls.devices = sim.Devices(sim.Simulation(seed=3), net)
        cls.devices.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sim.make_handler(cls.devices))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.devices.stop()

    def test_the_panel_links_to_the_twin(self):
        status, _, body = get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn(b":8791/", body)
        self.assertNotIn(b"__CONG_BAN_SAO__", body)

    def test_a_change_needs_json(self):
        status, _, _ = get(self.port, "/api/dat", "POST", b"mat_pha=b",
                           {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _, body = get(self.port, "/api/dat", "POST", b'{"mat_pha": "b"}',
                              {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["dong_co"]["mat_pha"], "b")
        status, _, _ = get(self.port, "/api/dat", "POST", b'{"tai_phan_tram": 999}',
                           {"Content-Type": "application/json"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
