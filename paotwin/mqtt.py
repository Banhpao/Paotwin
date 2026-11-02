"""MQTT 3.1.1, the small part a demo needs: a broker and a client.

Written with the standard library only, so the demo installs nothing.

What is covered (OASIS MQTT 3.1.1):
- CONNECT / CONNACK with a Last Will, clean session only.
- PUBLISH at QoS 0, and QoS 1 accepted from a client (answered with PUBACK,
  delivered onward at QoS 0). Retained messages, empty payload clears one.
- SUBSCRIBE / UNSUBSCRIBE with the ``+`` and ``#`` wildcards, granted at QoS 0.
- PINGREQ / PINGRESP, keep-alive enforced at 1.5 x the client's interval.
- The Will is published when a client goes away without DISCONNECT, which is
  how the twin learns that the gateway is gone.

Not covered: QoS 2, persistent sessions, authentication, TLS. The broker binds
to 127.0.0.1 by default. A real installation would use a full broker
(Mosquitto, EMQX) with TLS and accounts.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("paotwin.mqtt")

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14

MAX_PACKET = 256 * 1024          # far more than any message in this demo
MAX_CLIENT_BUFFER = 1024 * 1024  # a client this far behind is dropped


class ProtocolError(Exception):
    """The peer sent something MQTT 3.1.1 does not allow."""


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def encode_length(n: int) -> bytes:
    if n < 0 or n > 268_435_455:
        raise ValueError("remaining length out of range")
    out = bytearray()
    while True:
        digit, n = n % 128, n // 128
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("string too long")
    return struct.pack(">H", len(raw)) + raw


def encode_bin(raw: bytes) -> bytes:
    if len(raw) > 65535:
        raise ValueError("data too long")
    return struct.pack(">H", len(raw)) + raw


def packet(kind: int, flags: int, body: bytes) -> bytes:
    return bytes([(kind << 4) | flags]) + encode_length(len(body)) + body


def connect_packet(client_id: str, keepalive: int, will: tuple[str, bytes, bool] | None) -> bytes:
    flags = 0x02  # clean session
    payload = encode_str(client_id)
    if will is not None:
        topic, message, retain = will
        flags |= 0x04 | (0x20 if retain else 0)
        payload += encode_str(topic) + encode_bin(message)
    body = encode_str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + payload
    return packet(CONNECT, 0, body)


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    return packet(PUBLISH, 0x01 if retain else 0, encode_str(topic) + payload)


def subscribe_packet(packet_id: int, filters: list[str]) -> bytes:
    body = struct.pack(">H", packet_id) + b"".join(encode_str(f) + b"\x00" for f in filters)
    return packet(SUBSCRIBE, 0x02, body)


class _Reader:
    """Reads fields out of one packet body."""

    def __init__(self, body: bytes) -> None:
        self.body, self.pos = body, 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.body):
            raise ProtocolError("packet shorter than its fields")
        part = self.body[self.pos:self.pos + n]
        self.pos += n
        return part

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def bin(self) -> bytes:
        return self.take(self.u16())

    def str(self) -> str:
        try:
            text = self.bin().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("string is not UTF-8") from exc
        if "\x00" in text:
            raise ProtocolError("string contains U+0000")
        return text

    def rest(self) -> bytes:
        part = self.body[self.pos:]
        self.pos = len(self.body)
        return part

    def done(self) -> bool:
        return self.pos >= len(self.body)


def valid_topic(topic: str) -> bool:
    return bool(topic) and "+" not in topic and "#" not in topic


def valid_filter(flt: str) -> bool:
    if not flt:
        return False
    levels = flt.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


def topic_matches(flt: str, topic: str) -> bool:
    """MQTT 3.1.1 section 4.7 matching, including the ``$`` rule."""
    f_levels, t_levels = flt.split("/"), topic.split("/")
    if topic.startswith("$") and f_levels[0] in ("+", "#"):
        return False
    for i, level in enumerate(f_levels):
        if level == "#":
            return True
        if i >= len(t_levels):
            return False
        if level != "+" and level != t_levels[i]:
            return False
    return len(f_levels) == len(t_levels)


# --------------------------------------------------------------------------
# Broker
# --------------------------------------------------------------------------

@dataclass
class _Session:
    client_id: str
    writer: asyncio.StreamWriter
    keepalive: int
    will: tuple[str, bytes, bool] | None
    subscriptions: set[str] = field(default_factory=set)
    clean_exit: bool = False
    closed: bool = False


class Broker:
    """An asyncio MQTT 3.1.1 broker. ``serve()`` runs until cancelled."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1884) -> None:
        self.host, self.port = host, port
        self.sessions: dict[str, _Session] = {}
        self.retained: dict[str, bytes] = {}
        self._ids = itertools.count(1)
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("broker listening on %s:%s", self.host, self.port)

    async def serve(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for session in list(self.sessions.values()):
            session.clean_exit = True
            session.writer.close()

    # -- one connection ---------------------------------------------------

    async def _read_packet(self, reader: asyncio.StreamReader, timeout: float | None):
        head = await asyncio.wait_for(reader.readexactly(1), timeout)
        length, multiplier = 0, 1
        for _ in range(4):
            byte = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        else:
            raise ProtocolError("remaining length longer than 4 bytes")
        if length > MAX_PACKET:
            raise ProtocolError("packet too large")
        body = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
        return head[0] >> 4, head[0] & 0x0F, body

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session: _Session | None = None
        try:
            kind, _flags, body = await self._read_packet(reader, 10.0)
            if kind != CONNECT:
                raise ProtocolError("first packet is not CONNECT")
            session = self._connect(body, writer)
            if session is None:
                return
            timeout = session.keepalive * 1.5 if session.keepalive else None
            while True:
                kind, flags, body = await self._read_packet(reader, timeout)
                if not self._dispatch(session, kind, flags, body):
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except asyncio.TimeoutError:
            if session is not None:
                log.info("client %s missed its keep-alive", session.client_id)
        except ProtocolError as exc:
            log.warning("protocol error from %s: %s",
                        session.client_id if session else "?", exc)
        finally:
            self._drop(session, writer)

    def _connect(self, body: bytes, writer: asyncio.StreamWriter) -> _Session | None:
        r = _Reader(body)
        name, level, flags, keepalive = r.str(), r.u8(), r.u8(), r.u16()
        if name != "MQTT" or level != 4:
            writer.write(packet(CONNACK, 0, b"\x00\x01"))  # unacceptable protocol
            writer.close()
            return None
        if flags & 0x01:
            raise ProtocolError("reserved connect flag set")
        client_id = r.str()
        will = None
        if flags & 0x04:
            will = (r.str(), r.bin(), bool(flags & 0x20))
            if not valid_topic(will[0]):
                raise ProtocolError("invalid will topic")
        if flags & 0x80:
            r.str()  # user name: accepted, not checked (demo, localhost)
        if flags & 0x40:
            r.bin()
        if not client_id:
            if not flags & 0x02:
                writer.write(packet(CONNACK, 0, b"\x00\x02"))  # identifier rejected
                writer.close()
                return None
            client_id = f"auto-{next(self._ids)}"
        old = self.sessions.get(client_id)
        if old is not None:  # take-over (3.1.4-2): the old link is closed
            old.clean_exit = True
            old.writer.close()
        session = _Session(client_id, writer, keepalive, will)
        self.sessions[client_id] = session
        writer.write(packet(CONNACK, 0, b"\x00\x00"))
        log.info("client %s connected (keep-alive %ss)", client_id, keepalive)
        return session

    def _dispatch(self, s: _Session, kind: int, flags: int, body: bytes) -> bool:
        r = _Reader(body)
        if kind == PUBLISH:
            qos = (flags >> 1) & 0x03
            if qos == 3:
                raise ProtocolError("QoS 3")
            if qos == 2:
                raise ProtocolError("QoS 2 is not supported by this demo broker")
            topic = r.str()
            if not valid_topic(topic):
                raise ProtocolError("invalid topic")
            if qos == 1:
                packet_id = r.u16()
                s.writer.write(packet(PUBACK, 0, struct.pack(">H", packet_id)))
            self.publish(topic, r.rest(), bool(flags & 0x01))
        elif kind == SUBSCRIBE:
            if flags != 0x02:
                raise ProtocolError("bad SUBSCRIBE flags")
            packet_id, filters = r.u16(), []
            while not r.done():
                flt = r.str()
                r.u8()
                filters.append(flt)
            if not filters:
                raise ProtocolError("SUBSCRIBE without a filter")
            codes = bytearray()
            for flt in filters:
                if valid_filter(flt):
                    s.subscriptions.add(flt)
                    codes.append(0x00)
                else:
                    codes.append(0x80)
            s.writer.write(packet(SUBACK, 0, struct.pack(">H", packet_id) + bytes(codes)))
            for flt in filters:
                if not valid_filter(flt):
                    continue
                for topic, payload in self.retained.items():
                    if topic_matches(flt, topic):
                        s.writer.write(publish_packet(topic, payload, retain=True))
        elif kind == UNSUBSCRIBE:
            if flags != 0x02:
                raise ProtocolError("bad UNSUBSCRIBE flags")
            packet_id = r.u16()
            while not r.done():
                s.subscriptions.discard(r.str())
            s.writer.write(packet(UNSUBACK, 0, struct.pack(">H", packet_id)))
        elif kind == PINGREQ:
            s.writer.write(packet(PINGRESP, 0, b""))
        elif kind == DISCONNECT:
            s.clean_exit = True
            return False
        elif kind == PUBACK:
            pass
        else:
            raise ProtocolError(f"unexpected packet type {kind}")
        return True

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        data = publish_packet(topic, payload)
        for session in list(self.sessions.values()):
            if session.closed:
                continue
            if any(topic_matches(f, topic) for f in session.subscriptions):
                transport = session.writer.transport
                if transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
                    log.warning("client %s is too far behind, dropping it", session.client_id)
                    session.writer.close()
                    continue
                session.writer.write(data)

    def _drop(self, session: _Session | None, writer: asyncio.StreamWriter) -> None:
        writer.close()
        if session is None or session.closed:
            return
        session.closed = True
        if self.sessions.get(session.client_id) is session:
            del self.sessions[session.client_id]
        if not session.clean_exit and session.will is not None:
            topic, message, retain = session.will
            log.info("client %s left without DISCONNECT, publishing its will", session.client_id)
            self.publish(topic, message, retain)
        log.info("client %s disconnected", session.client_id)


# --------------------------------------------------------------------------
# Client (blocking sockets, one reader thread, reconnects on its own)
# --------------------------------------------------------------------------

MessageHandler = Callable[[str, bytes, bool], None]


class Client:
    """A small MQTT client that keeps itself connected.

    ``on_message(topic, payload, retained)`` runs on the reader thread.
    ``on_connect()`` runs after every successful connect, before messages,
    so a caller can publish its "online" status there.
    """

    def __init__(self, client_id: str, host: str = "127.0.0.1", port: int = 1884, *,
                 keepalive: int = 10, will: tuple[str, bytes, bool] | None = None,
                 subscriptions: list[str] | None = None,
                 on_message: MessageHandler | None = None,
                 on_connect: Callable[[], None] | None = None,
                 on_disconnect: Callable[[], None] | None = None) -> None:
        self.client_id, self.host, self.port = client_id, host, port
        self.keepalive, self.will = keepalive, will
        self.subscriptions = list(subscriptions or [])
        self.on_message, self.on_connect, self.on_disconnect = on_message, on_connect, on_disconnect
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sent = 0.0
        self._last_recv = 0.0
        self._last_ping = 0.0

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> "Client":
        self._thread = threading.Thread(target=self._run, name=f"mqtt-{self.client_id}",
                                        daemon=True)
        self._thread.start()
        return self

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def stop(self, clean: bool = True) -> None:
        """``clean=False`` closes the socket without DISCONNECT (the will fires)."""
        self._stop.set()
        with self._lock:
            sock = self._sock
            if sock is not None and clean and self._connected.is_set():
                try:
                    sock.sendall(packet(DISCONNECT, 0, b""))
                except OSError:
                    pass
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        if self._thread is not None:
            self._thread.join(5)

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        """Sends now if connected; returns False (and drops it) otherwise."""
        return self._send(publish_packet(topic, payload, retain))

    def _send(self, data: bytes) -> bool:
        with self._lock:
            if self._sock is None or not self._connected.is_set():
                return False
            try:
                self._sock.sendall(data)
                self._last_sent = time.monotonic()
                return True
            except OSError:
                return False

    def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            try:
                self._session()
                delay = 0.5
            except (OSError, ProtocolError, ConnectionError) as exc:
                log.debug("mqtt %s: %s", self.client_id, exc)
            finally:
                was = self._connected.is_set()
                self._connected.clear()
                with self._lock:
                    if self._sock is not None:
                        self._sock.close()
                        self._sock = None
                if was and self.on_disconnect:
                    self.on_disconnect()
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 5.0)

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            try:
                chunk = sock.recv(n - len(data))
            except socket.timeout:
                if self._stop.is_set():
                    raise ConnectionError("stopping")
                self._maybe_ping()
                continue
            if not chunk:
                raise ConnectionError("broker closed the connection")
            data += chunk
        return bytes(data)

    def _read_packet(self, sock: socket.socket):
        head = self._recv_exact(sock, 1)[0]
        length, multiplier = 0, 1
        for _ in range(4):
            byte = self._recv_exact(sock, 1)[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        else:
            raise ProtocolError("remaining length longer than 4 bytes")
        if length > MAX_PACKET:
            raise ProtocolError("packet too large")
        body = self._recv_exact(sock, length) if length else b""
        self._last_recv = time.monotonic()
        return head >> 4, head & 0x0F, body

    def _maybe_ping(self) -> None:
        if not self.keepalive:
            return
        now = time.monotonic()
        if now - self._last_recv > self.keepalive * 1.5:
            # The broker answers every PINGREQ, so this much silence means the
            # link is dead even if the socket still looks open.
            raise ConnectionError("broker silent for 1.5 x keep-alive")
        # Ping when either direction has been quiet for half the interval:
        # a client that only publishes still needs PINGRESP to prove the link.
        quiet = max(now - self._last_recv, now - self._last_sent)
        if quiet >= self.keepalive / 2 and now - self._last_ping >= self.keepalive / 2:
            self._last_ping = now
            self._send(packet(PINGREQ, 0, b""))

    def _session(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._sock = sock
        sock.sendall(connect_packet(self.client_id, self.keepalive, self.will))
        self._last_sent = self._last_recv = time.monotonic()
        self._last_ping = 0.0
        kind, _flags, body = self._read_packet(sock)
        if kind != CONNACK or len(body) != 2 or body[1] != 0:
            raise ProtocolError(f"connection refused ({body!r})")
        sock.settimeout(0.5)
        self._connected.set()
        if self.subscriptions:
            self._send(subscribe_packet(1, self.subscriptions))
        if self.on_connect:
            self.on_connect()
        while not self._stop.is_set():
            kind, flags, body = self._read_packet(sock)
            self._maybe_ping()
            if kind == PUBLISH:
                r = _Reader(body)
                topic = r.str()
                if (flags >> 1) & 0x03:
                    r.u16()
                if self.on_message:
                    try:
                        self.on_message(topic, r.rest(), bool(flags & 0x01))
                    except Exception:  # a handler bug must not drop the link
                        log.exception("message handler failed for %s", topic)
            elif kind in (SUBACK, UNSUBACK, PINGRESP, PUBACK):
                continue
            else:
                raise ProtocolError(f"unexpected packet type {kind} from broker")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Broker MQTT 3.1.1 tối giản cho demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1884)
    args = parser.parse_args(argv)
    from . import common

    common.setup_logging("broker")
    try:
        asyncio.run(Broker(args.host, args.port).serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
