"""Miner Hearth v1.17.0: moving over from the old name (Nonce Keeper, v1.8 - v1.16.3).

This is the one program module that may spell the old product name: it has to find the old program's files,
registry entries and data formats. Nothing here deletes or changes the old copy; it is only read and copied.

First start of Miner Hearth (migrate_userdata):
  * If this copy already has its own data (userdata/devices.json, userdata/miner_hearth.json or a
    userdata/nonce_keeper.json left by an in-place upgrade), nothing is copied.
  * Otherwise the old data is looked for in, in this order: the folder the Miner Hearth installer recorded
    (HKCU\\Software\\Miner Hearth, LegacyDataDir), the old installer's InstallLocation
    (HKCU\\...\\Uninstall\\NonceKeeper), %LOCALAPPDATA%\\Programs\\Nonce Keeper, and folders next to this one
    (miner-autotune, Nonce Keeper, NonceKeeper: the old portable zip unpacked to miner-autotune).
  * userdata\\ (without the old live-update files in userdata\\updates, which are program files of the
    old version), logs\\ and config.json are copied. The old settings file nonce_keeper.json is also
    copied as miner_hearth.json. userdata\\migration.json records what was copied and from where.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

LEGACY_NAME = "Nonce Keeper"
LEGACY_EXE = "NonceKeeper.exe"
LEGACY_AUTOSTART_VALUE = "NonceKeeper"
LEGACY_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\NonceKeeper"
LEGACY_HOME_ENV = "NONCE_KEEPER_HOME"
LEGACY_SETTINGS_FILE = "nonce_keeper.json"
LEGACY_UPDATE_FORMAT = "nonce-keeper-update"
LEGACY_POOLS_FORMAT = "nonce-keeper-pools"
LEGACY_BACKUP_KEY = "nonce_keeper_backup"
LEGACY_REQUEST_HEADER = "NonceKeeper"
LEGACY_INSTALL_DIRNAME = "Nonce Keeper"
LEGACY_PORTABLE_DIRNAMES = ("miner-autotune", "Nonce Keeper", "NonceKeeper")

NEW_SETTINGS_FILE = "miner_hearth.json"
HEARTH_REG_KEY = r"Software\Miner Hearth"
MIGRATION_RECORD = "migration.json"
SKIP_USERDATA = {"updates", "instance.json", "app.lock"}
SKIP_LOGS = {"app.lock", "instance.json"}
MARKERS = ("devices.json", NEW_SETTINGS_FILE, LEGACY_SETTINGS_FILE, "saved_pools.json")


def has_own_data(userdata_dir: Path) -> bool:
    ud = Path(userdata_dir)
    return any((ud / m).exists() for m in MARKERS)


def _is_old_copy(folder: Path) -> bool:
    ud = Path(folder) / "userdata"
    return (ud / "devices.json").is_file() or (ud / LEGACY_SETTINGS_FILE).is_file()


def _reg_read(path: str, value: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            v, _ = winreg.QueryValueEx(k, value)
            return str(v or "")
    except OSError:
        return ""


def candidate_dirs(home: Path, env: Optional[dict[str, str]] = None,
                   reg_read: Optional[Callable[[str, str], str]] = None) -> list[Path]:
    env = dict(os.environ) if env is None else env
    rr = reg_read or _reg_read
    out: list[Path] = []
    for raw in (rr(HEARTH_REG_KEY, "LegacyDataDir"), rr(LEGACY_UNINSTALL_KEY, "InstallLocation"), env.get(LEGACY_HOME_ENV, "")):
        if raw:
            out.append(Path(raw.strip().strip('"')))
    if env.get("LOCALAPPDATA"):
        out.append(Path(env["LOCALAPPDATA"]) / "Programs" / LEGACY_INSTALL_DIRNAME)
    home = Path(home)
    for name in LEGACY_PORTABLE_DIRNAMES:
        out.append(home.parent / name)
    seen: list[Path] = []
    for p in out:
        try:
            same = p.resolve() == home.resolve()
        except OSError:
            same = False
        if not same and p not in seen:
            seen.append(p)
    return seen


def _copy_tree(src: Path, dst: Path, skip: set[str], top: bool = True) -> int:
    n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if top and item.name in skip:
            continue
        if item.is_symlink():
            continue
        target = dst / item.name
        if item.is_dir():
            n += _copy_tree(item, target, skip, top=False)
        elif item.is_file() and not target.exists():
            shutil.copy2(item, target)
            n += 1
    return n


def _merge_config(old_cfg: Path, new_cfg: Path, shipped_cfg: Optional[Path]) -> bool:
    """Use the old config.json, unless this copy's config.json was already changed by the user."""
    try:
        old = json.loads(old_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(old, dict):
        return False
    if new_cfg.exists():
        try:
            cur = new_cfg.read_bytes()
            if shipped_cfg is None or not shipped_cfg.exists() or cur != shipped_cfg.read_bytes():
                return False  # not the untouched shipped default: keep it
        except OSError:
            return False
    comment = None
    if shipped_cfg is not None and shipped_cfg.exists():
        try:
            comment = json.loads(shipped_cfg.read_text(encoding="utf-8")).get("_comment")
        except (OSError, ValueError, AttributeError):
            comment = None
    if comment:
        old["_comment"] = comment
    elif "_comment" in old:
        old["_comment"] = str(old["_comment"]).replace(LEGACY_NAME, "Miner Hearth")
    new_cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp = new_cfg.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, new_cfg)
    return True


def migrate_userdata(home: Path, shipped_config: Optional[Path] = None, candidates: Optional[Iterable[Path]] = None,
                     now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Copy an old Nonce Keeper copy's data into this Miner Hearth folder (first start only).

    Returns the migration record, or None when nothing was copied. Never raises."""
    try:
        home = Path(home)
        ud = home / "userdata"
        if has_own_data(ud):
            return None
        for src in (candidate_dirs(home) if candidates is None else candidates):
            src = Path(src)
            if not _is_old_copy(src):
                continue
            rec: dict[str, Any] = {"migrated_from": "Nonce Keeper", "source": str(src),
                                   "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now if now is not None else time.time())),
                                   "files": {}, "config": False, "note": "The old copy was left exactly as it was."}
            rec["files"]["userdata"] = _copy_tree(src / "userdata", ud, SKIP_USERDATA)
            if (src / "logs").is_dir():
                rec["files"]["logs"] = _copy_tree(src / "logs", home / "logs", SKIP_LOGS)
            if (src / "config.json").is_file():
                rec["config"] = _merge_config(src / "config.json", home / "config.json", shipped_config)
            ensure_settings_file(ud)
            (ud / MIGRATION_RECORD).write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
            return rec
    except Exception:
        return None
    return None


def ensure_settings_file(userdata_dir: Path) -> bool:
    """In-place upgrade: copy userdata/nonce_keeper.json to userdata/miner_hearth.json (old file kept)."""
    ud = Path(userdata_dir)
    new, old = ud / NEW_SETTINGS_FILE, ud / LEGACY_SETTINGS_FILE
    try:
        if not new.exists() and old.is_file():
            shutil.copy2(old, new)
            return True
    except OSError:
        pass
    return False
