"""v1.17.0 tests: the rename to Miner Hearth (no old product name in user-facing files), migration from the old
name's data folders, live-update compatibility, node monitoring (mocked RPC / REST / Electrum, fake /proc and
/sys), the headless mode, trusted networks, and the Umbrel / 5tratumOS / Docker packaging files.
All data is synthetic."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import dashboard
import miner_dashboard
import miner_hearth
import nk_legacy
import nk_nodes
import nk_update

ROOT = Path(__file__).resolve().parent
OLD = re.compile(r"nonce[\s_-]?keeper", re.IGNORECASE)
RPC_PW = "synthetic-rpc-pass-71"
MAC_HEX = "0201036c6e640258030a10synthetic"
RUNE = "synthetic-rune-A1b2C3"


def _user_facing_files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(part in {"__pycache__", "userdata", "logs", "build", "build-wine", "dist", ".git"} for part in p.parts):
            continue
        if p.suffix.lower() in {".png", ".ico", ".exe", ".zip", ".pyc"}:
            continue
        out.append(p)
    return out


# Files allowed to name the old program: the changelog, the migration notes and code, and the tests.
ALLOWED_FILES = {"CHANGELOG.md", "docs/MIGRATION.md", "nk_legacy.py"}


class RenameTests(unittest.TestCase):
    def test_no_old_product_name_in_user_facing_files(self):
        bad = []
        for p in _user_facing_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in ALLOWED_FILES or rel.startswith("test_"):
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            in_migration_section = False
            for i, line in enumerate(lines, 1):
                if rel == "INSTALL.txt":
                    if line.startswith("MOVING FROM THE OLD NAME"):
                        in_migration_section = True
                    elif in_migration_section and re.match(r"^[A-Z][A-Za-z /]+$", line) and i < len(lines) and lines[i].startswith("---"):
                        in_migration_section = False
                if not OLD.search(line):
                    continue
                if rel == "INSTALL.txt" and in_migration_section:
                    continue
                if rel == "packaging/installer.nsi" and re.match(r"\s*!define OLD_", line):
                    continue
                bad.append(f"{rel}:{i}: {line.strip()[:100]}")
        self.assertEqual(bad, [], "old product name in user-facing files:\n" + "\n".join(bad))

    def test_dashboard_html_and_names(self):
        self.assertIsNone(OLD.search(dashboard.DASHBOARD_HTML))
        self.assertIn("<title>Miner Hearth", dashboard.DASHBOARD_HTML)
        self.assertIn('"MinerHearth.exe"', (ROOT / "miner_dashboard.py").read_text(encoding="utf-8"))
        self.assertEqual(miner_dashboard.APP_NAME, "Miner Hearth")
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "1.17.1")

    def test_installer_and_spec_names(self):
        nsi = (ROOT / "packaging" / "installer.nsi").read_text(encoding="utf-8")
        self.assertIn('!define EXE "MinerHearth.exe"', nsi)
        self.assertIn("$LOCALAPPDATA\\Programs\\Miner Hearth", nsi.replace("${APPNAME}", "Miner Hearth"))
        self.assertIn("Uninstall\\MinerHearth", nsi)
        self.assertIn("SecLegacy", nsi)
        spec = (ROOT / "packaging" / "MinerHearth.spec").read_text(encoding="utf-8")
        self.assertIn('name="MinerHearth"', spec)
        for mod in ("miner_hearth", "nk_legacy", "nk_nodes", "nk_ui117"):
            self.assertIn(f'"{mod}"', spec)
        self.assertIn('"test_v117"', spec)
        self.assertFalse((ROOT / "nonce_keeper.py").exists())

    def test_shipped_config_has_no_devices(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["devices"], [])
        self.assertIn("v1.17.1", cfg["_comment"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.old = self.base / "Nonce Keeper"
        (self.old / "userdata" / "logs").mkdir(parents=True)
        (self.old / "userdata" / "updates" / "current" / "app").mkdir(parents=True)
        (self.old / "logs").mkdir()
        (self.old / "userdata" / "devices.json").write_text(json.dumps({"devices": [{"name": "Synth Axe", "host": "192.0.2.10"}]}))
        (self.old / "userdata" / "nonce_keeper.json").write_text(json.dumps({"schema": 3, "ui": {"theme": "light"}}))
        (self.old / "userdata" / "saved_pools.json").write_text(json.dumps({"pools": []}))
        (self.old / "userdata" / "logs" / "nk-events-2026-11-01.jsonl").write_text("{}\n")
        (self.old / "userdata" / "updates" / "current" / "app" / "nonce_keeper.py").write_text("raise SystemExit\n")
        (self.old / "logs" / "autotune.log").write_text("old log\n")
        (self.old / "config.json").write_text(json.dumps({"_comment": "old", "devices": [], "scan_subnet": "192.0.2.0/24"}))
        self.shipped = self.base / "shipped.json"
        self.shipped.write_text(json.dumps({"_comment": "Miner Hearth v1.17.0", "devices": []}))
        self.new = self.base / "Miner Hearth"
        self.new.mkdir()
        self.snapshot = self._tree(self.old)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _tree(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def test_copies_data_and_leaves_old_copy_untouched(self):
        (self.new / "config.json").write_bytes(self.shipped.read_bytes())  # untouched shipped default
        rec = nk_legacy.migrate_userdata(self.new, shipped_config=self.shipped, candidates=[self.old], now=0)
        self.assertIsNotNone(rec)
        ud = self.new / "userdata"
        self.assertTrue((ud / "devices.json").is_file())
        self.assertTrue((ud / "saved_pools.json").is_file())
        self.assertTrue((ud / "logs" / "nk-events-2026-11-01.jsonl").is_file())
        self.assertTrue((self.new / "logs" / "autotune.log").is_file())
        self.assertFalse((ud / "updates").exists(), "old live-update files must not be copied")
        self.assertEqual(json.loads((ud / "miner_hearth.json").read_text())["ui"]["theme"], "light")
        cfg = json.loads((self.new / "config.json").read_text())
        self.assertEqual(cfg["scan_subnet"], "192.0.2.0/24")
        self.assertEqual(cfg["_comment"], "Miner Hearth v1.17.0")
        self.assertTrue(rec["config"])
        saved = json.loads((ud / "migration.json").read_text())
        self.assertEqual(saved["source"], str(self.old))
        self.assertEqual(self._tree(self.old), self.snapshot, "the old copy must be left exactly as it was")

    def test_no_migration_when_new_copy_has_data(self):
        (self.new / "userdata").mkdir()
        (self.new / "userdata" / "devices.json").write_text(json.dumps({"devices": []}))
        self.assertIsNone(nk_legacy.migrate_userdata(self.new, shipped_config=self.shipped, candidates=[self.old]))
        self.assertFalse((self.new / "userdata" / "saved_pools.json").exists())

    def test_never_overwrites_and_keeps_customised_config(self):
        (self.new / "userdata" / "logs").mkdir(parents=True)
        mine = self.new / "userdata" / "logs" / "nk-events-2026-11-01.jsonl"
        mine.write_text("mine\n")
        (self.new / "config.json").write_text(json.dumps({"_comment": "mine", "devices": []}))
        rec = nk_legacy.migrate_userdata(self.new, shipped_config=self.shipped, candidates=[self.old])
        self.assertIsNotNone(rec)
        self.assertEqual(mine.read_text(), "mine\n")
        self.assertFalse(rec["config"])
        self.assertEqual(json.loads((self.new / "config.json").read_text())["_comment"], "mine")

    def test_missing_or_empty_candidates(self):
        empty = self.base / "empty"
        empty.mkdir()
        self.assertIsNone(nk_legacy.migrate_userdata(self.new, candidates=[self.base / "nope", empty]))
        self.assertFalse((self.new / "userdata" / "migration.json").exists())

    def test_candidate_order_installed_and_portable(self):
        env = {"LOCALAPPDATA": str(self.base / "LAD"), "NONCE_KEEPER_HOME": str(self.base / "envhome")}
        reg = {(nk_legacy.HEARTH_REG_KEY, "LegacyDataDir"): str(self.base / "recorded"),
               (nk_legacy.LEGACY_UNINSTALL_KEY, "InstallLocation"): '"' + str(self.base / "oldinstall") + '"'}
        c = nk_legacy.candidate_dirs(self.new, env=env, reg_read=lambda k, v: reg.get((k, v), ""))
        self.assertEqual(c[:4], [self.base / "recorded", self.base / "oldinstall", self.base / "envhome",
                                 self.base / "LAD" / "Programs" / "Nonce Keeper"])
        self.assertIn(self.base / "miner-autotune", c)   # portable folder next to the new one
        self.assertIn(self.base / "Nonce Keeper", c)
        self.assertNotIn(self.new, c)

    def test_settings_file_upgrade_in_place(self):
        ud = self.base / "inplace"
        ud.mkdir()
        (ud / "nonce_keeper.json").write_text('{"schema": 3}')
        self.assertTrue(nk_legacy.ensure_settings_file(ud))
        self.assertTrue((ud / "miner_hearth.json").is_file())
        self.assertTrue((ud / "nonce_keeper.json").is_file())
        self.assertFalse(nk_legacy.ensure_settings_file(ud))

    def test_hub_reads_old_settings_file(self):
        ud = self.base / "hubud"
        ud.mkdir()
        s = miner_hearth.default_app_settings()
        s["ui"]["theme"] = "contrast"
        (ud / "nonce_keeper.json").write_text(json.dumps(s))
        hub = miner_hearth.FeatureHub(ud, fetch=lambda u, t=8: b"")
        self.assertEqual(hub.settings.section("ui")["theme"], "contrast")


class CompatTests(unittest.TestCase):
    def test_update_formats(self):
        self.assertEqual(nk_update.FORMAT, "miner-hearth-update")
        self.assertIn("nonce-keeper-update", nk_update.ACCEPTED_FORMATS)

    def test_old_backup_and_header_accepted(self):
        ok, _ = miner_hearth.validate_backup({"nonce_keeper_backup": 1, "userdata": {"devices": []}, "app_settings": {}})
        self.assertTrue(ok)
        ok, _ = miner_hearth.validate_backup({"miner_hearth_backup": 1, "userdata": {"devices": []}, "app_settings": {}})
        self.assertTrue(ok)
        g = miner_hearth.AccessGuard()
        lan = {"read_only": False, "pin": ""}
        self.assertIsNone(g.check("POST", "/api/x", "192.0.2.9", {"X-Requested-With": "NonceKeeper"}, lan))
        self.assertIsNone(g.check("POST", "/api/x", "192.0.2.9", {"X-Requested-With": "MinerHearth"}, lan))
        self.assertIsNotNone(g.check("POST", "/api/x", "192.0.2.9", {}, lan))

    def test_autostart_legacy_names(self):
        self.assertIn("NonceKeeper", miner_dashboard.LEGACY_AUTOSTART_VALUE_NAMES)
        self.assertIn("MinerAutotune", miner_dashboard.LEGACY_AUTOSTART_VALUE_NAMES)

    def test_old_overlay_not_loaded_over_newer_program(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            ud = Path(t)
            app = ud / "updates" / "current" / "app"
            app.mkdir(parents=True)
            (app / "nk_math.py").write_text("X = 1\n")
            nk_update._write_state(ud, {"enabled": True, "current": {"version": "1.16.2", "package_sha256": "0" * 64}})
            before = list(sys.meta_path)
            info = nk_update.install_overlay(ud, "1.17.0")
            self.assertFalse(info["app_overlay"])
            self.assertIn("older than this program", info["skipped"])
            self.assertEqual(sys.meta_path, before)
            nk_update._write_state(ud, {"enabled": True, "current": {"version": "1.17.1", "package_sha256": "1" * 64}})
            info = nk_update.install_overlay(ud, "1.17.0")
            try:
                self.assertTrue(info["app_overlay"])
            finally:
                sys.meta_path[:] = before


# ---------------------------------------------------------------------------
# Node monitoring (mocked)
# ---------------------------------------------------------------------------
class FakeRPC:
    """A Bitcoin-Core-like JSON-RPC endpoint for the injectable http function."""

    def __init__(self):
        self.blocks, self.headers, self.peers, self.calls, self.auth = 870000, 870000, 9, [], []
        self.down = False

    def __call__(self, url, body, headers, timeout, insecure):
        if self.down:
            raise OSError("[Errno 111] Connection refused")
        self.auth.append(headers.get("Authorization", ""))
        req = json.loads(body.decode())
        self.calls.append((url, req["method"]))
        res = {"getblockchaininfo": {"chain": "main", "blocks": self.blocks, "headers": self.headers, "verificationprogress": 0.99999999,
                                     "initialblockdownload": False, "size_on_disk": 650_000_000_000, "pruned": False, "time": 1_760_000_000},
               "getnetworkinfo": {"connections": self.peers, "connections_in": 1, "connections_out": self.peers - 1, "subversion": "/Satoshi:28.0.0/"},
               "getmempoolinfo": {"size": 4321, "bytes": 2_500_000, "usage": 9_000_000, "mempoolminfee": 0.00001}}[req["method"]]
        return 200, json.dumps({"result": res, "error": None, "id": req["id"]}).encode()


class FakeSock:
    def __init__(self, height):
        self.height, self.sent, self.closed = height, b"", False
        self._out = (json.dumps({"jsonrpc": "2.0", "id": 1, "result": ["Fulcrum 1.11.0", "1.4"]}) + "\n"
                     + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"height": height, "hex": "00"}}) + "\n").encode()

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        out, self._out = self._out, b""
        return out

    def close(self):
        self.closed = True


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ud = Path(self.tmp.name) / "userdata"
        self.ud.mkdir()
        self.rpc = FakeRPC()
        self.events = []
        self.s = dict(nk_nodes.default_settings(), enabled=True, behind_min=20, behind_blocks=3, poll_sec=60)
        self.now = [1_000_000.0]
        self.mon = nk_nodes.NodeMonitor(self.ud, lambda: self.s, lambda *a: self.events.append(a), clock=lambda: self.now[0],
                                        http=self.rpc, sock=lambda *a: FakeSock(self.rpc.blocks - 5), env={},
                                        network_height=lambda coin: None)

    def tearDown(self):
        self.tmp.cleanup()

    def add_btc(self, **kw):
        return self.mon.store.add(dict({"kind": "bitcoind", "name": "Home node", "host": "192.0.2.20", "user": "synthuser", "password": RPC_PW}, **kw))

    def test_bitcoind_metrics_and_auth(self):
        n = self.add_btc()
        self.mon.tick()
        st = self.mon.status[n["id"]]
        self.assertTrue(st["ok"], st)
        m = st["metrics"]
        self.assertEqual((m["blocks"], m["peers"], m["mempool_tx"], m["size_on_disk_gb"]), (870000, 9, 4321, 650.0))
        self.assertEqual(m["progress_pct"], 100.0)
        self.assertEqual(self.rpc.calls[0][0], "http://192.0.2.20:8332/")
        self.assertEqual(base64.b64decode(self.rpc.auth[0].split()[1]).decode(), f"synthuser:{RPC_PW}")
        self.assertIn("height 870000", st["summary"])

    def test_cookie_file_auth(self):
        ck = Path(self.tmp.name) / ".cookie"
        ck.write_text("__cookie__:synthcookievalue")
        self.add_btc(password="", user="", cookie_path=str(ck))
        self.mon.tick()
        self.assertEqual(base64.b64decode(self.rpc.auth[0].split()[1]).decode(), "__cookie__:synthcookievalue")

    def test_behind_alert_and_recovery(self):
        self.add_btc()
        self.rpc.headers = self.rpc.blocks + 5
        self.mon.tick()
        self.assertEqual(self.events, [])                       # held for behind_min
        self.now[0] += 21 * 60
        self.mon.tick()
        self.assertEqual(len(self.events), 1)
        self.assertIn("falling behind", self.events[0][0])
        self.assertEqual(self.events[0][2], "problem")
        self.rpc.headers = self.rpc.blocks
        self.now[0] += 61
        self.mon.tick()
        self.assertIn("caught up", self.events[-1][0])

    def test_down_after_three_failures_then_back(self):
        self.add_btc()
        self.rpc.down = True
        for _ in range(3):
            self.now[0] += 61
            self.mon.tick()
        self.assertTrue(any("unreachable" in e[0] for e in self.events))
        self.assertIn("connection refused", self.mon.public_status()["nodes"][0]["error"])
        self.rpc.down = False
        self.now[0] += 61
        self.mon.tick()
        self.assertIn("Node back", self.events[-1][0])

    def test_no_peers_alert(self):
        self.add_btc()
        self.rpc.peers = 0
        self.mon.tick()
        self.now[0] += 11 * 60
        self.mon.tick()
        self.assertTrue(any("no peers" in e[0] for e in self.events))

    def test_alerts_off_sends_nothing(self):
        self.s["alerts"] = False
        self.add_btc()
        self.rpc.down = True
        for _ in range(4):
            self.now[0] += 61
            self.mon.tick()
        self.assertEqual(self.events, [])

    def test_disabled_does_not_poll(self):
        self.s["enabled"] = False
        self.add_btc()
        self.mon.tick()
        self.assertEqual(self.rpc.calls, [])

    def test_other_coin_node(self):
        n = self.add_btc(coin="XEC", name="eCash node", port=8336)
        self.mon.tick()
        self.assertEqual(self.mon.status[n["id"]]["metrics"]["blocks"], 870000)
        self.assertEqual(self.mon.store.get(n["id"])["coin"], "XEC")

    def test_lnd_and_cln(self):
        seen = []

        def http(url, body, headers, timeout, insecure):
            seen.append((url, body, dict(headers)))
            if "8080" in url:
                return 200, json.dumps({"alias": "synth-lnd", "version": "0.18.0", "synced_to_chain": True, "block_height": 870000,
                                        "num_active_channels": 4, "num_inactive_channels": 1, "num_pending_channels": 0, "num_peers": 6}).encode()
            return 200, json.dumps({"alias": "synth-cln", "version": "v24.08", "blockheight": 869990, "num_active_channels": 2,
                                    "num_inactive_channels": 0, "num_pending_channels": 1, "num_peers": 3,
                                    "warning_bitcoind_sync": "Bitcoind is not up-to-date with network."}).encode()

        self.mon.http = http
        lnd = self.mon.store.add({"kind": "lnd", "host": "192.0.2.21", "macaroon_hex": base64.b64encode(b"synthmac").decode()})
        cln = self.mon.store.add({"kind": "cln", "host": "192.0.2.22", "rune": RUNE})
        self.mon.tick()
        ml, mc = self.mon.status[lnd["id"]]["metrics"], self.mon.status[cln["id"]]["metrics"]
        self.assertEqual((ml["channels_active"], ml["synced_to_chain"]), (4, True))
        self.assertEqual((mc["channels_pending"], mc["synced_to_chain"]), (1, False))
        lnd_req = [s for s in seen if "8080" in s[0]][0]
        self.assertEqual(lnd_req[0], "https://192.0.2.21:8080/v1/getinfo")
        self.assertEqual(lnd_req[2]["Grpc-Metadata-macaroon"], b"synthmac".hex())  # base64 converted to hex
        cln_req = [s for s in seen if "3010" in s[0]][0]
        self.assertEqual(cln_req[2]["Rune"], RUNE)
        self.assertIsNotNone(cln_req[1])  # clnrest needs POST

    def test_electrum_compared_with_node(self):
        btc = self.add_btc()
        el = self.mon.store.add({"kind": "electrum", "name": "Fulcrum", "host": "192.0.2.23", "compare_to": btc["id"]})
        self.mon.tick()
        self.assertEqual(self.mon.status[el["id"]]["metrics"]["blocks"], 869995)
        self.assertIn("Fulcrum", self.mon.status[el["id"]]["metrics"]["version"])
        self.now[0] += 21 * 60
        self.mon.tick(force=True)
        self.assertTrue(any("Electrum server behind" in e[0] for e in self.events))

    def test_umbrel_env_nodes_stay_in_memory(self):
        env = {"APP_BITCOIN_NODE_IP": "10.21.21.8", "APP_BITCOIN_RPC_PORT": "8332", "APP_BITCOIN_RPC_USER": "umbrel",
               "APP_BITCOIN_RPC_PASS": RPC_PW, "APP_ELECTRS_NODE_IP": "10.21.21.10", "APP_ELECTRS_NODE_PORT": "50001"}
        mon = nk_nodes.NodeMonitor(self.ud, lambda: self.s, lambda *a: None, http=self.rpc, sock=lambda *a: FakeSock(870000), env=env)
        ids = [n["id"] for n in mon.nodes()]
        self.assertEqual(ids, ["umbrel-bitcoin", "umbrel-electrs"])
        mon.tick()
        self.assertTrue(mon.status["umbrel-bitcoin"]["ok"])
        self.assertFalse((self.ud / "nodes.json").exists() and RPC_PW in (self.ud / "nodes.json").read_text())
        self.assertNotIn(RPC_PW, json.dumps(mon.public_status()))
        self.s["use_umbrel"] = False
        self.assertEqual(mon.nodes(), [])
        self.assertEqual(nk_nodes.umbrel_nodes({"APP_BITCOIN_NODE_IP": "", "APP_BITCOIN_RPC_PORT": ""}), [])

    def test_store_file_private_and_secrets_kept_on_edit(self):
        n = self.add_btc()
        p = self.ud / "nodes.json"
        self.assertEqual(json.loads(p.read_text())["format"], "miner-hearth-nodes")
        if os.name != "nt":
            self.assertEqual(p.stat().st_mode & 0o077, 0)
        self.mon.store.update(n["id"], {"kind": "bitcoind", "name": "Renamed", "host": "192.0.2.20", "user": "synthuser", "password": ""})
        self.assertEqual(self.mon.store.get(n["id"])["password"], RPC_PW)   # blank keeps the saved secret
        pub = self.mon.store.list_public()[0]
        self.assertTrue(pub["has_password"])
        self.assertNotIn("password", pub)

    def test_validation(self):
        with self.assertRaises(nk_nodes.NodeError):
            nk_nodes.validate_node({"kind": "nope", "host": "x"})
        with self.assertRaises(nk_nodes.NodeError):
            nk_nodes.validate_node({"kind": "bitcoind", "host": "192.0.2.1", "port": 70000})
        with self.assertRaises(nk_nodes.NodeError):
            nk_nodes.validate_node({"kind": "lnd", "host": "192.0.2.1", "macaroon_hex": "not hex or base64 !!"})
        e = nk_nodes.validate_node({"kind": "electrum", "host": "192.0.2.1", "ssl": True, "password": "x"})
        self.assertEqual((e["port"], e["password"]), (50002, ""))

    def test_host_stats_fake_proc_and_sys(self):
        root = Path(self.tmp.name)
        proc, sysr = root / "proc", root / "sys"
        (sysr / "class" / "thermal" / "thermal_zone0").mkdir(parents=True)
        (sysr / "class" / "thermal" / "thermal_zone1").mkdir(parents=True)
        proc.mkdir()
        (proc / "meminfo").write_text("MemTotal:       8000000 kB\nMemFree:  1000000 kB\nMemAvailable:   2000000 kB\n")
        (sysr / "class" / "thermal" / "thermal_zone0" / "temp").write_text("51000\n")
        (sysr / "class" / "thermal" / "thermal_zone1" / "temp").write_text("88500\n")
        hs = nk_nodes.HostStats(str(proc), str(sysr))
        if os.name == "nt":
            self.skipTest("Linux /proc and /sys readers")
        (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
        self.assertIsNone(hs.cpu_pct())
        (proc / "stat").write_text("cpu  150 0 150 900 0 0 0 0\n")
        self.assertEqual(hs.cpu_pct(), 50.0)
        self.assertEqual(hs.memory(), {"ram_total_gb": 8.2, "ram_used_pct": 75.0})
        self.assertEqual(hs.temperature_c(), 88.5)
        d = hs.disk(str(root))
        self.assertIn("disk_free_pct", d)
        self.assertEqual(hs.disk(str(root / "missing"))["disk_error"], "not readable")

    def test_host_alerts(self):
        class H:
            def snapshot(self, path):
                return {"disk_free_pct": 4.0, "disk_free_gb": 20.0, "disk_path": path, "temp_c": 90.0}
        self.mon.host = H()
        self.s.update(host_stats=True, low_disk_pct=10, temp_max_c=80)
        self.mon.tick()
        self.assertTrue(any("Low disk" in e[0] for e in self.events))
        self.now[0] += 400
        self.mon.tick(force=True)
        self.assertTrue(any("running hot" in e[0] for e in self.events))


class NodeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.hub = miner_hearth.FeatureHub(Path(self.tmp.name) / "userdata", fetch=lambda u, t=8: b"")
        self.hub.node_monitor.http = FakeRPC()

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, route, body=None, remote=False):
        st, data, _ = self.hub.handle(method, route, "", json.dumps(body or {}).encode(), remote)
        return st, json.loads(data)

    def test_save_list_and_no_secrets_anywhere(self):
        st, r = self.call("POST", "/api/nk/nodes/save", {"kind": "bitcoind", "name": "Synth", "host": "192.0.2.30", "user": "u", "password": RPC_PW})
        self.assertEqual(st, 200, r)
        self.assertNotIn(RPC_PW, json.dumps(r))
        st, r = self.call("GET", "/api/nk/nodes")
        self.assertTrue(r["nodes"][0]["has_password"])
        self.assertNotIn(RPC_PW, json.dumps(r))
        st, r = self.call("POST", "/api/nk/nodes/test", {"id": r["nodes"][0]["id"], "kind": "bitcoind", "host": "192.0.2.30"})
        self.assertTrue(r["ok"], r)                              # blank password uses the saved one
        self.hub.settings.update({"nodes": {"enabled": True}})
        self.hub.node_monitor.tick(force=True)
        payload = self.hub.status_payload([], remote=True)
        self.assertIn("nodes", payload)
        self.assertNotIn(RPC_PW, json.dumps(payload, default=str))
        self.assertNotIn(RPC_PW, json.dumps(self.hub.status_payload([], remote=False), default=str))

    def test_remote_clients_cannot_change_nodes(self):
        st, r = self.call("POST", "/api/nk/nodes/save", {"kind": "bitcoind", "host": "192.0.2.30"}, remote=True)
        self.assertEqual(st, 403)
        st, r = self.call("POST", "/api/nk/nodes/test", {"kind": "bitcoind", "host": "192.0.2.30"}, remote=True)
        self.assertEqual(st, 403)
        st, r = self.call("GET", "/api/nk/nodes", remote=True)
        self.assertEqual(st, 200)

    def test_settings_section_and_webhook_type(self):
        s = miner_hearth.default_app_settings()
        self.assertFalse(s["nodes"]["enabled"])
        self.assertIn("node", s["webhooks"]["types"])
        n = miner_hearth.normalize_app_settings(dict(s, nodes={"enabled": True, "poll_sec": 1, "low_disk_pct": 99}))["nodes"]
        self.assertTrue(n["enabled"])
        self.assertGreaterEqual(n["poll_sec"], 15)
        self.assertLessEqual(n["low_disk_pct"], 50)

    def test_dashboard_has_node_ui(self):
        h = dashboard.DASHBOARD_HTML
        for s in ('id="nkSetNodes"', 'id="nkNodes"', "nk117Render(data)", 'node: "Node alerts"'):
            self.assertIn(s, h)


# ---------------------------------------------------------------------------
# Headless mode and trusted networks
# ---------------------------------------------------------------------------
class HeadlessTests(unittest.TestCase):
    def tearDown(self):
        miner_hearth.set_trusted_networks("")

    def test_trusted_networks(self):
        self.assertFalse(miner_hearth.is_loopback("172.18.0.5"))
        self.assertEqual(miner_hearth.set_trusted_networks("172.16.0.0/12, 192.0.2.7 ,bogus"), ["172.16.0.0/12", "192.0.2.7/32"])
        self.assertTrue(miner_hearth.is_loopback("172.18.0.5"))
        self.assertTrue(miner_hearth.is_loopback("::ffff:192.0.2.7"))
        self.assertFalse(miner_hearth.is_loopback("192.0.2.8"))
        self.assertTrue(miner_hearth.is_loopback("127.0.0.1"))
        g = miner_hearth.AccessGuard()
        self.assertIsNone(g.check("POST", "/api/nk/settings", "172.18.0.5", {}, {"read_only": True, "pin": ""}))
        self.assertIsNotNone(g.check("POST", "/api/nk/settings", "192.0.2.8", {}, {"read_only": True, "pin": ""}))
        miner_hearth.set_trusted_networks("")
        self.assertFalse(miner_hearth.is_loopback("172.18.0.5"))

    def test_parser_and_env(self):
        old = {k: os.environ.get(k) for k in ("MINER_HEARTH_HEADLESS", "MINER_HEARTH_HOST", "MINER_HEARTH_TRUSTED_NETWORKS")}
        try:
            os.environ.update(MINER_HEARTH_HEADLESS="1", MINER_HEARTH_HOST="0.0.0.0", MINER_HEARTH_TRUSTED_NETWORKS="10.0.0.0/8")
            a = miner_dashboard.build_parser().parse_args([])
            self.assertTrue(a.headless)
            self.assertEqual((a.host, a.trusted_networks), ("0.0.0.0", "10.0.0.0/8"))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        a = miner_dashboard.build_parser().parse_args(["--headless", "--host", "127.0.0.1"])
        self.assertTrue(a.headless)

    @unittest.skipIf(os.name == "nt", "headless server test runs on Linux")
    def test_headless_server_runs_and_stops_on_sigterm(self):
        import signal
        import socket
        import urllib.request

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            env = dict(os.environ, MINER_HEARTH_HOME=t, PYTHONDONTWRITEBYTECODE="1")
            env.pop("DISPLAY", None)
            p = subprocess.Popen([sys.executable, str(ROOT / "miner_dashboard.py"), "--headless", "--dry-run", "--host", "127.0.0.1",
                                  "--port", str(port)], env=env, cwd=t, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                data = None
                for _ in range(60):
                    try:
                        data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2).read())
                        break
                    except OSError:
                        time.sleep(0.5)
                self.assertIsNotNone(data, "headless server did not answer")
                self.assertEqual(data["access"], {"remote": False, "read_only": False})
                self.assertIn("nodes", data["nk"])
                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
                self.assertIn("<title>Miner Hearth", html)
                p.send_signal(signal.SIGTERM)
                self.assertEqual(p.wait(timeout=30), 0)
                self.assertTrue((Path(t) / "config.json").is_file())
                self.assertEqual(json.loads((Path(t) / "config.json").read_text())["devices"], [])
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
                if p.stdout:
                    p.stdout.close()


# ---------------------------------------------------------------------------
# Packaging files (Docker, Umbrel, 5tratumOS)
# ---------------------------------------------------------------------------
class PackagingTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_dockerfile(self):
        d = self.read("Dockerfile")
        self.assertRegex(d, r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}")
        self.assertIn("USER 1000:1000", d)
        self.assertIn('VOLUME ["/data"]', d)
        self.assertIn("HEALTHCHECK", d)
        self.assertIn("MINER_HEARTH_HEADLESS=1", d)
        ign = self.read(".dockerignore")
        for s in ("userdata/", "logs/", "saved_pools.json", "*.exe"):
            self.assertIn(s, ign)
        wf = self.read(".github/workflows/docker.yml")
        self.assertIn("linux/amd64,linux/arm64", wf)
        self.assertIn("ghcr.io", wf)

    def test_umbrel_store(self):
        self.assertIn('id: "minerhearth"', self.read("umbrel/store/umbrel-app-store.yml"))
        app = self.read("umbrel/store/minerhearth-miner-hearth/umbrel-app.yml")
        self.assertIn("manifestVersion: 1", app)
        self.assertIn("id: minerhearth-miner-hearth", app)
        self.assertIn('version: "1.17.1"', app)
        dc = self.read("umbrel/store/minerhearth-miner-hearth/docker-compose.yml")
        self.assertIn("APP_HOST: minerhearth-miner-hearth_web_1", dc)
        self.assertIn("${APP_DATA_DIR}/data:/data", dc)
        self.assertIn("APP_BITCOIN_NODE_IP: ${APP_BITCOIN_NODE_IP:-}", dc)
        self.assertNotIn("ports:", dc)                          # only reachable through Umbrel's proxy
        self.assertTrue((ROOT / "umbrel/store/minerhearth-miner-hearth/data/.gitkeep").exists())

    def test_5tratumos_global_submission(self):
        base = ROOT / "5tratumos" / "global-submission" / "miner-hearth"
        for f in ("5tratstore-app.yml", "docker-compose.yml", "5tratstore-review.yml", "LICENSES.md", "icon.png"):
            self.assertTrue((base / f).is_file(), f)
        rev = (base / "5tratstore-review.yml").read_text()
        self.assertIn("status: proposed", rev)
        self.assertIn('reviewedBy: ""', rev)
        self.assertNotIn(": true", rev.split("compatibility:")[1].split("security:")[0])   # never self-approved
        for p in base.rglob("*"):
            if p.is_file() and p.suffix != ".png":
                self.assertNotRegex(p.read_text(), re.compile("umbrel", re.I), p.name)   # GLOBAL validator rule
        png = (base / "icon.png").read_bytes()
        self.assertEqual(png[16:24], (512).to_bytes(4, "big") * 2)

    def test_no_personal_data_in_packaging(self):
        txt = "".join((ROOT / r).read_text(encoding="utf-8") for r in (
            "Dockerfile", "deploy/docker-compose.yml", "deploy/miner-hearth.service", "umbrel/README.md",
            "umbrel/store/minerhearth-miner-hearth/docker-compose.yml", "5tratumos/README.md"))
        self.assertNotRegex(txt, r"\bbc1q[02-9ac-hj-np-z]{20,}|\bq[pz][02-9ac-hj-np-z]{38,}")


if __name__ == "__main__":
    unittest.main()
