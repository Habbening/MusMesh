#!/bin/sh
# Run the whole suite in WSL. Needs the x86-64 test build in dist/ and the x86-64 wheels in wheels/x86.
set -e
cd "$(dirname "$0")/.."
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
python3 -I -c "import zipfile,sys;zipfile.ZipFile(sys.argv[2]).extractall(sys.argv[1])" "$T" "dist/MusMesh_v${1:-0.7}-x86.muxapp"
# The screen tests need SDL2 and SDL2_ttf (installed, or in MUSMESH_SDL_DIR) and any .ttf font.
MUSMESH_FONT="${MUSMESH_FONT:-$(find /usr/share/fonts -name '*.ttf' | head -n 1)}" MUSMESH_VERSION="${1:-0.7}" MUSMESH_BUILD="$T/MusMesh/.musmesh" \
	timeout 300 python3 -B -m unittest discover tests
