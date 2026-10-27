"""`astock_advise` 单测：策略推荐器的措辞红线与分档规则。

**这个模块的风险不在算错，而在说错**：
把"历史 42% 跑赢"讲成"42% 概率赚钱"，或把 D 档策略推荐给用户，
都会直接导致错误决策。所以测试里既锁规则，也锁措辞。
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import astock_advise as adv_mod                    # noqa: E402


class Rec:
    """Recommendation 的最小替身（只实现被用到的字段）。"""

    def __init__(self, grade="C", label="谨慎", win_rate=40.0, sample=20,
                 max_drawdown_pct=-20.0, avg_excess_pct=1.0, reason="",
                 confidence="一般"):
        self.grade = grade
        self.label = label
        self.win_rate = win_rate
        self.sample = sample
        self.max_drawdown_pct = max_drawdown_pct
        self.avg_excess_pct = avg_excess_pct
        self.reason = reason
        self.confidence = confidence


class TestRanking(unittest.TestCase):
    def test_grade_beats_win_rate(self):
        """A 档但胜率略低，仍要排在 D 档高胜率之前（分档已含样本约束）。"""
        recs = {"momentum": Rec("D", "不建议", 70.0, 20),
                "quality": Rec("A", "推荐", 66.0, 20)}
        ranked = adv_mod.rank_strategies(recs)
        self.assertEqual(ranked[0][0], "quality")

    def test_same_grade_higher_win_first(self):
        recs = {"momentum": Rec("C", "谨慎", 45.0, 20),
                "quality": Rec("C", "谨慎", 55.0, 20)}
        self.assertEqual(adv_mod.rank_strategies(recs)[0][0], "quality")

    def test_same_grade_same_win_smaller_drawdown_first(self):
        recs = {"momentum": Rec("C", "谨慎", 50.0, 20, max_drawdown_pct=-50.0),
                "quality": Rec("C", "谨慎", 50.0, 20, max_drawdown_pct=-18.0)}
        self.assertEqual(adv_mod.rank_strategies(recs)[0][0], "quality")

    def test_insufficient_sample_excluded(self):
        """样本 <8 期不参与排序——否则 100% 胜率的 3 期样本会排第一。"""
        recs = {"momentum": Rec("D", "样本不足", 100.0, 3),
                "quality": Rec("C", "谨慎", 40.0, 20)}
        ranked = adv_mod.rank_strategies(recs)
        self.assertEqual([k for k, _ in ranked], ["quality"])

    def test_no_usable(self):
        self.assertEqual(adv_mod.rank_strategies({"x": Rec(sample=2)}), [])
        self.assertEqual(adv_mod.rank_strategies({}), [])


class TestAdvise(unittest.TestCase):
    def test_ab_grade_recommends_and_names_second(self):
        recs = {"quality": Rec("B", "可考虑", 55.0, 30),
                "momentum": Rec("C", "谨慎", 45.0, 30),
                "reversal": Rec("D", "不建议", 30.0, 30)}
        adv = adv_mod.advise(recs)
        self.assertEqual(adv["tone"], "good")
        self.assertIn("质量价值", adv["headline"])
        self.assertIn("优先用", adv["headline"])
        self.assertIn("次选", adv["headline"], "有第二选择时要点名")
        self.assertEqual(adv["picks"][0][0], "quality")

    def test_all_cd_says_dont_rush(self):
        """三套全 C/D 是真实常态，必须明说'先别下手'，不能硬推荐一套。"""
        recs = {"quality": Rec("D", "不建议", 42.0, 57),
                "momentum": Rec("D", "不建议", 49.0, 57),
                "reversal": Rec("D", "不建议", 36.0, 57)}
        adv = adv_mod.advise(recs)
        self.assertEqual(adv["tone"], "warn")
        self.assertIn("无优势策略", adv["headline"])
        self.assertIn("建议轻仓", adv["headline"])
        self.assertTrue(any("轻仓参与" in w for w in adv["warnings"]))
        self.assertTrue(any("C/D 档" in w for w in adv["warnings"]))

    def test_insufficient_sample_no_conclusion(self):
        adv = adv_mod.advise({"quality": Rec("D", "样本不足", None, 2)})
        self.assertEqual(adv["tone"], "warn")
        self.assertIn("无法推荐", adv["headline"])
        self.assertEqual(adv["picks"], [])

    def test_empty_recs(self):
        adv = adv_mod.advise({})
        self.assertIn("无法推荐", adv["headline"])
        self.assertEqual(adv["picks"], [])

    def test_big_drawdown_warned(self):
        recs = {"reversal": Rec("C", "谨慎", 45.0, 30, max_drawdown_pct=-51.6)}
        adv = adv_mod.advise(recs)
        self.assertTrue(any("51" in w or "52" in w for w in adv["warnings"]),
                        adv["warnings"])

    def test_reversal_warned_even_if_best(self):
        """reversal 排第一也必须带警告——用户只看排名会被误导。"""
        recs = {"reversal": Rec("B", "可考虑", 60.0, 30),
                "momentum": Rec("C", "谨慎", 40.0, 30)}
        adv = adv_mod.advise(recs)
        self.assertEqual(adv["picks"][0][0], "reversal")
        self.assertTrue(any("reversal" in w for w in adv["warnings"]),
                        adv["warnings"])


class TestWordingGuard(unittest.TestCase):
    """措辞红线：不能把历史胜率讲成未来概率。"""

    def _all_text(self, adv):
        return " ".join([adv["headline"]] + list(adv["warnings"]))

    def test_never_says_win_probability(self):
        recs = {"quality": Rec("A", "推荐", 70.0, 30)}
        text = self._all_text(adv_mod.advise(recs))
        for bad in ("上涨概率", "赚钱概率", "收益率 70", "胜率 70% 概率"):
            self.assertNotIn(bad, text)

    def test_render_includes_scope_disclaimer(self):
        recs = {"quality": Rec("B", "可考虑", 55.0, 30)}
        out = adv_mod.render_advice(adv_mod.advise(recs))
        self.assertIn("不是任何一只股票上涨的概率", out)
        self.assertIn("不能外推", out)

    def test_compact_drops_long_text(self):
        recs = {"quality": Rec("B", "可考虑", 55.0, 30),
                "reversal": Rec("C", "谨慎", 40.0, 30, max_drawdown_pct=-60.0)}
        full = adv_mod.render_advice(adv_mod.advise(recs))
        short = adv_mod.render_advice(adv_mod.advise(recs), compact=True)
        self.assertLess(len(short), len(full))
        self.assertNotIn("怎么用", short)
        # 但结论必须还在
        self.assertIn("质量价值", short)

    def test_compact_keeps_at_most_one_warning(self):
        recs = {"reversal": Rec("C", "谨慎", 40.0, 30, max_drawdown_pct=-60.0),
                "quality": Rec("D", "不建议", 30.0, 30)}
        short = adv_mod.render_advice(adv_mod.advise(recs), compact=True)
        self.assertLessEqual(short.count("⚠️"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
