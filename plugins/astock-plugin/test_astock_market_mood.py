"""`astock_market_mood` 大盘看板单测（v2.5.0）。

**为什么这批用例重要**：大盘看板的价值全在"口径对"，口径错了会给出
**完全相反的结论**。这里钉住三个真实风险：

| 用例 | 钉住的风险 |
|---|---|
| `TestBreadthParse` | 涨跌家数只取上证一个 secid → 显示 2356 家却标"全市场"（漏深市 2938 家）|
| `TestDivergence` | 指数上涨但多数个股下跌 → 若不提示，用户会把红盘当普涨 |
| `TestMoodCache` | 面板 5 秒刷一次，无缓存 = 打爆接口（东财 clist 实测就这样挂的）|

🚫 红线：零网络。所有取数函数都被替换成内存替身。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import astock_market_mood as mood                  # noqa: E402


class TestBreadthParse(unittest.TestCase):
    """东财 f104/f105/f106 → 涨跌家数。缺字段必须是 None，不能当 0。"""

    def test_normal(self):
        r = mood.parse_breadth({"f104": 1877, "f105": 440, "f106": 39})
        self.assertEqual(r["up"], 1877)
        self.assertEqual(r["down"], 440)
        self.assertEqual(r["flat"], 39)
        self.assertEqual(r["total"], 2356)

    def test_missing_up_returns_none(self):
        """缺上涨家数不能当成 0 家上涨——那会显示成"全跌"。"""
        self.assertIsNone(mood.parse_breadth({"f105": 440, "f106": 39}))
        self.assertIsNone(mood.parse_breadth({"f104": 1877}))

    def test_flat_optional(self):
        r = mood.parse_breadth({"f104": 10, "f105": 20})
        self.assertEqual(r["flat"], 0)
        self.assertEqual(r["total"], 30)

    def test_not_dict(self):
        for bad in (None, [], "x", 0):
            self.assertIsNone(mood.parse_breadth(bad))

    def test_non_numeric(self):
        self.assertIsNone(mood.parse_breadth({"f104": "x", "f105": 1}))


class TestMergeBreadth(unittest.TestCase):
    """★ 核心：必须沪深分别统计再相加。"""

    def test_merge_sh_sz(self):
        """真实数据：上证 2356 家 + 深证 2938 家 = 5294 家。"""
        sh = mood.parse_breadth({"f104": 1877, "f105": 440, "f106": 39})
        sz = mood.parse_breadth({"f104": 2552, "f105": 350, "f106": 36})
        m = mood.merge_breadth([sh, sz])
        self.assertEqual(m["up"], 4429)
        self.assertEqual(m["down"], 790)
        self.assertEqual(m["flat"], 75)
        self.assertEqual(m["total"], 5294)

    def test_single_market_is_not_whole_market(self):
        """只取上证会**漏掉深市**——这是必须合并的原因，写成用例防回归。"""
        sh_only = mood.parse_breadth({"f104": 1877, "f105": 440, "f106": 39})
        merged = mood.merge_breadth([sh_only,
                                     mood.parse_breadth({"f104": 2552, "f105": 350,
                                                         "f106": 36})])
        self.assertGreater(merged["total"], sh_only["total"] * 2)

    def test_skip_none(self):
        good = mood.parse_breadth({"f104": 10, "f105": 5})
        self.assertEqual(mood.merge_breadth([good, None])["up"], 10)

    def test_all_none(self):
        self.assertIsNone(mood.merge_breadth([None, None]))
        self.assertIsNone(mood.merge_breadth([]))


class TestAdvanceRatio(unittest.TestCase):
    def test_ratio_excludes_flat_from_denominator(self):
        """平盘不进分母：停牌/持平不表达方向，算进去会稀释信号。"""
        b = {"up": 4429, "down": 790, "flat": 75, "total": 5294}
        # 4429/(4429+790) = 84.86%
        self.assertAlmostEqual(mood.advance_ratio(b), 84.86, places=2)

    def test_none_and_zero(self):
        self.assertIsNone(mood.advance_ratio(None))
        self.assertIsNone(mood.advance_ratio({"up": 0, "down": 0, "flat": 5}))

    def test_all_up(self):
        self.assertEqual(mood.advance_ratio({"up": 100, "down": 0, "flat": 0}),
                         100.0)

    def test_all_down(self):
        self.assertEqual(mood.advance_ratio({"up": 0, "down": 100, "flat": 0}),
                         0.0)


class TestBreadthTone(unittest.TestCase):
    """分档是描述性的，不是买卖信号。"""

    def test_levels(self):
        self.assertEqual(mood.breadth_tone(85), "普涨")
        self.assertEqual(mood.breadth_tone(70), "普涨")
        self.assertEqual(mood.breadth_tone(60), "偏强")
        self.assertEqual(mood.breadth_tone(50), "分化")
        self.assertEqual(mood.breadth_tone(35), "偏弱")
        self.assertEqual(mood.breadth_tone(10), "普跌")
        self.assertEqual(mood.breadth_tone(None), "数据缺失")


class TestDivergence(unittest.TestCase):
    """★ 指数会骗人：红盘可能只是权重股拉的。"""

    def test_index_up_but_breadth_weak(self):
        idx = [{"code": "000001", "name": "上证指数", "change_pct": 1.0,
                "price": 3638}]
        b = {"up": 1500, "down": 3500, "flat": 100, "total": 5100}
        s = mood.market_summary(idx, b)
        self.assertIsNotNone(s["divergence"])
        self.assertIn("权重股拉动", s["divergence"])

    def test_index_down_but_breadth_strong(self):
        idx = [{"code": "000001", "name": "上证指数", "change_pct": -1.0,
                "price": 3638}]
        b = {"up": 3500, "down": 1500, "flat": 100, "total": 5100}
        s = mood.market_summary(idx, b)
        self.assertIsNotNone(s["divergence"])
        self.assertIn("权重股拖累", s["divergence"])

    def test_no_divergence_when_aligned(self):
        idx = [{"code": "000001", "name": "上证指数", "change_pct": 1.0,
                "price": 3638}]
        b = {"up": 4429, "down": 790, "flat": 75, "total": 5294}
        s = mood.market_summary(idx, b)
        self.assertIsNone(s["divergence"])

    def test_missing_breadth(self):
        idx = [{"code": "000001", "name": "上证指数", "change_pct": 1.0,
                "price": 3638}]
        s = mood.market_summary(idx, None)
        self.assertIsNone(s["divergence"])
        self.assertIn("未取到", s["detail"])


class TestMarketSummary(unittest.TestCase):
    def test_real_snapshot_20261102(self):
        """2026-11-02 真实收盘数据回归。"""
        idx = [
            {"code": "000001", "name": "上证指数", "change_pct": 1.08,
             "price": 3638.03, "amount_yuan": 847459070000.0},
            {"code": "399001", "name": "深证成指", "change_pct": 1.63,
             "price": 11010.68, "amount_yuan": 936236270000.0},
        ]
        b = {"up": 4429, "down": 790, "flat": 75, "total": 5294}
        amount = 847459070000.0 + 936236270000.0
        s = mood.market_summary(idx, b, amount)
        self.assertEqual(s["tone"], "普涨")
        self.assertAlmostEqual(s["ratio"], 84.86, places=1)
        self.assertEqual(s["amount_yi"], 17837.0)
        self.assertIsNone(s["divergence"])       # 指数涨、个股也涨，一致

    def test_amount_none(self):
        idx = [{"code": "000001", "name": "上证指数", "change_pct": 1.0,
                "price": 3638}]
        s = mood.market_summary(idx, None, None)
        self.assertIsNone(s["amount_yi"])

    def test_empty(self):
        s = mood.market_summary([], None)
        self.assertIsNone(s["sh_pct"])
        self.assertIsNone(s["ratio"])


class TestRelativeStrength(unittest.TestCase):
    """★ 绝对涨跌说明不了问题，必须跟指数比。"""

    def test_outperform(self):
        # 大盘跌 1%，持仓跌 0.5% → 实际是跑赢的
        self.assertEqual(mood.relative_strength(-0.5, -1.0), 0.5)

    def test_underperform_in_up_market(self):
        # 大盘涨 2%，持仓只涨 1% → 跑输（不是"我赚钱了"就没事）
        self.assertEqual(mood.relative_strength(1.0, 2.0), -1.0)

    def test_real_case(self):
        """真实：持仓 -0.29% vs 上证 +1.08% → 超额 -1.37pp。"""
        self.assertAlmostEqual(mood.relative_strength(-0.29, 1.08), -1.37,
                               places=2)

    def test_missing(self):
        self.assertIsNone(mood.relative_strength(None, 1.0))
        self.assertIsNone(mood.relative_strength(1.0, None))


class TestRsVerdict(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(mood.rs_verdict(3.0), "显著跑赢")
        self.assertEqual(mood.rs_verdict(1.0), "跑赢")
        self.assertEqual(mood.rs_verdict(0.1), "基本同步")
        self.assertEqual(mood.rs_verdict(-1.0), "跑输")
        self.assertEqual(mood.rs_verdict(-3.0), "显著跑输")
        self.assertEqual(mood.rs_verdict(None), "-")


class TestStyle(unittest.TestCase):
    def test_ranking_sorted_desc(self):
        idx = [{"code": "1", "name": "A", "change_pct": 1.0},
               {"code": "2", "name": "B", "change_pct": 3.0},
               {"code": "3", "name": "C", "change_pct": 2.0}]
        r = mood.style_ranking(idx)
        self.assertEqual([x["name"] for x in r], ["B", "C", "A"])

    def test_ranking_skips_missing(self):
        idx = [{"code": "1", "name": "A", "change_pct": None},
               {"code": "2", "name": "B", "change_pct": 1.0}]
        self.assertEqual(len(mood.style_ranking(idx)), 1)

    def test_verdict_spread(self):
        """真实 11-02：科创50 +3.11% vs 沪深300 +0.79%，分化 2.32pp。"""
        r = [{"name": "科创50", "change_pct": 3.11},
             {"name": "沪深300", "change_pct": 0.79}]
        v = mood.style_verdict(r)
        self.assertIn("科创50", v)
        self.assertIn("沪深300", v)
        self.assertIn("2.32", v)

    def test_verdict_balanced(self):
        r = [{"name": "A", "change_pct": 1.0}, {"name": "B", "change_pct": 0.5}]
        self.assertIn("均衡", mood.style_verdict(r))

    def test_verdict_insufficient(self):
        self.assertEqual(mood.style_verdict([]), "-")
        self.assertEqual(mood.style_verdict([{"name": "A", "change_pct": 1}]), "-")


class TestMoodCache(unittest.TestCase):
    """★ 缓存：面板 5 秒刷一次，无缓存会打爆接口。"""

    def setUp(self):
        self.cache = mood.MoodCache(ttl=15.0)

    def test_caches_within_ttl(self):
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = [{"code": "000001", "name": "上证指数",
                                "change_pct": 1.0, "price": 3638,
                                "amount_yuan": 1e11}]
            fb.return_value = {"up": 100, "down": 50, "flat": 5, "total": 155}
            self.cache.get(100.0)
            self.cache.get(105.0)          # 5 秒后，仍在 TTL 内
            self.cache.get(114.0)          # 14 秒后，仍在 TTL 内
            self.assertEqual(fi.call_count, 1)
            self.assertEqual(fb.call_count, 1)

    def test_refresh_after_ttl(self):
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = []
            fb.return_value = {"up": 1, "down": 1, "flat": 0, "total": 2}
            self.cache.get(100.0)
            self.cache.get(120.0)          # 20 秒后，超 TTL
            self.assertEqual(fb.call_count, 2)

    def test_force_refresh(self):
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = []
            fb.return_value = {"up": 1, "down": 1, "flat": 0, "total": 2}
            self.cache.get(100.0)
            self.cache.get(101.0, force=True)
            self.assertEqual(fb.call_count, 2)

    def test_keeps_last_good_on_failure(self):
        """★ 接口失败时保留上次有效值——宁可显示 15 秒前的数，也不显示空白。"""
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = [{"code": "000001", "name": "上证指数",
                                "change_pct": 1.0, "price": 3638}]
            fb.return_value = {"up": 100, "down": 50, "flat": 5, "total": 155}
            first = self.cache.get(100.0)
            self.assertEqual(first["breadth"]["up"], 100)

            fi.return_value = []           # 第二次全部失败
            fb.return_value = None
            second = self.cache.get(200.0)
            self.assertEqual(second["breadth"]["up"], 100)   # 保留旧值
            self.assertEqual(len(second["indices"]), 1)

    def test_amount_sums_sh_sz_only(self):
        """两市成交额只加沪+深，不重复计创业板/科创。"""
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = [
                {"code": "000001", "amount_yuan": 8.4e11},
                {"code": "399001", "amount_yuan": 9.3e11},
                {"code": "399006", "amount_yuan": 4.6e11},   # 创业板，不重复计
            ]
            fb.return_value = None
            snap = self.cache.get(100.0)
            self.assertAlmostEqual(snap["amount"], 8.4e11 + 9.3e11, delta=1e6)

    def test_clear(self):
        with mock.patch.object(mood, "fetch_indices") as fi, \
                mock.patch.object(mood, "fetch_breadth") as fb:
            fi.return_value = []
            fb.return_value = {"up": 1, "down": 1, "flat": 0, "total": 2}
            self.cache.get(100.0)
            self.cache.clear()
            self.assertIsNone(self.cache._breadth)


class TestIndicesConfig(unittest.TestCase):
    def test_indices_have_roles(self):
        for code, venue, name, role in mood.INDICES:
            self.assertEqual(len(code), 6, f"{name} 代码应为 6 位")
            self.assertIn(venue, ("sh", "sz"))
            self.assertTrue(role, f"{name} 缺角色说明")

    def test_sh_index_first(self):
        """上证必须排第一——先看大势再看风格。"""
        self.assertEqual(mood.INDICES[0][0], "000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
