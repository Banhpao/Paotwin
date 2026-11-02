import asyncio
import socket
import threading
import time
import unittest

from paotwin import mqtt


class BrokerThread:
    """A broker on its own loop thread, on a free port."""

    def __init__(self) -> None:
        self.broker = mqtt.Broker("127.0.0.1", 0)
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.broker.start())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(5)
        self.port = self.broker.port

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self.broker.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def wait_until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


class Collector:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()

    def __call__(self, topic, payload, retained):
        with self.lock:
            self.items.append((topic, payload, retained))

    def topics(self):
        with self.lock:
            return [t for t, _, _ in self.items]


class EncodingTest(unittest.TestCase):
    def test_remaining_length_examples_from_the_standard(self):
        self.assertEqual(mqtt.encode_length(0), b"\x00")
        self.assertEqual(mqtt.encode_length(127), b"\x7f")
        self.assertEqual(mqtt.encode_length(128), b"\x80\x01")
        self.assertEqual(mqtt.encode_length(16383), b"\xff\x7f")
        self.assertEqual(mqtt.encode_length(16384), b"\x80\x80\x01")
        self.assertEqual(mqtt.encode_length(268_435_455), b"\xff\xff\xff\x7f")

    def test_topic_matching(self):
        m = mqtt.topic_matches
        self.assertTrue(m("paotwin/#", "paotwin/laptop/cpu_tai"))
        self.assertTrue(m("paotwin/#", "paotwin"))
        self.assertTrue(m("paotwin/+/cpu_tai", "paotwin/laptop/cpu_tai"))
        self.assertFalse(m("paotwin/+", "paotwin/laptop/cpu_tai"))
        self.assertFalse(m("paotwin/laptop", "paotwin/laptop/cpu_tai"))
        self.assertTrue(m("+/+", "/finance"))
        self.assertFalse(m("#", "$SYS/broker"))
        self.assertFalse(m("+/broker", "$SYS/broker"))
        self.assertTrue(m("$SYS/#", "$SYS/broker"))

    def test_filter_validation(self):
        self.assertTrue(mqtt.valid_filter("a/+/b/#"))
        self.assertFalse(mqtt.valid_filter("a/#/b"))
        self.assertFalse(mqtt.valid_filter("a/b#"))
        self.assertFalse(mqtt.valid_filter("a+/b"))
        self.assertFalse(mqtt.valid_filter(""))


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.server = BrokerThread()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.stop()
        self.server.close()

    def client(self, name, **kwargs):
        client = mqtt.Client(name, "127.0.0.1", self.server.port, **kwargs).start()
        self.clients.append(client)
        self.assertTrue(client.wait_connected(5))
        return client

    def test_publish_reaches_matching_subscribers_only(self):
        seen, other = Collector(), Collector()
        self.client("a", subscriptions=["paotwin/laptop/#"], on_message=seen)
        self.client("b", subscriptions=["khac/#"], on_message=other)
        time.sleep(0.2)
        pub = self.client("pub")
        pub.publish("paotwin/laptop/cpu_tai", b'{"v":1}')
        self.assertTrue(wait_until(lambda: seen.topics() == ["paotwin/laptop/cpu_tai"]))
        time.sleep(0.2)
        self.assertEqual(other.topics(), [])
        self.assertEqual(seen.items[0][1], b'{"v":1}')
        self.assertFalse(seen.items[0][2])

    def test_retained_message_is_given_to_a_late_subscriber(self):
        pub = self.client("pub")
        pub.publish("paotwin/gateway/trang_thai", b"online", retain=True)
        self.assertTrue(wait_until(lambda: "paotwin/gateway/trang_thai" in self.server.broker.retained))
        late = Collector()
        self.client("late", subscriptions=["paotwin/#"], on_message=late)
        self.assertTrue(wait_until(lambda: late.items))
        self.assertEqual(late.items[0], ("paotwin/gateway/trang_thai", b"online", True))
        pub.publish("paotwin/gateway/trang_thai", b"", retain=True)
        self.assertTrue(wait_until(lambda: not self.server.broker.retained))

    def test_will_is_published_when_a_client_vanishes(self):
        watcher = Collector()
        self.client("watch", subscriptions=["paotwin/gateway/#"], on_message=watcher)
        gone = self.client("gw", will=("paotwin/gateway/trang_thai", b"offline", True))
        time.sleep(0.2)
        gone.stop(clean=False)
        self.clients.remove(gone)
        self.assertTrue(wait_until(lambda: watcher.items))
        self.assertEqual(watcher.items[-1][:2], ("paotwin/gateway/trang_thai", b"offline"))
        self.assertEqual(self.server.broker.retained["paotwin/gateway/trang_thai"], b"offline")

    def test_clean_disconnect_does_not_publish_the_will(self):
        watcher = Collector()
        self.client("watch", subscriptions=["paotwin/#"], on_message=watcher)
        polite = self.client("gw", will=("paotwin/gateway/trang_thai", b"offline", True))
        time.sleep(0.2)
        polite.stop(clean=True)
        self.clients.remove(polite)
        time.sleep(0.5)
        self.assertEqual(watcher.items, [])

    def test_silent_client_is_dropped_after_one_and_a_half_keepalives(self):
        watcher = Collector()
        self.client("watch", subscriptions=["paotwin/#"], on_message=watcher)
        raw = socket.create_connection(("127.0.0.1", self.server.port))
        raw.sendall(mqtt.connect_packet("mute", 1, ("paotwin/gateway/trang_thai", b"offline", False)))
        self.assertEqual(raw.recv(4), b"\x20\x02\x00\x00")
        started = time.monotonic()
        self.assertTrue(wait_until(lambda: watcher.items, timeout=4))
        self.assertGreaterEqual(time.monotonic() - started, 1.3)
        raw.close()

    def test_client_reconnects_after_the_link_drops(self):
        connects = []
        client = self.client("re", on_connect=lambda: connects.append(1))
        session = self.server.broker.sessions["re"]
        self.server.loop.call_soon_threadsafe(session.writer.close)
        self.assertTrue(wait_until(lambda: len(connects) >= 2, timeout=6))
        self.assertTrue(client.connected)

    def test_a_client_that_only_publishes_stays_connected(self):
        # Found on the first full run: the gateway publishes every 2 s, never
        # pinged, so it heard nothing back and dropped its own link every 15 s.
        connects = []
        client = self.client("talker", keepalive=1, on_connect=lambda: connects.append(1))
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            client.publish("paotwin/x", b"1")
            time.sleep(0.2)
        self.assertEqual(len(connects), 1)
        self.assertTrue(client.connected)

    def test_garbage_is_refused(self):
        raw = socket.create_connection(("127.0.0.1", self.server.port))
        raw.settimeout(3)
        raw.sendall(b"\x30\x02\x00\x00")  # PUBLISH before CONNECT
        self.assertEqual(raw.recv(10), b"")
        raw.close()


if __name__ == "__main__":
    unittest.main()
