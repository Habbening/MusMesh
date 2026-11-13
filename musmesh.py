#!/usr/bin/env python3
"""MusMesh: pick a mesh client and run it against a USB node inside Mustard Terminal.

The clients do the real work. muxterm supplies the controls: D-pad = arrows, Start = Enter,
B = Backspace, Y = Space, Select = keyboard, Menu = settings and quit.

Copyright (C) 2026 Habbening. GPL-3.0-only, no warranty, see LICENSE.
"""
import glob
import json
import os
import re
import select
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PORT_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")
# Contact logs channel and WiFi keys at INFO when settings are saved. RAM only, gone on reboot.
CONTACT_LOG = "/tmp/musmesh-contact.log"

CLIENTS = (
    ("Meshtastic  (Contact)", "from contact.__main__ import start; start()",
     lambda port: ["--port", port],
     "Left/Right: panes  Up/Down: list  Start: send\n"
     "Select: keyboard, press again to move it to the top\n"
     "Esc is on the keyboard, Mesh layer (L1/R1)"),
    ("MeshCore    (meshcore-cli)", "from meshcore_cli.meshcore_cli import cli; cli()",
     lambda port: ["-s", port, "-b", "115200", "chat"],
     "Type commands with the keyboard (Select)\n"
     "Press Select again to move it to the top\n"
     "Try: help. C-d on the control layer leaves"),
)
STORAGE_WARNING = (
    "MusMesh: before you start\n\n"
    "Your SD card is not encrypted. MusMesh keeps these on it:\n"
    "- Meshtastic messages, node list, config exports (channel keys)\n"
    "- MeshCore settings, repeater passwords, messages, history\n"
    "Anyone with the card can read them. To remove them, delete\n"
    "MUOS/application/MusMesh/.musmesh/data\n\n"
    "Also:\n"
    "- Saving settings in Contact writes keys to a log in RAM,\n"
    "  /tmp/musmesh-contact.log, gone on reboot\n"
    "- MusMesh runs as root, like everything on muOS\n"
    "- What you send goes out over your radio, on your channel\n\n"
    "Details: .musmesh/README.md, What you accept by using it\n\n"
    "Start: accept  B: quit\n")
# Seconds after Start on the warning during which more presses are dropped.
ACCEPT_SETTLE = 0.5
ITEMS = [c[0] for c in CLIENTS] + ["Credits and licences", "Quit"]
CREDITS, QUIT = len(CLIENTS), len(CLIENTS) + 1

KEYS = {b"\x1b[A": "up", b"\x1bOA": "up", b"\x1b[B": "down", b"\x1bOB": "down",
        b"\x1b[C": "right", b"\x1bOC": "right", b"\x1b[D": "left", b"\x1bOD": "left",
        b"\r": "enter", b"\n": "enter", b"q": "quit", b"\x7f": "back", b"\x08": "back"}
# A client that dies can leave these on. The alternate screen is left only after a client was
# killed by a signal: leaving it when not in it moves the cursor back over what the client printed.
RESET_MODES = "\x1b[?25h\x1b[0m\x1b[?1l\x1b>"
LEAVE_ALT_SCREEN = "\x1b[?1049l"
UNKNOWN_SEQUENCE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.)?", re.S)
pending = b""


def find_ports():
    return sorted(p for pattern in PORT_GLOBS for p in glob.glob(pattern))


def last_client_path():
    return os.path.join(DATA, "last-client")


def load_last_client():
    try:
        with open(last_client_path(), encoding="ascii") as fh:
            index = int(fh.read())
    except (OSError, ValueError):
        return 0
    return index if 0 <= index < len(CLIENTS) else 0


def save_last_client(index):
    # A convenience: a full or read-only card must not stop the client.
    try:
        with open(last_client_path(), "w", encoding="ascii") as fh:
            fh.write(str(index))
    except OSError:
        pass


def storage_accepted_path():
    # Lives in DATA, so deleting the stored data also brings the warning back.
    return os.path.join(DATA, "storage-accepted")


def accept_storage(fd):
    """Show the card storage warning until it is accepted. False means the user quit."""
    global pending
    if os.path.exists(storage_accepted_path()):
        return True
    sys.stdout.write("\x1b[2J\x1b[H" + STORAGE_WARNING)
    sys.stdout.flush()
    while True:
        key = read_key(fd, None)
        if key == "enter":
            break
        if key in ("back", "quit"):
            return False
    # A fast second Start must not also pick a menu item: let it arrive, then drop it.
    time.sleep(ACCEPT_SETTLE)
    termios.tcflush(fd, termios.TCIFLUSH)
    pending = b""
    # A card that cannot store the answer only means the warning shows again next time.
    try:
        open(storage_accepted_path(), "w").close()
    except OSError:
        pass
    return True


def keep_contact_log_off_card(contact_dir):
    path = os.path.join(contact_dir, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    if config.get("log_file_path") != CONTACT_LOG:
        config["log_file_path"] = CONTACT_LOG
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
    stale = os.path.join(contact_dir, "client.log")
    if os.path.exists(stale):
        os.remove(stale)


def client_env():
    contact_dir = os.path.join(DATA, "contact")
    # meshcore-cli keeps its files in ~/.config/meshcore, and only if that directory exists.
    os.makedirs(os.path.join(DATA, ".config", "meshcore"), exist_ok=True)
    os.makedirs(contact_dir, exist_ok=True)
    keep_contact_log_off_card(contact_dir)
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join((os.path.join(HERE, "vendor"), os.path.join(HERE, "vendor.zip"))),
        PYTHONDONTWRITEBYTECODE="1",
        CONTACT_CONFIG_ROOT=contact_dir,
        HOME=DATA,
        ESCDELAY="25",
    )
    # muxterm sets these once. ncurses would trust them and ignore a later font size change.
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    return env


def client_command(index, port):
    _, code, args, _ = CLIENTS[index]
    return [sys.executable, "-c", code] + args(port)


def draw(selected, ports, port):
    out = ["\x1b[2J\x1b[HMusMesh\n\n"]
    out.append("Node: %s\n" % (port or "none, plug one in"))
    out.append("      Left/Right: change node\n\n" if len(ports) > 1 else "\n")
    for i, item in enumerate(ITEMS):
        out.append("%s %s\n" % (">" if i == selected else " ", item))
    out.append("\nD-pad: move  Start: select\n")
    if selected < CREDITS:
        out.append("\n%s\n" % CLIENTS[selected][3])
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def read_key(fd, timeout):
    """Return a key name, or None if nothing we know arrived before the timeout."""
    global pending
    if not pending:
        if not select.select([fd], [], [], timeout)[0]:
            return None
        pending = os.read(fd, 64)
        if not pending:
            raise SystemExit(0)
    for sequence, name in KEYS.items():
        if pending.startswith(sequence):
            pending = pending[len(sequence):]
            return name
    # Skip a whole unknown sequence, so its bytes are not read as keys.
    match = UNKNOWN_SEQUENCE.match(pending)
    pending = pending[match.end() if match else 1:]
    return None


def wait_for_enter(fd):
    sys.stdout.write("\nPress Start to go back\n")
    sys.stdout.flush()
    while read_key(fd, None) != "enter":
        pass


def show_credits(fd):
    with open(os.path.join(HERE, "CREDITS.txt"), encoding="utf-8") as fh:
        text = fh.read()
    cols, rows = os.get_terminal_size(fd)
    lines = [part for line in text.splitlines() for part in textwrap.wrap(line, max(cols - 1, 20)) or [""]]
    height = max(rows - 2, 1)
    pages = [lines[i:i + height] for i in range(0, len(lines), height)] or [[]]
    page = 0
    while True:
        sys.stdout.write("\x1b[2J\x1b[H%s\n\nPage %d/%d  Up/Down: page  Start: back"
                         % ("\n".join(pages[page]), page + 1, len(pages)))
        sys.stdout.flush()
        key = None
        while key is None:
            key = read_key(fd, None)
        if key == "enter":
            return
        if key == "down":
            page = min(page + 1, len(pages) - 1)
        elif key == "up":
            page = max(page - 1, 0)


def run_client(index, port, fd, saved):
    global pending
    save_last_client(index)
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    sys.stdout.write("\x1b[2J\x1b[HConnecting to %s...\n" % port)
    sys.stdout.flush()
    code = subprocess.call(client_command(index, port), env=client_env(), cwd=DATA)
    # The client may have died with the terminal raw. Restore before reading any key.
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    sys.stdout.write(RESET_MODES + (LEAVE_ALT_SCREEN if code < 0 else ""))
    # Presses made while the client ran must not dismiss the pause below. setcbreak flushes input.
    tty.setcbreak(fd)
    pending = b""
    # meshcore-cli exits 0 when it cannot connect, so always leave the output on screen.
    sys.stdout.write("\nClient exited with code %d\n" % code if code else "\nClient closed\n")
    wait_for_enter(fd)


def main():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    os.makedirs(DATA, exist_ok=True)
    tty.setcbreak(fd)
    # A function, not SIG_IGN: handlers reset on exec, so a client still gets Ctrl-C.
    signal.signal(signal.SIGINT, lambda *_: None)
    selected = load_last_client()
    port_index = 0
    shown = None
    try:
        if not accept_storage(fd):
            return
        while True:
            ports = find_ports()
            port = ports[port_index % len(ports)] if ports else None
            if (selected, ports, port) != shown:
                draw(selected, ports, port)
                shown = (selected, ports, port)
            key = read_key(fd, 1)
            if key == "up":
                selected = (selected - 1) % len(ITEMS)
            elif key == "down":
                selected = (selected + 1) % len(ITEMS)
            elif key == "left":
                port_index -= 1
            elif key == "right":
                port_index += 1
            elif key == "quit":
                return
            elif key == "enter":
                if selected == QUIT:
                    return
                if selected == CREDITS:
                    show_credits(fd)
                elif not ports:
                    sys.stdout.write("\nNo node found. Plug one in and try again.\n")
                    sys.stdout.flush()
                    continue
                else:
                    run_client(selected, port, fd, saved)
                shown = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    main()
