# MusMesh

Use Meshtastic nodes and MeshCore companion nodes from a muOS handheld, over USB.

MusMesh opens on its own screens: chats, direct messages and the node list, driven by the
handheld's buttons. For deeper settings it also carries two terminal clients, under **Terminal
clients**. Their code is unchanged. To save space the package leaves out tests, type stubs, C
sources, packaging data, unused crypto modules and optional compiled speedups.

| Part | Built on | Licence |
|---|---|---|
| MusMesh screens | The [Meshtastic Python library](https://github.com/meshtastic/python) and [meshcore_py](https://github.com/fdlamotte/meshcore_py), drawn with the SDL2 and SDL2_ttf that muOS has | GPL-3.0-only (Meshtastic), MIT (meshcore_py) |
| Terminal client, Meshtastic | [Contact](https://github.com/pdxlocations/contact) by Ben Lipsey | GPL-3.0-only |
| Terminal client, MeshCore (companion only) | [meshcore-cli](https://github.com/fdlamotte/meshcore-cli) by fdlamotte | MIT |

The app shows every bundled package in **Credits and licences**. Full texts are in
`.musmesh/THIRD_PARTY_LICENSES.txt`.

MusMesh is copyright (C) 2026 Habbening, GPL-3.0-only, no warranty. Not affiliated with Meshtastic,
MeshCore or muOS.

## Source

Each release has `MusMesh_v<version>-sources.zip` next to the package: this repo, plus the source
archive of every bundled package. `fetch_sources.py` builds it.

## Status

Tested on a Linux PC: every screen with a demo node, the radio adapters against stand-ins, the
launcher against stubs, the bundle, the Contact demo screen and `meshcore-cli -v`. On a GKD Pixel 2:
the v0.3 crypto modules, and the v0.6 screens with a Meshtastic node on USB against a second node
(channel and direct messages both ways, 200 bytes, accents, node list). Unplugging the node shows
"Connection lost"; in v0.6 plugging it back did not reconnect. Since v0.7 the screens connect again
by themselves when the node is back, and keep the chats; this was tested once on the GKD Pixel 2
with a Meshtastic node. Not tested yet: MeshCore with a real node, the Contact and meshcore-cli
terminal clients with a real node, any other handheld.

## Install

Needs an aarch64 muOS device with Python 3, a USB port in host mode, and kernel drivers for your
node's serial chip (`/dev/ttyACM*` or `/dev/ttyUSB*`).

Copy `MusMesh_v<version>.muxapp` to `ARCHIVE/` on the SD card and install it with Archive Manager.
The download is about 6 MB. It uses about 25 MB of card on 128 KB clusters, less on smaller ones.

## Using it

Pick **Meshtastic** or **MeshCore (companion)** to match your node's firmware. MusMesh does not
detect it. The menu starts on your last choice. With more than one node plugged in, Left/Right
changes node. **Demo, no node** shows the screens with made-up messages.

Planned: detect the node's firmware and pre-select the client.

Once connected:

- **Chats**: channels first, then direct messages, newest first. A number shows unread messages.
- **Nodes**: everyone your node has heard, with signal, hops and battery when the node reports
  them. A on a node opens a direct message to it.
- In a chat, A or X opens the keyboard. Start sends.

A message can be up to 200 bytes on Meshtastic and 140 on MeshCore. The keyboard counts them.
Letters like é take 2 bytes.

Wrong pick? The connect screen shows an error after a while. Press B and pick the other one.

MeshCore support is for companion firmware only. Repeater and room server firmware is not
supported. Pull requests welcome.

## What you accept by using it

### What is stored on the card

**Your SD card is not encrypted. Anyone with the card can read these files.** They live in
`MUOS/application/MusMesh/.musmesh/data/`. Delete that folder to remove them. Uninstalling the app
may leave it behind.

- `contact/client.db`: Meshtastic messages and node list, from the Contact terminal client.
- `contact/node-configs/`: config exports you make in Contact. These include channel keys.
- `.config/meshcore/`: meshcore-cli settings, saved repeater passwords, messages and command history.
  A private key you type at the prompt ends up in that history.

The MusMesh screens keep messages in memory only. They are gone when you leave.

Contact writes channel and WiFi keys to its log when you save settings. MusMesh points that log at
`/tmp/musmesh-contact.log` instead of the card, and it is gone on reboot. Terminal scrollback is not
saved.

MusMesh runs as root, like everything on muOS.

Anything you send goes out over your radio, on your channel.

### The warning screen

The first launch shows a summary of this on screen. A or Start accepts it, B quits. Deleting the data
folder brings the warning back.

## Controls

### MusMesh screens

| Button | Does |
|---|---|
| D-pad | Move. Hold to repeat |
| A | Open or select. In the keyboard: type the key |
| B | Back. In the keyboard: delete, or leave when empty |
| X | Write a message. In the keyboard: shift |
| Y | In the keyboard: space |
| L1 / R1 | Chats or Nodes. In the keyboard: symbols |
| Start | Send |
| Menu | Disconnect, back to the start screen |

### Terminal clients

These come from Mustard Terminal.

| Button | Does |
|---|---|
| D-pad | Arrow keys |
| Start | Enter |
| B | Backspace |
| Y | Space |
| Select | Show or move the keyboard |
| A (keyboard up) | Type the selected key |
| L1 / R1 (keyboard up) | Change keyboard layer |
| L2 / R2 | Scroll history |
| Menu | Terminal settings, font size, quit |

MusMesh adds a **Mesh** keyboard layer with the keys Contact uses: Esc, Help, Reply, Search, Setup,
Fav, Ignore, Trace, Packets.

Trace sends a traceroute over the air. Anything you send uses your radio and your channel.

## Display

The screens draw with the SDL2 and SDL2_ttf libraries of muOS, the ones the muOS terminal uses.
MusMesh carries no copy of them.

If the screens fail to start, the launcher opens the terminal clients instead. The reason is in
`/tmp/musmesh-launch.log`.

## Build

Python 3.12 is required, because the zipped libraries carry `.pyc` files for the device's Python.

```sh
mkdir -p wheels/aarch64
sed -n '/==/s/ --hash.*//p' requirements.lock | while read -r pkg; do
  python -m pip download --no-deps --only-binary=:all: --implementation cp --python-version 3.12 \
    --abi cp312 --abi abi3 --abi none --platform manylinux_2_28_aarch64 --platform manylinux_2_17_aarch64 \
    --platform manylinux2014_aarch64 --platform any -d wheels/aarch64 "$pkg"
done
python build_muxapp.py wheels/aarch64 0.7
python fetch_sources.py 0.7
```

The build stops if any wheel differs from the hash in `requirements.lock`, or a package has no
licence text.

## Test

On Linux, from the repo root:

```sh
python3 -m unittest discover tests
```

That runs the menu, launcher, store and radio adapter tests. To run the bundled clients and the
screens too, build an x86-64 test package (`build_muxapp.py <x86 wheels> <version> --no-lock`, then
rename it `MusMesh_v<version>-x86.muxapp`) and run `tests/run_wsl.sh <version>`. The screen tests
need SDL2 and SDL2_ttf: install them (Debian: `libsdl2-2.0-0`, `libsdl2-ttf-2.0-0`) or set
`MUSMESH_SDL_DIR` to a folder that holds them.
