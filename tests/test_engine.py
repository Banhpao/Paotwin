import json
import unittest

from paotwin import common
from paotwin.engine import (BINH_THUONG, CANH_BAO, CHUA_CO, Engine, GATEWAY_STATUS,
                            GPU_LIMITS, LOI, MAT)
from paotwin.sensors import parse_gpu_limits, parse_gpu_query, parse_wifi_signal

T0 = 1_800_000_000.0


def msg(value, t=T0, ok=True, reason=None):
    body = {"v": value if ok else None, "q": "tot" if ok else "mat", "t": t * 1000.0}
    if reason:
        body["ly_do"] = reason
    return json.dumps(body).encode()


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(common.signals_config(), common.model())
        self.engine.set_broker_connected(True, T0)
        self.engine.on_message(GATEWAY_STATUS, b"online", T0)

    def send(self, sid, value, now=T0, **kwargs):
        self.engine.on_message("paotwin/" + sid, msg(value, now, **kwargs), now)

    def state(self, sid, now=T0):
        return self.engine.evaluate(now)[0][sid]

    def zone(self, zid, now=T0):
        return self.engine.evaluate(now)[1][zid]

    # -- nothing is ever shown as normal without fresh data ------------------

    def test_a_signal_never_heard_is_lost_not_normal(self):
        self.assertEqual(self.state("laptop/cpu_tai").state, MAT)
        self.assertEqual(self.zone("cpu").state, MAT)

    def test_stale_data_becomes_lost(self):
        self.send("laptop/cpu_tai", 30)
        self.assertEqual(self.state("laptop/cpu_tai", T0 + 6).state, BINH_THUONG)
        stale = self.state("laptop/cpu_tai", T0 + 6.1)
        self.assertEqual(stale.state, MAT)
        self.assertIn("6 s", stale.reason)

    def test_gateway_offline_makes_every_reading_lost(self):
        self.send("laptop/cpu_tai", 30)
        self.engine.on_message(GATEWAY_STATUS, b"offline", T0)
        signal = self.state("laptop/cpu_tai")
        self.assertEqual(signal.state, MAT)
        self.assertIn("Last Will", signal.reason)
        self.assertEqual(self.zone("gateway").state, MAT)

    def test_broker_lost_makes_everything_lost(self):
        self.send("laptop/cpu_tai", 30)
        self.engine.set_broker_connected(False, T0)
        signals, zones = self.engine.evaluate(T0)
        measured = [z for z in zones.values() if z.state != CHUA_CO]
        self.assertTrue(measured)
        self.assertTrue(all(z.state == MAT for z in measured))

    def test_a_reading_that_arrives_late_is_old_not_new(self):
        # Read at T0, received 8 s later: it is 8 s old, not fresh.
        self.engine.on_message("paotwin/laptop/cpu_tai", msg(30, T0), T0 + 8)
        signal = self.state("laptop/cpu_tai", T0 + 8)
        self.assertEqual(signal.state, MAT)
        self.assertIn("đến trễ", signal.reason)
        # Two seconds late is inside the 6 s rule: shown, with its true age.
        self.engine.on_message("paotwin/laptop/cpu_tai", msg(30, T0 + 10), T0 + 12)
        signal = self.state("laptop/cpu_tai", T0 + 12)
        self.assertEqual(signal.state, BINH_THUONG)
        self.assertAlmostEqual(signal.age_s, 2.0, places=3)

    def test_history_keeps_readings_at_the_time_they_were_taken(self):
        for i in range(5):
            self.engine.on_message("paotwin/laptop/cpu_tai", msg(10 + i, T0 + 2 * i), T0 + 2 * i + 0.1)
        self.send("laptop/cpu_tai", None, ok=False, reason="x")
        points = self.engine.history_of("laptop/cpu_tai")
        self.assertEqual([v for _, v in points], [10, 11, 12, 13, 14])
        self.assertEqual(points[0][0], T0)
        self.assertEqual(len(self.engine.history_of("laptop/cpu_tai", T0 + 5)), 2)

    def test_the_derived_imbalance_has_a_history_too(self):
        self.phases(7.0, 7.0, 7.0)
        self.engine.evaluate(T0)
        self.engine.evaluate(T0 + 0.5)          # nothing new: no second point
        self.phases(7.0, 0.0, 7.0, T0 + 2)
        self.engine.evaluate(T0 + 2)
        values = [v for _, v in self.engine.history_of("dong_co/lech_pha")]
        self.assertEqual(len(values), 2)
        self.assertGreater(values[1], 40)

    # -- machine-level state (M64 words) --------------------------------------

    def machine(self, now=T0):
        signals, _ = self.engine.evaluate(now)
        return self.engine.machines_snapshot(signals)[0]

    def test_machine_state_follows_the_stack_light(self):
        self.phases(7.0, 7.0, 7.0)
        for value, expected in (("chay", "gia_cong"), ("cho", "cho"), ("tat", "tat"), ("loi", "loi")):
            self.send("den_thap/trang_thai", value)
            self.assertEqual(self.machine()["trang_thai"], expected, value)

    def test_lost_light_means_off_or_lost_not_off(self):
        machine = self.machine()
        self.assertEqual(machine["trang_thai"], "khong_ro")
        self.assertIn("tắt máy hoặc mất tín hiệu", machine["nhan"])

    def test_light_and_current_disagreeing_is_noted(self):
        self.send("den_thap/trang_thai", "chay")
        self.phases(0.0, 0.0, 0.0)
        self.assertIn("không có dòng", self.machine()["ghi_chu"])
        self.send("den_thap/trang_thai", "cho")
        self.phases(7.0, 7.0, 7.0)
        self.assertIn("đang có dòng", self.machine()["ghi_chu"])

    def test_time_in_each_machine_state_adds_up(self):
        self.send("den_thap/trang_thai", "chay")
        self.engine.evaluate(T0)
        self.send("den_thap/trang_thai", "cho", T0 + 3)
        self.engine.evaluate(T0 + 3)
        self.engine.evaluate(T0 + 5)
        times = self.machine(T0 + 5)["thoi_gian_s"]
        self.assertAlmostEqual(times["gia_cong"], 3.0)
        self.assertAlmostEqual(times["cho"], 2.0)
        self.assertEqual(self.engine.events[0]["den_may"], "cho")

    def test_a_reading_the_gateway_could_not_take_is_lost_with_its_reason(self):
        self.send("dong_co/pha_a", None, ok=False, reason="Modbus động cơ: hết thời gian chờ")
        signal = self.state("dong_co/pha_a")
        self.assertEqual(signal.state, MAT)
        self.assertIn("hết thời gian", signal.reason)

    def test_broken_payload_is_lost(self):
        self.engine.on_message("paotwin/laptop/cpu_tai", b"{not json", T0)
        self.assertEqual(self.state("laptop/cpu_tai").state, MAT)

    def test_a_non_number_is_lost(self):
        self.send("laptop/cpu_tai", "nhieu")
        self.assertEqual(self.state("laptop/cpu_tai").state, MAT)
        self.send("laptop/cpu_tai", True)
        self.assertEqual(self.state("laptop/cpu_tai").state, MAT)

    # -- thresholds ------------------------------------------------------------

    def test_thresholds_above(self):
        for value, expected in ((84.9, BINH_THUONG), (85, CANH_BAO), (96.9, CANH_BAO),
                                (97, LOI)):
            self.send("laptop/cpu_tai", value)
            self.assertEqual(self.state("laptop/cpu_tai").state, expected, value)

    def test_thresholds_below(self):
        for value, expected in ((50, BINH_THUONG), (15, CANH_BAO), (5, LOI)):
            self.send("laptop/o_dia_trong", value)
            self.assertEqual(self.state("laptop/o_dia_trong").state, expected, value)

    def test_demo_thresholds_say_so(self):
        self.send("laptop/cpu_tai", 10)
        self.assertEqual(self.state("laptop/cpu_tai").threshold["nguon"], "demo")

    def test_gpu_thresholds_come_from_the_card_or_not_at_all(self):
        self.send("laptop/gpu_nhiet_do", 95)
        signal = self.state("laptop/gpu_nhiet_do")
        self.assertEqual(signal.state, BINH_THUONG)
        self.assertIsNone(signal.threshold)
        self.assertIn("không báo ngưỡng", signal.reason)
        self.engine.on_message(GPU_LIMITS, json.dumps(
            {"max_operating": 87, "slowdown": 98, "shutdown": 103}).encode(), T0)
        signal = self.state("laptop/gpu_nhiet_do")
        self.assertEqual(signal.state, CANH_BAO)
        self.assertEqual(signal.threshold["nguon"], "thiet_bi")
        self.send("laptop/gpu_nhiet_do", 98)
        self.assertEqual(self.state("laptop/gpu_nhiet_do").state, LOI)

    def test_card_limits_in_an_unexpected_order_give_no_warning_level(self):
        # What the RTX 3050 Laptop on the test machine reported on 02/11.
        self.engine.on_message(GPU_LIMITS, json.dumps(
            {"max_operating": 105, "slowdown": 97, "shutdown": 100}).encode(), T0)
        self.send("laptop/gpu_nhiet_do", 96)
        signal = self.state("laptop/gpu_nhiet_do")
        self.assertEqual(signal.state, BINH_THUONG)
        self.assertIsNone(signal.threshold["canh_bao"])
        self.assertEqual(signal.threshold["loi"], 97)
        self.assertIn("không dùng", signal.threshold["giai_thich"])
        self.assertIn("Slowdown 97", signal.threshold["giai_thich"])
        self.send("laptop/gpu_nhiet_do", 97)
        self.assertEqual(self.state("laptop/gpu_nhiet_do").state, LOI)

    def test_enumerations(self):
        self.send("den_thap/trang_thai", "loi")
        self.assertEqual(self.state("den_thap/trang_thai").state, LOI)
        self.send("den_thap/trang_thai", "cho")
        signal = self.state("den_thap/trang_thai")
        self.assertEqual((signal.state, signal.text), (BINH_THUONG, "Chờ (vàng)"))
        self.send("den_thap/trang_thai", "nhap_nhay")
        self.assertEqual(self.state("den_thap/trang_thai").state, MAT)

    # -- the derived phase imbalance ------------------------------------------

    def phases(self, a, b, c, now=T0):
        for sid, value in (("pha_a", a), ("pha_b", b), ("pha_c", c)):
            self.send("dong_co/" + sid, value, now)

    def test_balanced_motor(self):
        self.phases(7.0, 7.1, 6.95)
        signal = self.state("dong_co/lech_pha")
        self.assertEqual(signal.state, BINH_THUONG)
        self.assertLess(signal.value, 2)

    def test_lost_phase_is_a_fault(self):
        self.phases(12.1, 0.0, 12.0)
        self.assertEqual(self.state("dong_co/lech_pha").state, LOI)
        self.assertEqual(self.zone("dong_co").state, LOI)

    def test_stopped_motor_is_not_judged(self):
        self.phases(0.0, 0.0, 0.0)
        signal = self.state("dong_co/lech_pha")
        self.assertEqual(signal.state, BINH_THUONG)
        self.assertIn("dừng", signal.text)

    def test_imbalance_needs_all_three_phases(self):
        self.send("dong_co/pha_a", 7)
        self.send("dong_co/pha_b", 7)
        signal = self.state("dong_co/lech_pha")
        self.assertEqual(signal.state, MAT)
        self.assertIn("Dòng pha C", signal.reason)

    # -- zones -------------------------------------------------------------------

    def test_zone_takes_the_worst_and_lost_ranks_above_warning(self):
        self.send("laptop/gpu_tai", 92)        # warning
        self.send("laptop/gpu_vram", 10)
        self.send("laptop/gpu_cong_suat", 20)
        self.send("laptop/gpu_nhiet_do", 50)
        self.assertEqual(self.zone("gpu").state, CANH_BAO)
        self.send("laptop/gpu_vram", None, ok=False, reason="card báo [N/A]")
        zone = self.zone("gpu")
        self.assertEqual(zone.state, MAT)
        self.assertIn("VRAM", zone.reason)
        self.send("laptop/gpu_tai", 99.5)
        self.assertEqual(self.zone("gpu").state, LOI)

    def test_a_zone_with_no_sensor_says_so(self):
        zone = self.zone("quat")
        self.assertEqual(zone.state, CHUA_CO)
        self.assertIn("tốc độ quạt", zone.reason)

    def test_unmeasured_signal_does_not_hide_a_measured_one(self):
        self.send("laptop/cpu_tai", 20)
        self.assertEqual(self.zone("cpu").state, BINH_THUONG)

    def test_zone_changes_are_recorded_as_events(self):
        self.send("laptop/cpu_tai", 20)
        self.engine.evaluate(T0)
        self.send("laptop/cpu_tai", 99, T0 + 1)
        self.engine.evaluate(T0 + 1)
        event = self.engine.events[0]
        self.assertEqual((event["vung"], event["tu"], event["den"]), ("cpu", BINH_THUONG, LOI))
        self.assertIn("Tải CPU", event["ly_do"])

    def test_latency_is_measured_from_the_reading_time(self):
        self.engine.on_message("paotwin/laptop/cpu_tai", msg(20, T0), T0 + 0.040)
        self.engine.on_message("paotwin/laptop/cpu_tai", msg(20, T0), T0 + 0.060)
        latency = self.engine.latency()
        self.assertEqual(latency["so_mau"], 2)
        self.assertAlmostEqual(latency["p50_ms"], 50.0, places=3)
        self.assertAlmostEqual(latency["max_ms"], 60.0, places=3)

    def test_snapshot_is_json(self):
        self.send("laptop/cpu_tai", 20)
        snap = self.engine.snapshot(T0)
        json.dumps(snap, ensure_ascii=False)
        self.assertEqual(sum(snap["dem"].values()), len(common.model()["vung"]))

    def test_every_zone_has_a_signal_entry_and_every_signal_a_zone(self):
        config, model = common.signals_config(), common.model()
        zones = {z["id"] for z in model["vung"]}
        used = {s["vung"] for s in config["tin_hieu"]}
        self.assertEqual(zones, used)


class ParserTest(unittest.TestCase):
    def test_gpu_query(self):
        out = parse_gpu_query("52, 7, [N/A], 512, 4096\n")
        self.assertEqual(out["temperature.gpu"].value, 52.0)
        self.assertEqual(out["utilization.gpu"].value, 7.0)
        self.assertFalse(out["power.draw"].ok)
        self.assertIn("N/A", out["power.draw"].reason)
        self.assertFalse(parse_gpu_query("")["temperature.gpu"].ok)
        self.assertFalse(parse_gpu_query("1, 2")["temperature.gpu"].ok)

    def test_gpu_limits(self):
        text = """
    Temperature
        GPU Current Temp                  : 48 C
        GPU T.Limit Temp                  : N/A
        GPU Shutdown Temp                 : 105 C
        GPU Slowdown Temp                 : 102 C
        GPU Max Operating Temp            : 87 C
        GPU Target Temperature            : N/A
        Memory Current Temp               : N/A
"""
        self.assertEqual(parse_gpu_limits(text),
                         {"shutdown": 105.0, "slowdown": 102.0, "max_operating": 87.0})
        self.assertEqual(parse_gpu_limits("nothing here"), {})

    def test_wifi_signal_in_any_language(self):
        english = "    Name : Wi-Fi\n    State : connected\n    Signal : 82%\n"
        vietnamese = "    Tên : Wi-Fi\n    Trạng thái : đã kết nối\n    Tín hiệu : 64%\n"
        self.assertEqual(parse_wifi_signal(english).value, 82)
        self.assertEqual(parse_wifi_signal(vietnamese).value, 64)
        self.assertFalse(parse_wifi_signal("There is no wireless interface").ok)
        blocked = parse_wifi_signal("Network shell commands need location permission")
        self.assertIn("Vị trí", blocked.reason)


if __name__ == "__main__":
    unittest.main()
