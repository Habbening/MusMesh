"""GUI tests. Store and radio adapters need nothing extra. The screen tests need MUSMESH_BUILD (an
extracted x86-64 test build), MUSMESH_FONT (any .ttf), and SDL2 and SDL2_ttf: installed, or in the
folder MUSMESH_SDL_DIR names.
Run on Linux: tests/run_wsl.sh sets both.
"""
import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.environ.get("MUSMESH_BUILD")
sys.path.insert(0, os.path.join(ROOT, "gui"))
import radios  # noqa: E402
import store as store_module  # noqa: E402
from store import Store  # noqa: E402


def drain(store):
    while store.pump():
        pass
    return store


class StoreTest(unittest.TestCase):
    def test_messages_unread_and_open_conversation(self):
        s = Store()
        s.post(type="channel", key="ch:0", title="Primary")
        s.post(type="message", key="ch:0", title="Channel 0", kind="channel", sender="A", text="hi", time=5)
        s.post(type="message", key="dm:!1", title="Bob", kind="dm", sender="Bob", text="yo", time=9)
        drain(s)
        self.assertEqual(s.conversations["ch:0"].title, "Primary")  # a message does not rename a channel
        self.assertEqual(s.unread(), 2)
        s.open("dm:!1")
        self.assertEqual(s.unread(), 1)
        s.post(type="message", key="dm:!1", title="Bob", kind="dm", sender="Bob", text="again")
        drain(s)
        self.assertEqual(s.conversations["dm:!1"].unread, 0)  # on screen, so read
        self.assertEqual([c.key for c in s.ordered()], ["ch:0", "dm:!1"])

    def test_own_message_status_and_cap(self):
        s = Store()
        s.post(type="message", key="ch:1", kind="channel", sender="Me", text="x", mine=True, status="sending", id=5)
        s.post(type="sent", key="ch:1", status="failed")  # no id: ignored
        s.post(type="sent", key="ch:1", id=5, status="failed")
        drain(s)
        self.assertEqual(s.conversations["ch:1"].messages[-1].status, "failed")
        self.assertEqual(s.unread(), 0)
        for i in range(store_module.MAX_MESSAGES + 20):
            s.post(type="message", key="ch:1", kind="channel", sender="A", text=str(i))
        drain(s)
        self.assertEqual(len(s.conversations["ch:1"].messages), store_module.MAX_MESSAGES)
        self.assertEqual(s.conversations["ch:1"].messages[-1].text, str(store_module.MAX_MESSAGES + 19))

    def test_node_updates_keep_known_fields_and_rename_dm(self):
        s = Store()
        s.post(type="node", key="!1", name="!1", battery=50, snr=1.0, last_heard=10)
        s.post(type="message", key="dm:!1", title="!1", kind="dm", sender="!1", text="hi")
        s.post(type="node", key="!1", name="Alice", battery=None, snr=None, last_heard=20)
        drain(s)
        node = s.nodes["!1"]
        self.assertEqual((node.name, node.battery, node.snr, node.last_heard), ("Alice", 50, 1.0, 20))
        self.assertEqual(s.conversations["dm:!1"].title, "Alice")

    def test_radio_text_is_cleaned(self):
        s = Store()
        s.post(type="node", key="!1", name="Nul\x00name\x1b[31m", short="A\x07B")
        s.post(type="message", key="dm:!1", title="T\x00itle\nx", kind="dm", sender="S\x00", text="a\x00b\nc\x7f")
        s.post(type="channel", key="ch:0", title="Pri\x00mary")
        s.post(type="me", name="M\x00e")
        drain(s)
        self.assertEqual((s.nodes["!1"].name, s.nodes["!1"].short), ("Nulname[31m", "AB"))
        conv = s.conversations["dm:!1"]
        self.assertEqual(conv.title, "Title x")
        self.assertEqual((conv.messages[0].sender, conv.messages[0].text), ("S", "ab\nc"))
        self.assertEqual((s.conversations["ch:0"].title, s.me), ("Primary", "Me"))
        s.post(type="node", key="!2", name="x" * 500)
        drain(s)
        self.assertEqual(len(s.nodes["!2"].name), store_module.LONGEST_NAME)

    def test_sent_matches_its_own_message(self):
        s = Store()
        for sid in (1, 2):
            s.post(type="message", key="ch:0", kind="channel", sender="Me", text=str(sid), mine=True,
                   status="sending", id=sid)
        s.post(type="sent", key="ch:0", id=1, status="failed")
        drain(s)
        self.assertEqual([m.status for m in s.conversations["ch:0"].messages], ["failed", "sending"])

    def test_notice_keeps_the_link_and_drop_node(self):
        s = Store()
        s.post(type="status", connected=True, text="Connected")
        s.post(type="notice", text="Send failed: x")
        s.post(type="node", key="!me", name="Me")
        s.post(type="drop_node", key="!me")
        drain(s)
        self.assertTrue(s.connected)
        self.assertEqual(s.current_notice(), "Send failed: x")
        self.assertEqual(s.nodes, {})

    def test_error_marks_disconnected(self):
        s = Store()
        s.post(type="status", connected=True, text="Connected")
        s.post(type="error", text="Could not connect: boom")
        drain(s)
        self.assertFalse(s.connected)
        self.assertIn("boom", s.error)


class FakeInterface:
    def __init__(self):
        self.sent = []
        self.nodes = {
            "!me": {"user": {"id": "!me", "longName": "My node"}},
            "!b0b": {"user": {"id": "!b0b", "longName": "Bob", "shortName": "BOB"}, "lastHeard": 100,
                     "snr": 3.5, "hopsAway": 1, "deviceMetrics": {"batteryLevel": 77}},
        }

    def sendText(self, text, **kwargs):
        self.sent.append((text, kwargs))


class MeshtasticAdapterTest(unittest.TestCase):
    def radio(self):
        s = Store()
        r = radios.MeshtasticRadio("/dev/ttyACM0", s)
        r.iface = FakeInterface()
        r.my_id = "!me"
        return r, s

    def test_text_to_me_is_a_dm_and_others_go_to_their_channel(self):
        r, s = self.radio()
        r._on_text({"decoded": {"text": "hello"}, "fromId": "!b0b", "toId": "!me", "rxTime": 50})
        r._on_text({"decoded": {"text": "all"}, "fromId": "!b0b", "toId": "^all", "channel": 2})
        r._on_text({"decoded": {"text": "primary"}, "fromId": "!b0b", "toId": "^all"})
        r._on_text({"decoded": {}, "fromId": "!b0b"})  # not text: ignored
        drain(s)
        dm = s.conversations["dm:!b0b"]
        self.assertEqual((dm.kind, dm.title, dm.messages[0].sender, dm.messages[0].time), ("dm", "Bob", "Bob", 50))
        self.assertEqual(s.conversations["ch:2"].messages[0].text, "all")
        self.assertEqual(s.conversations["ch:0"].messages[0].text, "primary")
        self.assertEqual(len(s.conversations), 3)

    def test_nodes_skip_self(self):
        r, s = self.radio()
        for node in r.iface.nodes.values():
            r._post_node(node)
        drain(s)
        self.assertEqual(list(s.nodes), ["!b0b"])
        bob = s.nodes["!b0b"]
        self.assertEqual((bob.short, bob.battery, bob.hops, bob.snr), ("BOB", 77, 1, 3.5))

    def test_send_channel_and_dm(self):
        r, s = self.radio()
        r._send("ch:1", "ch", "1", "to channel", 1)
        r._send("dm:!b0b", "dm", "!b0b", "to bob", 2)
        self.assertEqual(r.iface.sent, [("to channel", {"channelIndex": 1}),
                                        ("to bob", {"destinationId": "!b0b", "wantAck": True})])

    def test_failed_send_is_a_notice_not_a_disconnect(self):
        r, s = self.radio()
        r.iface.sendText = lambda *a, **k: (_ for _ in ()).throw(OSError("port gone"))
        s.post(type="status", connected=True, text="Connected")
        s.post(type="message", key="ch:0", kind="channel", sender="Me", text="x", mine=True, status="sending", id=9)
        r._send("ch:0", "ch", "0", "x", 9)
        drain(s)
        self.assertEqual(s.conversations["ch:0"].messages[-1].status, "failed")
        self.assertIn("port gone", s.current_notice())
        self.assertTrue(s.connected)

    def test_own_node_skipped_during_config(self):
        # Before the constructor returns, my_id is unknown; the interface already knows our number.
        r, s = self.radio()
        r.my_id = None
        iface = types.SimpleNamespace(myInfo=types.SimpleNamespace(my_node_num=42))
        r.iface = None
        r._on_node({"num": 42, "user": {"id": "!me", "longName": "Me"}}, interface=iface)
        r._on_node({"num": 7, "user": {"id": "!b0b", "longName": "Bob"}}, interface=iface)
        drain(s)
        self.assertEqual(list(s.nodes), ["!b0b"])

    def test_events_from_another_interface_are_ignored(self):
        r, s = self.radio()
        other = object()
        r._on_text({"decoded": {"text": "old"}, "fromId": "!b0b", "toId": "^all"}, interface=other)
        r._on_lost(interface=other)
        r.closed = True
        r._on_text({"decoded": {"text": "late"}, "fromId": "!b0b", "toId": "^all"}, interface=r.iface)
        self.assertFalse(drain(s).conversations)
        self.assertTrue(s.events.empty())


class FakePub:
    def __init__(self):
        self.listeners = {}

    def subscribe(self, listener, topic):
        self.listeners.setdefault(topic, []).append(listener)

    def isSubscribed(self, listener, topic):
        return listener in self.listeners.get(topic, [])

    def unsubscribe(self, listener, topic):
        self.listeners[topic].remove(listener)


class MeshtasticConnectTest(unittest.TestCase):
    """_run with stand-in pubsub and meshtastic modules."""

    def setUp(self):
        self.pub = FakePub()
        self.closed = []
        test = self

        class SerialInterface:
            def __init__(self, devPath):
                test.radio_under_test.closed = test.close_during_connect
                self.nodes = {"!me": {"num": 1, "user": {"id": "!me", "longName": "Me"}},
                              "!b0b": {"num": 2, "user": {"id": "!b0b", "longName": "Bob"},
                                       "deviceMetrics": {"batteryLevel": 101}}}
                self.localNode = types.SimpleNamespace(channels=[
                    types.SimpleNamespace(role=1, index=0, settings=types.SimpleNamespace(name="")),
                    types.SimpleNamespace(role=0, index=1, settings=types.SimpleNamespace(name="off")),
                    types.SimpleNamespace(role=2, index=2, settings=types.SimpleNamespace(name="Hike"))])

            def getMyNodeInfo(self):
                return {"num": 1, "user": {"id": "!me", "longName": "Me"}, "deviceMetrics": {"batteryLevel": 55}}

            def close(self):
                test.closed.append(self)

        modules = {"pubsub": types.SimpleNamespace(pub=self.pub),
                   "meshtastic": types.ModuleType("meshtastic"),
                   "meshtastic.serial_interface": types.SimpleNamespace(SerialInterface=SerialInterface)}
        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        self.addCleanup(lambda: [sys.modules.pop(n) if v is None else sys.modules.__setitem__(n, v)
                                 for n, v in saved.items()])

    def run_radio(self, close_during_connect=False):
        self.close_during_connect = close_during_connect
        s = Store()
        r = radios.MeshtasticRadio("/dev/ttyACM0", s)
        self.radio_under_test = r
        r._run()
        return r, drain(s)

    def test_connect_posts_me_channels_nodes(self):
        r, s = self.run_radio()
        self.assertTrue(s.connected)
        self.assertEqual((s.me, s.battery), ("Me", "55%"))
        self.assertEqual({k: c.title for k, c in s.conversations.items()}, {"ch:0": "Primary", "ch:2": "Hike"})
        self.assertEqual(list(s.nodes), ["!b0b"])
        self.assertEqual(s.nodes["!b0b"].battery, "powered")
        r.close()
        self.assertEqual(len(self.closed), 1)
        self.assertFalse(any(self.pub.listeners.values()))

    def test_cancel_while_connecting_releases_the_port(self):
        r, s = self.run_radio(close_during_connect=True)
        self.assertEqual(len(self.closed), 1)
        self.assertIsNone(r.iface)
        self.assertFalse(s.connected)

    def test_closed_radio_does_not_report_a_failed_connect(self):
        # After a reconnect the store belongs to the next radio: a late error must not reach it.
        serial = sys.modules["meshtastic.serial_interface"]

        def boom(devPath):
            self.radio_under_test.closed = self.close_during_connect
            raise OSError("no such port")
        serial.SerialInterface = boom
        r, s = self.run_radio(close_during_connect=True)
        self.assertEqual(s.error, "")
        r, s = self.run_radio()
        self.assertIn("no such port", s.error)


class MeshCoreAdapterTest(unittest.TestCase):
    def radio(self):
        s = Store()
        r = radios.MeshCoreRadio("/dev/ttyACM0", s)
        r.mc = types.SimpleNamespace(contacts={"aabbccddeeff00112233": {"public_key": "aabbccddeeff00112233",
                                                                        "adv_name": "Carol", "last_advert": 7}})
        return r, s

    def test_dm_uses_contact_name_and_channel_splits_sender(self):
        r, s = self.radio()
        ev = types.SimpleNamespace
        asyncio.run(r._on_dm(ev(payload={"pubkey_prefix": "aabbccddeeff", "text": "hey", "sender_timestamp": 9})))
        asyncio.run(r._on_channel(ev(payload={"channel_idx": 1, "text": "Dave: hello: there"})))
        asyncio.run(r._on_channel(ev(payload={"channel_idx": 0, "text": "no sender"})))
        drain(s)
        dm = s.conversations["dm:aabbccddeeff"]
        self.assertEqual((dm.title, dm.messages[0].text, dm.messages[0].time), ("Carol", "hey", 9))
        msg = s.conversations["ch:1"].messages[0]
        self.assertEqual((msg.sender, msg.text), ("Dave", "hello: there"))
        self.assertEqual(s.conversations["ch:0"].messages[0].sender, "?")

    def test_contacts_keyed_by_6_byte_prefix(self):
        r, s = self.radio()
        r._post_contact(r.mc.contacts["aabbccddeeff00112233"])
        drain(s)
        self.assertEqual(list(s.nodes), ["aabbccddeeff"])
        self.assertEqual(s.nodes["aabbccddeeff"].name, "Carol")

    def test_limits(self):
        self.assertLess(radios.MeshCoreRadio.limit, radios.MeshtasticRadio.limit)


class MeshCoreConnectTest(unittest.TestCase):
    """_connect with a stand-in meshcore module."""

    def setUp(self):
        self.disconnected = []
        test = self
        EventType = types.SimpleNamespace(**{n: n for n in (
            "ERROR", "CONTACT_MSG_RECV", "CHANNEL_MSG_RECV", "DISCONNECTED", "NEW_CONTACT", "ADVERTISEMENT")})
        self.EventType = EventType

        class FakeMC:
            self_info = {"name": "Core"}
            contacts = {"aabbccddeeff0011": {"public_key": "aabbccddeeff0011", "adv_name": "Carol"}}

            def __init__(self):
                self.subscribed = []
                self.commands = types.SimpleNamespace(get_channel=self.get_channel, get_bat=self.get_bat)

            def subscribe(self, kind, callback):
                self.subscribed.append(kind)

            async def ensure_contacts(self):
                if test.fail_contacts:
                    raise OSError("unplugged")

            async def get_channel(self, index):
                if index >= 2:
                    return types.SimpleNamespace(type="ERROR", payload={})
                return types.SimpleNamespace(type="CHANNEL_INFO", payload={"channel_name": ["Public", ""][index]})

            async def get_bat(self):
                return types.SimpleNamespace(type="BATTERY", payload={"level": 4012})

            async def start_auto_message_fetching(self):
                pass

            async def disconnect(self):
                test.disconnected.append(self)

        class MeshCore:
            @staticmethod
            async def create_serial(port, baud):
                if test.close_during_connect:
                    test.radio_under_test.closed = True
                return FakeMC()

        module = types.SimpleNamespace(MeshCore=MeshCore, EventType=EventType)
        saved = sys.modules.get("meshcore")
        sys.modules["meshcore"] = module
        self.addCleanup(lambda: sys.modules.pop("meshcore") if saved is None else
                        sys.modules.__setitem__("meshcore", saved))

    def run_radio(self, close_during_connect=False, fail_contacts=False):
        self.close_during_connect, self.fail_contacts = close_during_connect, fail_contacts
        s = Store()
        r = radios.MeshCoreRadio("/dev/ttyACM0", s)
        self.radio_under_test = r
        loop = asyncio.new_event_loop()
        r.loop = loop
        loop.create_task(r._connect())
        loop.call_later(1, loop.stop)
        loop.run_forever()
        loop.close()
        return r, drain(s)

    def test_connect_posts_me_channels_nodes_battery(self):
        r, s = self.run_radio()
        self.assertTrue(s.connected)
        self.assertEqual((s.me, s.battery), ("Core", "4.01 V"))
        self.assertEqual(list(s.conversations), ["ch:0"])  # unnamed channel 1 skipped
        self.assertEqual(list(s.nodes), ["aabbccddeeff"])
        self.assertIn("NEW_CONTACT", r.mc.subscribed)

    def test_cancel_while_connecting_disconnects(self):
        r, s = self.run_radio(close_during_connect=True)
        self.assertEqual(len(self.disconnected), 1)
        self.assertFalse(s.connected)

    def test_failure_after_connect_is_reported(self):
        r, s = self.run_radio(fail_contacts=True)
        self.assertFalse(s.connected)
        self.assertIn("unplugged", s.error)
        self.assertEqual(len(self.disconnected), 1)  # the port is let go


@unittest.skipUnless(BUILD and os.environ.get("MUSMESH_FONT"), "needs MUSMESH_BUILD and MUSMESH_FONT")
class ScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        sys.path[:0] = [os.path.join(BUILD, "vendor"), os.path.join(BUILD, "gui"), BUILD]
        cls.sdl = importlib.import_module("sdl")
        cls.musmesh = importlib.import_module("musmesh")
        cls.gui = importlib.import_module("app")
        # The bundled copy, not the repo one, so the shipped files are what is tested.
        assert cls.gui.__file__.startswith(BUILD), cls.gui.__file__

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.musmesh.DATA = tmp.name
        self.musmesh.ACCEPT_SETTLE = 0
        self.musmesh.PORT_GLOBS = (os.path.join(tmp.name, "ttyACM*"),)
        self.tmp = tmp.name

    def app(self, accepted=True, size=(640, 480)):
        if accepted:
            open(os.path.join(self.tmp, "storage-accepted"), "w").close()
        a = self.gui.App(self.sdl.set_mode(size))
        self.addCleanup(a.disconnect, False)
        return a

    def settle(self, a):
        for _ in range(5):
            a.store.pump()
            a.screen.tick()
        a.draw()

    def names(self, a):
        return [type(s).__name__ for s in a.stack]

    def demo(self, a):
        a.screen.selected = self.gui.DEMO
        a.handle("ok")
        self.settle(a)
        self.assertEqual(self.names(a)[-1], "MainScreen")

    def test_warning_first_then_remembered(self):
        a = self.app(accepted=False)
        self.assertEqual(self.names(a), ["WarningScreen"])
        a.draw()
        a.handle("down")  # not an answer
        self.assertEqual(self.names(a), ["WarningScreen"])
        a.handle("ok")
        self.assertEqual(self.names(a), ["HomeScreen"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "storage-accepted")))
        self.assertEqual(self.names(self.gui.App(a.surface)), ["HomeScreen"])

    def test_warning_b_quits(self):
        a = self.app(accepted=False)
        a.handle("back")
        self.assertEqual(a.exit_code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "storage-accepted")))

    def test_warning_shows_what_is_accepted(self):
        text = self.musmesh.STORAGE_WARNING
        for phrase in ("not encrypted", "repeater passwords", "/tmp/musmesh-contact.log", "root", "radio"):
            self.assertIn(phrase, text)

    def test_home_needs_a_node_and_remembers_choice(self):
        a = self.app()
        a.screen.tick()
        a.screen.selected = 1
        a.handle("ok")
        self.assertIn("No node found", a.screen.message)
        self.assertIsNone(a.radio)
        open(os.path.join(self.tmp, "ttyACM0"), "w").close()
        a.screen.last_scan = 0
        a.screen.tick()
        self.assertEqual(a.screen.port(), os.path.join(self.tmp, "ttyACM0"))
        started = []
        self.gui.RADIOS["meshcore"] = lambda port, store: types.SimpleNamespace(
            start=lambda: started.append(port), close=lambda: None, limit=140)
        self.addCleanup(self.gui.RADIOS.__setitem__, "meshcore", radios.MeshCoreRadio)
        a.handle("ok")
        self.assertEqual(started, [os.path.join(self.tmp, "ttyACM0")])
        self.assertEqual(self.musmesh.load_last_client(), 1)
        self.assertEqual(self.names(a), ["HomeScreen", "ConnectScreen"])
        a.store.post(type="error", text="No answer. Is this a MeshCore companion node?")
        self.settle(a)
        a.handle("back")
        self.assertEqual(self.names(a), ["HomeScreen"])
        self.assertEqual(self.gui.HomeScreen(a).selected, 1)

    def fake_radios(self):
        made = []

        def make(port, store):
            radio = types.SimpleNamespace(port=port, store=store, started=False, closed=False, limit=200,
                                          sent=[])
            radio.start = lambda: setattr(radio, "started", True)
            radio.close = lambda: setattr(radio, "closed", True)
            radio.send = lambda key, text: radio.sent.append((key, text))
            made.append(radio)
            return radio
        self.gui.RADIOS["meshtastic"] = make
        self.addCleanup(self.gui.RADIOS.__setitem__, "meshtastic", radios.MeshtasticRadio)
        return made

    def pass_(self, a, wait_closed=None):
        """One pass of the reconnect check, with the retry wait skipped."""
        a.store.pump()
        a.retry_at = 0
        changed = a.reconnect()
        if wait_closed is not None:
            for _ in range(200):  # the close runs in a thread
                if wait_closed.closed:
                    break
                self.gui.time.sleep(0.005)
        return changed

    def test_reconnects_when_the_node_comes_back_and_keeps_chats(self):
        made = self.fake_radios()
        port0, port1 = os.path.join(self.tmp, "ttyACM0"), os.path.join(self.tmp, "ttyACM1")
        open(port0, "w").close()
        a = self.app()
        a.connect("meshtastic", port0)
        store = a.store
        store.post(type="channel", key="ch:0", title="testnet")
        store.post(type="message", key="ch:0", kind="channel", sender="Bob", text="kept")
        store.post(type="status", connected=True, text="Connected")
        self.settle(a)
        self.assertEqual(self.names(a)[-1], "MainScreen")
        self.assertFalse(self.pass_(a))  # connected: nothing to do
        a.handle("ok")
        self.assertEqual(self.names(a)[-1], "ChatScreen")

        # Unplugged.
        os.remove(port0)
        store.post(type="status", connected=False, text="Connection lost")
        self.assertTrue(self.pass_(a, wait_closed=made[0]))
        self.assertTrue(made[0].closed)
        self.assertEqual(len(made), 1)
        self.assertTrue(self.pass_(a))
        self.assertIn("plug the node back in", store.status)
        self.assertEqual(len(made), 1)
        a.screen.send("into the void")
        self.settle(a)
        self.assertEqual(made[0].sent, [])
        self.assertIn("Not connected", store.current_notice())

        # Plugged back in under another name: wait for the node to start, then one new radio.
        open(port1, "w").close()
        self.assertTrue(self.pass_(a))
        self.assertEqual((store.status, len(made)), ("Reconnecting...", 1))
        a.seen_at -= self.gui.PORT_SETTLE
        self.assertTrue(self.pass_(a))
        self.assertEqual(len(made), 2)
        self.assertEqual((made[1].port, made[1].started, made[1].store is store), (port1, True, True))
        self.assertFalse(self.pass_(a))  # the try is running: no third radio
        self.assertEqual(len(made), 2)

        # That try fails: it is closed, and the pass after starts another.
        store.post(type="error", text="Could not connect: busy")
        self.assertTrue(self.pass_(a, wait_closed=made[1]))
        self.assertEqual(len(made), 2)
        self.assertTrue(self.pass_(a))
        self.assertEqual((len(made), store.error), (3, ""))

        store.post(type="status", connected=True, text="Connected")
        self.assertFalse(self.pass_(a))
        self.assertFalse(made[2].closed)
        self.assertIs(a.store, store)
        self.assertEqual(self.names(a)[-1], "ChatScreen")
        self.assertEqual(store.conversations["ch:0"].messages[0].text, "kept")
        a.screen.send("back")
        self.assertEqual(made[2].sent, [("ch:0", "back")])

    def test_no_reconnect_after_a_failed_first_connect_or_a_disconnect(self):
        made = self.fake_radios()
        port = os.path.join(self.tmp, "ttyACM0")
        open(port, "w").close()
        a = self.app()
        a.connect("meshtastic", port)
        a.store.post(type="error", text="Could not connect: boom")
        self.assertFalse(self.pass_(a))
        self.assertEqual((len(made), made[0].closed), (1, False))
        self.assertEqual(self.names(a)[-1], "ConnectScreen")
        a.connect("meshtastic", port)
        a.store.post(type="status", connected=True, text="Connected")
        self.pass_(a)
        a.disconnect()
        a.store.post(type="status", connected=False, text="Connection lost")
        self.assertFalse(self.pass_(a))
        self.assertEqual(len(made), 2)

    def test_terminal_and_quit_exit_codes(self):
        a = self.app()
        a.screen.selected = self.gui.TERMINAL
        a.handle("ok")
        self.assertEqual(a.exit_code, 3)
        a = self.app()
        a.screen.selected = self.gui.QUIT
        a.handle("ok")
        self.assertEqual(a.exit_code, 0)

    def test_demo_chat_write_and_send(self):
        a = self.app()
        self.demo(a)
        a.handle("ok")  # first chat: Primary
        self.assertEqual(self.names(a)[-1], "ChatScreen")
        self.assertEqual(a.store.conversations["ch:0"].unread, 0)
        a.handle("x")
        self.assertEqual(self.names(a)[-1], "KeyboardScreen")
        kb = a.screen
        kb.row, kb.col = 2, 0  # "a"
        a.handle("ok")
        a.handle("x")  # shift once
        kb.row, kb.col = 1, 0  # "Q" then back to lower case
        a.handle("ok")
        a.handle("y")
        kb.row, kb.col = 1, 1
        a.handle("ok")
        self.assertEqual(kb.text, "aQ w")
        a.handle("back")
        self.assertEqual(kb.text, "aQ ")
        a.draw()
        a.handle("start")
        self.settle(a)
        last = a.store.conversations["ch:0"].messages[-1]
        self.assertEqual((last.text, last.mine, last.status), ("aQ", True, "sent"))
        self.assertEqual(self.names(a)[-1], "ChatScreen")

    def test_keyboard_limit_cancel_and_specials(self):
        a = self.app()
        self.demo(a)
        sent = []
        kb = self.gui.KeyboardScreen(a, sent.append)
        a.push(kb)
        a.radio.limit = 5
        for _ in range(8):
            a.handle("y")
        self.assertEqual(kb.text, " " * 5)
        kb.text = "é" * 2  # 4 bytes
        kb.row, kb.col = 1, 0
        a.handle("ok")
        self.assertEqual(kb.text, "ééq")
        a.handle("ok")  # would be 6 bytes
        self.assertEqual(kb.text, "ééq")
        kb.row, kb.col = 4, 1  # odd column snaps to its key
        a.handle("right")
        self.assertEqual(kb.col % 2, 0)
        kb.col = 2  # "#+="
        a.handle("ok")
        self.assertEqual(kb.layer, "#+=")
        a.draw()
        kb.text = ""
        a.handle("back")  # empty: leaves the keyboard
        self.assertNotIn("KeyboardScreen", self.names(a))
        self.assertEqual(sent, [])

    def test_empty_message_is_not_sent(self):
        a = self.app()
        self.demo(a)
        sent = []
        a.push(self.gui.KeyboardScreen(a, sent.append, text="   "))
        a.handle("start")
        self.assertEqual(sent, [])

    def test_node_opens_dm(self):
        a = self.app()
        self.demo(a)
        a.handle("r1")
        a.draw()
        a.handle("ok")
        chat = a.screen
        self.assertEqual(type(chat).__name__, "ChatScreen")
        self.assertTrue(chat.key.startswith("dm:"))
        self.assertTrue(chat.title.startswith("@ "))

    def test_menu_from_main_disconnects(self):
        a = self.app()
        self.demo(a)
        a.handle("menu")
        self.assertEqual(self.names(a), ["HomeScreen"])
        self.assertIsNone(a.radio)

    def test_every_screen_draws_at_two_sizes(self):
        for size in ((640, 480), (1024, 768)):
            a = self.app(accepted=False, size=size)
            a.draw()
            a.handle("ok")
            a.draw()
            a.screen.selected = self.gui.CREDITS
            a.handle("ok")
            a.draw()
            a.handle("back")
            self.demo(a)
            a.handle("r1")
            a.draw()
            a.handle("l1")
            a.handle("x")
            a.draw()
            for key in ("up", "up", "left", "ok", "l1", "ok"):
                a.handle(key)
            a.draw()
            a.disconnect()

    def test_hostile_text_from_the_radio_draws(self):
        a = self.app()
        self.demo(a)
        bad = "Nul\x00name " + "W" * 80
        a.store.post(type="node", key="!bad", name=bad, short="\x00", battery="powered", snr=1.0, hops=3,
                     last_heard=1)
        a.store.post(type="message", key="dm:!bad", title=bad, kind="dm", sender=bad, text="hi\x00\x1b there\n")
        a.store.post(type="notice", text="Send failed: " + "x" * 200)
        self.settle(a)
        a.handle("r1")
        a.draw()
        a.handle("l1")
        a.draw()
        a.screen.selected = len(a.store.ordered()) - 1
        a.handle("ok")
        a.draw()

    def test_long_messages_wrap_inside_the_bubble(self):
        a = self.app()
        self.demo(a)
        a.store.post(type="message", key="ch:0", kind="channel", sender="X", text="word " * 60 + "x" * 200)
        a.handle("ok")
        self.settle(a)
        width = a.surface.get_width()
        for head, lines, mine, failed in a.screen.layout(width):
            for line in lines:
                self.assertLessEqual(a.fonts.normal.size(line)[0], width)

    def test_controller_and_raw_joystick_events(self):
        pg = self.sdl
        a = self.app()
        a.controllers = {7: object()}
        event = pg.Event(pg.CONTROLLERBUTTONDOWN, button=pg.BUTTON_A, instance_id=7)
        self.assertEqual(a.action_for(event), "ok")
        event = pg.Event(pg.CONTROLLERBUTTONDOWN, button=pg.BUTTON_GUIDE, instance_id=7)
        self.assertEqual(a.action_for(event), "menu")
        # A pad SDL has no mapping for: muOS-Keys numbers.
        self.assertEqual(a.action_for(pg.Event(pg.JOYBUTTONDOWN, button=3, instance_id=1)), "ok")
        self.assertEqual(a.action_for(pg.Event(pg.JOYBUTTONDOWN, button=2, instance_id=1)), "back")
        self.assertEqual(a.action_for(pg.Event(pg.JOYHATMOTION, value=(0, 1), instance_id=1)), "up")
        # A mapped pad's raw events are ignored, so a press does not count twice.
        self.assertIsNone(a.action_for(pg.Event(pg.JOYBUTTONDOWN, button=3, instance_id=7)))
        self.assertEqual(a.action_for(pg.Event(pg.KEYDOWN, key=pg.K_RETURN)), "ok")


    def test_sdl_events_are_read_from_their_bytes(self):
        import struct
        pg = self.sdl

        def raw(kind, layout="", *values):
            return struct.pack("<I4x" + layout, kind, *values).ljust(64, b"\0")
        key = pg._event(raw(pg.KEYDOWN, "4xBB6xi", 1, 0, pg.K_UP))
        self.assertEqual((key.type, key.key), (pg.KEYDOWN, pg.K_UP))
        self.assertIsNone(pg._event(raw(pg.KEYDOWN, "4xBB6xi", 1, 1, pg.K_UP)))  # a held key repeating
        button = pg._event(raw(pg.CONTROLLERBUTTONDOWN, "iB", 7, pg.BUTTON_DPAD_LEFT))
        self.assertEqual((button.instance_id, button.button), (7, pg.BUTTON_DPAD_LEFT))
        for bits, value in ((1, (0, 1)), (2, (1, 0)), (4, (0, -1)), (8, (-1, 0)), (0, (0, 0))):
            hat = pg._event(raw(pg.JOYHATMOTION, "iBB", 3, 0, bits))
            self.assertEqual((hat.instance_id, hat.value), (3, value))
        self.assertEqual(pg._event(raw(pg.QUIT)).type, pg.QUIT)
        self.assertIsNone(pg._event(raw(0x400)))  # mouse motion

    def test_drawing_reaches_the_canvas(self):
        import ctypes
        import struct
        a = self.app()
        a.draw()
        self.sdl.flip()
        raw = a.surface.raw.contents

        def pixel(x, y):
            return struct.unpack_from("<I", ctypes.string_at(raw.pixels + y * raw.pitch + x * 4, 4))[0] & 0xFFFFFF

        def rgb(colour):
            return colour[0] << 16 | colour[1] << 8 | colour[2]
        self.assertEqual(pixel(raw.w - 2, 2), rgb(self.gui.PANEL))  # title bar
        self.assertEqual(pixel(raw.w - 2, raw.h // 2), rgb(self.gui.BG))
        # The title is drawn in the text colour somewhere in the bar.
        bar = ctypes.string_at(raw.pixels, raw.pitch * 30)
        self.assertIn(rgb(self.gui.TEXT), {p & 0xFFFFFF for p in struct.unpack("<%dI" % (len(bar) // 4), bar)})
        # A rounded box leaves its corner alone and fills its middle.
        self.sdl.draw_rect(a.surface, (255, 0, 0), (100, 100, 40, 40), border_radius=10)
        self.assertEqual(pixel(100, 100), rgb(self.gui.BG))
        self.assertEqual(pixel(120, 100), 0xFF0000)
        self.assertEqual(pixel(100, 120), 0xFF0000)
        self.assertEqual(self.sdl.Font(os.environ["MUSMESH_FONT"], 20).render("", (0, 0, 0)).get_width(), 0)


if __name__ == "__main__":
    unittest.main()
