"""Miner Hearth v1.17.1 live-update helper (no miner adapters).

Quick restart starts a new copy of MinerHearth.exe and closes the old one. The one-file Windows program is built with
PyInstaller 6.9+, where a copy started that way reuses the old copy's temporary files, which are deleted as the old
copy closes, so the new copy failed to start. Setting PYINSTALLER_RESET_ENVIRONMENT=1 makes it start as a fresh copy.
This file loads as soon as the update is applied, so the very first Quick restart (still run by v1.17.0) works too."""

import os
import sys

if getattr(sys, "frozen", False):
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

ADAPTERS: list = []
