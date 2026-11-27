#!/usr/bin/env python3
"""Tests for Miner Hearth v1.16.3: no console windows from child processes (synthetic values only)."""

from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import nk_proc
import miner_hearth

ROOT = Path(__file__).resolve().parent
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
OS_CALLS = {"system", "popen", "startfile", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
            "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
            "posix_spawn", "posix_spawnp"}


def _program_files() -> list[Path]:
    skip = {"build", "dist", "build-wine", "__pycache__", "userdata", "logs", "tools", "packaging"}
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in skip or part.startswith(".venv") for part in rel.parts[:-1]):
            continue
        if p.name.startswith("test_") or p.name == "mock_server.py":
            continue
        out.append(p)
    return out


class FakeStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 1


class NoBypassTest(unittest.TestCase):
    def test_every_process_start_goes_through_nk_proc(self):
        offenders = []
        files = _program_files()
        self.assertIn("miner_hearth.py", [p.name for p in files])
        for p in files:
            if p.name == "nk_proc.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "os"):
                    bad = [a.name for a in node.names if a.name in SUBPROCESS_CALLS | OS_CALLS or a.name == "*"]
                    if node.module == "subprocess" or bad:
                        offenders.append(f"{p.name}:{node.lineno} from {node.module} import {bad or '...'}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    mod, fn = node.func.value.id, node.func.attr
                    if (mod == "subprocess" and fn in SUBPROCESS_CALLS) or (mod == "os" and fn in OS_CALLS):
                        offenders.append(f"{p.name}:{node.lineno} {mod}.{fn}()")
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "_winapi":
                    offenders.append(f"{p.name}:{node.lineno} _winapi.{node.attr}")
        self.assertEqual(offenders, [], "start child processes with nk_proc.run / check_output / popen")

    def test_single_webview_window_created_only_at_startup(self):
        hits = []
        for p in _program_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_window":
                            hits.append((p.name, fn.name))
        self.assertTrue(hits)
        self.assertEqual({h for h in hits}, {("miner_dashboard.py", "run_native_window")})


class HiddenKwargsTest(unittest.TestCase):
    def test_windows_console_tool_is_hidden(self):
        kw = nk_proc.hidden_kwargs({"timeout": 2}, is_windows=True, startupinfo_factory=FakeStartupInfo)
        self.assertTrue(kw["creationflags"] & nk_proc.CREATE_NO_WINDOW)
        self.assertFalse(kw["creationflags"] & nk_proc.DETACHED_PROCESS)
        self.assertTrue(kw["startupinfo"].dwFlags & nk_proc.STARTF_USESHOWWINDOW)
        self.assertEqual(kw["startupinfo"].wShowWindow, nk_proc.SW_HIDE)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["timeout"], 2)

    def test_caller_flags_are_kept_but_never_detached_for_console_tools(self):
        kw = nk_proc.hidden_kwargs({"creationflags": nk_proc.DETACHED_PROCESS | 0x10}, is_windows=True,
                                   startupinfo_factory=FakeStartupInfo)
        self.assertEqual(kw["creationflags"], nk_proc.CREATE_NO_WINDOW | 0x10)

    def test_gui_relaunch_is_detached_without_sw_hide(self):
        kw = nk_proc.hidden_kwargs({"cwd": "x"}, gui=True, is_windows=True, startupinfo_factory=FakeStartupInfo)
        self.assertTrue(kw["creationflags"] & nk_proc.DETACHED_PROCESS)
        self.assertNotIn("startupinfo", kw)

    def test_non_windows_drops_windows_only_kwargs(self):
        kw = nk_proc.hidden_kwargs({"creationflags": 8, "timeout": 1}, is_windows=False)
        self.assertNotIn("creationflags", kw)
        self.assertNotIn("startupinfo", kw)

    def test_helpers_pass_hidden_kwargs_to_subprocess(self):
        with mock.patch.object(nk_proc, "IS_WINDOWS", True), \
                mock.patch.object(nk_proc.subprocess, "STARTUPINFO", FakeStartupInfo, create=True), \
                mock.patch.object(nk_proc.subprocess, "check_output", return_value="") as co, \
                mock.patch.object(nk_proc.subprocess, "run") as rn, \
                mock.patch.object(nk_proc.subprocess, "Popen") as po:
            nk_proc.check_output(["ipconfig"], text=True)
            nk_proc.run(["powershell.exe"])
            nk_proc.popen(["MinerHearth.exe"], gui=True)
        for m in (co, rn):
            kw = m.call_args.kwargs
            self.assertTrue(kw["creationflags"] & nk_proc.CREATE_NO_WINDOW)
            self.assertEqual(kw["startupinfo"].wShowWindow, nk_proc.SW_HIDE)
        self.assertTrue(po.call_args.kwargs["creationflags"] & nk_proc.DETACHED_PROCESS)


class TailscaleFallbackTest(unittest.TestCase):
    def setUp(self):
        miner_hearth._TS_FALLBACK.update({"ts": None, "ips": []})

    def tearDown(self):
        miner_hearth._TS_FALLBACK.update({"ts": None, "ips": []})

    def test_status_polls_do_not_run_ipconfig_every_time(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return "IPv4 Address. . . : 192.168.1.20\n"

        with mock.patch.object(miner_hearth.nk_proc, "check_output", side_effect=fake), \
                mock.patch.object(miner_hearth.socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(miner_hearth, "lan_ip_addresses", return_value=["192.168.1.20"]):
            for _ in range(20):  # one minute of 3-second dashboard refreshes
                self.assertEqual(miner_hearth.tailscale_ip_addresses(), [])
        self.assertLessEqual(len(calls), 2)  # Linux tries `ip` then `ifconfig` once; Windows `ipconfig` once

    def test_fallback_finds_tailnet_ip_and_refreshes_after_ttl(self):
        outs = ["IPv4 Address. . . : 100.99.42.10\n", "nothing\n"]
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return outs[0] if len(calls) == 1 else outs[1]

        with mock.patch.object(miner_hearth.nk_proc, "check_output", side_effect=fake):
            self.assertEqual(miner_hearth._tailscale_fallback_ips(now=1000.0), ["100.99.42.10"])
            self.assertEqual(miner_hearth._tailscale_fallback_ips(now=1060.0), ["100.99.42.10"])
            self.assertEqual(len(calls), 1)
            miner_hearth._tailscale_fallback_ips(now=1000.0 + miner_hearth.TAILSCALE_FALLBACK_TTL_SEC + 1)
            self.assertGreater(len(calls), 1)

    def test_toast_uses_hidden_helper(self):
        with mock.patch.object(miner_hearth, "IS_WINDOWS", True), \
                mock.patch.object(miner_hearth.nk_proc, "run") as rn, \
                mock.patch.object(miner_hearth.threading, "Thread") as th:
            th.side_effect = lambda target, **kw: mock.Mock(start=target)
            self.assertTrue(miner_hearth.Notifier().toast("Test", "Synthetic message"))
        self.assertEqual(rn.call_args.args[0][0], "powershell.exe")


class VersionTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "1.17.1")
        self.assertIn('"nk_proc"', (ROOT / "packaging" / "MinerHearth.spec").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
