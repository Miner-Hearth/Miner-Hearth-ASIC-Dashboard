#!/usr/bin/env python3
"""Tests for Miner Hearth v1.15.5: CheetahCoin (CHTA), pool-reported blocks, per-block dedupe, hourly batching,
and "coin unknown" block notices.

All data is synthetic: addresses are built from made-up key hashes, machine names / workers / hosts other than the
public pool host name are invented, and the explorer and pool APIs are local fakes.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

import autotune
import nk_coins
import nk_extras
import nk_poolfeed
import miner_hearth

CHTA_H160 = bytes(range(31, 51))
CHTA_ADDR = nk_coins.b58check_encode(b"\x1c" + CHTA_H160)
CHTA_SCRIPT = "76a914" + CHTA_H160.hex() + "88ac"
OTHER_CHTA = nk_coins.b58check_encode(b"\x1c" + bytes(range(151, 171)))
FEE_SCRIPT = "76a914" + "ee" * 20 + "88ac"
CHTA_HOST = "chta.heliospool.com"
XEC_DIFF = 6.3e9
BTC_DIFF = 1.5e14
CHTA_TIP = 5_000_000
T0 = 1_790_000_000.0
DISCORD = "https://discord.com/api/webhooks/123/synthetic-token"


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeChain:
    def __init__(self, clock, tip, diff):
        self.clock, self.tip, self.diff = clock, tip, diff
        self.blocks: dict[int, tuple[float, list[str]]] = {}

    def mine(self, n=1, pays="", ts=0.0):
        for _ in range(n):
            self.tip += 1
            self.blocks[self.tip] = (ts or self.clock.t, [pays or "76a914" + "00" * 20 + "88ac", FEE_SCRIPT])
        return self.tip

    def tip_height(self):
        return self.tip

    def block_hash_at(self, h):
        return f"{h:064x}"

    def block(self, bh):
        h = int(bh, 16)
        return {"timestamp": self.blocks.get(h, (0.0, []))[0], "height": h}

    def coinbase_addresses(self, bh):
        return sorted(nk_coins.coinbase_tokens([], self.blocks.get(int(bh, 16), (0, []))[1]))

    def stats(self):
        return {"height": self.tip, "difficulty": self.diff}


class FakeHelios:
    """HeliosPool API: JSON lines per file; only the us-west server answers (others are 'down')."""

    def __init__(self):
        self.files = {"blocks.pending.json": [], "blocks.json": [], "blocks.orphaned.json": []}
        self.calls = 0

    def add(self, fname, ts, height, addr=CHTA_ADDR, worker="gamma1", netdiff=0.004):
        self.files[fname].append({"timestamp": utc(ts), "height": height, "address": addr, "worker": worker, "device": "bitaxe/BM1370",
                                  "diff": 0.9, "netdiff": netdiff, "shares": 10, "luck": 1.0, "blockhash": f"{height:064x}"})

    def confirm(self, height):
        row = next(r for r in self.files["blocks.pending.json"] if r["height"] == height)
        self.files["blocks.pending.json"].remove(row)
        self.files["blocks.json"].append(dict(row, reward_total=31250000000, reward_miner=31000000000))

    def __call__(self, url, headers, timeout):
        self.calls += 1
        if "us-west" not in url:
            raise RuntimeError("down in tests")
        fname = url.rsplit("/", 1)[1]
        rows = self.files[fname]
        lines = [json.dumps(r) for r in rows]
        if headers.get("Range"):
            lines = ['ialrow": 1}'] + lines  # a Range response starts mid-line
        return ("\n".join(lines) + "\n").encode()


def gamma_raw(best_session=5.0e5, netdiff=3.7e6, host=CHTA_HOST, user=None, **extra):
    raw = {"ASICModel": "BM1370", "hashRate": 1200.0, "temp": 55.0, "vrTemp": 50.0, "power": 18.0, "frequency": 800,
           "uptimeSeconds": 50000, "sharesAccepted": 900, "sharesRejected": 1, "bestDiff": 9.0e8, "bestSessionDiff": best_session,
           "networkDifficulty": netdiff, "blockHeight": CHTA_TIP + 1, "stratumURL": host, "stratumPort": 3336,
           "stratumUser": (user if user is not None else CHTA_ADDR + ".gamma1"), "poolConnectionInfo": "Connected",
           "blockFound": 0, "showNewBlock": False}
    raw.update(extra)
    return raw


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ud = Path(self.tmp.name) / "userdata"
        self.clk = Clock()
        self.chta = FakeChain(self.clk, CHTA_TIP, 3.7e6)
        self.xec = FakeChain(self.clk, 900_000, XEC_DIFF)
        self.pool = FakeHelios()
        self.sent: list[tuple[str, dict]] = []
        self.explorers = {"CHTA": self.chta, "XEC": self.xec}
        self.hub = self.make_hub()

    def tearDown(self):
        self.tmp.cleanup()

    def poster(self, url, body, hdr, timeout):
        self.sent.append((url, json.loads(body)))
        return 204, ""

    def make_hub(self, overrides=None, **settings):
        hub = miner_hearth.FeatureHub(self.ud, clock=self.clk, fetch=self._offline)
        hub.notifier.toast = lambda *a, **k: False
        hub.notifier.play_local = lambda *a, **k: False
        hub.webhooks = nk_extras.Webhooks(poster=self.poster, async_send=False)
        hub.webhooks.sleep = lambda s: None
        hub.poolfeed.fetch = self.pool
        hub.poolfeed_async = False
        patch = {"webhooks": {"enabled": True, "discord_url": DISCORD}, "coins": {"overrides": overrides or {}}}
        patch.update(settings)
        hub.update_settings(patch)
        hub.chain = lambda coin: self.explorers.get(coin)
        hub.coin_stats["CHTA"] = {"height": self.chta.tip, "difficulty": 3.7e6}
        hub.coin_stats["XEC"] = {"height": self.xec.tip, "difficulty": XEC_DIFF}
        hub.network = {"height": 950_000, "difficulty": BTC_DIFF}
        return hub

    @staticmethod
    def _offline(url, timeout=8):
        raise miner_hearth.nk_net.NetError("offline in tests")

    def cfg(self, name="Axe One", coin=""):
        return types.SimpleNamespace(name=name, type="gamma", bos_password="", bos_power_mgmt=False, web_password="", asic_max_c=65,
                                     vr_max_c=70, vr_target_c=65, error_high_pct=3.0, algorithm="", coin=coin, merged_coin="")

    def poll(self, cfg, raw, dt=15.0, hub=None):
        (hub or self.hub).on_poll(cfg, autotune.parse_gamma_info(raw), self.clk.t)
        self.clk.t += dt

    def ticks(self, n=4, hub=None):
        for _ in range(n):
            (hub or self.hub).tick(self.clk.t)
            self.clk.t += 61

    def msgs(self, word):
        return [p["content"] for u, p in self.sent if u == DISCORD and ("**" + word) in p["content"]]


class CoinDataTests(unittest.TestCase):
    def test_chta_address_and_pool_host(self):
        self.assertTrue(CHTA_ADDR.startswith("C"))
        self.assertEqual(nk_coins.address_coins(CHTA_ADDR), {"CHTA"})
        self.assertEqual(nk_coins.address_to_script(CHTA_ADDR), CHTA_SCRIPT)
        self.assertEqual(nk_coins.host_hint("stratum+tcp://chta.heliospool.com:3336"), "CHTA")
        res = nk_coins.detect_coin({"pool_url": "stratum+tcp://chta.heliospool.com:3336", "pool_user": CHTA_ADDR + ".w"}, {})
        self.assertEqual(res["coin"], "CHTA")
        self.assertEqual(nk_poolfeed.helios_coin("stratum+tcp://chta.heliospool.com:4336"), "chta")
        self.assertIsNone(nk_poolfeed.helios_coin("stratum+tcp://pool.example.org:3333"))

    def test_spiky_difficulty_does_not_break_chain_matching(self):
        stats = {"CHTA": {"height": CHTA_TIP, "difficulty": 3.7e6}, "XEC": {"height": 900_000, "difficulty": XEC_DIFF}}
        self.assertEqual(nk_coins.chain_matches(CHTA_TIP + 1, 0.002, stats), ["CHTA"])

    def test_iquidus_adapter(self):
        txid = "aa" * 32
        bh = "0b" * 32
        pages = {
            "/api/getblockcount": b"5000123",
            "/api/getblockhash?index=5000123": ('"' + bh + '"').encode(),
            f"/api/getblock?hash={bh}": json.dumps({"hash": bh, "height": 5000123, "time": 1790000000, "tx": [txid, "bb" * 32]}).encode(),
            f"/api/getrawtransaction?txid={txid}&decrypt=1": json.dumps({"vout": [
                {"value": 310.0, "n": 0, "scriptPubKey": {"hex": CHTA_SCRIPT, "addresses": [CHTA_ADDR]}},
                {"value": 2.5, "n": 1, "scriptPubKey": {"hex": FEE_SCRIPT}}]}).encode(),
            "/api/getdifficulty": json.dumps({"proof-of-work": 0.0123}).encode(),
        }
        seen = []

        def fetch(url, timeout=8):
            seen.append(url)
            path = url.split(":3002", 1)[1]
            return pages[path]
        ch = nk_coins.MultiChain("CHTA", fetch=fetch)
        self.assertEqual(ch.tip_height(), 5000123)
        self.assertEqual(ch.block_hash_at(5000123), bh)
        self.assertEqual(ch.block(bh)["timestamp"], 1790000000)
        toks = ch.coinbase_addresses(bh)
        self.assertIn("script:" + CHTA_SCRIPT, toks)
        self.assertIn("script:" + CHTA_SCRIPT, nk_coins.target_tokens([CHTA_ADDR]))
        st = ch.stats()
        self.assertEqual((st["height"], st["difficulty"]), (5000123, 0.0123))
        self.assertTrue(all(u.startswith("http://chtaexplorer.mooo.com:3002/") for u in seen))

    def test_axeos_new_block_flag_and_settings_defaults(self):
        r = autotune.parse_gamma_info(gamma_raw(showNewBlock=True))
        self.assertIs(r.new_block_flag, True)
        self.assertIsNone(autotune.parse_gamma_info({k: v for k, v in gamma_raw().items() if k != "showNewBlock"}).new_block_flag)
        d = miner_hearth.normalize_app_settings({})
        self.assertEqual((d["block"]["pool_feed"], d["block"]["notify_unknown_coin"], d["block"]["max_messages_per_hour"]), (True, True, 6))
        d = miner_hearth.normalize_app_settings({"block": {"notify_unknown_coin": False, "max_messages_per_hour": 999}})
        self.assertEqual((d["block"]["notify_unknown_coin"], d["block"]["max_messages_per_hour"]), (False, 120))


class MinerTriggerTests(Base):
    def test_new_block_banner_flag_then_explorer_confirmation_with_chta_link(self):
        cfg = self.cfg()
        self.poll(cfg, gamma_raw())
        self.poll(cfg, gamma_raw())
        h = self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, gamma_raw(blockFound=1, showNewBlock=True))
        self.ticks(3)
        self.assertEqual(len(self.msgs("Block Found")), 1, self.sent)
        found = self.msgs("Block Confirmed")
        self.assertEqual(len(found), 1, self.sent)
        self.assertIn(f"http://chtaexplorer.mooo.com:3002/block/{h:064x}", found[0])

    def test_each_winning_share_is_its_own_block_and_never_counted_twice(self):
        cfg = self.cfg()
        self.poll(cfg, gamma_raw(lastSubmittedDiff=100.0))
        self.poll(cfg, gamma_raw(lastSubmittedDiff=200.0))            # 200 < 3.7M: not a block
        self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, gamma_raw(netdiff=0.002, lastSubmittedDiff=0.5))   # difficulty spiked down: the share is a block
        self.poll(cfg, gamma_raw(netdiff=0.002, lastSubmittedDiff=0.5))   # same share again: nothing new
        self.clk.t += 100
        self.chta.mine(1)
        self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, gamma_raw(netdiff=0.003, lastSubmittedDiff=0.8, showNewBlock=True))  # 2nd block (share + banner, one block)
        self.ticks(4)
        self.assertEqual(len(self.msgs("Block Found")), 2, self.sent)
        found = self.msgs("Block Confirmed")
        self.assertEqual(len(found), 2, self.sent)
        self.assertEqual(len({f.split("/block/")[1] for f in found}), 2, "two different blocks")

    def test_coin_set_to_xec_while_mining_chta_still_catches_the_block(self):
        self.hub = self.make_hub(overrides={"Axe One": "XEC"})
        cfg = self.cfg()
        self.poll(cfg, gamma_raw(netdiff=0.002))
        self.poll(cfg, gamma_raw(netdiff=0.002))
        self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, gamma_raw(netdiff=0.002, showNewBlock=True))
        self.ticks(3)
        self.assertEqual(len(self.msgs("Block Confirmed")), 1, self.sent)
        self.assertFalse([m for m in self.msgs("Block") if "Coin: XEC" in m])

    def test_no_explorer_still_sends_block_found_but_never_confirmed(self):
        self.explorers.pop("CHTA")
        cfg = self.cfg()
        self.poll(cfg, gamma_raw(host="cheetah.solo.example", user=CHTA_ADDR))
        self.poll(cfg, gamma_raw(host="cheetah.solo.example", user=CHTA_ADDR))
        self.poll(cfg, gamma_raw(host="cheetah.solo.example", user=CHTA_ADDR, showNewBlock=True))
        self.ticks(3)
        p = self.msgs("Block Found")
        self.assertEqual(len(p), 1, self.sent)
        self.assertTrue(p[0].startswith("\u26cf\ufe0f **Block Found**\n"), p[0])  # no height known: no number
        self.assertEqual(self.msgs("Block Confirmed"), [])


class UnknownCoinTests(Base):
    def _run(self):
        cfg = self.cfg()
        raw = dict(gamma_raw(host="mystery.pool.example", user="someaccount.rig", netdiff=1048576.0, best_session=2.0e6),
                   blockHeight=77_000_000)
        raw.pop("blockFound")
        self.poll(cfg, dict(raw, foundBlocks=0))
        self.poll(cfg, dict(raw, foundBlocks=0))
        self.poll(cfg, dict(raw, foundBlocks=1))
        self.ticks(3)

    def test_unknown_coin_counter_increment_notifies(self):
        self._run()
        m = self.msgs("Block Found")
        self.assertEqual(len(m), 1, self.sent)
        self.assertIn("Coin: unknown (pool mystery.pool.example)", m[0])
        self.assertIn("Payout address: someaccount (pool account name, not an address)", m[0])
        self.assertIn("Worker: rig", m[0])
        self.assertEqual(len(self.sent), 1, "no 'not confirmed' follow-ups, no BTC explorer guess")

    def test_unknown_coin_notice_can_be_turned_off(self):
        self.hub = self.make_hub(block={"notify_unknown_coin": False})
        self._run()
        self.assertEqual(self.sent, [])


class PoolFeedTests(Base):
    def _register(self, name="Axe One", worker="gamma1"):
        cfg = self.cfg(name)
        self.poll(cfg, gamma_raw(user=CHTA_ADDR + "." + worker))
        return cfg

    def test_pool_reported_block_found_then_chain_confirmed_once(self):
        self._register()
        self._register("Axe Two", "gamma2")
        h = self.chta.mine(1, pays=CHTA_SCRIPT, ts=self.clk.t - 20)
        self.pool.add("blocks.pending.json", self.clk.t - 20, h)
        self.pool.add("blocks.pending.json", self.clk.t - 20, h + 1, addr=OTHER_CHTA)   # somebody else's block
        self.ticks(1)
        p = self.msgs("Block Found")
        self.assertEqual(len(p), 1, self.sent)
        self.assertIn(f"**Block Found #{h}**", p[0])
        for line in (f"Payout address: {CHTA_ADDR}", "Worker: gamma1", "Share difficulty: 0.9", "Network difficulty: 0.004"):
            self.assertIn(line, p[0])
        self.chta.mine(2)
        self.ticks(3)
        conf = self.msgs("Block Confirmed")
        self.assertEqual(len(conf), 1, self.sent)
        self.assertIn(f"**Block Confirmed #{h}**", conf[0])
        self.assertIn(f"http://chtaexplorer.mooo.com:3002/block/{h:064x}", conf[0])
        self.assertEqual(self.msgs("Block Found"), p)
        self.assertFalse([m for m in self.msgs("Block") if "Axe Two" in m])
        hub2 = self.make_hub()   # restart: nothing is sent again
        hub2._pool_users = dict(self.hub._pool_users)
        n = len(self.sent)
        self.ticks(3, hub=hub2)
        self.assertEqual(len(self.sent), n, self.sent[n:])

    def test_pool_confirmed_list_is_not_called_confirmed_without_the_chain(self):
        self.explorers.pop("CHTA")  # plain-HTTP explorer unreachable
        self._register()
        self.pool.add("blocks.pending.json", self.clk.t - 20, 5_000_321)
        self.pool.confirm(5_000_321)                     # first seen already confirmed by the pool
        self.ticks(4)
        p = self.msgs("Block Found #5000321")
        self.assertEqual(len(p), 1, self.sent)
        self.assertIn("HeliosPool lists this block as confirmed", p[0])
        self.assertEqual(self.msgs("Block Confirmed"), [])

    def test_old_pool_history_is_not_announced_on_first_start(self):
        self._register()
        self.pool.add("blocks.json", self.clk.t - 5 * 86400, 4_990_000)
        self.ticks(2)
        self.assertEqual(self.sent, [])

    def test_miner_trigger_waits_for_the_pool_height_and_merges(self):
        cfg = self._register()
        self.poll(cfg, gamma_raw())
        h = self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, gamma_raw(showNewBlock=True))           # miner side first: waits for the pool's height
        self.assertEqual(self.sent, [])
        self.pool.add("blocks.pending.json", self.clk.t - 10, h)
        self.ticks(1)
        self.assertEqual(len(self.msgs(f"Block Found #{h}")), 1, self.sent)
        self.chta.mine(2)
        self.ticks(3)
        self.assertEqual(len(self.msgs("Block Found")), 1, self.sent)
        self.assertEqual(len(self.msgs(f"Block Confirmed #{h}")), 1, self.sent)
        # pool first, then the miner's banner flag for the same block
        h2 = self.chta.mine(1, pays=CHTA_SCRIPT)
        self.pool.add("blocks.pending.json", self.clk.t - 5, h2)
        self.ticks(2)
        self.poll(cfg, gamma_raw(showNewBlock=False))
        self.poll(cfg, gamma_raw(showNewBlock=True))
        self.ticks(4)
        self.assertEqual(len(self.msgs("Block Found")), 2, self.sent)

    def test_miner_trigger_without_pool_report_goes_out_without_a_number(self):
        cfg = self._register()
        self.poll(cfg, gamma_raw())
        self.poll(cfg, gamma_raw(showNewBlock=True))
        self.ticks(4)   # > POOL_WAIT_SEC, the pool never lists it
        p = self.msgs("Block Found")
        self.assertEqual(len(p), 1, self.sent)
        self.assertIn("**Block Found**\n", p[0])

    def test_many_blocks_in_an_hour_are_batched(self):
        self.hub = self.make_hub(block={"max_messages_per_hour": 3})
        self._register()
        hs = []
        for i in range(10):
            hs.append(self.chta.mine(1, pays=CHTA_SCRIPT))
            self.pool.add("blocks.pending.json", self.clk.t - 5, hs[-1])
            self.ticks(2)
        self.chta.mine(3)
        self.ticks(6)
        self.assertEqual(len(self.msgs("Block Found")), 3, self.sent)
        self.assertEqual(len(self.msgs("Block Confirmed")), 3, self.sent)
        self.ticks(45)
        summary = self.msgs("7 more Blocks Found in the last hour")
        self.assertEqual(len(summary), 1, [p["content"] for _, p in self.sent][-3:])
        self.assertIn("Confirmed on the blockchain: 7 of 7", summary[0])
        self.assertIn(f"Payout address: {CHTA_ADDR}", summary[0])
        self.assertIn("Blocks: #" + str(hs[3]), summary[0])
        self.assertIn("chtaexplorer.mooo.com:3002/block/", summary[0])
        self.assertEqual(len(self.sent), 7, "3 blocks one by one (found + confirmed each) + 1 summary")


if __name__ == "__main__":
    unittest.main()
