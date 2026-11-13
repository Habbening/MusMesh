#!/usr/bin/env python3
"""Package MusMesh as a .muxapp for the muOS Archive Manager.

Usage: build_muxapp.py <wheel dir> <version> [--no-lock]
Writes dist/MusMesh_v<version>.muxapp. Run with Python 3.12, the version on the device: the
zipped libraries carry .pyc files, because a zip import cannot cache them at run time.
--no-lock skips the hash check, for test builds from wheels of another architecture.
"""
import glob
import hashlib
import io
import os
import py_compile
import re
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP = time.localtime()[:6]
APP = "MusMesh"
# Read their own files by path, so they cannot live in the zip. Native wheels are found by .so.
FILE_PACKAGES = {"contact", "certifi"}
# importlib.metadata lookups at run time need these dist-info directories.
KEEP_METADATA = {"contact", "meshtastic"}
# These fall back to pure Python without their .so, and speed does not matter here: zip them.
OPTIONAL_NATIVE = {"wcwidth", "dbus_fast", "pyyaml", "charset_normalizer"}
# meshcore only needs AES and SHA-256/HMAC, and the rest of Crypto is 200 loose files.
# The only Crypto native modules meshcore loads (AES-ECB, HMAC-SHA256, SHA256), traced on x86 with
# /proc/self/maps and read from the pycryptodome sources for aarch64. The aarch64 and x86 accelerated
# variants are kept beside the portable ones.
KEEP_CRYPTO_NATIVE = {"Cipher/_raw_aes", "Cipher/_raw_aes_armv8", "Cipher/_raw_aesni", "Cipher/_raw_ecb",
                      "Hash/_SHA256", "Hash/_SHA256_armv8", "Hash/_SHA256_shani", "Util/_cpuid_c", "Util/_strxor"}
SKIP_DIRS = {"tests", "test", "__pycache__", "SelfTest", "Protocol", "PublicKey", "Signature", "IO", "Math"}
APP_FILES = [
    ("app/mux_launch.sh", "mux_launch.sh", 0o755),
    ("app/glyph/musmesh.svg", "glyph/musmesh.svg", 0o644),
    ("app/osk-mesh.conf", ".musmesh/osk-mesh.conf", 0o644),
    ("musmesh.py", ".musmesh/musmesh.py", 0o644),
    ("LICENSE", ".musmesh/LICENSE", 0o644),
    # The warning screen points here for the details.
    ("README.md", ".musmesh/README.md", 0o644),
    ("gui/app.py", ".musmesh/gui/app.py", 0o644),
    ("gui/radios.py", ".musmesh/gui/radios.py", 0o644),
    ("gui/sdl.py", ".musmesh/gui/sdl.py", 0o644),
    ("gui/store.py", ".musmesh/gui/store.py", 0o644),
]
# Wheels that ship without the licence text their licence requires. From the projects' own sources.
EXTRA_LICENCES = {"pyserial": ["pyserial-LICENSE.txt"], "certifi": ["MPL-2.0.txt"],
                  # Compiled into protobuf's _message.so. The sdist names the licence but leaves it out.
                  "protobuf": ["utf8_range-LICENSE.txt"],
                  # Its licence points at per-file headers in C sources the wheel leaves out.
                  "pycryptodome": ["pycryptodome-NOTICES.txt"],
                  # Both ship code copied from the Python standard library, under the PSF licence.
                  "urllib3": ["PSF-2.0.txt"], "charset_normalizer": ["PSF-2.0.txt"]}
# Copyright holders whose notice sits in a shipped source file or README, not in the licence file.
# Each is quoted from, or attributed by, the package's own sdist.
EXTRA_COPYRIGHTS = {
    "dbus_fast": ["Copyright 2022 Bluetooth Devices authors, 2019 Tony Crisci (src/dbus_fast/__version__.py)"],
    "packaging": ["Copyright (c) 2017-present Ofek Lev, MIT (src/packaging/licenses/__init__.py, from hatch)"],
    "protobuf": ["Copyright 2023 Google LLC (utf8_range/utf8_range.c)"],
    "pyserial": ["(C) 2019 Google LLC, BSD-3-Clause (serial/urlhandler/protocol_cp2110.py)"],
    "charset_normalizer": ["Characters frequencies © 2012 Denny Vrandečić (README.md)",
                           "Copyright (c) Python Software Foundation, PSF-2.0 (cli/__main__.py, from CPython 3.12)"],
    "urllib3": ["Copyright (c) Python Software Foundation, PSF-2.0 (src/urllib3/util/ssl_match_hostname.py, "
                "src/urllib3/util/connection.py and parts of src/urllib3/connection.py, from CPython)"],
    "wcwidth": ["Includes work by Markus Kuhn (LICENSE)"],
    "prompt_toolkit": ["Vi digraph table taken from Neovim (src/prompt_toolkit/key_binding/digraphs.py)"],
}
# Shown on the Credits screen and at the top of the licence file.
MODIFIED_NOTE = ("Bundled packages are modified only by removing files: tests, type stubs, C sources,\n"
                 "packaging data, unused Crypto modules, optional compiled speedups.\n"
                 "Kept files are unchanged; compiled .pyc files are added next to them.\n"
                 "The full originals are in the release's -sources.zip.")
# Packages whose metadata has no project address.
KNOWN_URLS = {"meshtastic": "https://github.com/meshtastic/python"}


def normalise(name):
    return re.sub(r"[-_.]+", "_", name).lower()


def read_lock():
    lock = {}
    with open(os.path.join(HERE, "requirements.lock"), encoding="utf-8") as fh:
        for line in fh:
            match = re.match(r"([\w.-]+)==([\w.]+) --hash=sha256:(\w+)", line)
            if match:
                lock[normalise(match[1])] = (match[2], match[3])
    return lock


def check_lock(wheels, lock):
    """Every wheel must be locked with a matching hash, and every locked package present."""
    seen = set()
    for wheel in wheels:
        name, version = os.path.basename(wheel).split("-")[:2]
        with open(wheel, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if lock.get(normalise(name)) != (version, digest):
            raise SystemExit(f"{os.path.basename(wheel)} does not match requirements.lock")
        seen.add(normalise(name))
    missing = set(lock) - seen
    if missing:
        raise SystemExit(f"missing wheels: {sorted(missing)}")


def wanted(path):
    parts = path.split("/")
    if parts[0] == "Crypto" and parts[-1].endswith(".so"):
        return "/".join(parts[1:-1] + [parts[-1].split(".")[0]]) in KEEP_CRYPTO_NATIVE
    return not SKIP_DIRS.intersection(parts[:-1]) and not parts[-1].endswith((".pyc", ".pyi", ".pxd", ".c", ".h"))


def split_wheel(wheel):
    """Sort one wheel's files into loose files, zipped files, licences and metadata."""
    name = normalise(os.path.basename(wheel).split("-")[0])
    out = {"loose": {}, "zip": {}, "licence": {}, "metadata": ""}
    with zipfile.ZipFile(wheel) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        optional = name in OPTIONAL_NATIVE
        loose = name in FILE_PACKAGES or (not optional and any(n.endswith(".so") for n in names))
        for path in names:
            top, base = path.split("/")[0], path.rsplit("/", 1)[-1]
            if optional and base.endswith(".so"):
                continue
            # google is a namespace package: the protobuf .py files can zip, only the .so stays out.
            if name == "protobuf" and top == "google" and wanted(path):
                out["loose" if base.endswith(".so") else "zip"][path] = zf.read(path)
                continue
            if top.endswith(".dist-info"):
                if re.search(r"LICEN[SC]E|COPYING|NOTICE|AUTHORS|CREDITS", base, re.I):
                    out["licence"][path.split("/", 1)[1]] = zf.read(path)
                elif base == "METADATA":
                    out["metadata"] = zf.read(path).decode("utf-8", "replace")
                    if name in KEEP_METADATA:
                        out["loose"][path] = zf.read(path)
            elif not top.endswith(".data") and wanted(path):
                out["loose" if loose else "zip"][path] = zf.read(path)
    return name, out


def compiled(path, source):
    """Compile to an unchecked-hash .pyc, so zip timestamps never invalidate it."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "m.py"), os.path.join(tmp, "m.pyc")
        with open(src, "wb") as fh:
            fh.write(source)
        try:
            py_compile.compile(src, dst, dfile=path, doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        except py_compile.PyCompileError:
            return None
        with open(dst, "rb") as fh:
            return fh.read()


def build_vendor_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # A zip import only sees a namespace package (google) through explicit directory entries.
        dirs = {path.rsplit("/", i)[0] + "/" for path in files for i in range(1, path.count("/") + 1)}
        for path in sorted(dirs):
            zf.writestr(path, b"")
        for path, data in sorted(files.items()):
            zf.writestr(path, data)
            if path.endswith(".py"):
                pyc = compiled(path, data)
                if pyc:
                    zf.writestr(path + "c", pyc)
    return buf.getvalue()


def package_url(name, meta):
    home = re.search(r"^Home-page: (\S+)", meta, re.M)
    if home:
        return home[1]
    urls = {k.lower(): v for k, v in re.findall(r"^Project-URL: ([^,]+), (\S+)", meta, re.M)}
    for key in ("homepage", "source", "source code", "code", "repository"):
        if key in urls:
            return urls[key]
    others = [url for key, url in urls.items() if "changelog" not in key and "changes" not in key]
    return KNOWN_URLS.get(name) or next(iter(others), "")


def package_author(meta):
    match = re.search(r"^Author: (.+)$", meta, re.M) or re.search(r"^Author-email: (.+)$", meta, re.M)
    # Names only on a menu screen. Addresses stay in the licence file.
    name = re.sub(r"\s*<[^>]*>|\S+@\S+", "", match[1]) if match else ""
    return name.replace('"', "").replace("\\", "").strip()


def text_of(data):
    # Some licence files are Latin-1, and a name must not turn into a replacement character.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def copyright_lines(texts):
    """The copyright lines of a package's licence files, addresses removed, template lines skipped."""
    found = []
    for _, data in sorted(texts.items()):
        lines = text_of(data).splitlines()
        for number, line in enumerate(lines):
            # "Copyright 1996-2002, 2006 by" or "Copyright 2001-2015" with the holders on the next line.
            if re.search(r"\bby\s*$", line) or re.match(r"\s*Copyright [\d, -]+$", line):
                line += " " + next((later.strip() for later in lines[number + 1:] if later.strip()), "")
            line = re.sub(r"\s*<[^>]*(@|://)[^>]*>|\S+@\S+|\S+ \[at\] \S+", "", line).strip().rstrip(",")
            # libpng lists its holders as a " * Copyright ..." block.
            line = re.sub(r"^\*\s+", "", line)
            # SDL_gfx: "(C) A. Schiffler, <address> 2012-2018, licensed under the zlib license".
            line = re.sub(r",\s*licensed under .*$", "", line)
            # libpng's "Copyright (c) 1998-2000 Glenn Randers-Pehrson, are derived from ...": the holder only.
            line = re.sub(r",\s*are(\s.*)?$", "", line)
            line = re.sub(r"\s{2,}", " ", line)
            spdx = re.match(r"SPDX-FileCopyrightText:\s*(.+)", line)
            if spdx:
                line = spdx[1] if spdx[1].startswith("Copyright") else "Copyright " + spdx[1]
            if not re.match(r"(Copyright\s+(\(c\)|\(C\)|©|\d|[A-Z])|\([cC]\) (\d|[A-Z])|© ?\d)", line):
                continue
            # Licence templates and licence text that only looks like a holder line.
            if re.search(r"<|\[yyyy\]|Free Software Foundation|\bYou must\b", line) or line in found:
                continue
            found.append(line)
    return found


def packaged_note():
    return f"Packaged for MusMesh on {time.strftime('%Y-%m-%d')}.\n{MODIFIED_NOTE}"


def credits_text(packages, licences=None):
    lines = ["MusMesh, copyright (C) 2026 Habbening.", "Free software under the GPL-3.0-only. No warranty.",
             "Not affiliated with Meshtastic, MeshCore or muOS.",
             "The screens draw with the SDL2 and SDL2_ttf libraries that are part of muOS.",
             "The terminal clients run inside Mustard Terminal (muxterm), part of muOS.", "",
             "Licence texts: .musmesh/THIRD_PARTY_LICENSES.txt",
             "Source: the -sources.zip published with each release.", "", packaged_note(), "", "Built on:", ""]
    for name, meta in sorted(packages.items()):
        meta = meta.replace("\r", "")
        version = re.search(r"^Version: (.+)$", meta, re.M)[1]
        match = re.search(r"^License(?:-Expression)?: (.+)$", meta, re.M)
        classifier = re.search(r"^Classifier: License :: (?:OSI Approved :: )?(.+)$", meta, re.M)
        licence = match[1] if match else classifier[1] if classifier else "see licence file"
        lines.append(f"{name} {version}: {licence[:40]}")
        lines += [f"  {part}" for part in (package_author(meta), package_url(name, meta)) if part]
        lines += [f"  {part}" for part in copyright_lines(package_licences(name, licences))]
        lines += [f"  {part}" for part in EXTRA_COPYRIGHTS.get(name, [])]
    return "\n".join(lines) + "\n"


def package_licences(name, licences):
    texts = dict((licences or {}).get(name) or {})
    for extra in EXTRA_LICENCES.get(name, []):
        with open(os.path.join(HERE, "licences", extra), "rb") as fh:
            texts[extra] = fh.read()
    return texts


def licences_text(licences, metadata):
    """One file for every licence text. The build stops if a package has none."""
    out = ["MusMesh, copyright (C) 2026 Habbening. GPL-3.0-only, see LICENSE.\n"
           f"{packaged_note()}\n"]
    for name in sorted(metadata):
        texts = package_licences(name, licences)
        if not texts:
            raise SystemExit(f"{name}: no licence text. Add one to licences/ and EXTRA_LICENCES.")
        out.append(f"{'=' * 72}\n{name}\n{'=' * 72}\n")
        for base, data in sorted(texts.items()):
            out.append(f"--- {base} ---\n{text_of(data)}\n")
        if name in EXTRA_COPYRIGHTS:
            out.append("--- other copyright notices in this package ---\n" + "\n".join(EXTRA_COPYRIGHTS[name]) + "\n")
    return "\n".join(out)


def add_dir(zf, name):
    info = zipfile.ZipInfo(name.rstrip("/") + "/", STAMP)
    info.create_system = 3
    info.external_attr = (0o40755 << 16) | 0x10
    zf.writestr(info, b"")


def add_file(zf, name, data, mode=0o644):
    info = zipfile.ZipInfo(name, STAMP)
    info.create_system = 3
    info.external_attr = (0o100000 | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def main():
    wheel_dir, version = sys.argv[1], sys.argv[2]
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("build with Python 3.12, the zipped .pyc files must match the device")
    wheels = sorted(glob.glob(os.path.join(wheel_dir, "*.whl")))
    if "--no-lock" not in sys.argv:
        check_lock(wheels, read_lock())

    loose, zipped, metadata, licences = {}, {}, {}, {}
    for wheel in wheels:
        name, parts = split_wheel(wheel)
        loose.update(parts["loose"])
        zipped.update(parts["zip"])
        metadata[name] = parts["metadata"]
        licences[name] = parts["licence"]
    # Before the output is opened, so a licence gap leaves no partial package.
    licence_text = licences_text(licences, metadata)
    out_dir = os.path.join(HERE, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{APP}_v{version}.muxapp")
    with zipfile.ZipFile(out, "w") as zf:
        add_dir(zf, APP)
        for src, name, mode in APP_FILES:
            with open(os.path.join(HERE, src), "rb") as fh:
                add_file(zf, f"{APP}/{name}", fh.read(), mode)
        add_file(zf, f"{APP}/.musmesh/THIRD_PARTY_LICENSES.txt", licence_text.encode())
        for path, data in sorted(loose.items()):
            add_file(zf, f"{APP}/.musmesh/vendor/{path}", data, 0o755 if path.endswith(".so") else 0o644)
        add_file(zf, f"{APP}/.musmesh/vendor.zip", build_vendor_zip(zipped))
        add_file(zf, f"{APP}/.musmesh/CREDITS.txt", credits_text(metadata, licences).encode())
        total = len(zf.namelist())
    print(f"{out}: {os.path.getsize(out) / 1e6:.1f} MB, {total} entries, {len(loose)} loose, {len(zipped)} zipped")


if __name__ == "__main__":
    main()
