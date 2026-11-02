"""The virtual gateway: what the ESP32 box did in the M64 PoC.

Every cycle it reads the laptop's own sensors and the virtual devices (over
Modbus TCP, read-only), stamps each reading with the time it was read, and
publishes it over MQTT. Its Last Will ("offline", retained) tells the twin
when the gateway disappears without saying goodbye.

Payload of every signal topic ``paotwin/<device>/<signal>``::

    {"v": <value or null>, "q": "tot" | "mat", "t": <unix ms>, "ly_do": <text>}

Readings are not buffered while the broker is unreachable: the twin shows
"Mất tín hiệu" for that time, which is the truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time

from . import common, mqtt
from .modbus import Client as ModbusClient, ModbusError
from .sensors import Laptop, Reading

log = logging.getLogger("paotwin.gateway")

STATUS_TOPIC = "paotwin/gateway/trang_thai"
LIMITS_TOPIC = "paotwin/laptop/gpu_gioi_han"
CURRENT_SCALE = 100.0  # the virtual motor's registers hold 0.01 A


def payload(reading: Reading, read_ms: float) -> bytes:
    body = {"v": reading.value, "q": "tot" if reading.ok else "mat", "t": round(read_ms, 1)}
    if not reading.ok:
        body["ly_do"] = reading.reason
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def stack_light_state(green: bool, yellow: bool, red: bool) -> str:
    """Red wins (the machine reports a fault), then green, then yellow."""
    if red:
        return "loi"
    if green:
        return "chay"
    if yellow:
        return "cho"
    return "tat"


class Gateway:
    def __init__(self, net: dict, laptop: Laptop | None = None,
                 broker_host: str | None = None, broker_port: int | None = None) -> None:
        self.net = net
        self.period = float(net["chu_ky_gui_s"])
        self.laptop = laptop if laptop is not None else Laptop()
        modbus = net["modbus"]
        self.modbus = ModbusClient(modbus["host"], modbus["port"], timeout=1.0)
        self.motor_unit = modbus["don_vi_dong_co"]
        self.light_unit = modbus["don_vi_den_thap"]
        link = net["duong_mang_ao"]
        self.limits = self.laptop.gpu.limits() if hasattr(self.laptop, "gpu") else {}
        self.client = mqtt.Client(
            "paotwin-gateway",
            broker_host or link["host"], broker_port or link["port"],
            keepalive=int(net["gateway_keepalive_s"]),
            will=(STATUS_TOPIC, b"offline", True),
            on_connect=self._on_connect)
        self._stop = threading.Event()

    def _on_connect(self) -> None:
        log.info("connected to the broker, announcing online")
        self.client.publish(STATUS_TOPIC, b"online", retain=True)
        limits = dict(self.limits, nguon="nvidia-smi -q -d TEMPERATURE")
        self.client.publish(LIMITS_TOPIC, json.dumps(limits).encode("utf-8"), retain=True)

    def read_devices(self) -> dict[str, tuple[Reading, float]]:
        out: dict[str, tuple[Reading, float]] = {}
        try:
            registers = self.modbus.read_registers(self.motor_unit, 0, 3)
            stamp = time.time() * 1000.0
            for name, raw in zip(("pha_a", "pha_b", "pha_c"), registers):
                out["dong_co/" + name] = (Reading(round(raw / CURRENT_SCALE, 2)), stamp)
        except ModbusError as exc:
            stamp = time.time() * 1000.0
            for name in ("pha_a", "pha_b", "pha_c"):
                out["dong_co/" + name] = (Reading(None, f"Modbus động cơ: {exc}"), stamp)
        try:
            green, yellow, red = self.modbus.read_discrete_inputs(self.light_unit, 0, 3)
            out["den_thap/trang_thai"] = (Reading(stack_light_state(green, yellow, red)),
                                          time.time() * 1000.0)
        except ModbusError as exc:
            out["den_thap/trang_thai"] = (Reading(None, f"Modbus đèn tháp: {exc}"),
                                          time.time() * 1000.0)
        return out

    def cycle(self) -> int:
        readings = {"laptop/" + k: v for k, v in self.laptop.read().items()}
        readings.update(self.read_devices())
        sent = 0
        for sid, (reading, stamp) in readings.items():
            if self.client.publish("paotwin/" + sid, payload(reading, stamp)):
                sent += 1
        return sent

    def run(self) -> None:
        self.client.start()
        log.info("gateway reading every %.1f s", self.period)
        next_at = time.monotonic()
        while not self._stop.is_set():
            sent = self.cycle()
            if not sent:
                log.info("broker unreachable, readings of this cycle dropped")
            next_at += self.period
            delay = next_at - time.monotonic()
            if delay < 0:
                next_at = time.monotonic()
                delay = 0
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        # A clean DISCONNECT cancels the will, so say "offline" ourselves.
        self.client.publish(STATUS_TOPIC, b"offline", retain=True)
        self.client.stop(clean=True)
        self.modbus.close()


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Gateway ảo").parse_args(argv)
    common.setup_logging("gateway")
    gateway = Gateway(common.network())
    try:
        gateway.run()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
