"""Copy the licence files of the Python packages bundled into the Windows server build into <out>/<package>/ and write a notices file.

    python collect_licenses.py <out-dir> <notices-file>

Reads the installed distributions' metadata (the licence/notice files that ship in each dist-info). Run with the server build venv's Python.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

BUNDLED = ["fastapi", "uvicorn", "cryptography", "av", "Pillow", "numpy", "telethon", "httpx"]
if os.environ.get("OPENSEEK_LICENSE_PACKAGES"):          # e.g. the server bundle: a comma-separated package list
    BUNDLED = [p.strip() for p in os.environ["OPENSEEK_LICENSE_PACKAGES"].split(",") if p.strip()]
LICENSE_FILE = re.compile(r"(^|/)(licen[cs]es?|copying|notice|authors)([^/]*)$", re.IGNORECASE)


def main() -> int:
    out, notices = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in BUNDLED:
        d = metadata.distribution(name)
        md = d.metadata
        lic = md.get("License-Expression") or ""
        if not lic:
            cls = [c.split("::")[-1].strip() for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]
            raw = (md.get("License") or "").strip()
            lic = "; ".join(cls) or (raw.splitlines()[0][:80] if raw else "")
        copied = 0
        for f in d.files or []:
            sp = str(f).replace("\\", "/")
            if ".dist-info/" in sp and LICENSE_FILE.search(sp) and not sp.endswith((".py", ".pyc")):
                dest = out / name / Path(sp).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(d.locate_file(f), dest)
                copied += 1
        rows.append((md["Name"], md["Version"], lic or "see licence files", copied))
    lines = [
        "OpenSeek Server: third-party Python packages bundled in this build",
        "",
        "OpenSeek itself is licensed under the GNU Affero General Public License v3.0 or later (see LICENSE).",
        "Python itself (PSF License) and the packages below are included by PyInstaller. Each package's own licence files are in the",
        "licenses folder next to this file.",
        "",
        "FFmpeg, x264, x265 and the other native libraries bundled with the server: see SOURCE-OFFER.txt and",
        "licenses\\third-party\\SOURCES-MANIFEST.md.",
        "",
        f"{'package':24} {'version':14} licence (as declared in the package metadata)",
    ]
    for n, v, l, c in rows:
        lines.append(f"{n:24} {v:14} {l}   [{c} licence file(s) in licenses/]")
    lines += ["", "Not bundled: the OpenSeek server source tree or test dependencies."]
    notices.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"collected {sum(r[3] for r in rows)} licence files for {len(rows)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
