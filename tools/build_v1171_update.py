#!/usr/bin/env python3
"""Build the v1.17.1 live update for v1.17.0 (dashboard background, its setting, version footer).

    python tools/build_v1171_update.py <out.zip> [--key my-update-key.txt]

The package holds the changed program files (app/*.py: need Quick restart), the background stylesheet with the
image embedded (ui/mh-background.css: loads at once), plugins/mh_restart_fix.py (loads at once; makes v1.17.0's
Quick restart work in the one-file Windows program) and NOTES.md."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import nk_ui1171  # noqa: E402

APP_FILES = ["autotune.py", "dashboard.py", "miner_hearth.py", "nk_ui1171.py"]
VERSION = "1.17.1"
MIN_APP = "1.17.0"
TITLE = "Miner Hearth v1.17.1: dashboard background"
NOTES = """# Miner Hearth v1.17.1

- Firelit stone-wall dashboard background (shows straight away).
- Settings > Appearance > "Dashboard background" (on by default) to turn it off.
- A footer with the running version ("Miner Hearth v1.17.1").

The setting and the footer are program changes: press **Quick restart** after applying.
**Roll back** returns to v1.17.0 (the footer disappears).
"""


def assemble(folder: Path) -> Path:
    folder = Path(folder)
    (folder / "app").mkdir(parents=True, exist_ok=True)
    (folder / "ui").mkdir(parents=True, exist_ok=True)
    (folder / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "tools" / "v1171_plugins" / "mh_restart_fix.py", folder / "plugins" / "mh_restart_fix.py")
    for name in APP_FILES:
        shutil.copy2(ROOT / name, folder / "app" / name)
    css = nk_ui1171.bundled_background_css(ROOT)
    if css is None:
        raise SystemExit("assets/dashboard-bg.jpg is missing")
    (folder / "ui" / nk_ui1171.BG_CSS_NAME).write_bytes(css)
    (folder / "NOTES.md").write_text(NOTES, encoding="utf-8")
    return folder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_v1171_update")
    ap.add_argument("out")
    ap.add_argument("--key", default="")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT / "tools"))
    import make_update
    with tempfile.TemporaryDirectory() as td:
        src = assemble(Path(td) / "pkg")
        args = ["build", str(src), a.out, "--version", VERSION, "--min-app", MIN_APP, "--title", TITLE,
                "--notes", "Dashboard background (shows at once). The Appearance setting and the version footer need Quick restart."]
        if a.key:
            args += ["--key", a.key]
        return make_update.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
