"""The virtual devices and the virtual network, with their own control page.

- Unit 1, the virtual motor: input registers 0..2 hold the phase currents
  A, B, C in 0.01 A. Rated current 10 A, a value this simulator sets for
  itself (it is not the rating of any real motor).
- Unit 2, the virtual stack light: discrete inputs 0..2 are the green
  (running), yellow (waiting) and red (fault) lamps.
- The virtual network: a TCP relay between the gateway and the broker.
  "cat" closes the link (as when the Wi-Fi password changed at M64);
  "treo" keeps the sockets open but forwards nothing, so only time-outs
  can tell; "cham" delivers what the gateway sends, in order, but 8 s late.

The register map is this simulator's own. A real clamp meter (for example the
HD-273I in the M64 PoC) has its own map, taken from its datasheet.

This page changes the simulation only. The twin itself has no way to write
to a device.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common
from .modbus import DeviceImage, Server as ModbusServer

log = logging.getLogger("paotwin.sim")

RATED_A = 10.0
SINGLE_PHASING_FACTOR = math.sqrt(3)  # rough: same load carried on two phases
LIGHT_MODES = ("tu_dong", "chay", "cho", "loi", "tat")
LINK_MODES = ("tot", "cat", "treo", "cham")
SLOW_LINK_S = 8.0  # longer than the twin's 6 s, so late readings must show as lost


class Simulation:
    def __init__(self, seed: int | None = None) -> None:
        self.lock = threading.Lock()
        self.running = True
        self.load_percent = 70.0
        self.lost_phase: str | None = None
        self.cable_pulled = False
        self.light_mode = "tu_dong"
        self.link_mode = "tot"
        self.link_delay_s = SLOW_LINK_S
        self.random = random.Random(seed)
        # A small fixed difference between phases, as in any real motor.
        self.phase_bias = {"a": 1.0, "b": 1.012, "c": 0.991}
        self.motor = DeviceImage(input_registers=3)
        self.light = DeviceImage(discrete_inputs=3)
        self.refresh()

    def currents(self) -> dict[str, float]:
        if not self.running:
            return {p: 0.0 for p in "abc"}
        base = RATED_A * self.load_percent / 100.0
        out = {}
        for phase in "abc":
            if phase == self.lost_phase:
                out[phase] = 0.0
                continue
            value = base * self.phase_bias[phase]
            if self.lost_phase:
                value *= SINGLE_PHASING_FACTOR
            out[phase] = max(0.0, value * (1.0 + self.random.uniform(-0.01, 0.01)))
        return out

    def lamps(self) -> tuple[bool, bool, bool]:
        mode = self.light_mode
        if mode == "tu_dong":
            if self.lost_phase is not None:
                mode = "loi"   # as a motor protection relay would trip
            else:
                mode = "chay" if self.running else "cho"
        return mode == "chay", mode == "cho", mode == "loi"

    def refresh(self) -> None:
        with self.lock:
            currents = self.currents()
            self.motor.input_registers = [min(65535, round(currents[p] * 100)) for p in "abc"]
            self.light.discrete_inputs = list(self.lamps())

    def state(self) -> dict:
        with self.lock:
            currents = self.currents()
            green, yellow, red = self.lamps()
            return {"dong_co": {"chay": self.running, "tai_phan_tram": self.load_percent,
                                "mat_pha": self.lost_phase, "rut_cap": self.cable_pulled,
                                "dinh_muc_A": RATED_A,
                                "dong_A": {p: round(v, 2) for p, v in currents.items()}},
                    "den_thap": {"che_do": self.light_mode,
                                 "den": {"xanh": green, "vang": yellow, "do": red}},
                    "mang": self.link_mode, "mang_cham_s": self.link_delay_s}

    def apply(self, change: dict) -> None:
        with self.lock:
            if "chay" in change:
                self.running = bool(change["chay"])
            if "tai_phan_tram" in change:
                value = float(change["tai_phan_tram"])
                if not 0 <= value <= 150:
                    raise ValueError("tải phải trong khoảng 0 đến 150 %")
                self.load_percent = value
            if "mat_pha" in change:
                if change["mat_pha"] not in (None, "a", "b", "c"):
                    raise ValueError("mat_pha phải là a, b, c hoặc null")
                self.lost_phase = change["mat_pha"]
            if "rut_cap" in change:
                self.cable_pulled = bool(change["rut_cap"])
            if "den_che_do" in change:
                if change["den_che_do"] not in LIGHT_MODES:
                    raise ValueError("chế độ đèn không hợp lệ")
                self.light_mode = change["den_che_do"]
            if "mang" in change:
                if change["mang"] not in LINK_MODES:
                    raise ValueError("trạng thái mạng không hợp lệ")
                self.link_mode = change["mang"]


class Relay:
    """The virtual network between the gateway and the broker."""

    def __init__(self, sim: Simulation, host: str, port: int, upstream: tuple[str, int]) -> None:
        self.sim, self.host, self.port, self.upstream = sim, host, port, upstream
        self._pairs: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("virtual network on %s:%s -> %s:%s", self.host, self.port, *self.upstream)

    def cut(self) -> None:
        for a, b in list(self._pairs):
            a.close()
            b.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.cut()

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    slows: bool = False) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def deliver() -> None:
            # One writer per direction, so bytes always leave in the order
            # they came in, even when the delay changes mid-stream.
            while True:
                release, data = await queue.get()
                if data is None:
                    return
                wait = release - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if self.sim.link_mode == "cat":
                    return
                try:
                    writer.write(data)
                    await writer.drain()
                except (ConnectionError, OSError):
                    return  # the far side went away: nothing left to deliver

        sender = asyncio.ensure_future(deliver())
        last_release = 0.0
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                mode = self.sim.link_mode
                if mode == "cat":
                    break
                if mode == "treo":
                    continue  # the bytes are lost, the sockets stay open
                # Only the gateway-to-broker direction is slowed: readings
                # arrive late, while the broker's answers (PINGRESP) still
                # come back in time, so the link itself stays up.
                delay = self.sim.link_delay_s if mode == "cham" and slows else 0.0
                last_release = max(loop.time() + delay, last_release)
                queue.put_nowait((last_release, data))
                if sender.done():
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            queue.put_nowait((0.0, None))
            try:
                await asyncio.wait_for(sender, self.sim.link_delay_s + 1)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                sender.cancel()
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.sim.link_mode == "cat":
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.upstream)
        except OSError:
            writer.close()
            return
        pair = (writer, up_writer)
        self._pairs.add(pair)
        try:
            await asyncio.gather(self._pump(reader, up_writer, slows=True),
                                 self._pump(up_reader, writer))
        finally:
            self._pairs.discard(pair)


class Devices:
    """Runs the Modbus server and the relay on their own event loop thread."""

    def __init__(self, sim: Simulation, net: dict) -> None:
        self.sim, self.net = sim, net
        modbus = net["modbus"]
        self.modbus = ModbusServer({modbus["don_vi_dong_co"]: sim.motor,
                                    modbus["don_vi_den_thap"]: sim.light},
                                   modbus["host"], modbus["port"], before_read=sim.refresh)
        link, broker = net["duong_mang_ao"], net["broker"]
        self.relay = Relay(sim, link["host"], link["port"], (broker["host"], broker["port"]))
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sim-devices", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.modbus.start())
        self.loop.run_until_complete(self.relay.start())
        self._ready.set()
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("thiết bị ảo không khởi động được")

    def apply(self, change: dict) -> None:
        self.sim.apply(change)

        def follow() -> None:
            self.modbus.set_responding(not self.sim.cable_pulled)
            if self.sim.link_mode == "cat":
                self.relay.cut()

        self.loop.call_soon_threadsafe(follow)

    def stop(self) -> None:
        async def close() -> None:
            await self.modbus.stop()
            await self.relay.stop()
            # Finish every connection task, so nothing is left pending when
            # the loop closes (Windows prints "Task was destroyed" otherwise).
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(5)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        if not self.loop.is_running():
            self.loop.close()


def make_handler(devices: Devices):
    page = (common.WEB_DIR / "mo-phong.html").read_bytes().replace(
        b"__CONG_BAN_SAO__", str(devices.net["web"]["port"]).encode("ascii"))

    class Handler(BaseHTTPRequestHandler):
        server_version = "PaotwinSim"

        def log_message(self, fmt, *args):  # keep the console for real events
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif self.path == "/api/trang-thai":
                self._json(HTTPStatus.OK, devices.sim.state())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"loi": "không có trang này"})

        def do_POST(self) -> None:
            if self.path != "/api/dat":
                self._json(HTTPStatus.NOT_FOUND, {"loi": "không có trang này"})
                return
            # Only this page may change the simulation (no cross-site form posts).
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"loi": "cần JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("kích thước yêu cầu không hợp lệ")
                change = json.loads(self.rfile.read(length))
                if not isinstance(change, dict):
                    raise ValueError("cần một đối tượng JSON")
                devices.apply(change)
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"loi": str(exc)})
                return
            log.info("simulation changed: %s", json.dumps(change, ensure_ascii=False))
            self._json(HTTPStatus.OK, devices.sim.state())

    return Handler


def main(argv: list[str] | None = None) -> int:
    import argparse

    argparse.ArgumentParser(description="Thiết bị ảo và bảng mô phỏng").parse_args(argv)
    common.setup_logging("mo-phong")
    net = common.network()
    devices = Devices(Simulation(), net)
    devices.start()
    web = net["mo_phong_web"]
    httpd = ThreadingHTTPServer((web["host"], web["port"]), make_handler(devices))
    httpd.daemon_threads = True
    log.info("control page on http://%s:%s/", web["host"], web["port"])
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        devices.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
