#!/bin/sh
# HELP: Use and configure Meshtastic and MeshCore companion nodes over USB
# ICON: musmesh
# GRID: MusMesh

. /opt/muos/script/var/func.sh

APP_NAME="MusMesh"
APP_SUBPATH="application/$APP_NAME"
APP_DIRECTORY="$MUOS_SHARE_DIR/$APP_SUBPATH"
[ -d "$APP_DIRECTORY" ] || APP_DIRECTORY="$MUOS_STORE_DIR/$APP_SUBPATH"
LOG="/tmp/musmesh-launch.log"
MUXTERM="/opt/muos/frontend/muxterm"

TERM_CFG_DIR="/opt/muos/share/info/config/Terminal"
TERM_CFG="$TERM_CFG_DIR/$APP_NAME.conf"
mkdir -p "$TERM_CFG_DIR"
# Contact's three panes need about 80 columns. The font advance is 0.6 em, so size = width / 48.
if [ ! -e "$TERM_CFG" ]; then
	WIDTH=$(GET_VAR "device" "mux/width")
	case "$WIDTH" in
		"" | *[!0-9]*) WIDTH=640 ;;
	esac
	FONT_SIZE=$((WIDTH / 48))
	[ "$FONT_SIZE" -ge 10 ] || FONT_SIZE=10
	echo "term_font_size = $FONT_SIZE" >"$TERM_CFG"
fi

SETUP_STAGE_OVERLAY

cd "$APP_DIRECTORY/.musmesh" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
	echo "python3 not found" >"$LOG"
	SETUP_APP "muxterm" ""
	"$MUXTERM" --gl -- sh -c 'echo "MusMesh needs Python 3, not found on this device."; sleep 10'
	exit 1
fi

# The graphical client. It draws with the muOS SDL, like the terminal does.
SETUP_APP "python3" ""
: >"$LOG"
PYTHONPATH="vendor:vendor.zip" PYTHONDONTWRITEBYTECODE=1 HOME="$APP_DIRECTORY/.musmesh/data" python3 gui/app.py 2>>"$LOG"
GUI_EXIT=$?
echo "gui exit: $GUI_EXIT" >>"$LOG"
# 0: quit. 137/143: muOS killed it (KILL/TERM), so leave too. 3: the user picked the terminal
# clients. Anything else: the screens failed, so the terminal clients still give the user a way in.
# The reason is in the log.
case "$GUI_EXIT" in
	0 | 137 | 143) exit 0 ;;
esac

SETUP_APP "muxterm" ""
"$MUXTERM" --gl --no-sb-persist -c "$TERM_CFG" --osk-layout "$APP_DIRECTORY/.musmesh/osk-mesh.conf" -- python3 musmesh.py 2>>"$LOG"
