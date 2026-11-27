#!/usr/bin/env python3
"""Build (and optionally sign) a Miner Hearth live-update package.

    python tools/make_update.py genkey  my-update-key.txt          # once; keep the secret key file private
    python tools/make_update.py build   <folder> <out.zip> --version 1.17.1 --min-app 1.17.0 \
                                        [--title "..."] [--notes "..."] [--key my-update-key.txt]

<folder> holds only updatable files, laid out like the package:
    coins.json, plugins/<name>.py, ui/<file>.css|js|..., app/<module>.py, app/nk_adapters/<name>.py, NOTES.md
Share the .zip and its SHA-256 (printed); people who trust you add your public key in Settings > Live Updates.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nk_update  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make_update")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genkey", help="create an Ed25519 signing key")
    g.add_argument("keyfile")
    b = sub.add_parser("build", help="build a package from a folder")
    b.add_argument("folder")
    b.add_argument("out")
    b.add_argument("--version", required=True)
    b.add_argument("--min-app", default="")
    b.add_argument("--max-app", default="")
    b.add_argument("--title", default="")
    b.add_argument("--notes", default="")
    b.add_argument("--key", default="", help="secret key file from 'genkey' (signs the package)")
    a = ap.parse_args(argv)
    if a.cmd == "genkey":
        sk = secrets.token_bytes(32)
        p = Path(a.keyfile)
        if p.exists():
            print(f"{p} already exists; not overwritten", file=sys.stderr)
            return 1
        p.write_text(sk.hex() + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        print("secret key written to", p)
        print("public key (share this):", nk_update.ed25519_public_key(sk).hex())
        return 0
    sk = Path(a.key).read_text(encoding="utf-8").strip() if a.key else ""
    res = nk_update.build_package(Path(a.folder), Path(a.out), a.version, a.min_app, a.max_app, a.title, a.notes, sk)
    print(f"{a.out}: {res['files']} file(s), sha256 {res['sha256']}" + (" (signed)" if sk else " (unsigned)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
