import socket
import struct
import time
import unittest

from paotwin import modbus
from paotwin.gateway import stack_light_state
from paotwin.sim import Devices, Simulation, RATED_A
from tests.test_mqtt import BrokerThread, Collector, wait_until
from paotwin import mqtt


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.image = modbus.DeviceImage(discrete_inputs=3, input_registers=3)
        self.image.input_registers = [700, 708, 694]
        self.image.discrete_inputs = [True, False, True]

    def test_input_registers(self):
        reply = modbus.answer(struct.pack(">BHH", 4, 0, 3), self.image)
        self.assertEqual(reply, bytes([4, 6]) + struct.pack(">3H", 700, 708, 694))

    def test_discrete_inputs_are_packed_lsb_first(self):
        reply = modbus.answer(struct.pack(">BHH", 2, 0, 3), self.image)
        self.assertEqual(reply, bytes([2, 1, 0b101]))

    def test_exceptions(self):
        self.assertEqual(modbus.answer(struct.pack(">BHH", 4, 2, 5), self.image), bytes([0x84, 2]))
        self.assertEqual(modbus.answer(struct.pack(">BHH", 4, 0, 0), self.image), bytes([0x84, 3]))
        self.assertEqual(modbus.answer(struct.pack(">BHH", 6, 0, 1), self.image), bytes([0x86, 1]))

    def test_bits_round_trip(self):
        bits = [True, False, False, True, True, False, True, False, True, True]
        self.assertEqual(modbus.unpack_bits(modbus.pack_bits(bits), len(bits)), bits)


class SimulationTest(unittest.TestCase):
    def test_currents_follow_the_load(self):
        sim = Simulation(seed=1)
        sim.apply({"tai_phan_tram": 50})
        for value in sim.currents().values():
            self.assertAlmostEqual(value, RATED_A * 0.5, delta=0.2)

    def test_lost_phase_reads_zero_and_trips_the_light(self):
        sim = Simulation(seed=1)
        sim.apply({"mat_pha": "b"})
        currents = sim.currents()
        self.assertEqual(currents["b"], 0.0)
        self.assertGreater(currents["a"], RATED_A * 0.7)
        self.assertEqual(sim.lamps(), (False, False, True))

    def test_stopped_motor(self):
        sim = Simulation(seed=1)
        sim.apply({"chay": False})
        self.assertEqual(set(sim.currents().values()), {0.0})
        self.assertEqual(sim.lamps(), (False, True, False))

    def test_bad_input_is_refused(self):
        sim = Simulation()
        for change in ({"tai_phan_tram": 400}, {"mat_pha": "d"}, {"mang": "x"},
                       {"den_che_do": "nhay"}):
            with self.assertRaises(ValueError):
                sim.apply(change)

    def test_stack_light_precedence(self):
        self.assertEqual(stack_light_state(True, True, True), "loi")
        self.assertEqual(stack_light_state(True, True, False), "chay")
        self.assertEqual(stack_light_state(False, True, False), "cho")
        self.assertEqual(stack_light_state(False, False, False), "tat")


class DevicesOverTheNetworkTest(unittest.TestCase):
    def setUp(self):
        self.broker = BrokerThread()
        self.net = {
            "modbus": {"host": "127.0.0.1", "port": free_port(),
                       "don_vi_dong_co": 1, "don_vi_den_thap": 2},
            "duong_mang_ao": {"host": "127.0.0.1", "port": free_port()},
            "broker": {"host": "127.0.0.1", "port": self.broker.port},
        }
        self.devices = Devices(Simulation(seed=2), self.net)
        self.devices.start()
        self.client = modbus.Client("127.0.0.1", self.net["modbus"]["port"], timeout=0.5)

    def tearDown(self):
        self.client.close()
        self.devices.stop()
        self.broker.close()

    def test_motor_and_light_read_over_modbus_tcp(self):
        currents = self.client.read_registers(1, 0, 3)
        for raw in currents:
            self.assertAlmostEqual(raw / 100.0, 7.0, delta=0.3)
        self.assertEqual(self.client.read_discrete_inputs(2, 0, 3), [True, False, False])

    def test_unknown_unit_and_pulled_cable(self):
        with self.assertRaises(modbus.ModbusError):
            self.client.read_registers(9, 0, 1)
        self.devices.apply({"rut_cap": True})
        time.sleep(0.1)
        with self.assertRaises(modbus.ModbusError):
            self.client.read_registers(1, 0, 3)
        self.devices.apply({"rut_cap": False})
        time.sleep(0.1)
        self.assertEqual(len(self.client.read_registers(1, 0, 3)), 3)

    def test_cut_link_fires_the_will_and_hung_link_times_out(self):
        watcher = Collector()
        watch = mqtt.Client("watch", "127.0.0.1", self.broker.port,
                            subscriptions=["paotwin/#"], on_message=watcher).start()
        gw = mqtt.Client("gw", "127.0.0.1", self.net["duong_mang_ao"]["port"], keepalive=1,
                         will=("paotwin/gateway/trang_thai", b"offline", True)).start()
        try:
            self.assertTrue(watch.wait_connected(5) and gw.wait_connected(5))
            time.sleep(0.2)
            self.devices.apply({"mang": "cat"})
            self.assertTrue(wait_until(lambda: b"offline" in [p for _, p, _ in watcher.items], 3))
            self.assertTrue(wait_until(lambda: not gw.connected, 3))
            # Back to normal, the gateway comes back on its own.
            self.devices.apply({"mang": "tot"})
            self.assertTrue(gw.wait_connected(8))
            watcher.items.clear()
            self.devices.apply({"mang": "treo"})
            started = time.monotonic()
            self.assertTrue(wait_until(lambda: b"offline" in [p for _, p, _ in watcher.items], 5))
            self.assertGreaterEqual(time.monotonic() - started, 1.0)
        finally:
            gw.stop(clean=False)
            watch.stop()

    def test_slow_link_delivers_late_in_order_and_keeps_the_link(self):
        watcher = Collector()
        watch = mqtt.Client("watch2", "127.0.0.1", self.broker.port,
                            subscriptions=["paotwin/x"], on_message=watcher).start()
        gw = mqtt.Client("gw2", "127.0.0.1", self.net["duong_mang_ao"]["port"], keepalive=10,
                         will=("paotwin/gateway/trang_thai", b"offline", True)).start()
        self.devices.sim.link_delay_s = 1.0
        try:
            self.assertTrue(watch.wait_connected(5) and gw.wait_connected(5))
            self.devices.apply({"mang": "cham"})
            sent_at = time.monotonic()
            for i in range(20):
                gw.publish("paotwin/x", str(i).encode())
            self.assertTrue(wait_until(lambda: len(watcher.items) == 20, 5))
            self.assertGreaterEqual(time.monotonic() - sent_at, 0.9)
            self.assertEqual([p for _, p, _ in watcher.items], [str(i).encode() for i in range(20)])
            self.assertTrue(gw.connected)
        finally:
            self.devices.apply({"mang": "tot"})
            gw.stop()
            watch.stop()


if __name__ == "__main__":
    unittest.main()
