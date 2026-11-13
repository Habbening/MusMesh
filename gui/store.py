"""What the GUI shows: conversations, nodes and the node's own status. Radios post events, the UI
thread applies them here. Nothing is written to disk: messages live for this session only.

Copyright (C) 2026 Habbening. GPL-3.0-only, no warranty, see LICENSE.
"""
import itertools
import queue
import re
import time

# Per-conversation cap, so a busy channel cannot grow without bound on a small handheld.
MAX_MESSAGES = 300
# Text from the radio is untrusted: any node can send control characters, and the font renderer
# refuses NUL. Message text keeps its line breaks; names and titles become one line.
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
LONGEST_NAME = 64
_send_ids = itertools.count(1)


def clean(text, one_line=True):
    text = CONTROL.sub("", str(text or ""))
    if one_line:
        text = text.replace("\n", " ")[:LONGEST_NAME]
    return text


def send_id():
    return next(_send_ids)


class Message:
    __slots__ = ("time", "sender", "text", "mine", "status", "id")

    def __init__(self, sender, text, mine=False, when=None, status="", id=None):
        self.time = when or time.time()
        self.sender = sender
        self.text = text
        self.mine = mine
        self.status = status
        self.id = id


class Conversation:
    def __init__(self, key, title, kind):
        self.key = key
        self.title = title
        self.kind = kind  # "channel" or "dm"
        self.messages = []
        self.unread = 0
        self.last = 0.0

    def add(self, message, seen):
        self.messages.append(message)
        del self.messages[:-MAX_MESSAGES]
        self.last = max(self.last, message.time)
        if not message.mine and not seen:
            self.unread += 1


class Node:
    __slots__ = ("key", "name", "short", "last_heard", "snr", "battery", "hops")

    def __init__(self, key, name, short="", last_heard=0, snr=None, battery=None, hops=None):
        self.key = key
        self.name = name
        self.short = short
        self.last_heard = last_heard
        self.snr = snr
        self.battery = battery
        self.hops = hops


class Store:
    def __init__(self):
        self.events = queue.Queue()
        self.conversations = {}
        self.nodes = {}
        self.me = ""
        self.connected = False
        self.status = "Connecting..."
        self.battery = ""
        self.error = ""
        self.notice = ""
        self.notice_until = 0
        # The conversation on screen: its new messages are not counted as unread.
        self.open_key = None

    def post(self, **event):
        """Called from radio threads."""
        self.events.put(event)

    def pump(self, limit=200):
        """Apply queued events on the UI thread. Returns True if anything changed."""
        changed = False
        for _ in range(limit):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self.apply(event)
            changed = True
        return changed

    def conversation(self, key, title, kind):
        conv = self.conversations.get(key)
        if conv is None:
            conv = self.conversations[key] = Conversation(key, title, kind)
        elif title and conv.title != title and kind == "dm":
            conv.title = title
        return conv

    def apply(self, event):
        kind = event["type"]
        if kind == "status":
            self.connected = event["connected"]
            self.status = clean(event["text"])
        elif kind == "me":
            self.me = clean(event["name"])
        elif kind == "battery":
            self.battery = clean(event["text"])
        elif kind == "error":
            # The link is gone or never came up.
            self.error = clean(event["text"], one_line=False)
            self.connected = False
            self.status = "Not connected"
        elif kind == "notice":
            # Something went wrong, the link is still up (a failed send).
            self.notice = clean(event["text"])
            self.notice_until = time.monotonic() + 8
        elif kind == "channel":
            self.conversation(event["key"], clean(event["title"]), "channel")
        elif kind == "node":
            node = self.nodes.get(event["key"])
            fields = {k: (clean(v) if k in ("name", "short") else v)
                      for k, v in event.items() if k in Node.__slots__}
            if node is None:
                self.nodes[event["key"]] = Node(**fields)
            else:
                for name, value in fields.items():
                    if value is not None and value != "":
                        setattr(node, name, value)
            conv = self.conversations.get("dm:" + event["key"])
            if conv and fields.get("name"):
                conv.title = fields["name"]
        elif kind == "drop_node":
            self.nodes.pop(event["key"], None)
        elif kind == "message":
            conv = self.conversation(event["key"], clean(event.get("title", "")), event.get("kind", "channel"))
            conv.add(Message(clean(event["sender"]), clean(event["text"], one_line=False), event.get("mine", False),
                             event.get("time"), event.get("status", ""), event.get("id")),
                     seen=conv.key == self.open_key)
        elif kind == "sent":
            conv = self.conversations.get(event["key"])
            for message in conv.messages if conv else []:
                if message.id is not None and message.id == event.get("id"):
                    message.status = event["status"]
                    break

    def current_notice(self):
        return self.notice if self.notice and time.monotonic() < self.notice_until else ""

    def ordered(self):
        """Channels in index order first, then direct messages, newest first."""
        channels = sorted((c for c in self.conversations.values() if c.kind == "channel"),
                          key=lambda c: int(c.key.split(":")[1]) if c.key.split(":")[1].isdigit() else 99)
        dms = sorted((c for c in self.conversations.values() if c.kind == "dm"), key=lambda c: -c.last)
        return channels + dms

    def ordered_nodes(self):
        return sorted(self.nodes.values(), key=lambda n: -(n.last_heard or 0))

    def unread(self):
        return sum(c.unread for c in self.conversations.values())

    def open(self, key):
        self.open_key = key
        conv = self.conversations.get(key)
        if conv:
            conv.unread = 0
