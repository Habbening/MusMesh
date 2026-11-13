"""Radio adapters. Each runs its client library in its own thread and posts events to the Store.
send() may be called from the UI thread.

Conversation keys: "ch:<index>" for channels, "dm:<node>" for direct messages, where <node> is
the Meshtastic node id ("!a1b2c3d4") or the first 6 bytes of a MeshCore public key in hex.

Copyright (C) 2026 Habbening. GPL-3.0-only, no warranty, see LICENSE.
"""
import asyncio
import threading
import time

from store import send_id

# Byte limits for one text message. Meshtastic's payload is about 230 bytes; MeshCore's is smaller.
MESHTASTIC_MAX = 200
MESHCORE_MAX = 140


def battery_text(level):
    # Meshtastic reports 101 for "on external power".
    return "powered" if level > 100 else f"{level}%"


class MeshtasticRadio:
    limit = MESHTASTIC_MAX

    def __init__(self, port, store):
        self.port = port
        self.store = store
        self.iface = None
        self.my_id = None
        self.my_num = None
        self.closed = False

    def start(self):
        threading.Thread(target=self._run, name="meshtastic", daemon=True).start()

    def _subscribe(self, pub):
        pub.subscribe(self._on_text, "meshtastic.receive.text")
        pub.subscribe(self._on_node, "meshtastic.node.updated")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")

    def _run(self):
        try:
            from pubsub import pub
            from meshtastic.serial_interface import SerialInterface
            self._subscribe(pub)
            # Blocks until the node has sent its config, or raises.
            iface = SerialInterface(devPath=self.port)
        except Exception as error:  # the library raises many types; the user only needs the text
            # A closed radio stays quiet: its store may now belong to the next try.
            if not self.closed:
                self.store.post(type="error", text=f"Could not connect: {error}")
            return
        if self.closed:
            # Cancelled while connecting: let go of the port, so the next try can lock it.
            iface.close()
            return
        self.iface = iface
        try:
            self._connected()
        except Exception as error:
            self.store.post(type="error", text=f"Connection failed: {error}")

    def _connected(self):
        info = self.iface.getMyNodeInfo() or {}
        user = info.get("user", {})
        self.my_id = user.get("id")
        self.my_num = info.get("num")
        if self.my_id:
            # Node updates during the config download can come before we know which node is ours.
            self.store.post(type="drop_node", key=self.my_id)
        self.store.post(type="me", name=user.get("longName") or self.my_id or "This node")
        battery = info.get("deviceMetrics", {}).get("batteryLevel")
        if battery is not None:
            self.store.post(type="battery", text=battery_text(battery))
        for channel in self.iface.localNode.channels or []:
            if channel.role == 0:  # DISABLED
                continue
            name = channel.settings.name or ("Primary" if channel.role == 1 else f"Channel {channel.index}")
            self.store.post(type="channel", key=f"ch:{channel.index}", title=name)
        # The reader thread can add nodes while this runs.
        for node in list((self.iface.nodes or {}).values()):
            self._post_node(node)
        self.store.post(type="status", connected=True, text="Connected")

    def _ours(self, interface):
        """pubsub is global: ignore an interface that is not this radio's (an old one closing)."""
        return not self.closed and (self.iface is None or interface is None or interface is self.iface)

    def _post_node(self, node, interface=None):
        user = node.get("user", {})
        key = user.get("id")
        my_num = self.my_num
        if my_num is None and interface is not None:
            my_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
        if not key or key == self.my_id or (my_num is not None and node.get("num") == my_num):
            return
        metrics = node.get("deviceMetrics", {})
        battery = metrics.get("batteryLevel")
        self.store.post(type="node", key=key, name=user.get("longName") or key, short=user.get("shortName", ""),
                        last_heard=node.get("lastHeard") or 0, snr=node.get("snr"),
                        battery=battery if battery is None or battery <= 100 else "powered", hops=node.get("hopsAway"))

    def _name(self, node_id):
        node = (self.iface.nodes or {}).get(node_id, {}) if self.iface else {}
        return node.get("user", {}).get("longName") or node_id or "?"

    def _on_text(self, packet, interface=None):
        if not self._ours(interface):
            return
        text = packet.get("decoded", {}).get("text")
        sender = packet.get("fromId")
        if text is None or sender is None:
            return
        when = packet.get("rxTime") or time.time()
        if self.my_id and packet.get("toId") == self.my_id:
            self.store.post(type="message", key=f"dm:{sender}", title=self._name(sender), kind="dm",
                            sender=self._name(sender), text=text, time=when)
        else:
            channel = packet.get("channel", 0)
            self.store.post(type="message", key=f"ch:{channel}", title=f"Channel {channel}", kind="channel",
                            sender=self._name(sender), text=text, time=when)

    def _on_node(self, node, interface=None):
        if self._ours(interface):
            self._post_node(node, interface)

    def _on_lost(self, interface=None):
        if self._ours(interface):
            self.store.post(type="status", connected=False, text="Connection lost")

    def send(self, key, text):
        kind, target = key.split(":", 1)
        sid = send_id()
        self.store.post(type="message", key=key, title="", kind="dm" if kind == "dm" else "channel",
                        sender=self.store.me or "Me", text=text, mine=True, status="sending", id=sid)
        threading.Thread(target=self._send, args=(key, kind, target, text, sid), daemon=True).start()

    def _send(self, key, kind, target, text, sid):
        try:
            if kind == "ch":
                self.iface.sendText(text, channelIndex=int(target))
            else:
                self.iface.sendText(text, destinationId=target, wantAck=True)
            self.store.post(type="sent", key=key, id=sid, status="sent")
        except Exception as error:
            self.store.post(type="sent", key=key, id=sid, status="failed")
            self.store.post(type="notice", text=f"Send failed: {error}")

    def close(self):
        """May be called from any thread; does not wait for the port."""
        self.closed = True
        try:
            from pubsub import pub
            for listener, topic in ((self._on_text, "meshtastic.receive.text"),
                                    (self._on_node, "meshtastic.node.updated"),
                                    (self._on_lost, "meshtastic.connection.lost")):
                if pub.isSubscribed(listener, topic):
                    pub.unsubscribe(listener, topic)
        except Exception:  # pubsub missing or topic never created: nothing to undo
            pass
        if self.iface:
            try:
                self.iface.close()
            except Exception:  # closing a dead port can raise anything; we are leaving anyway
                pass


class MeshCoreRadio:
    limit = MESHCORE_MAX

    def __init__(self, port, store):
        self.port = port
        self.store = store
        self.loop = None
        self.mc = None
        self.closed = False

    def start(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="meshcore", daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    async def _connect(self):
        try:
            from meshcore import MeshCore
            mc = await MeshCore.create_serial(self.port, 115200)
        except Exception as error:
            if not self.closed:
                self.store.post(type="error", text=f"Could not connect: {error}")
            asyncio.get_running_loop().stop()
            return
        if mc is None:
            if not self.closed:
                self.store.post(type="error", text="No answer. Is this a MeshCore companion node?")
            asyncio.get_running_loop().stop()
            return
        self.mc = mc
        if self.closed:
            # Cancelled while connecting: close() could not reach the port yet.
            await self._shutdown()
            return
        try:
            await self._connected(mc)
        except Exception as error:  # e.g. the node unplugged half way
            self.store.post(type="error", text=f"Connection failed: {error}")
            await self._shutdown()

    async def _connected(self, mc):
        from meshcore import EventType
        mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
        mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel)
        mc.subscribe(EventType.DISCONNECTED, self._on_lost)
        mc.subscribe(EventType.NEW_CONTACT, self._on_contact)
        mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)
        self.store.post(type="me", name=(mc.self_info or {}).get("name") or "This node")
        await mc.ensure_contacts()
        for contact in (mc.contacts or {}).values():
            self._post_contact(contact)
        for index in range(8):
            event = await mc.commands.get_channel(index)
            if event.type == EventType.ERROR:
                break
            name = event.payload.get("channel_name", "")
            if name:
                self.store.post(type="channel", key=f"ch:{index}", title=name)
        battery = await mc.commands.get_bat()
        if battery.type != EventType.ERROR and "level" in battery.payload:
            self.store.post(type="battery", text=f"{battery.payload['level'] / 1000:.2f} V")
        await mc.start_auto_message_fetching()
        self.store.post(type="status", connected=True, text="Connected")

    def _post_contact(self, contact):
        key = contact.get("public_key", "")[:12]
        if key:
            self.store.post(type="node", key=key, name=contact.get("adv_name") or key,
                            last_heard=contact.get("last_advert") or 0)

    def _contact_name(self, prefix):
        for contact in (self.mc.contacts or {}).values() if self.mc else []:
            if contact.get("public_key", "").startswith(prefix):
                return contact.get("adv_name") or prefix
        return prefix

    async def _on_dm(self, event):
        prefix = event.payload.get("pubkey_prefix", "")
        name = self._contact_name(prefix)
        self.store.post(type="message", key=f"dm:{prefix}", title=name, kind="dm", sender=name,
                        text=event.payload.get("text", ""), time=event.payload.get("sender_timestamp"))

    async def _on_channel(self, event):
        index = event.payload.get("channel_idx", 0)
        # Channel messages arrive as "Sender: text".
        sender, sep, text = event.payload.get("text", "").partition(": ")
        if not sep:
            sender, text = "?", sender
        self.store.post(type="message", key=f"ch:{index}", title=f"Channel {index}", kind="channel",
                        sender=sender, text=text, time=event.payload.get("sender_timestamp"))

    async def _on_lost(self, event):
        if not self.closed:
            self.store.post(type="status", connected=False, text="Connection lost")

    async def _on_contact(self, event):
        self._post_contact(event.payload)

    async def _on_advert(self, event):
        key = event.payload.get("public_key", "")[:12]
        if key:
            self.store.post(type="node", key=key, name=self._contact_name(key), last_heard=time.time())

    def send(self, key, text):
        kind, target = key.split(":", 1)
        sid = send_id()
        self.store.post(type="message", key=key, title="", kind="dm" if kind == "dm" else "channel",
                        sender=self.store.me or "Me", text=text, mine=True, status="sending", id=sid)
        asyncio.run_coroutine_threadsafe(self._send(key, kind, target, text, sid), self.loop)

    async def _send(self, key, kind, target, text, sid):
        from meshcore import EventType
        error = ""
        try:
            if kind == "ch":
                event = await self.mc.commands.send_chan_msg(int(target), text)
            else:
                event = await self.mc.commands.send_msg(target, text)
            if event.type == EventType.ERROR:
                error = "the node refused it"
        except Exception as problem:
            error = str(problem) or type(problem).__name__
        self.store.post(type="sent", key=key, id=sid, status="failed" if error else "sent")
        if error:
            self.store.post(type="notice", text=f"Send failed: {error}")

    async def _shutdown(self):
        try:
            await asyncio.wait_for(self.mc.disconnect(), 3)
        except Exception:  # a dead port can fail any way; we are leaving anyway
            pass
        asyncio.get_running_loop().stop()

    def close(self):
        """May be called from any thread; does not wait for the port."""
        self.closed = True
        if self.loop and self.mc and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
        # Without self.mc, _connect is still waiting on the port; it shuts down when it gets there.


class DemoRadio:
    """No node: a scripted conversation, for trying the screens and for tests."""
    limit = MESHTASTIC_MAX

    def __init__(self, port, store):
        self.store = store

    def start(self):
        post = self.store.post
        post(type="me", name="Demo node")
        post(type="battery", text="87%")
        post(type="channel", key="ch:0", title="Primary")
        post(type="channel", key="ch:1", title="Hiking")
        now = time.time()
        post(type="node", key="!a1b2c3d4", name="Base station", short="BASE", last_heard=now - 60, snr=6.5,
             battery=100, hops=0)
        post(type="node", key="!0badf00d", name="Trail repeater", short="TRL", last_heard=now - 1800, snr=-7.25,
             battery=54, hops=2)
        post(type="message", key="ch:0", title="Primary", kind="channel", sender="Base station",
             text="Morning all, mesh check.", time=now - 600)
        post(type="message", key="ch:0", title="Primary", kind="channel", sender="Trail repeater",
             text="Loud and clear from the ridge.", time=now - 540)
        post(type="message", key="dm:!a1b2c3d4", title="Base station", kind="dm", sender="Base station",
             text="Are you on the trail today?", time=now - 120)
        post(type="status", connected=True, text="Demo, no node")

    def send(self, key, text):
        sid = send_id()
        self.store.post(type="message", key=key, title="", kind="dm" if key.startswith("dm:") else "channel",
                        sender=self.store.me or "Me", text=text, mine=True, status="sending", id=sid)
        self.store.post(type="sent", key=key, id=sid, status="sent")

    def close(self):
        pass


RADIOS = {"meshtastic": MeshtasticRadio, "meshcore": MeshCoreRadio, "demo": DemoRadio}
