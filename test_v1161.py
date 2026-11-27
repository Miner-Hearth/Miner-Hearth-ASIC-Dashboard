#!/usr/bin/env python3
"""Tests for Miner Hearth v1.16.1: the block alerts' "Share difficulty" is the winning share's own difficulty.

Sources, in order: the pool's block-list entry (HeliosPool "diff"), the miner's last-share difficulty captured
in the poll that fires the trigger (AxeOS lastSubmittedDiff), and the block hash from the coin's explorer.
Best / session-best difficulties are never shown; CGMiner's "Last Share Difficulty" (the pool's share target)
is never shown. Unknown stays "unknown".

All values are synthetic and chosen so a wrong source is visible: best diff 900M, session best 88M, CGMiner best
50G and share target 6.4G never appear in a block alert.
"""

from __future__ import annotations

import unittest

import autotune
import miner_hearth
import test_v1155_chta as C
from test_v1155_chta import CHTA_ADDR, CHTA_SCRIPT, gamma_raw

SOLO_HOST = "cheetah.solo.example"   # not HeliosPool: the alert goes out without waiting for a pool feed
NET = 3.7e6
BEST_STRINGS = ("900M", "88M")        # bestDiff 9.0e8, bestSessionDiff 8.8e7 in every poll below


class RealHashChain(C.FakeChain):
    """CHTA explorer whose block hashes give a winning-share difficulty of 4.4M (synthetic)."""
    WIN = 4.4e6

    def block_hash_at(self, h):
        return f"{(0xFFFF * 2 ** 208) // int(self.WIN) // 10 ** 8 * 10 ** 8 + h:064x}"

    def _h(self, bh):
        return int(bh, 16) % 10 ** 8

    def block(self, bh):
        h = self._h(bh)
        return {"timestamp": self.blocks.get(h, (0.0, []))[0], "height": h}

    def coinbase_addresses(self, bh):
        return sorted(C.nk_coins.coinbase_tokens([], self.blocks.get(self._h(bh), (0, []))[1]))


def raw(**kw):
    kw.setdefault("best_session", 8.8e7)
    return gamma_raw(**kw)


class Base(C.Base):
    def setUp(self):
        super().setUp()
        self.chta = RealHashChain(self.clk, C.CHTA_TIP, NET)
        self.explorers["CHTA"] = self.chta

    def all_block_msgs(self):
        return self.msgs("Block") + self.msgs("TEST")

    def assert_no_best(self):
        for m in self.msgs("Block"):
            for b in BEST_STRINGS:
                self.assertNotIn(b, m, "a best difficulty was shown as the share difficulty")


class MinerSideTests(Base):
    def test_counter_trigger_without_last_share_is_unknown_then_block_hash(self):
        cfg = self.cfg()
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1"))
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1"))
        self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", blockFound=1, showNewBlock=True))
        self.ticks(3)
        found, conf = self.msgs("Block Found"), self.msgs("Block Confirmed")
        self.assertEqual((len(found), len(conf)), (1, 1), self.sent)
        self.assertIn("Share difficulty: unknown", found[0])
        self.assertIn("Share difficulty: 4.4M", conf[0])       # (c) from the block hash
        self.assert_no_best()

    def test_counter_trigger_uses_the_new_last_share_in_that_poll_and_keeps_it(self):
        cfg = self.cfg()
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=2160.0))
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=4096.0))
        self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=5.1e6, blockFound=1, showNewBlock=True))
        self.ticks(3)
        found, conf = self.msgs("Block Found"), self.msgs("Block Confirmed")
        self.assertEqual((len(found), len(conf)), (1, 1), self.sent)
        for m in (found[0], conf[0]):
            self.assertIn("Share difficulty: 5.1M", m)         # (b), same in both alerts (not the 4.4M block hash)
        self.assert_no_best()

    def test_last_share_below_network_difficulty_or_not_new_is_not_the_winner(self):
        cfg = self.cfg()
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=5.1e6))   # old share, baseline only
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=5.1e6))
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=5.1e6, showNewBlock=True))  # not new
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=5.1e6, showNewBlock=False))
        self.clk.t += 200
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", lastSubmittedDiff=2160.0, showNewBlock=True))  # < net
        self.ticks(3)
        found = self.msgs("Block Found")
        self.assertEqual(len(found), 2, self.sent)
        for m in found:
            self.assertIn("Share difficulty: unknown", m)
            self.assertNotIn("5.1M", m)
        self.assert_no_best()

    def test_session_best_jump_trigger_never_shows_the_best(self):
        self.explorers.pop("CHTA")
        cfg = self.cfg()
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", best_session=5.0e5))
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", best_session=5.0e5))
        self.poll(cfg, raw(host=SOLO_HOST, user=CHTA_ADDR + ".g1", best_session=8.8e7))   # session best jumps over NET
        self.ticks(2)
        found = self.msgs("Block Found")
        self.assertEqual(len(found), 1, self.sent)
        self.assertIn("Share difficulty: unknown", found[0])
        self.assert_no_best()
        cand = self.hub.blocks.candidates[-1]
        self.assertEqual(cand["trigger_diff"], 8.8e7)      # kept for coin picking / logs only
        self.assertIsNone(cand["share_diff"])

    def test_cgminer_last_share_difficulty_is_a_target_and_never_used(self):
        def rd(found):
            return autotune.parse_antminer_readings(
                version_data={"VERSION": [{"BOSminer": "24.04", "Type": "Antminer S19 Pro"}]},
                summary_data={"SUMMARY": [{"Elapsed": 900000, "GHS 5s": 95000.0, "Accepted": 5000, "Best Share": 5.0e10,
                                           "Found Blocks": found}]},
                stats_data={"STATS": [{}, {"BMMiner": "BOSminer", "temp_chip1": 65.0, "frequency": 625.0}]},
                pools_data={"POOLS": [{"POOL": 0, "URL": "stratum+tcp://xec.pool.example:3339", "User": C.nk_coins.cashaddr_encode("ecash", 0, bytes(range(1, 21))) + ".ant",
                                       "Status": "Alive", "Priority": 0, "Stratum Active": True, "Accepted": 5000,
                                       "Best Share": 5.0e10, "Last Share Difficulty": 6.4e9 + found, "Last Share Time": 5}]})
        r = rd(12)
        self.assertTrue(r.last_share_is_target)
        self.assertGreater(r.last_submitted_diff, C.XEC_DIFF)
        self.explorers.pop("XEC")
        cfg = self.cfg("S19 One")
        for n in (12, 12, 13):
            self.hub.on_poll(cfg, rd(n), self.clk.t)
            self.clk.t += 15
        self.ticks(2)
        found = self.msgs("Block Found")
        self.assertEqual(len(found), 1, self.sent)
        self.assertIn("Share difficulty: unknown", found[0])
        self.assertNotIn("50G", found[0])
        self.assertNotIn("6.4G", found[0])


class PoolFeedTests(Base):
    def _register(self):
        cfg = self.cfg()
        self.poll(cfg, raw(lastSubmittedDiff=2160.0))
        return cfg

    def _pool_row(self, h, diff, ts=None):
        self.pool.add("blocks.pending.json", (ts or self.clk.t) - 10, h, netdiff=NET)
        self.pool.files["blocks.pending.json"][-1]["diff"] = diff
        self.pool.files["blocks.pending.json"][-1]["blockhash"] = self.chta.block_hash_at(h)

    def test_pool_entry_difficulty_wins_and_stays_the_same_in_block_confirmed(self):
        cfg = self._register()
        h = self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, raw(lastSubmittedDiff=5.1e6, showNewBlock=True))   # miner side first: waits for the pool
        self.assertEqual(self.sent, [])
        self._pool_row(h, 6.2e6)
        self.ticks(1)
        self.chta.mine(2)
        self.ticks(3)
        found, conf = self.msgs(f"Block Found #{h}"), self.msgs(f"Block Confirmed #{h}")
        self.assertEqual((len(found), len(conf)), (1, 1), self.sent)
        for m in (found[0], conf[0]):
            self.assertIn("Share difficulty: 6.2M", m)          # (a) pool entry, not 5.1M (miner) or 4.4M (hash)
        self.assert_no_best()

    def test_value_shown_in_block_found_is_kept_when_the_pool_reports_later(self):
        cfg = self._register()
        h = self.chta.mine(1, pays=CHTA_SCRIPT)
        self.poll(cfg, raw(lastSubmittedDiff=5.1e6, showNewBlock=True))
        self.ticks(4)                                                  # pool silent past POOL_WAIT_SEC
        found = self.msgs("Block Found")
        self.assertEqual(len(found), 1, self.sent)
        self.assertIn("Share difficulty: 5.1M", found[0])
        self._pool_row(h, 6.2e6, ts=self.clk.t - 240)
        self.chta.mine(2)
        self.ticks(4)
        conf = self.msgs("Block Confirmed")
        self.assertEqual(len(conf), 1, self.sent)
        self.assertIn("Share difficulty: 5.1M", conf[0])              # consistent with Block Found
        self.assertEqual(len(self.msgs("Block Found")), 1)

    def test_hourly_summary_lists_each_winning_share(self):
        self.hub = self.make_hub(block={"max_messages_per_hour": 1})
        self.explorers.pop("CHTA")
        self._register()
        hs = []
        for i in range(3):
            hs.append(self.chta.mine(1, pays=CHTA_SCRIPT))
            self._pool_row(hs[-1], 4.0e6 + (i + 1) * 1.0e5)
            self.ticks(2)
        self.ticks(60)
        summary = self.msgs("2 more Blocks Found")
        self.assertEqual(len(summary), 1, [p["content"] for _, p in self.sent])
        self.assertIn(f"Share difficulty: #{hs[1]} 4.2M, #{hs[2]} 4.3M", summary[0])
        self.assertIn("Share difficulty: 4.1M", self.msgs(f"Block Found #{hs[0]}")[0])
        self.assert_no_best()


class UnitTests(unittest.TestCase):
    def test_share_difficulty_from_block_hash(self):
        genesis = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"   # public Bitcoin genesis block
        self.assertAlmostEqual(miner_hearth.share_diff_from_block_hash(genesis, "BTC"), 2536.43, delta=0.01)
        self.assertIsNone(miner_hearth.share_diff_from_block_hash(genesis, "LTC"))     # scrypt: not this formula
        self.assertIsNone(miner_hearth.share_diff_from_block_hash("xyz", "BTC"))
        self.assertIsNone(miner_hearth.share_diff_from_block_hash("0" * 64, "BTC"))

    def test_priority_lock_and_old_candidates(self):
        c = {"coin": "BTC"}
        self.assertTrue(miner_hearth.set_winning_share(c, 5.0, "block_hash"))
        self.assertTrue(miner_hearth.set_winning_share(c, 6.0, "miner_last_share"))
        self.assertFalse(miner_hearth.set_winning_share(c, 7.0, "block_hash"))
        self.assertTrue(miner_hearth.set_winning_share(c, 8.0, "pool"))
        c["share_locked"] = True
        self.assertFalse(miner_hearth.set_winning_share(c, 9.0, "pool"))
        self.assertEqual(miner_hearth.winning_share(c), (8.0, "pool"))
        for bad in (None, 0, -1, "x", float("nan")):
            self.assertFalse(miner_hearth.set_winning_share({}, bad, "pool"))
        legacy = {"coin": "BTC", "share_diff": 9.0e8}          # saved by v1.16.0: that was a best difficulty
        self.assertEqual(miner_hearth.winning_share(legacy), (None, ""))
        self.assertEqual(miner_hearth.winning_share({"coin": "BTC", "network_difficulty": 1.0e30,
                                                     "block_hash": "0" * 20 + "f" * 44}), (None, ""))  # below network difficulty

    def test_watcher_never_takes_a_best_field(self):
        w = miner_hearth.BlockWatcher(None)
        base = {"best_session_diff": 1.0e5, "best_diff": 2.0e5, "last_submitted_diff": 300.0}
        w.observe("m", base, NET, None, 0.0, coin="CHTA")
        c = w.observe("m", dict(base, best_session_diff=8.8e7, best_diff=9.0e8), NET, None, 10.0, coin="CHTA")
        self.assertIsNotNone(c)
        self.assertIsNone(c["share_diff"])
        c2 = w.observe("n", base, NET, None, 0.0, coin="CHTA", found_blocks=1)
        c2 = w.observe("n", dict(base, last_submitted_diff=5.1e6), NET, None, 10.0, coin="CHTA", found_blocks=2)
        self.assertEqual((c2["share_diff"], c2["share_src"]), (5.1e6, "miner_last_share"))
        w.observe("t", base, NET, None, 0.0, coin="CHTA", found_blocks=1, last_share_actual=False)
        c3 = w.observe("t", dict(base, last_submitted_diff=5.1e6), NET, None, 10.0, coin="CHTA", found_blocks=2, last_share_actual=False)
        self.assertIsNone(c3["share_diff"])


if __name__ == "__main__":
    unittest.main()
