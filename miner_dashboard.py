#!/usr/bin/env python3
"""Miner Hearth desktop app (formerly Miner Autotune, v1.6+).

Single-window launcher used for MinerHearth.exe (PyInstaller --onefile --windowed;
it was MinerDashboard.exe up to v1.7):

* starts the autotune loop in a background thread,
* starts the dashboard HTTP server on 127.0.0.1 (configured port, or a free one if busy),
* opens the dashboard in a native window (pywebview, Edge WebView2 backend) instead of a browser,
* falls back to the default browser with a message box if WebView2 is missing,
* shuts everything down cleanly when the window is closed,
* allows only one running instance (a second launch focuses the existing window),
* writes logs to logs/ next to the exe because there is no console.

config.json, userdata/ and logs/ always live next to the .exe (or next to this
script when run from source), never in PyInstaller's temporary folder.

This module only uses the Python standard library, except the optional
`webview` (pywebview) import used for the native window.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

import nk_legacy

APP_NAME = "Miner Hearth"
LEGACY_APP_NAME = "Miner Autotune"
EXE_NAME = "MinerHearth.exe"
LEGACY_EXE_NAME = "MinerDashboard.exe"
# Same mutex as v1.6/v1.7 on purpose: an old MinerDashboard.exe and the new
# MinerHearth.exe can never run (and tune the same miners) at the same time.
MUTEX_NAME = "Local\\MinerAutotuneDashboardSingleInstance"
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
AUTOSTART_VALUE_NAME = "MinerHearth"
LEGACY_AUTOSTART_VALUE_NAME = "MinerAutotune"
# v1.17.0 rename: sign-in entries of older names that are moved to / replaced by this one
LEGACY_AUTOSTART_VALUE_NAMES = (LEGACY_AUTOSTART_VALUE_NAME, nk_legacy.LEGACY_AUTOSTART_VALUE)
MIGRATION_RESULT: Optional[dict[str, Any]] = None
RESTART_EXIT_CODE = 75  # container mode: "restart me" (the restart policy brings the new copy up)
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """Folder that holds config.json, userdata/ and logs/ (next to the exe)."""
    env_home = (os.environ.get("MINER_HEARTH_HOME", "") or os.environ.get(nk_legacy.LEGACY_HOME_ENV, "") or os.environ.get("MINER_AUTOTUNE_HOME", "")).strip()
    if env_home:
        return Path(env_home).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def ensure_config(home: Path) -> Path:
    """Make sure config.json exists next to the exe; copy the shipped default if missing.

    An existing config.json (and userdata/) from a v1.3-v1.5 folder is left untouched,
    so dropping the exe into that folder picks up the saved machines.
    """
    cfg = home / "config.json"
    if cfg.exists():
        return cfg
    shipped = bundle_dir() / "config.json"
    home.mkdir(parents=True, exist_ok=True)
    if shipped.exists() and shipped.resolve() != cfg.resolve():
        shutil.copy2(shipped, cfg)
    else:
        cfg.write_text(json.dumps({"devices": [], "userdata_file": "userdata/devices.json"}, indent=2) + "\n", encoding="utf-8")
    return cfg


def redirect_missing_std_streams(log_dir: Path) -> None:
    """A --noconsole exe has sys.stdout/sys.stderr = None.

    Normal logging already goes to logs/autotune.log, so stdout is discarded and
    stray tracebacks on stderr go to logs/app-errors.log.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, "_miner_stdout_discarded", True)
    if sys.stderr is None:
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(log_dir / "app-errors.log", "a", encoding="utf-8", buffering=1)


# ---------------------------------------------------------------------------
# Native message boxes (no console available)
# ---------------------------------------------------------------------------

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def message_box(text: str, title: str = APP_NAME, flags: int = MB_ICONINFO) -> None:
    if IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags | MB_SETFOREGROUND | MB_TOPMOST)
            return
        except Exception:
            pass
    try:
        print(f"[{title}] {text}", file=sys.stderr or sys.stdout)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------

class SingleInstance:
    """Windows named mutex; on other OSes an exclusive lock file (used by tests)."""

    def __init__(self, lock_dir: Path, name: str = MUTEX_NAME):
        self.lock_dir = Path(lock_dir)
        self.name = name
        self._handle: Any = None
        self._fh: Any = None
        self.acquired = False

    def acquire(self) -> bool:
        if IS_WINDOWS:
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                kernel32.CreateMutexW.restype = ctypes.c_void_p
                handle = kernel32.CreateMutexW(None, False, self.name)
                last_error = kernel32.GetLastError()
                if not handle:
                    self.acquired = True  # cannot create mutex: do not block the user
                    return True
                if last_error == 183:  # ERROR_ALREADY_EXISTS
                    kernel32.CloseHandle(ctypes.c_void_p(handle))
                    self.acquired = False
                    return False
                self._handle = handle
                self.acquired = True
                return True
            except Exception:
                self.acquired = True
                return True
        try:
            import fcntl

            self.lock_dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.lock_dir / ".miner-dashboard.lock", "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
            return True
        except (OSError, ImportError):
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            self.acquired = False
            return False

    def release(self) -> None:
        if IS_WINDOWS and self._handle:
            try:
                import ctypes

                ctypes.windll.kernel32.ReleaseMutex(ctypes.c_void_p(self._handle))
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            except Exception:
                pass
            self._handle = None
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self.acquired = False


def instance_file(home: Path) -> Path:
    return home / "logs" / "instance.json"


def write_instance_info(home: Path, url: str, port: int, mode: str) -> None:
    path = instance_file(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"pid": os.getpid(), "url": url, "port": port, "mode": mode, "started": time.time()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass


def read_instance_info(home: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(instance_file(home).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_instance_info(home: Path) -> None:
    try:
        instance_file(home).unlink()
    except OSError:
        pass


def focus_existing_window(title_prefix: Any = (APP_NAME, nk_legacy.LEGACY_NAME, LEGACY_APP_NAME)) -> bool:
    """Bring an existing Miner Hearth (or legacy Miner Autotune) window to the front (Windows only)."""
    prefixes = (title_prefix,) if isinstance(title_prefix, str) else tuple(title_prefix)
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith(prefixes):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_proc_type(_cb), 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ports / WebView2
# ---------------------------------------------------------------------------

def port_is_free(host: str, port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def choose_port(host: str, preferred: int) -> int:
    """Use the configured port if free, else let the OS pick a free one."""
    if port_is_free(host, preferred):
        return int(preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def webview2_installed() -> bool:
    """Detect the Evergreen WebView2 Runtime (shipped with Edge on Windows 10/11)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
    except ImportError:
        return False
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    ]
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and str(pv).strip() not in {"", "0.0.0.0"}:
                    return True
        except OSError:
            continue
    return False


def dotnet_framework_ok(min_release: int = 461808) -> bool:
    """pywebview's WebView2 backend runs on .NET Framework 4.7.2+ (built into Windows 10 1803+)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as k:
            release, _ = winreg.QueryValueEx(k, "Release")
            return int(release) >= int(min_release)
    except (OSError, ImportError, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Autostart (per-user Run key)
# ---------------------------------------------------------------------------

def set_autostart(enable: bool, exe_path: Optional[Path] = None) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Autostart is only available on Windows."
    try:
        import winreg
    except ImportError:
        return False, "winreg unavailable"
    exe = Path(exe_path or sys.executable).resolve()
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as k:
            # Always drop the entries of older names (they pointed at MinerDashboard.exe / the old exe).
            for legacy in LEGACY_AUTOSTART_VALUE_NAMES:
                try:
                    winreg.DeleteValue(k, legacy)
                except FileNotFoundError:
                    pass
            if enable:
                winreg.SetValueEx(k, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, f'"{exe}"')
                return True, f"{APP_NAME} will start automatically when you sign in to Windows."
            try:
                winreg.DeleteValue(k, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, f"{APP_NAME} will no longer start automatically."
    except OSError as exc:
        return False, f"Could not change autostart: {exc}"


def migrate_legacy_autostart(exe_path: Optional[Path] = None) -> Optional[str]:
    """v1.8 rename: if sign-in autostart was on for MinerDashboard.exe, move it to MinerHearth.exe.

    Only runs from the frozen MinerHearth.exe. Returns a log message when something changed.
    """
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return None
    try:
        import winreg
    except ImportError:
        return None
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            moved = []
            for legacy in LEGACY_AUTOSTART_VALUE_NAMES:
                try:
                    old_val, _ = winreg.QueryValueEx(k, legacy)
                except FileNotFoundError:
                    continue
                moved.append(str(old_val))
                winreg.DeleteValue(k, legacy)
            if not moved:
                return None
            exe = Path(exe_path or sys.executable).resolve()
            winreg.SetValueEx(k, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, f'"{exe}"')
            return f"Moved sign-in autostart from {', '.join(moved)} to {exe}"
    except OSError:
        return None


class DesktopApi:
    """Exposed to the dashboard page as window.pywebview.api (clipboard + file save)."""

    def __init__(self) -> None:
        self._window: Any = None

    def copy_text(self, text: str) -> bool:
        try:
            from miner_hearth import copy_to_clipboard

            return bool(copy_to_clipboard(str(text)))
        except Exception:
            return False

    def read_clipboard(self) -> str:
        try:
            from miner_hearth import read_from_clipboard

            return str(read_from_clipboard() or "")
        except Exception:
            return ""

    def save_file_b64(self, filename: str, b64: str) -> str:
        """v1.15.4: binary exports (the full event-log zip). Same dialog as save_file."""
        import base64 as _b64

        try:
            data = _b64.b64decode(str(b64 or ""), validate=False)
        except Exception as exc:
            return f"could not save: {exc}"
        safe = os.path.basename(str(filename or "export.zip")) or "export.zip"
        target: Optional[Path] = None
        webview = _load_webview()
        if webview is not None and self._window is not None:
            try:
                dialog = getattr(webview, "SAVE_DIALOG", 30)
                try:
                    dialog = webview.FileDialog.SAVE  # pywebview 5+
                except AttributeError:
                    pass
                res = self._window.create_file_dialog(dialog, save_filename=safe)
                if not res:
                    return ""
                target = Path(res if isinstance(res, str) else res[0])
            except Exception:
                target = None
        if target is None:
            downloads = Path.home() / "Downloads"
            target = (downloads if downloads.exists() else app_dir()) / safe
        try:
            target.write_bytes(data)
            return f"saved to {target}"
        except OSError as exc:
            return f"could not save: {exc}"

    def save_file(self, filename: str, content: str) -> str:
        """Ask where to save an export (CSV / backup JSON) and write it. Returns a status string."""
        safe = os.path.basename(str(filename or "export.txt")) or "export.txt"
        target: Optional[Path] = None
        webview = _load_webview()
        if webview is not None and self._window is not None:
            try:
                dialog = getattr(webview, "SAVE_DIALOG", 30)
                try:
                    dialog = webview.FileDialog.SAVE  # pywebview 5+
                except AttributeError:
                    pass
                res = self._window.create_file_dialog(dialog, save_filename=safe)
                if not res:
                    return ""
                target = Path(res if isinstance(res, str) else res[0])
            except Exception:
                target = None
        if target is None:
            downloads = Path.home() / "Downloads"
            target = (downloads if downloads.exists() else app_dir()) / safe
        try:
            target.write_text(str(content), encoding="utf-8", newline="")
            return f"saved to {target}"
        except OSError as exc:
            return f"could not save: {exc}"


# ---------------------------------------------------------------------------
# Background services (autotune loop + dashboard server)
# ---------------------------------------------------------------------------

class _StopRequested(Exception):
    pass


class AppServices:
    """Owns the tuner thread and the dashboard HTTP server for the desktop app."""

    def __init__(
        self,
        home: Path,
        config_path: Optional[Path] = None,
        dry_run: bool = False,
        port: int = 0,
        host: str = "",
    ):
        self.home = Path(home)
        self.config_path = Path(config_path) if config_path else self.home / "config.json"
        self.dry_run = bool(dry_run)
        self.port_override = int(port or 0)
        self.host_override = host or ""
        self.stop_event = threading.Event()
        self.tuner: Any = None
        self.shared: Any = None
        self.dashboard: Any = None
        self.csv_logger: Any = None
        self.thread: Optional[threading.Thread] = None
        self.hub: Any = None
        self.config: Any = None
        self.url = ""
        self.port = 0
        self._stopped = False
        self.instance: Any = None          # SingleInstance lock, released before a quick restart
        self.argv: list[str] = []          # command-line options to pass on after a quick restart

    def start(self) -> "AppServices":
        import autotune
        from dashboard import DashboardServer, SharedState

        raw = autotune.load_raw_config(self.config_path)
        config = autotune.parse_config(raw, self.home, dry_run=self.dry_run, config_path=self.config_path)
        self.config = config
        # The desktop app is always local-only unless config explicitly asks otherwise.
        host = self.host_override or config.dashboard_host or "127.0.0.1"
        preferred = self.port_override or int(config.dashboard_port or 8080)
        port = choose_port(host, preferred)

        device_defaults = {
            d.name: (False if not autotune.is_tunable(d) else (bool(d.enabled_default) if d.enabled_default is not None else False))
            for d in config.devices
        }
        self.shared = SharedState(
            [d.name for d in config.devices],
            config.state_file,
            dry_run=bool(config.dry_run),
            device_defaults=device_defaults,
            scan_subnet=config.scan_subnet,
            autotune_enabled=config.autotune_enabled,
        )
        config.dry_run = self.shared.is_dry_run()
        autotune.setup_logging(config.log_file, extra_handlers=[self.shared.log_handler()])
        log = autotune.LOG
        log.info("%s desktop app v%s", APP_NAME, autotune.APP_VERSION)
        log.info("App folder: %s", self.home)
        log.info("Loaded config %s", self.config_path)
        log.info("User data file: %s", config.userdata_file)
        log.info("Text log: %s", config.log_file)
        if MIGRATION_RESULT:
            log.info("First start after the rename: copied the data of %s from %s (%s); the old copy was not changed",
                     nk_legacy.LEGACY_NAME, MIGRATION_RESULT.get("source"), MIGRATION_RESULT.get("files"))
        if port != preferred:
            log.warning("Port %s is busy; dashboard using free port %s instead", preferred, port)

        self.csv_logger = autotune.CsvLogger(config.csv_file)
        self.tuner = autotune.Tuner(config, csv_logger=self.csv_logger, shared=self.shared)
        self.shared.attach_tuner(self.tuner)
        try:  # v1.16: after a quick restart, carry on where the tuner left off
            import nk_update

            n = nk_update.restore_restart_state(Path(config.userdata_file).parent, self.tuner)
            if n:
                log.info("Quick restart: tuner state restored for %d machine(s)", n)
        except Exception:
            log.exception("Could not restore the tuner state after a quick restart")
        self.dashboard = DashboardServer(self.shared, host, port).start()
        try:
            import miner_hearth

            self.hub = miner_hearth.attach_feature_hub(self.tuner, self.shared, start=True)
            self.hub.restart_hook = self.quick_restart
            try:
                import nk_update

                nk_update.mark_boot_ok(Path(config.userdata_file).parent)
                self.hub.updates.boot_info = dict(BOOT_OVERLAY)
            except Exception:
                pass
        except Exception:
            log.exception("Miner Hearth features could not start; autotune continues without them")
        moved = migrate_legacy_autostart()
        if moved:
            log.info(moved)
        self.port = int(self.dashboard.port)
        self.url = self.dashboard.url

        stop_event = self.stop_event
        wake_event = self.tuner._wake

        def _sleep(seconds: float) -> None:
            if stop_event.is_set():
                raise _StopRequested()
            wake_event.wait(max(0.0, float(seconds)))
            wake_event.clear()
            if stop_event.is_set():
                raise _StopRequested()

        self.tuner.sleeper = _sleep

        def _run() -> None:
            try:
                self.tuner.run(once=False)
            except _StopRequested:
                log.info("Autotune loop stopped")
            except Exception:
                log.exception("Autotune loop crashed")

        self.thread = threading.Thread(target=_run, name="autotune", daemon=True)
        self.thread.start()
        return self

    def quick_restart(self) -> None:
        """v1.16: finish the current tuner step, save the tuner state, start a new copy of Miner Hearth and exit.
        Miner settings are not touched: frequency / voltage stay where the tuner left them."""
        import autotune
        import nk_update

        log = autotune.LOG
        log.info("Quick restart requested (live update)")
        self.stop_event.set()
        if self.tuner is not None and hasattr(self.tuner, "wake"):
            self.tuner.wake()
        if self.thread is not None:
            self.thread.join(timeout=30.0)  # never in the middle of a frequency / voltage change
        try:
            n = nk_update.save_restart_state(Path(self.config.userdata_file).parent, self.tuner)
            log.info("Quick restart: saved tuner state for %d machine(s)", n)
        except Exception:
            log.exception("Quick restart: could not save the tuner state")
        self.stop(timeout=5.0)
        clear_instance_info(self.home)
        if self.instance is not None:
            try:
                self.instance.release()
            except Exception:
                pass
        if os.environ.get("MINER_HEARTH_RESTART_BY_EXIT", "").strip() == "1":
            # v1.17.0 container mode: exit and let the container's restart policy start the new copy
            # (a child started from PID 1 would be killed with it).
            log.info("Quick restart: exiting with code %d so the container restarts", RESTART_EXIT_CODE)
            try:
                import logging

                logging.shutdown()
            except Exception:
                pass
            os._exit(RESTART_EXIT_CODE)
        cmd = [sys.executable] + ([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())])
        cmd += [a for a in self.argv if not a.startswith("--resume-after")] + [f"--resume-after={os.getpid()}"]
        try:
            import nk_proc

            # our own exe: detached, no console, but not SW_HIDE (that would hide the dashboard window)
            # v1.17.1: PyInstaller 6.9+ one-file: the new copy must unpack itself, not reuse this copy's temp files
            env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
            nk_proc.popen(cmd, gui=True, cwd=str(self.home), close_fds=True, env=env)
        except Exception:
            log.exception("Quick restart: could not start Miner Hearth again")
        try:
            import logging

            logging.shutdown()
        except Exception:
            pass
        os._exit(0)

    def stop(self, timeout: float = 10.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        if self.tuner is not None and hasattr(self.tuner, "wake"):
            self.tuner.wake()
        try:
            import autotune

            autotune.LOG.info("Shutting down %s", APP_NAME)
        except Exception:
            pass
        if self.hub is not None:
            try:
                self.hub.stop()
            except Exception:
                pass
        if self.dashboard is not None:
            try:
                self.dashboard.stop()
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        if self.csv_logger is not None:
            try:
                self.csv_logger.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Window / browser front-ends
# ---------------------------------------------------------------------------

def _load_webview() -> Any:
    try:
        import webview  # type: ignore[import-not-found]

        return webview
    except Exception:
        return None


def run_native_window(url: str, title: str, icon: Optional[Path] = None) -> bool:
    """Open the dashboard in a native WebView2 window. Returns False if it could not be shown."""
    webview = _load_webview()
    if webview is None:
        return False
    try:
        api = DesktopApi()
        try:
            settings = getattr(webview, "settings", None)
            if isinstance(settings, dict):
                settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        create_kwargs: dict[str, Any] = dict(width=1440, height=960, min_size=(360, 560), text_select=True, confirm_close=False, js_api=api)
        try:
            api._window = webview.create_window(title, url, **create_kwargs)
        except TypeError:
            create_kwargs.pop("js_api", None)
            api._window = webview.create_window(title, url, **create_kwargs)
        kwargs: dict[str, Any] = {"debug": False}
        if IS_WINDOWS:
            kwargs["gui"] = "edgechromium"
        if icon is not None and icon.exists():
            kwargs["icon"] = str(icon)
        try:
            webview.start(**kwargs)
        except TypeError:
            kwargs.pop("icon", None)
            webview.start(**kwargs)
        return True
    except Exception:
        try:
            import autotune

            autotune.LOG.exception("Native window failed; falling back to the default browser")
        except Exception:
            pass
        return False


def run_browser_fallback(url: str, reason: str, wait: Callable[[], None]) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass
    if IS_WINDOWS:
        message_box(
            f"{reason}\n\n"
            f"The dashboard was opened in your default web browser instead:\n{url}\n\n"
            f"To get the app window, install the Microsoft Edge WebView2 Runtime:\n{WEBVIEW2_DOWNLOAD_URL}\n\n"
            f"{APP_NAME} keeps running while this message is open.\n"
            f"Click OK to stop {APP_NAME}.",
            flags=MB_ICONWARNING,
        )
    else:
        wait()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

BOOT_OVERLAY: dict[str, Any] = {}


def _wait_pid_gone(pid: int, timeout: float) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if IS_WINDOWS:
                import ctypes

                h = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))  # SYNCHRONIZE
                if not h:
                    return
                r = ctypes.windll.kernel32.WaitForSingleObject(h, 500)
                ctypes.windll.kernel32.CloseHandle(h)
                if r == 0:
                    return
            else:
                os.kill(int(pid), 0)
                time.sleep(0.3)
        except (OSError, AttributeError):
            return


def _bundled_version() -> str:
    """VERSION shipped with this program (read without importing the program modules)."""
    for d in (bundle_dir(), app_dir()):
        try:
            v = (d / "VERSION").read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return ""


def _userdata_dir(home: Path, config_arg: str = "") -> Path:
    """userdata folder without importing the program modules (they may come from a live update)."""
    try:
        cfgp = Path(config_arg) if config_arg else home / "config.json"
        if not cfgp.is_absolute():
            cfgp = home / cfgp
        raw = json.loads(cfgp.read_text(encoding="utf-8"))
        v = raw.get("userdata_file") or (raw.get("dashboard") or {}).get("userdata_file")
        if v:
            p = Path(str(v))
            return (p if p.is_absolute() else home / p).parent
    except Exception:
        pass
    return home / "userdata"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def run_headless(services: "AppServices", home: Path) -> int:
    """v1.17.0: serve the dashboard until SIGTERM / SIGINT / Ctrl+C (Linux server, Docker, Umbrel, 5tratumOS)."""
    import signal

    import autotune

    write_instance_info(home, services.url, services.port, "headless")
    autotune.LOG.info("Headless mode: dashboard at %s (open it from a browser; no app window)", services.url)

    def _stop(*_a: Any) -> None:
        services.stop_event.set()

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass
    try:
        while not services.stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="MinerHearth", description=f"{APP_NAME} desktop app")
    p.add_argument("--config", default="", help="Path to config.json (default: next to the exe)")
    p.add_argument("--dry-run", action="store_true", help="Never change frequency/voltage")
    p.add_argument("--port", type=int, default=0, help="Dashboard port (default: config, 8080; a free port if busy)")
    p.add_argument("--browser", action="store_true", help="Use the default browser instead of the app window")
    p.add_argument("--autostart", choices=["on", "off"], default="", help="Start automatically at Windows sign-in (on/off) and exit")
    p.add_argument("--headless", action="store_true", default=_env_flag("MINER_HEARTH_HEADLESS"),
                   help="Run only the web dashboard server (no app window), e.g. on a Linux server, Umbrel or in Docker")
    p.add_argument("--host", default=os.environ.get("MINER_HEARTH_HOST", ""),
                   help="Address the dashboard listens on (default: config, 127.0.0.1; 0.0.0.0 in Docker)")
    p.add_argument("--trusted-networks", default=os.environ.get("MINER_HEARTH_TRUSTED_NETWORKS", ""), metavar="CIDRS",
                   help="Comma-separated networks whose browsers get full control like this computer (behind Umbrel's or "
                        "5tratumOS's login proxy). Everyone else is a LAN client (PIN / read-only rules).")
    p.add_argument("--smoke-test", type=float, default=0.0, metavar="SECONDS", help=argparse.SUPPRESS)
    p.add_argument("--resume-after", type=int, default=0, metavar="PID", help=argparse.SUPPRESS)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    home = app_dir()
    redirect_missing_std_streams(home / "logs")

    if args.autostart:
        ok, msg = set_autostart(args.autostart == "on")
        message_box(msg, flags=MB_ICONINFO if ok else MB_ICONERROR)
        return 0 if ok else 1

    if args.resume_after:  # v1.16 quick restart: wait for the old copy to finish exiting
        _wait_pid_gone(args.resume_after, 30.0)
    instance = SingleInstance(home / "logs")
    acquired = instance.acquire()
    if not acquired and args.resume_after:
        for _ in range(60):
            time.sleep(0.5)
            if instance.acquire():
                acquired = True
                break
    if not acquired:
        if focus_existing_window():
            return 0
        info = read_instance_info(home)
        if info and info.get("url") and info.get("mode") == "browser":
            try:
                webbrowser.open(str(info["url"]))
            except Exception:
                pass
        message_box(f"{APP_NAME} is already running.", flags=MB_ICONINFO)
        return 0

    services: Optional[AppServices] = None
    try:
        global BOOT_OVERLAY, MIGRATION_RESULT
        if not args.config:  # v1.17.0: first start after the rename copies the old copy's data (old copy kept)
            MIGRATION_RESULT = nk_legacy.migrate_userdata(home, shipped_config=bundle_dir() / "config.json")
        else:
            nk_legacy.ensure_settings_file(_userdata_dir(home, args.config))
        try:  # v1.16: live-update program files load before any Miner Hearth module
            import nk_update

            BOOT_OVERLAY = nk_update.install_overlay(_userdata_dir(home, args.config), _bundled_version())
        except Exception:
            BOOT_OVERLAY = {"error": "overlay not loaded"}
        config_path = Path(args.config) if args.config else ensure_config(home)
        if not config_path.is_absolute():
            config_path = home / config_path
        if args.trusted_networks:
            import miner_hearth

            nets = miner_hearth.set_trusted_networks(args.trusted_networks)
            if nets:
                print(f"Trusted networks (full control like this computer): {', '.join(nets)}", file=sys.stderr or sys.stdout)
        env_port = int(os.environ.get("MINER_HEARTH_PORT", "0") or 0) if os.environ.get("MINER_HEARTH_PORT", "").isdigit() else 0
        services = AppServices(home, config_path=config_path, dry_run=args.dry_run, port=args.port or env_port, host=args.host)
        services.instance = instance
        services.argv = [a for a in (argv if argv is not None else sys.argv[1:])]
        services.start()
        import autotune

        title = f"{APP_NAME} v{autotune.APP_VERSION}"

        if args.smoke_test and args.smoke_test > 0:
            write_instance_info(home, services.url, services.port, "smoke-test")
            autotune.LOG.info("Smoke test: dashboard at %s for %.0fs", services.url, args.smoke_test)
            services.stop_event.wait(args.smoke_test)
            return 0

        def _wait_forever() -> None:
            try:
                while not services.stop_event.wait(1.0):
                    pass
            except KeyboardInterrupt:
                pass

        if args.headless or (not IS_WINDOWS and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") and sys.platform != "darwin"):
            return run_headless(services, home)

        icon = bundle_dir() / "miner_dashboard.ico"
        use_window = not args.browser
        if use_window and IS_WINDOWS and not webview2_installed():
            autotune.LOG.warning("Microsoft Edge WebView2 Runtime not found; using the default browser")
            write_instance_info(home, services.url, services.port, "browser")
            run_browser_fallback(services.url, "Microsoft Edge WebView2 Runtime was not found on this PC.", _wait_forever)
            return 0
        if use_window and IS_WINDOWS and not dotnet_framework_ok():
            autotune.LOG.warning(".NET Framework 4.7.2+ not found; using the default browser")
            write_instance_info(home, services.url, services.port, "browser")
            run_browser_fallback(
                services.url,
                "The app window needs Microsoft .NET Framework 4.7.2 or newer (included with Windows 10 version 1803+), which was not found.",
                _wait_forever,
            )
            return 0
        if use_window:
            write_instance_info(home, services.url, services.port, "window")
            autotune.LOG.info("Opening app window: %s", services.url)
            if run_native_window(services.url, title, icon):
                autotune.LOG.info("App window closed")
                return 0
            reason = "The app window could not be opened (pywebview / WebView2 error, see logs\\autotune.log)."
        else:
            reason = "Browser mode was requested (--browser)."
        write_instance_info(home, services.url, services.port, "browser")
        run_browser_fallback(services.url, reason, _wait_forever)
        return 0
    except Exception as exc:
        try:
            import autotune

            autotune.LOG.exception("Fatal error in desktop app")
        except Exception:
            pass
        message_box(f"{APP_NAME} could not start:\n\n{exc}\n\nSee logs\\autotune.log next to the program.", flags=MB_ICONERROR)
        return 1
    finally:
        if services is not None:
            services.stop()
        clear_instance_info(home)
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
