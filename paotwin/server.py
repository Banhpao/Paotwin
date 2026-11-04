"""The twin server: listens to the broker, keeps the state engine, serves the
3D page and pushes every change to it (Server-Sent Events).

Read-only by design: the HTTP API has GET routes only, and nothing here
publishes to the broker or talks to a device.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import ban_ve, common, mqtt
from .engine import Engine

log = logging.getLogger("paotwin.server")

TICK_S = 0.5          # how often time-based rules (stale data) are checked
PUSH_EVERY_S = 1.0    # a snapshot at least this often, so ages keep moving
STATIC = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
          ".svg": "image/svg+xml"}


class Twin:
    def __init__(self, config: dict, model: dict, broker: tuple[str, int],
                 event_log: Path | None = None, minute_dir: Path | None = None) -> None:
        self.engine = Engine(config, model)
        self.config, self.model = config, model
        self.event_log = event_log
        self.minute_dir = minute_dir
        self._minute_done = int(time.time() // 60)
        self._changed = threading.Condition()
        self._snapshot = b""
        self._snapshot_version = -1
        self._stop = threading.Event()
        self._last_event_t = 0.0
        self.client = mqtt.Client(
            f"paotwin-twin-{int(time.time())}", broker[0], broker[1], keepalive=10,
            subscriptions=["paotwin/#"], on_message=self._on_message,
            on_connect=lambda: self._broker(True), on_disconnect=lambda: self._broker(False))
        self._ticker = threading.Thread(target=self._tick, name="twin-tick", daemon=True)

    def start(self) -> None:
        self.client.start()
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        self.client.stop()
        with self._changed:
            self._changed.notify_all()

    def _broker(self, connected: bool) -> None:
        log.info("broker %s", "connected" if connected else "lost")
        self.engine.set_broker_connected(connected, time.time())
        self._publish_snapshot()

    def _on_message(self, topic: str, payload: bytes, _retained: bool) -> None:
        self.engine.on_message(topic, payload, time.time())

    def _write_events(self) -> None:
        """Appends zone changes not yet written, oldest first, one JSON per line."""
        if self.event_log is None:
            return
        fresh = [e for e in self.engine.events if e["t"] > self._last_event_t]
        if not fresh:
            return
        self._last_event_t = max(e["t"] for e in fresh)
        try:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.event_log, "a", encoding="utf-8") as handle:
                for event in reversed(fresh):
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:  # a full or missing drive must not stop the twin
            log.warning("could not write the event log: %s", exc)

    def _publish_snapshot(self) -> None:
        snapshot = self.engine.snapshot(time.time())
        self._write_events()
        data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._changed:
            self._snapshot = data
            self._snapshot_version += 1
            self._changed.notify_all()

    def _tick(self) -> None:
        last_push, last_version = 0.0, -1
        while not self._stop.wait(TICK_S):
            version = self.engine.version
            now = time.monotonic()
            if version != last_version or now - last_push >= PUSH_EVERY_S:
                self._publish_snapshot()
                last_push, last_version = now, self.engine.version
            self.write_minutes(time.time())

    def write_minutes(self, now: float) -> int:
        """Appends one row per signal for each finished minute (min, mean, max).

        Raw readings stay in memory only (about 30 minutes); a day of minute
        rows is a little over a megabyte, so the removable drive is spared.
        """
        current = int(now // 60)
        if self.minute_dir is None or current <= self._minute_done:
            return 0
        rows = []
        for minute in range(self._minute_done, current):
            start, end = minute * 60.0, minute * 60.0 + 60.0
            for sid in self.engine.signals:
                values = [v for t, v in self.engine.history_of(sid, start) if t < end]
                if values:
                    rows.append((minute, sid, values))
        self._minute_done = current
        if not rows:
            return 0
        try:
            self.minute_dir.mkdir(parents=True, exist_ok=True)
            by_day: dict[str, list] = {}
            for minute, sid, values in rows:
                day = time.strftime("%Y%m%d", time.localtime(minute * 60))
                by_day.setdefault(day, []).append((minute, sid, values))
            for day, items in by_day.items():
                path = self.minute_dir / f"so-do-{day}.csv"
                new = not path.exists()
                with open(path, "a", encoding="utf-8", newline="") as handle:
                    out = csv.writer(handle)
                    if new:
                        out.writerow(["phut", "tin_hieu", "so_mau", "nho_nhat",
                                      "trung_binh", "lon_nhat"])
                    for minute, sid, values in items:
                        out.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(minute * 60)),
                                      sid, len(values), round(min(values), 3),
                                      round(sum(values) / len(values), 3), round(max(values), 3)])
        except OSError as exc:
            log.warning("could not write the minute file: %s", exc)
            return 0
        return len(rows)

    def current(self) -> tuple[int, bytes]:
        with self._changed:
            if self._snapshot:
                return self._snapshot_version, self._snapshot
        self._publish_snapshot()
        with self._changed:
            return self._snapshot_version, self._snapshot

    def wait_newer(self, version: int, timeout: float) -> tuple[int, bytes]:
        with self._changed:
            self._changed.wait_for(lambda: self._snapshot_version != version
                                   or self._stop.is_set(), timeout)
            return self._snapshot_version, self._snapshot


def make_handler(twin: Twin, web_dir: Path, sim_port: int | None = None):
    model_body = json.dumps({"mo_hinh": twin.model, "tin_hieu": twin.config,
                             "cong_mo_phong": sim_port},
                            ensure_ascii=False).encode("utf-8")
    web_root = web_dir.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Paotwin"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/mo-hinh":
                self._send(HTTPStatus.OK, model_body, STATIC[".json"])
            elif path == "/api/trang-thai":
                self._send(HTTPStatus.OK, twin.current()[1], STATIC[".json"])
            elif path == "/api/luong":
                self._stream()
            elif path == "/api/lich-su":
                self._history()
            else:
                self._static(path)

        def _history(self) -> None:
            query = parse_qs(self.path.partition("?")[2])
            sid = (query.get("tin_hieu") or [""])[0]
            if sid not in twin.engine.signals:
                self._send(HTTPStatus.NOT_FOUND, b'{"loi":"khong co tin hieu nay"}',
                           STATIC[".json"])
                return
            try:
                minutes = min(30.0, max(1.0, float((query.get("phut") or ["30"])[0])))
            except ValueError:
                minutes = 30.0
            points = twin.engine.history_of(sid, time.time() - minutes * 60)
            body = json.dumps({"tin_hieu": sid, "phut": minutes,
                               "diem": [[round(t, 1), v] for t, v in points]},
                              separators=(",", ":")).encode("utf-8")
            self._send(HTTPStatus.OK, body, STATIC[".json"])

        def do_POST(self) -> None:  # the twin never writes anywhere
            # Close afterwards, so a request body is never taken for the next
            # request on a kept-alive connection; and read it first (up to 1 MB),
            # because on Windows a socket closed with unread data is reset and
            # the sender gets "connection aborted" instead of this answer.
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if 0 < length <= 1024 * 1024:
                self.rfile.read(length)
            body = "Bản sao chỉ đọc, không nhận lệnh ghi.".encode("utf-8")
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (web_root / name).resolve()
            if web_root not in target.parents or not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
                return
            kind = STATIC.get(target.suffix) or mimetypes.guess_type(target.name)[0] \
                or "application/octet-stream"
            self._send(HTTPStatus.OK, target.read_bytes(), kind)

        def _stream(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            version, data = twin.current()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while True:
                    self.wfile.write(b"data: " + data + b"\n\n")
                    self.wfile.flush()
                    new_version, data = twin.wait_newer(version, PUSH_EVERY_S * 2)
                    if twin._stop.is_set():
                        return
                    if new_version == version:
                        self.wfile.write(b": con song\n\n")  # keep-alive comment
                        self.wfile.flush()
                        new_version, data = twin.current()
                    version = new_version
            except (ConnectionError, OSError):
                return

    return Handler


def serve(host: str, port: int, twin: Twin, web_dir: Path,
          sim_port: int | None = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(twin, web_dir, sim_port))
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Máy chủ bản sao số").parse_args(argv)
    common.setup_logging("ban-sao")
    net = common.network()
    broker = net["broker"]
    model = ban_ve.model_in_use()
    config, where = ban_ve.signals_for(model)
    log.info("model %s, signals from %s", (model.get("ban_ve") or {}).get("id", "tham-khao"), where)
    twin = Twin(config, model, (broker["host"], broker["port"]),
                event_log=common.LOG_DIR / "su-kien.jsonl", minute_dir=common.LOG_DIR)
    twin.start()
    web = net["web"]
    httpd = serve(web["host"], web["port"], twin, common.WEB_DIR, net["mo_phong_web"]["port"])
    log.info("twin on http://%s:%s/", web["host"], web["port"])
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        twin.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
