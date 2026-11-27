"""Miner Hearth v1.16.3: the only place that starts child processes.

MinerHearth.exe is a windowed (no-console) program. On Windows, when a windowed program starts a console
tool (ipconfig, powershell, ...) without CREATE_NO_WINDOW, Windows opens a new console window for the tool,
and it flashes on screen until the tool exits. Every child process therefore goes through run(),
check_output() or popen() here, which always pass CREATE_NO_WINDOW and a hidden STARTUPINFO on Windows.
test_v1163 fails if any other module calls subprocess / os.system / os.popen directly.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Optional

IS_WINDOWS = os.name == "nt"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def hidden_kwargs(kwargs: Optional[dict[str, Any]] = None, gui: bool = False, is_windows: Optional[bool] = None,
                  startupinfo_factory: Optional[Callable[[], Any]] = None) -> dict[str, Any]:
    """Return a copy of subprocess kwargs that cannot open a console window on Windows.

    gui=True is only for relaunching Miner Hearth itself: the child has its own window, so it is started
    detached (no console) but not with SW_HIDE, which would hide the dashboard window.
    """
    out = dict(kwargs or {})
    win = IS_WINDOWS if is_windows is None else bool(is_windows)
    out.setdefault("stdin", subprocess.DEVNULL)
    if not win:
        out.pop("creationflags", None)
        out.pop("startupinfo", None)
        return out
    flags = int(out.pop("creationflags", 0) or 0)
    if gui:
        flags |= DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        flags &= ~DETACHED_PROCESS  # CREATE_NO_WINDOW is ignored together with DETACHED_PROCESS
        flags |= CREATE_NO_WINDOW
        factory = startupinfo_factory or getattr(subprocess, "STARTUPINFO", None)
        si = out.get("startupinfo") or (factory() if factory else None)
        if si is not None:
            si.dwFlags = int(getattr(si, "dwFlags", 0) or 0) | STARTF_USESHOWWINDOW
            si.wShowWindow = SW_HIDE
            out["startupinfo"] = si
    out["creationflags"] = flags
    return out


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **hidden_kwargs(kwargs))  # noqa: S603


def check_output(cmd: list[str], **kwargs: Any) -> Any:
    return subprocess.check_output(cmd, **hidden_kwargs(kwargs))  # noqa: S603


def popen(cmd: list[str], gui: bool = False, **kwargs: Any) -> subprocess.Popen:
    return subprocess.Popen(cmd, **hidden_kwargs(kwargs, gui=gui))  # noqa: S603
