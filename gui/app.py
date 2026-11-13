#!/usr/bin/env python3
"""MusMesh graphical client: chats, direct messages and nodes for Meshtastic and MeshCore companion
nodes, driven by the handheld's buttons.

A: open or select. B: back (in the keyboard: delete). X: write. Y: space in the keyboard.
L1/R1: change tab. Start: send. Menu: one screen back; on the chat list, disconnect.

Exit codes: 0 quit, 3 open the terminal clients (the launcher runs them).

Copyright (C) 2026 Habbening. GPL-3.0-only, no warranty, see LICENSE.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import musmesh  # noqa: E402  shared warning text, data dir, port search and last-client memory
import sdl  # noqa: E402
from radios import RADIOS  # noqa: E402
from store import Store  # noqa: E402

EXIT_TERMINAL = 3
FONT_PATHS = (
    "/opt/muos/share/font/NotoSans/NotoSans-SemiBold.ttf",
    "/opt/muos/share/font/internal/Inter Display.ttf",
)
CLIENTS = (("Meshtastic", "meshtastic"), ("MeshCore (companion)", "meshcore"))
HOME_ITEMS = [c[0] for c in CLIENTS] + ["Demo, no node", "Terminal clients", "Credits and licences", "Quit"]
DEMO, TERMINAL, CREDITS, QUIT = range(len(CLIENTS), len(CLIENTS) + 4)
REPEAT_DELAY, REPEAT_EVERY = 0.35, 0.07
REDRAW_EVERY = 0.5
# After the link drops: look for the node this often, give it this long to boot once its port is
# back, and give up on one try after this long.
RETRY_EVERY, PORT_SETTLE, ATTEMPT_LIMIT = 3, 5, 60

BG, PANEL, LINE = (16, 18, 22), (32, 36, 43), (52, 58, 68)
TEXT, DIM, ACCENT = (236, 238, 240), (150, 156, 166), (70, 160, 255)
MINE, THEIRS, GOOD, WARN, BAD = (34, 84, 140), (44, 49, 58), (90, 200, 120), (235, 180, 70), (230, 90, 90)

# muOS-Keys raw joystick numbers, used only when SDL has no controller mapping for the pad.
RAW_BUTTONS = {3: "ok", 2: "back", 5: "x", 4: "y", 6: "l1", 7: "r1", 11: "start", 12: "menu"}


def find_font():
    for path in (os.environ.get("MUSMESH_FONT", ""),) + FONT_PATHS:
        if path and os.path.exists(path):
            return path
    raise SystemExit("MusMesh: no font found")


def ago(when):
    if not when:
        return ""
    seconds = max(0, int(time.time() - when))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size} {unit}"
    return "now"


class Fonts:
    def __init__(self, height):
        path = find_font()
        scale = height / 480
        self.big = sdl.Font(path, round(26 * scale))
        self.normal = sdl.Font(path, round(21 * scale))
        self.small = sdl.Font(path, round(16 * scale))
        self.scale = scale


def fit(font, text, width):
    """text, cut with an ellipsis so it fits in width."""
    if font.size(text)[0] <= width:
        return text
    while text and font.size(text + "…")[0] > width:
        text = text[:-1]
    return text + "…"


def wrap(font, text, width):
    """Lines that fit in width. Long words are broken."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if font.size(candidate)[0] <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while font.size(word)[0] > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and font.size(word[:cut])[0] > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


class Screen:
    title = "MusMesh"
    hints = ""

    def __init__(self, app):
        self.app = app

    def handle(self, action):
        pass

    def draw(self, surface, area):
        pass

    def tick(self):
        """Called on every loop. Return True to redraw."""
        return False


class ListScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.selected = 0
        self.top = 0

    def items(self):
        return []

    def row_height(self):
        return round(self.app.fonts.normal.get_height() * 1.6)

    def move(self, action, count):
        if not count:
            return
        if action == "up":
            self.selected = (self.selected - 1) % count
        elif action == "down":
            self.selected = (self.selected + 1) % count

    def draw_rows(self, surface, area, rows, draw_row):
        height = self.row_height()
        visible = max(1, area.height // height)
        self.selected = min(self.selected, max(0, len(rows) - 1))
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + visible:
            self.top = self.selected - visible + 1
        for slot, index in enumerate(range(self.top, min(len(rows), self.top + visible))):
            rect = sdl.Rect(area.x, area.y + slot * height, area.width, height)
            if index == self.selected:
                sdl.draw_rect(surface, PANEL, rect, border_radius=round(6 * self.app.fonts.scale))
                sdl.draw_rect(surface, ACCENT, (rect.x, rect.y + 4, 4, rect.height - 8))
            draw_row(surface, rect, rows[index], index == self.selected)


class WarningScreen(Screen):
    title = "Before you start"
    hints = "A or Start: accept    B: quit"

    def handle(self, action):
        if action in ("ok", "start"):
            try:
                open(musmesh.storage_accepted_path(), "w").close()
            except OSError:
                pass
            # Same rule as the terminal menu: a fast second press must not also act on the next screen.
            self.app.settle()
            self.app.replace(HomeScreen(self.app))
        elif action in ("back", "menu"):
            self.app.quit(0)

    def draw(self, surface, area):
        fonts = self.app.fonts
        text = musmesh.STORAGE_WARNING.replace("MusMesh: before you start\n\n", "")
        text = text.replace("Start: accept  B: quit\n", "")
        y = area.y
        for line in [part for raw in text.rstrip().split("\n") for part in wrap(fonts.small, raw, area.width)]:
            surface.blit(fonts.small.render(line, WARN if "not encrypted" in line else TEXT), (area.x, y))
            y += fonts.small.get_linesize()


class HomeScreen(ListScreen):
    title = "MusMesh"
    hints = "A: select    Left/Right: change node"

    def __init__(self, app):
        super().__init__(app)
        self.selected = musmesh.load_last_client()
        self.port_index = 0
        self.ports = []
        self.message = ""
        self.last_scan = 0

    def tick(self):
        if time.monotonic() - self.last_scan < 1:
            return False
        self.last_scan = time.monotonic()
        ports = musmesh.find_ports()
        changed = ports != self.ports
        self.ports = ports
        return changed

    def port(self):
        return self.ports[self.port_index % len(self.ports)] if self.ports else None

    def row_height(self):
        # All six items fit on a 480-line screen.
        return round(self.app.fonts.normal.get_height() * 1.4)

    def handle(self, action):
        self.message = ""
        self.move(action, len(HOME_ITEMS))
        if action == "left":
            self.port_index -= 1
        elif action == "right":
            self.port_index += 1
        elif action in ("ok", "start"):
            if self.selected == QUIT:
                self.app.quit(0)
            elif self.selected == TERMINAL:
                self.app.quit(EXIT_TERMINAL)
            elif self.selected == CREDITS:
                self.app.push(CreditsScreen(self.app))
            elif self.selected == DEMO:
                self.app.connect("demo", None)
            elif not self.port():
                self.message = "No node found. Plug one in and try again."
            else:
                musmesh.save_last_client(self.selected)
                self.app.connect(CLIENTS[self.selected][1], self.port())
        elif action == "back":
            self.app.quit(0)

    def draw(self, surface, area):
        fonts = self.app.fonts
        node = self.port() or "none, plug one in"
        surface.blit(fonts.normal.render(f"Node: {node}", TEXT if self.port() else DIM), (area.x, area.y))
        y = area.y + fonts.normal.get_linesize()
        if len(self.ports) > 1:
            surface.blit(fonts.small.render("Left/Right: change node", DIM), (area.x, y))
        y += fonts.normal.get_linesize()
        lines = round(fonts.small.get_linesize() * 2.2)
        rows = sdl.Rect(area.x, y, area.width, area.bottom - y - lines)

        def row(surf, rect, label, selected):
            surf.blit(fonts.normal.render(label, TEXT if selected else DIM),
                      (rect.x + 14, rect.centery - fonts.normal.get_height() // 2))
        self.draw_rows(surface, rows, HOME_ITEMS, row)
        if self.message:
            surface.blit(fonts.small.render(self.message, WARN), (area.x, area.bottom - lines))
        elif self.selected < len(CLIENTS):
            note = "Pick the one that matches your node's firmware."
            surface.blit(fonts.small.render(note, DIM), (area.x, area.bottom - lines))


class ConnectScreen(Screen):
    title = "Connecting"
    hints = "B: back"

    def __init__(self, app, port):
        super().__init__(app)
        self.port = port
        self.started = time.monotonic()

    def tick(self):
        store = self.app.store
        if store.connected:
            self.app.replace(MainScreen(self.app))
            return True
        return True  # the dots move

    def handle(self, action):
        if action in ("back", "menu"):
            self.app.disconnect()

    def draw(self, surface, area):
        fonts, store = self.app.fonts, self.app.store
        if store.error:
            for i, line in enumerate(wrap(fonts.normal, store.error, area.width)):
                surface.blit(fonts.normal.render(line, BAD), (area.x, area.y + i * fonts.normal.get_linesize()))
            return
        dots = "." * (1 + int(time.monotonic() - self.started) % 3)
        surface.blit(fonts.normal.render(f"Connecting to {self.port or 'demo'}{dots}", TEXT), area.topleft)
        surface.blit(fonts.small.render("Meshtastic nodes can take 20 seconds to send their config.", DIM),
                     (area.x, area.y + fonts.normal.get_linesize() * 2))


class MainScreen(ListScreen):
    hints = "A: open    X: write    L1/R1: tab    B: disconnect"
    TABS = ("Chats", "Nodes")

    def __init__(self, app):
        super().__init__(app)
        self.tab = 0
        self.last_minute = 0

    @property
    def title(self):
        return self.app.store.me or "MusMesh"

    def tick(self):
        minute = int(time.time() // 60)
        if minute != self.last_minute:
            self.last_minute = minute
            return True
        return False

    def rows(self):
        store = self.app.store
        return store.ordered() if self.tab == 0 else store.ordered_nodes()

    def open_selected(self, write=False):
        rows = self.rows()
        if not rows:
            return
        item = rows[min(self.selected, len(rows) - 1)]
        if self.tab == 0:
            key = item.key
        else:
            key = "dm:" + item.key
            self.app.store.conversation(key, item.name, "dm")
        chat = ChatScreen(self.app, key)
        self.app.push(chat)
        if write:
            chat.compose()

    def handle(self, action):
        rows = self.rows()
        self.move(action, len(rows))
        if action in ("l1", "r1", "left", "right"):
            self.tab = 1 - self.tab
            self.selected = self.top = 0
        elif action in ("ok", "start"):
            self.open_selected()
        elif action == "x":
            self.open_selected(write=True)
        elif action in ("back", "menu"):
            self.app.disconnect()

    def draw(self, surface, area):
        fonts, store = self.app.fonts, self.app.store
        x = area.x
        for index, name in enumerate(self.TABS):
            label = name if index or not store.unread() else f"{name} ({store.unread()})"
            image = fonts.normal.render(label, TEXT if index == self.tab else DIM)
            surface.blit(image, (x, area.y))
            if index == self.tab:
                sdl.draw_rect(surface, ACCENT, (x, area.y + image.get_height() + 2, image.get_width(), 3))
            x += image.get_width() + round(28 * fonts.scale)
        top = area.y + fonts.normal.get_linesize() + round(12 * fonts.scale)
        rows_area = sdl.Rect(area.x, top, area.width, area.bottom - top)
        rows = self.rows()
        if not rows:
            empty = "No messages yet. Channels show up once the node sends them." if self.tab == 0 \
                else "No other nodes heard yet."
            for i, line in enumerate(wrap(fonts.small, empty, area.width)):
                surface.blit(fonts.small.render(line, DIM), (area.x, top + i * fonts.small.get_linesize()))
            return
        self.draw_rows(surface, rows_area, rows, self.draw_chat_row if self.tab == 0 else self.draw_node_row)

    def row_height(self):
        fonts = self.app.fonts
        return fonts.normal.get_linesize() + fonts.small.get_linesize() + round(10 * fonts.scale)

    def draw_chat_row(self, surface, rect, conv, selected):
        fonts = self.app.fonts
        pad = round(14 * fonts.scale)
        right = rect.right - pad
        title = ("# " if conv.kind == "channel" else "@ ") + conv.title
        title_width = right - rect.x - pad - fonts.small.size("99 m")[0] - round(40 * fonts.scale)
        surface.blit(fonts.normal.render(fit(fonts.normal, title, title_width), TEXT), (rect.x + pad, rect.y + 4))
        if conv.unread:
            badge = fonts.small.render(str(conv.unread), BG)
            box = badge.get_rect(topright=(right, rect.y + 6)).inflate(12, 4)
            sdl.draw_rect(surface, ACCENT, box, border_radius=box.height // 2)
            surface.blit(badge, badge.get_rect(center=box.center))
            right = box.left - 8
        if conv.messages:
            last = conv.messages[-1]
            when = fonts.small.render(ago(last.time), DIM)
            surface.blit(when, when.get_rect(topright=(right, rect.y + 8)))
            preview = fit(fonts.small, f"{last.sender}: {last.text}".replace("\n", " "), rect.width - 2 * pad)
            surface.blit(fonts.small.render(preview, DIM), (rect.x + pad, rect.y + fonts.normal.get_linesize() + 4))

    def draw_node_row(self, surface, rect, node, selected):
        fonts = self.app.fonts
        pad = round(14 * fonts.scale)
        name = f"{node.name} ({node.short})" if node.short and node.short != node.name else node.name
        surface.blit(fonts.normal.render(fit(fonts.normal, name, rect.width - 2 * pad), TEXT),
                     (rect.x + pad, rect.y + 4))
        details = [f"heard {ago(node.last_heard)} ago" if node.last_heard else "not heard"]
        if node.snr is not None:
            details.append(f"SNR {node.snr:.1f}")
        if node.hops is not None:
            details.append("direct" if node.hops == 0 else f"{node.hops} hops")
        if node.battery is not None:
            details.append("on power" if node.battery == "powered" else f"battery {node.battery}%")
        surface.blit(fonts.small.render(fit(fonts.small, "   ".join(details), rect.width - 2 * pad), DIM),
                     (rect.x + pad, rect.y + fonts.normal.get_linesize() + 4))


class ChatScreen(Screen):
    hints = "A/X: write    Up/Down: scroll    B: back"

    def __init__(self, app, key):
        super().__init__(app)
        self.key = key
        self.scroll = 0  # lines up from the newest
        # (id(message), width) -> (message, lines). The message is kept so its id is not reused.
        self.wrapped = {}
        app.store.open(key)

    @property
    def conv(self):
        return self.app.store.conversations.get(self.key)

    @property
    def title(self):
        conv = self.conv
        return (("# " if conv.kind == "channel" else "@ ") + conv.title) if conv else "Chat"

    def compose(self):
        self.app.push(KeyboardScreen(self.app, self.send))

    def send(self, text):
        if not self.app.store.connected:
            self.app.store.post(type="notice", text="Not connected, message not sent")
            return
        self.app.radio.send(self.key, text)
        self.scroll = 0

    def tick(self):
        self.app.store.open(self.key)
        return False

    def handle(self, action):
        if action in ("ok", "x", "start"):
            self.compose()
        elif action == "up":
            self.scroll += 1
        elif action == "down":
            self.scroll = max(0, self.scroll - 1)
        elif action in ("back", "menu"):
            self.app.store.open(None)
            self.app.pop()

    def layout(self, width):
        """(lines, mine, status) per message, newest last."""
        fonts = self.app.fonts
        inner = int(width * 0.78)
        blocks = []
        messages = self.conv.messages if self.conv else []
        # Wrapping measures every word, which is slow on a handheld: do it once per message.
        if len(self.wrapped) > 2 * len(messages) + 50:
            self.wrapped = {}
        for message in messages:
            head = "You" if message.mine else message.sender
            stamp = time.strftime("%H:%M", time.localtime(message.time))
            status = f"  {message.status}" if message.mine and message.status not in ("", "sent") else ""
            key = (id(message), inner)
            if key not in self.wrapped:
                self.wrapped[key] = (message, wrap(fonts.normal, message.text, inner))
            blocks.append((f"{head}  {stamp}{status}", self.wrapped[key][1], message.mine,
                           message.status == "failed"))
        return blocks

    def draw(self, surface, area):
        fonts = self.app.fonts
        pad = round(10 * fonts.scale)
        blocks = self.layout(area.width - 2 * pad)
        if not blocks:
            surface.blit(fonts.small.render("No messages yet. Press A to write one.", DIM), area.topleft)
            return
        line = fonts.normal.get_linesize()
        small = fonts.small.get_linesize()
        heights = [small + len(lines) * line + 2 * pad + pad for _, lines, _, _ in blocks]
        total = sum(heights)
        self.scroll = min(self.scroll, max(0, (total - area.height) // line + 1))
        y = area.bottom - total + self.scroll * line
        clip = surface.get_clip()
        surface.set_clip(area)
        for (head, lines, mine, failed), height in zip(blocks, heights):
            if y + height >= area.y and y <= area.bottom:
                width = max([fonts.normal.size(t)[0] for t in lines] + [fonts.small.size(head)[0]]) + 2 * pad
                x = area.right - width if mine else area.x
                box = sdl.Rect(x, y, width, height - pad)
                sdl.draw_rect(surface, MINE if mine else THEIRS, box, border_radius=round(10 * fonts.scale))
                surface.blit(fonts.small.render(head, BAD if failed else DIM), (x + pad, y + pad // 2))
                for i, text in enumerate(lines):
                    surface.blit(fonts.normal.render(text, TEXT), (x + pad, y + pad // 2 + small + i * line))
            y += height
        surface.set_clip(clip)


KEY_ROWS = {
    "abc": ["1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm,.?"],
    "ABC": ["1234567890", "QWERTYUIOP", "ASDFGHJKL'", "ZXCVBNM,.?"],
    "#+=": ["!@#$%^&*()", "-_=+[]{};:", "/\\|<>\"~`€£", "°…•·¡¿«»€!"],
}
SPECIAL = ["Shift", "#+=", "Space", "Del", "Send"]


class KeyboardScreen(Screen):
    title = "Write"
    hints = "A: type   B: delete   Y: space   X: shift   Start: send"

    def __init__(self, app, on_send, text=""):
        super().__init__(app)
        self.on_send = on_send
        self.text = text
        self.layer = "abc"
        self.row = 1
        self.col = 0

    def limit(self):
        return getattr(self.app.radio, "limit", 200)

    def type(self, char):
        if len((self.text + char).encode("utf-8")) <= self.limit():
            self.text += char

    def press(self):
        if self.row < 4:
            self.type(KEY_ROWS[self.layer][self.row][self.col])
            if self.layer == "ABC":
                self.layer = "abc"
            return
        key = SPECIAL[min(self.col * len(SPECIAL) // 10, len(SPECIAL) - 1)]
        if key == "Shift":
            self.layer = "ABC" if self.layer == "abc" else "abc"
        elif key == "#+=":
            self.layer = "abc" if self.layer == "#+=" else "#+="
        elif key == "Space":
            self.type(" ")
        elif key == "Del":
            self.text = self.text[:-1]
        elif key == "Send":
            self.send()

    def send(self):
        text = self.text.strip()
        self.app.pop()
        if text:
            self.on_send(text)

    def handle(self, action):
        if action == "up":
            self.row = (self.row - 1) % 5
        elif action == "down":
            self.row = (self.row + 1) % 5
        elif action == "left":
            self.col = (self.col - (1 if self.row < 4 else 2)) % 10
        elif action == "right":
            self.col = (self.col + (1 if self.row < 4 else 2)) % 10
        elif action == "ok":
            self.press()
        elif action == "back":
            if self.text:
                self.text = self.text[:-1]
            else:
                self.app.pop()
        elif action == "y":
            self.type(" ")
        elif action == "x":
            self.layer = "ABC" if self.layer == "abc" else "abc"
        elif action in ("l1", "r1"):
            self.layer = "abc" if self.layer == "#+=" else "#+="
        elif action == "start":
            self.send()
        elif action == "menu":
            self.app.pop()
        if self.row == 4:
            self.col -= self.col % 2

    def draw(self, surface, area):
        fonts = self.app.fonts
        pad = round(8 * fonts.scale)
        field = sdl.Rect(area.x, area.y, area.width, fonts.normal.get_linesize() * 3 + 2 * pad)
        sdl.draw_rect(surface, PANEL, field, border_radius=round(8 * fonts.scale))
        lines = wrap(fonts.normal, self.text + "_", field.width - 2 * pad)[-3:]
        for i, line in enumerate(lines):
            surface.blit(fonts.normal.render(line, TEXT), (field.x + pad, field.y + pad + i * fonts.normal.get_linesize()))
        used = len(self.text.encode("utf-8"))
        count = fonts.small.render(f"{used}/{self.limit()}", WARN if used > self.limit() * 0.9 else DIM)
        surface.blit(count, count.get_rect(topright=(field.right, field.bottom + 2)))
        top = field.bottom + fonts.small.get_linesize() + pad
        key_h = (area.bottom - top) // 5
        key_w = area.width // 10
        for r in range(5):
            y = top + r * key_h
            if r < 4:
                cells = [(c, 1, ch) for c, ch in enumerate(KEY_ROWS[self.layer][r])]
            else:
                cells = [(c * 2, 2, label) for c, label in enumerate(SPECIAL)]
            for col, span, label in cells:
                rect = sdl.Rect(area.x + col * key_w + 2, y + 2, key_w * span - 4, key_h - 4)
                chosen = r == self.row and (col == self.col if span == 1 else col == self.col - self.col % 2)
                if label == "Shift" and self.layer == "ABC" or label == "#+=" and self.layer == "#+=":
                    colour = LINE
                else:
                    colour = PANEL
                sdl.draw_rect(surface, ACCENT if chosen else colour, rect, border_radius=round(6 * fonts.scale))
                font = fonts.normal if span == 1 else fonts.small
                image = font.render(label, BG if chosen else TEXT)
                surface.blit(image, image.get_rect(center=rect.center))


class CreditsScreen(Screen):
    title = "Credits and licences"
    hints = "Up/Down: scroll    B: back"

    def __init__(self, app):
        super().__init__(app)
        try:
            with open(os.path.join(musmesh.HERE, "CREDITS.txt"), encoding="utf-8") as fh:
                self.text = fh.read()
        except OSError:
            self.text = "CREDITS.txt is missing."
        self.top = 0
        self.lines = None

    def handle(self, action):
        if action == "up":
            self.top = max(0, self.top - 3)
        elif action == "down":
            self.top += 3
        elif action in ("left", "l1"):
            self.top = max(0, self.top - 15)
        elif action in ("right", "r1"):
            self.top += 15
        elif action in ("back", "menu", "ok"):
            self.app.pop()

    def draw(self, surface, area):
        font = self.app.fonts.small
        if self.lines is None:
            self.lines = [part for line in self.text.splitlines()
                          for part in (wrap(font, line, area.width) if line.strip() else [""])]
        visible = area.height // font.get_linesize()
        self.top = min(self.top, max(0, len(self.lines) - visible))
        for i, line in enumerate(self.lines[self.top:self.top + visible]):
            surface.blit(font.render(line, TEXT), (area.x, area.y + i * font.get_linesize()))


class App:
    def __init__(self, surface):
        self.surface = surface
        self.fonts = Fonts(surface.get_height())
        self.store = Store()
        self.radio = None
        self.stack = []
        self.exit_code = None
        self.held = None
        self.controllers = set()  # pads SDL has a controller mapping for
        self.settled = False
        self.shown_notice = ""
        self.kind = self.port = None
        self.linked = False  # this radio has been connected at least once
        self.retry_at = self.attempt_until = 0
        self.seen_at = None  # when the port came back
        os.makedirs(musmesh.DATA, exist_ok=True)
        first = HomeScreen(self) if os.path.exists(musmesh.storage_accepted_path()) else WarningScreen(self)
        self.stack.append(first)

    # Screen stack
    @property
    def screen(self):
        return self.stack[-1]

    def push(self, screen):
        self.stack.append(screen)

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()

    def replace(self, screen):
        self.stack[-1] = screen

    def quit(self, code):
        self.exit_code = code

    def settle(self):
        time.sleep(musmesh.ACCEPT_SETTLE)
        sdl.clear_events()
        # Presses already taken from the queue in this batch are dropped too (see run).
        self.settled = True

    # Radio
    def connect(self, kind, port):
        self.disconnect(go_home=False)
        self.store = Store()
        self.kind, self.port = kind, port
        self.radio = RADIOS[kind](port, self.store)
        self.radio.start()
        self.stack = [HomeScreen(self), ConnectScreen(self, port)]

    def disconnect(self, go_home=True):
        if self.radio:
            # Closing a serial port can take a second; the screen does not wait for it.
            threading.Thread(target=self.radio.close, daemon=True).start()
            self.radio = None
        self.linked = False
        if go_home:
            self.stack = [HomeScreen(self)]

    def reconnect(self):
        """After the link to a node drops (unplugged, node restarted), connect again once its port
        is back. The store is kept, so the chats stay. Returns True if the screen changed."""
        store, now = self.store, time.monotonic()
        if not self.radio:
            return False
        if store.connected:
            self.linked, self.attempt_until, self.seen_at = True, 0, None
            return False
        # A first connect that fails is not retried: the connect screen shows why.
        if not self.linked or now < self.retry_at:
            return False
        self.retry_at = now + RETRY_EVERY
        if now < self.attempt_until and not store.error:
            return False  # a try is still running
        if not self.radio.closed:
            # Let go of the port first. The next try starts on a later pass, when the old radio's
            # last events are out of the way.
            threading.Thread(target=self.radio.close, daemon=True).start()
            self.attempt_until = 0
            store.status = "Connection lost"
            return True
        ports = musmesh.find_ports()
        # Plugged back in, the node can come up under another name (ttyACM0, then ttyACM1).
        port = self.port if self.port in ports else ports[0] if len(ports) == 1 else None
        store.error = ""
        if port is None:
            self.seen_at = None
            store.status = "Connection lost, plug the node back in"
            return True
        if self.seen_at is None:
            self.seen_at = now
        store.status = "Reconnecting..."
        if now - self.seen_at < PORT_SETTLE:
            return True  # the node is still starting
        self.port = port
        self.radio = RADIOS[self.kind](port, store)
        self.radio.start()
        self.attempt_until = now + ATTEMPT_LIMIT
        return True

    # Drawing
    def draw(self):
        surface, fonts = self.surface, self.fonts
        width, height = surface.get_size()
        surface.fill(BG)
        bar = round(fonts.normal.get_linesize() * 1.5)
        sdl.draw_rect(surface, PANEL, (0, 0, width, bar))
        pad = round(14 * fonts.scale)
        title = fit(fonts.normal, self.screen.title, width // 2 - pad)
        surface.blit(fonts.normal.render(title, TEXT), (pad, (bar - fonts.normal.get_height()) // 2))
        if self.radio:
            store = self.store
            colour = GOOD if store.connected else (BAD if store.error else WARN)
            notice = store.current_notice()
            parts = [p for p in (store.battery, notice or store.status) if p]
            label = fonts.small.render(fit(fonts.small, "   ".join(parts), width // 2 - 2 * pad),
                                       WARN if notice else DIM)
            spot = label.get_rect(midright=(width - pad, bar // 2))
            surface.blit(label, spot)
            sdl.draw_circle(surface, colour, (spot.left - 10, bar // 2), round(5 * fonts.scale))
        foot = round(fonts.small.get_linesize() * 1.6)
        sdl.draw_rect(surface, PANEL, (0, height - foot, width, foot))
        surface.blit(fonts.small.render(self.screen.hints, DIM),
                     (pad, height - foot + (foot - fonts.small.get_height()) // 2))
        area = sdl.Rect(pad, bar + pad, width - 2 * pad, height - bar - foot - 2 * pad)
        self.screen.draw(surface, area)

    # Input
    def handle(self, action):
        self.screen.handle(action)

    def action_for(self, event):
        if event.type == sdl.KEYDOWN:
            return {sdl.K_UP: "up", sdl.K_DOWN: "down", sdl.K_LEFT: "left", sdl.K_RIGHT: "right",
                    sdl.K_RETURN: "ok", sdl.K_ESCAPE: "back", sdl.K_x: "x", sdl.K_y: "y",
                    sdl.K_q: "l1", sdl.K_w: "r1", sdl.K_s: "start", sdl.K_m: "menu"}.get(event.key)
        if event.type == sdl.CONTROLLERBUTTONDOWN:
            return {sdl.BUTTON_DPAD_UP: "up", sdl.BUTTON_DPAD_DOWN: "down",
                    sdl.BUTTON_DPAD_LEFT: "left", sdl.BUTTON_DPAD_RIGHT: "right",
                    sdl.BUTTON_A: "ok", sdl.BUTTON_B: "back",
                    sdl.BUTTON_X: "x", sdl.BUTTON_Y: "y",
                    sdl.BUTTON_LEFTSHOULDER: "l1", sdl.BUTTON_RIGHTSHOULDER: "r1",
                    sdl.BUTTON_START: "start", sdl.BUTTON_GUIDE: "menu"}.get(event.button)
        if event.type == sdl.JOYBUTTONDOWN and not self.mapped(event.instance_id):
            return RAW_BUTTONS.get(event.button)
        if event.type == sdl.JOYHATMOTION and not self.mapped(event.instance_id):
            x, y = event.value
            return {(0, 1): "up", (0, -1): "down", (-1, 0): "left", (1, 0): "right"}.get((x, y))
        return None

    def mapped(self, instance_id):
        return instance_id in self.controllers

    def run(self):
        self.controllers = sdl.open_pads()
        repeat_at = shown_at = 0
        dirty = True
        while self.exit_code is None:
            self.settled = False
            for event in sdl.events():
                if self.settled:
                    break
                if event.type == sdl.QUIT:
                    self.quit(0)
                action = self.action_for(event)
                if action:
                    self.handle(action)
                    dirty = True
                    if action in ("up", "down", "left", "right"):
                        self.held, repeat_at = (event, action), time.monotonic() + REPEAT_DELAY
                if event.type in (sdl.KEYUP, sdl.CONTROLLERBUTTONUP, sdl.JOYBUTTONUP) or \
                        event.type == sdl.JOYHATMOTION and event.value == (0, 0):
                    self.held = None
            if self.held and time.monotonic() >= repeat_at:
                self.handle(self.held[1])
                repeat_at = time.monotonic() + REPEAT_EVERY
                dirty = True
            dirty |= self.store.pump()
            dirty |= self.reconnect()
            if self.store.current_notice() != self.shown_notice:
                self.shown_notice = self.store.current_notice()
                dirty = True
            dirty |= self.screen.tick()
            # Also redraw twice a second: on the handheld a frame can be lost (the first one after the
            # window opens, or one shown while the screen was off), and nothing else would replace it.
            if dirty or time.monotonic() - shown_at >= REDRAW_EVERY:
                self.draw()
                sdl.flip()
                dirty, shown_at = False, time.monotonic()
            time.sleep(0.03 if self.held else 0.06)
        if self.radio:
            self.radio.close()
            self.radio = None
        return self.exit_code


def open_display():
    size = os.environ.get("MUSMESH_WINDOW")
    return sdl.set_mode(tuple(int(v) for v in size.split("x")) if size else None)


def main():
    surface = open_display()
    code = App(surface).run()
    sdl.quit()
    return code


if __name__ == "__main__":
    sys.exit(main())
