#!/usr/bin/env python3
"""Tests for Miner Hearth v1.15.5: block-found notifications reach every webhook, for any coin.

All data is synthetic: addresses are generated from made-up key hashes, machine names and pool
hosts are invented, and the block explorer is a local fake.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

import autotune
import nk_coins
import nk_extras
import miner_hearth

MINER_ADDR = nk_coins.cashaddr_encode("ecash", 0, bytes(range(1, 21)))          # pool username address
MINER_SCRIPT = "76a914" + bytes(range(1, 21)).hex() + "88ac"
OTHER_ADDR = nk_coins.cashaddr_encode("ecash", 0, bytes(range(101, 121)))       # a different wallet of the user
POOL_FEE_SCRIPT = "76a914" + "ee" * 20 + "88ac"
XEC_DIFF = 6.3e9
BTC_DIFF = 1.5e14
T0 = 1_790_000_000.0
DISCORD = "https://discord.com/api/webhooks/123/synthetic-token"
TG_TOKEN = "123456:ABCdefGHIjklMNOpqrSTUvwx"
NTFY = "https://ntfy.example.org/rigs"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeChain:
    """Block explorer for one coin: height -> (timestamp, coinbase scripts)."""

    def __init__(self, clock: Clock, tip: int = 900_000):
        self.clock = clock
        self.tip = tip
        self.blocks: dict[int, tuple[float, list[str]]] = {}
        self.calls = 0

    def mine(self, n: int = 1, pays: str = "", ts: float = 0.0) -> int:
        for _ in range(n):
            self.tip += 1
            self.blocks[self.tip] = (ts or self.clock.t, [pays or "76a914" + "00" * 20 + "88ac", POOL_FEE_SCRIPT])
        return self.tip

    def tip_height(self):
        return self.tip

    # v1.16.1: block hashes look real: hash = diff-1 target / WIN_DIFF (+ height in the low bits), so the winning
    # share's difficulty derived from the hash is WIN_DIFF (7.7G, synthetic).
    WIN_DIFF = 7.7e9

    def _hash(self, h):
        return f"{(0xFFFF * 2 ** 208) // int(self.WIN_DIFF) // 1000000 * 1000000 + h:064x}"

    def _height(self, bh):
        return int(bh, 16) % 1000000 + (self.tip // 1000000) * 1000000

    def block_hash_at(self, h):
        self.calls += 1
        return self._hash(h)

    def block(self, bh):
        h = self._height(bh)
        return {"timestamp": self.blocks.get(h, (0.0, []))[0], "height": h}

    def coinbase_addresses(self, bh):
        return sorted(nk_coins.coinbase_tokens([], self.blocks.get(self._height(bh), (0, []))[1]))

    def stats(self):
        return {"height": self.tip, "difficulty": XEC_DIFF}


def axe_raw(best_session: float, found: int | None = None, best: float = 3.0e10, **extra) -> dict:
    raw = {"ASICModel": "BM1370", "hashRate": 1200.0, "temp": 55.0, "vrTemp": 50.0, "power": 18.0, "frequency": 800,
           "uptimeSeconds": 50000, "sharesAccepted": 900, "sharesRejected": 1, "bestDiff": best, "bestSessionDiff": best_session,
           "networkDifficulty": XEC_DIFF, "blockHeight": 900_001, "stratumURL": "xec.pool.example", "stratumPort": 3339,
           "stratumUser": MINER_ADDR + ".rig1", "poolConnectionInfo": "Connected"}
    if found is not None:
        raw["foundBlocks"] = found
    raw.update(extra)
    return raw


def braiins_reading(best: float, found: int, host: str = "stratum+tcp://xec.pool.example:3339") -> autotune.Reading:
    return autotune.parse_antminer_readings(
        version_data={"VERSION": [{"BOSminer": "24.04", "Type": "Antminer S19 Pro"}]},
        summary_data={"SUMMARY": [{"Elapsed": 900000, "GHS 5s": 95000.0, "Accepted": 5000, "Best Share": best, "Found Blocks": found}]},
        stats_data={"STATS": [{}, {"BMMiner": "BOSminer", "temp_chip1": 65.0, "frequency": 625.0}]},
        pools_data={"POOLS": [{"POOL": 0, "URL": host, "User": MINER_ADDR + ".ant", "Status": "Alive", "Priority": 0,
                               "Stratum Active": True, "Accepted": 5000, "Best Share": best}]},
    )


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ud = Path(self.tmp.name) / "userdata"
        self.clk = Clock()
        self.chain = FakeChain(self.clk)
        self.sent: list[tuple[str, dict | bytes]] = []
        self.codes: list[int] = []
        self.hub = self.make_hub()

    def tearDown(self):
        self.tmp.cleanup()

    def poster(self, url, body, hdr, timeout):
        try:
            payload = json.loads(body)
        except Exception:
            payload = body
        self.sent.append((url, payload))
        return (self.codes.pop(0), "") if self.codes else (204, "")

    def make_hub(self, overrides: dict | None = None, **settings):
        hub = miner_hearth.FeatureHub(self.ud, clock=self.clk, fetch=self._offline)
        hub.notifier.toast = lambda *a, **k: False
        hub.notifier.play_local = lambda *a, **k: False
        hub.webhooks = nk_extras.Webhooks(poster=self.poster, async_send=False)
        try:
            hub.webhooks.sleep = lambda s: None
        except Exception:
            pass
        patch = {"webhooks": {"enabled": True, "discord_url": DISCORD}, "coins": {"overrides": overrides or {}}}
        patch.update(settings)
        hub.update_settings(patch)
        hub.chain = lambda coin: self.chain if coin == "XEC" else None
        hub.coin_stats["XEC"] = {"height": self.chain.tip, "difficulty": XEC_DIFF}
        hub.network = {"height": 950_000, "difficulty": BTC_DIFF}
        return hub

    @staticmethod
    def _offline(url, timeout=8):
        raise miner_hearth.nk_net.NetError("offline in tests")

    def cfg(self, name="Axe One", dtype="gamma"):
        return types.SimpleNamespace(name=name, type=dtype, bos_password="", bos_power_mgmt=False, web_password="", asic_max_c=65,
                                     vr_max_c=70, vr_target_c=65, error_high_pct=3.0, algorithm="", coin="", merged_coin="")

    def poll(self, cfg, reading, dt=15.0, hub=None):
        (hub or self.hub).on_poll(cfg, reading, self.clk.t)
        self.clk.t += dt

    def ticks(self, n=4, hub=None):
        for _ in range(n):
            (hub or self.hub).tick(self.clk.t)
            self.clk.t += 61

    def discord(self) -> list[str]:
        return [p["content"] for u, p in self.sent if u == DISCORD]

    def block_msgs(self, kind_word: str) -> list[str]:
        """Messages whose bold title starts with kind_word ("Block Confirmed" / "Block Found")."""
        return [m for m in self.discord() if ("**" + kind_word) in m]


class DetectionTests(Base):
    def test_second_block_in_a_session_is_detected_from_the_found_counter(self):
        """Session best is already above network difficulty after block 1, so it never moves for block 2."""
        cfg = self.cfg()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.0e10, found=1)))
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.0e10, found=1)))
        self.chain.mine(1, pays=MINER_SCRIPT)
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.0e10, found=2)))
        self.assertEqual(len(self.block_msgs("Block Found")), 1, self.discord())
        self.ticks()
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_braiins_found_blocks_counter_on_xec(self):
        cfg = self.cfg("Ant Two", "antminer")
        self.poll(cfg, braiins_reading(5.1e10, 3))   # all-time best already far above XEC difficulty
        self.poll(cfg, braiins_reading(5.1e10, 3))
        self.chain.mine(1, pays=MINER_SCRIPT)
        self.poll(cfg, braiins_reading(5.1e10, 4))
        self.ticks()
        self.assertEqual(len(self.block_msgs("Block Found")), 1, self.discord())
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_manual_btc_override_on_an_xec_pool_still_catches_the_block(self):
        cfg = self.cfg("Ant One", "antminer")
        self.hub = self.make_hub(overrides={"Ant One": "BTC"})
        self.poll(cfg, braiins_reading(4.0e8, 0))
        self.poll(cfg, braiins_reading(4.0e8, 0))
        self.chain.mine(1, pays=MINER_SCRIPT)
        r = braiins_reading(5.0e10, 0)
        r.found_blocks = None  # firmware that does not count blocks: only the share difficulty is left
        self.poll(cfg, r)
        self.ticks()
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_block_found_while_miner_hearth_was_closed(self):
        cfg = self.cfg()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        closed_at = self.clk.t
        # NK closed for 3 h; the miner finds a block 1 h in; 18 blocks pass
        self.chain.mine(5)
        self.chain.mine(1, pays=MINER_SCRIPT, ts=closed_at + 3600)
        self.chain.mine(12)
        self.clk.t = closed_at + 3 * 3600
        hub2 = self.make_hub()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10)), hub=hub2)
        self.ticks(8, hub=hub2)
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_manual_payout_address_does_not_hide_the_pool_username_address(self):
        self.hub = self.make_hub(coins={"payout_addresses": {"XEC": [OTHER_ADDR]}, "overrides": {}})
        cfg = self.cfg()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)))
        self.chain.mine(1, pays=MINER_SCRIPT)
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10)))
        self.ticks()
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_unrelated_coin_job_does_not_raise_false_xec_blocks(self):
        """Machine set to XEC but now on a coin Miner Hearth does not know (tiny job difficulty)."""
        self.hub = self.make_hub(overrides={"Axe One": "XEC"})
        cfg = self.cfg()
        raw = dict(axe_raw(1.0e8), networkDifficulty=1048576.0, stratumURL="other.pool.example", blockHeight=5_000_000)
        self.poll(cfg, autotune.parse_gamma_info(raw))
        self.poll(cfg, autotune.parse_gamma_info(dict(raw, bestSessionDiff=2.0e9)))
        self.ticks(2)
        self.assertEqual(self.discord(), [])


class DeliveryTests(Base):
    def _find_block(self, hub=None):
        cfg = self.cfg()
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)), hub=hub)
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(1.0e8)), hub=hub)
        self.chain.mine(1, pays=MINER_SCRIPT)
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10, found=1)), hub=hub)
        return cfg

    def test_possible_goes_out_at_once_then_confirmed_once(self):
        cfg = self._find_block()
        self.assertEqual(len(self.block_msgs("Block Found")), 1, "possible must not wait for the explorer")
        self.assertEqual(self.block_msgs("Block Confirmed"), [])
        self.ticks(6)
        # more polls of the same block (counter / session best unchanged) and a repeated counter bump
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10, found=1)))
        self.poll(cfg, autotune.parse_gamma_info(axe_raw(2.9e10, found=2)))
        self.ticks(3)
        hub2 = self.make_hub()  # restart
        self.ticks(3, hub=hub2)
        self.assertEqual(len(self.block_msgs("Block Found")), 1, self.discord())
        confirmed = self.block_msgs("Block Confirmed")
        self.assertEqual(len(confirmed), 1, self.discord())
        self.assertIn("explorer.e.cash/block-height/", confirmed[0])

    def test_telegram_and_ntfy_get_block_messages(self):
        self.hub = self.make_hub(webhooks={"enabled": True, "discord_url": DISCORD, "telegram_token": TG_TOKEN,
                                            "telegram_chat_id": "-100123", "ntfy_url": NTFY})
        self._find_block()
        self.ticks()
        urls = [u for u, _ in self.sent]
        self.assertEqual(sum(1 for u in urls if "api.telegram.org" in u), 2)
        self.assertEqual(sum(1 for u in urls if u == NTFY), 2)
        tg = [p for u, p in self.sent if "api.telegram.org" in u]
        self.assertIn("Block Confirmed", tg[-1]["text"])

    def test_maintenance_pause_never_hides_blocks(self):
        self.hub.pause_alerts("Axe One", 60)
        self._find_block()
        self.ticks()
        self.assertEqual(len(self.block_msgs("Block Confirmed")), 1, self.discord())

    def test_webhook_results_logged_with_status_and_rate_limit_retry(self):
        self.codes = [429, 204, 400]
        self._find_block()     # possible: 429 then 204 on retry
        self.ticks()           # confirmed: 400
        res = [e for e in self.hub.eventlog.iter_entries() if e["type"] == "webhook_result"]
        kinds = [(e["kind"], e["status"], e["ok"]) for e in res]
        self.assertIn(("block_possible", 204, True), kinds)
        self.assertIn(("block_confirmed", 400, False), kinds)
        self.assertEqual(next(e for e in res if e["kind"] == "block_possible")["attempts"], 2)
        logged = json.dumps(res)
        self.assertNotIn("synthetic-token", logged)
        self.assertNotIn(TG_TOKEN, logged)

    def test_send_test_block_notification(self):
        code, body, _ = self.hub.handle("POST", "/api/nk/test-webhook", "", b'{"kind":"block"}', False)
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("TEST: Block Found #", self.discord()[-1])
        self.assertTrue(self.discord()[-1].startswith("\u26CF"))
        code, body, _ = self.hub.handle("POST", "/api/nk/test-webhook", "", b'{}', False)
        self.assertIn("test alert", self.discord()[-1])
        import dashboard
        self.assertIn("Send test block notification", dashboard.DASHBOARD_HTML)


class ParserTests(unittest.TestCase):
    def test_found_block_counters(self):
        self.assertEqual(autotune.parse_gamma_info(axe_raw(1.0, found=3)).found_blocks, 3)
        self.assertEqual(autotune.parse_gamma_info(dict(axe_raw(1.0), blockFound=True)).found_blocks, 1)
        self.assertIsNone(autotune.parse_gamma_info(axe_raw(1.0)).found_blocks)
        self.assertEqual(braiins_reading(1.0, 7).found_blocks, 7)


if __name__ == "__main__":
    unittest.main()
