#!/usr/bin/env python3
"""Build the corresponding source archive for a release.

Usage: fetch_sources.py <version>
Writes dist/MusMesh_v<version>-sources.zip: this repo's tracked files, plus the PyPI source archive
of every package in requirements.lock. Archives are checked against PyPI's SHA-256 and never run.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def source_archive(name, version):
    info = json.loads(fetch(f"https://pypi.org/pypi/{name}/{version}/json"))
    for item in info["urls"]:
        if item["packagetype"] == "sdist":
            data = fetch(item["url"])
            if hashlib.sha256(data).hexdigest() != item["digests"]["sha256"]:
                raise SystemExit(f"{item['filename']}: hash does not match PyPI")
            return item["filename"], data
    raise SystemExit(f"{name} {version}: no source archive on PyPI")


def main():
    version = sys.argv[1]
    with open(os.path.join(HERE, "requirements.lock"), encoding="utf-8") as fh:
        locked = re.findall(r"^([\w.-]+)==([\w.]+) ", fh.read(), re.M)
    tracked = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True, check=True).stdout.split()
    # Working notes (tasks/) are not part of the source.
    tracked = [path for path in tracked if not path.startswith("tasks/")]
    # Everything the package is built from must be in the archive, so commit first.
    sys.path.insert(0, HERE)  # python -I leaves the script's folder off the path
    from build_muxapp import APP_FILES, EXTRA_LICENCES
    needed = [src for src, _, _ in APP_FILES] + ["licences/" + name for names in EXTRA_LICENCES.values()
                                                 for name in names]
    missing = sorted(set(needed) - set(tracked))
    if missing:
        raise SystemExit(f"not tracked, commit first: {', '.join(missing)}")
    out = os.path.join(HERE, "dist", f"MusMesh_v{version}-sources.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for path in tracked:
            zf.write(os.path.join(HERE, path), f"MusMesh/{path}")
        for name, ver in locked:
            filename, data = source_archive(name, ver)
            zf.writestr(f"third_party/{filename}", data)
            print(filename, len(data))
    print(f"{out}: {os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
