"""`astock_formula` 纯函数单测（v2.4.0）。

**为什么这些用例值得写**：本模块的每个"奇怪"行为都对应一次**真实踩坑**，
不是假想的边界。用例即是把踩过的坑钉住：

| 用例 | 钉住的坑 |
|---|---|
| `TestRuleDirection` | 格力 38.60 配 `lt` 但现价 38.52 → 永远不响的死规则 |
| `TestMaDrift` | 天山减仓线挂 11.55，三天后 MA10 已 11.593 |
| `TestAdjustFactor` | 格力 08-27 除权，不复权算 MA60 虚高约 0.9 元 |
| `TestSTDEVNone` | 不足 2 点若返回 0，会把"数据不足"评成"低波动最优" |
| `TestRiskAdjusted` | momentum 收益高但回撤 50%，比值反而差 2.4 倍 |

🚫 红线：零网络、零文件、零真实台账。纯数字进、纯数字出。
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import astock_formula as fx                       # noqa: E402


class TestBasics(unittest.TestCase):
    """基础统计：缺数据必须 None，不能是 0。"""

    def test_mean_skips_none(self):
        self.assertEqual(fx.mean([1, None, 3]), 2.0)
        self.assertIsNone(fx.mean([]))
        self.assertIsNone(fx.mean([None, None]))

    def test_stdev_none_below_two(self):
        """不足 2 个值必须是 None——返回 0 会让"低波动"因子误判为最优。"""
        self.assertIsNone(fx.stdev([]))
        self.assertIsNone(fx.stdev([5.0]))
        self.assertIsNone(fx.stdev([5.0, None]))
        self.assertIsNotNone(fx.stdev([1.0, 2.0]))

    def test_stdev_sample_vs_population(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        sample = fx.stdev(vals, sample=True)
        pop = fx.stdev(vals, sample=False)
        self.assertGreater(sample, pop)          # n-1 分母更大

    def test_safe_div_zero(self):
        self.assertIsNone(fx.safe_div(1, 0))
        self.assertIsNone(fx.safe_div(None, 2))
        self.assertEqual(fx.safe_div(1, 0, default=0), 0)
        self.assertEqual(fx.safe_div(10, 4), 2.5)


class TestSma(unittest.TestCase):
    def test_basic(self):
        closes = [1, 2, 3, 4, 5]
        self.assertEqual(fx.sma(closes, 5), 3.0)
        self.assertEqual(fx.sma(closes, 2), 4.5)     # 最近 2 根 (4+5)/2

    def test_insufficient_returns_none(self):
        self.assertIsNone(fx.sma([1, 2], 5))
        self.assertIsNone(fx.sma([], 5))

    def test_zero_prices_filtered(self):
        """停牌 0 价不能进均线（会把 MA 拉低）。"""
        self.assertEqual(fx.sma([10, 0, 10, 10], 3), 10.0)

    def test_digits_4_not_2(self):
        """均线必须 4 位：截成 2 位会让"漂移是否>1%"判定失真。"""
        v = fx.sma([11.588, 11.591, 11.600], 3)
        self.assertEqual(v, 11.593)
        self.assertNotEqual(v, 11.59)

    def test_ma_set(self):
        closes = list(range(1, 71))
        out = fx.ma_set(closes)
        self.assertEqual(set(out), {"ma5", "ma10", "ma20", "ma60"})
        self.assertAlmostEqual(out["ma5"], 68.0, places=3)
        self.assertIsNone(fx.ma_set([1, 2, 3])["ma60"])


class TestDeviationAndPosition(unittest.TestCase):
    def test_deviation(self):
        self.assertEqual(fx.deviation_pct(11.93, 11.593), 2.91)
        self.assertEqual(fx.deviation_pct(11.50, 11.593), -0.80)
        self.assertIsNone(fx.deviation_pct(11.93, None))
        self.assertIsNone(fx.deviation_pct(11.93, 0))

    def test_ma_position(self):
        mas = {"ma5": 11.9, "ma10": 11.6, "ma20": 11.76, "ma60": None}
        # 11.93 在三条上方
        self.assertIn("全部上方", fx.ma_position(11.93, mas))
        # 11.00 在三条下方
        self.assertIn("全部下方", fx.ma_position(11.00, mas))
        # 11.70 在 ma5 下方、ma10/ma20 上方
        mixed = fx.ma_position(11.70, mas)
        self.assertIn("上方", mixed)
        self.assertIn("下方", mixed)

    def test_alignment(self):
        # 打分只问三个不等式：close>ma5 / ma5>ma10 / ma10>ma20，各 1 分。
        # mas 里 ma5(11.9)>ma10(11.6) 成立但 ma10(11.6)<ma20(11.7) 不成立：
        #   · 现价 12.0（在上方）→ 3 项中 2 项成立 = 2 分
        #   · 现价 11.0（在下方）→ close>ma5 不成立，但 ma5>ma10 仍成立 = 1 分
        #     ⚠️ 这正是与 astock_quant 一致的口径：它衡量"均线是否向上交织"，
        #     不是"价格位置"，所以低价时也可能得 1 分。
        mas = {"ma5": 11.9, "ma10": 11.6, "ma20": 11.7}
        self.assertEqual(fx.alignment_score(12.0, mas), 2)
        self.assertEqual(fx.alignment_score(11.0, mas), 1)
        # 真多头排列才 3 分
        full = {"ma5": 11.9, "ma10": 11.8, "ma20": 11.7}
        self.assertEqual(fx.alignment_score(12.0, full), 3)
        # 全空头：价格在 ma5 下、ma5<ma10、ma10<ma20 → 0 分
        bear = {"ma5": 10.0, "ma10": 10.5, "ma20": 11.0}
        self.assertEqual(fx.alignment_score(9.0, bear), 0)
        self.assertIsNone(fx.alignment_score(12.0, {"ma5": None}))

    def test_alignment_matches_quant_impl(self):
        """与 astock_quant.compute_technical 的 ma_alignment 必须一致。"""
        try:
            import astock_quant as quant
        except Exception:
            self.skipTest("astock_quant 不可导入")
        closes = [10.0 + i * 0.2 for i in range(40)]
        bars = [{"close": c, "high": c, "low": c, "volume_lot": 1000}
                for c in closes]
        tech = quant.compute_technical(bars)
        mas = fx.ma_set(closes)
        self.assertEqual(fx.alignment_score(closes[-1], mas),
                         tech["ma_alignment"])


class TestAdjustFactor(unittest.TestCase):
    """格力 08-27「10 派 20 元」真实案例。"""

    def test_gree_factor(self):
        # 除权前收 41.58，每股派 2 元
        f = fx.dividend_adjust_factor(41.58, 2.0)
        self.assertAlmostEqual(f, 0.9519, places=4)
        # 即除权前价格要打 4.8% 折才与今天可比
        self.assertAlmostEqual((1 - f) * 100, 4.81, places=2)

    def test_no_dividend(self):
        self.assertEqual(fx.dividend_adjust_factor(10, 0), 1.0)
        self.assertEqual(fx.dividend_adjust_factor(0, 2), 1.0)

    def test_ma_distortion_negative(self):
        """虚高估算必须是负数（不复权均线偏高）。"""
        d = fx.ma_distortion_estimate(41.58, 2.0, 60)
        self.assertIsNotNone(d)
        self.assertLess(d, 0)
        self.assertGreater(abs(d), 0.0)

    def test_adjust_policy_present(self):
        self.assertIn("均线", fx.ADJUST_POLICY)
        self.assertIn("qfq", fx.adjust_for("均线"))
        self.assertIn("raw", fx.adjust_for("止损/止盈价"))


class TestMomentumRsi(unittest.TestCase):
    def test_momentum(self):
        # 口径：现价 vs **n 根前**的收盘（与 astock_quant 的 mom{n} 一致）
        closes = [10, 11, 12, 13]
        self.assertEqual(fx.momentum_pct(closes, 1), 8.33)    # (13-12)/12
        self.assertEqual(fx.momentum_pct(closes, 3), 30.0)    # (13-10)/10
        self.assertIsNone(fx.momentum_pct([10, 11], 5))

    def test_momentum_matches_quant_impl(self):
        """与 astock_quant 的 mom 口径必须一致（否则同一指标出现两个数）。"""
        try:
            import astock_quant as quant
        except Exception:
            self.skipTest("astock_quant 不可导入")
        closes = [10.0 + i * 0.3 for i in range(70)]
        bars = [{"close": c, "high": c, "low": c, "volume_lot": 1000}
                for c in closes]
        tech = quant.compute_technical(bars)
        for n in (5, 20, 60):
            self.assertEqual(fx.momentum_pct(closes, n), tech[f"mom{n}"])

    def test_rsi_bounds(self):
        up = list(range(1, 30))
        self.assertEqual(fx.rsi(up), 100.0)
        down = list(range(30, 1, -1))
        self.assertEqual(fx.rsi(down), 0.0)

    def test_rsi_flat(self):
        self.assertEqual(fx.rsi([10] * 20), 50.0)

    def test_rsi_matches_quant_impl(self):
        """与 astock_quant.compute_rsi 口径必须一致（同一算法两处实现）。"""
        try:
            import astock_quant as quant
        except Exception:
            self.skipTest("astock_quant 不可导入")
        closes = [10.5, 10.8, 10.3, 11.0, 11.4, 11.1, 11.6,
                  12.0, 11.7, 12.2, 12.5, 12.1, 12.8, 13.0, 12.9, 13.2]
        self.assertEqual(fx.rsi(closes), quant.compute_rsi(closes))

    def test_volatility_none_when_short(self):
        self.assertIsNone(fx.volatility_annual_pct([10]))
        v = fx.volatility_annual_pct([10, 10.5, 10.2, 10.8])
        self.assertIsNotNone(v)
        self.assertGreater(v, 0)

    def test_annualize(self):
        self.assertEqual(fx.annualize_pct(1.0, 244), 1.0)
        self.assertEqual(fx.annualize_pct(1.0, 122), 2.0)
        self.assertIsNone(fx.annualize_pct(1.0, 0))


class TestDrawdown(unittest.TestCase):
    def test_basic(self):
        r = fx.max_drawdown([100, 120, 60, 80])
        self.assertEqual(r["pct"], -50.0)        # 120 → 60
        self.assertEqual(r["peak"], 120.0)
        self.assertEqual(r["trough"], 60.0)

    def test_monotonic_up_no_drawdown(self):
        r = fx.max_drawdown([1, 2, 3, 4])
        self.assertEqual(r["pct"], 0.0)

    def test_insufficient(self):
        self.assertIsNone(fx.max_drawdown([1]))
        self.assertIsNone(fx.max_drawdown([]))

    def test_backtest_real_numbers(self):
        """真实回测数字：momentum 权益曲线见过 -50%，不能报 0。"""
        curve = [1.0, 1.5, 1.2, 0.75, 1.1, 1.98]
        r = fx.max_drawdown(curve)
        self.assertAlmostEqual(r["pct"], -50.0, places=2)


class TestRange(unittest.TestCase):
    def test_pos_in_range(self):
        self.assertEqual(fx.pos_in_range_pct(50, 0, 100), 50.0)
        self.assertEqual(fx.pos_in_range_pct(10, 10, 20), 0.0)
        self.assertEqual(fx.pos_in_range_pct(20, 10, 20), 100.0)
        self.assertIsNone(fx.pos_in_range_pct(10, 20, 20))   # high<=low

    def test_drawdown_from_high(self):
        self.assertEqual(fx.drawdown_from_high_pct(19.89, 19.89), 0.0)
        self.assertEqual(fx.drawdown_from_high_pct(9.0, 10.0), -10.0)
        self.assertIsNone(fx.drawdown_from_high_pct(9, 0))


class TestRiskAdjusted(unittest.TestCase):
    """真实回测：momentum 与 reversal 的收益/回撤比。"""

    def test_momentum_vs_reversal(self):
        mom = fx.risk_adjusted_ratio(98.33, -50.05)
        rev = fx.risk_adjusted_ratio(85.28, -17.75)
        self.assertAlmostEqual(mom, 1.96, places=2)
        self.assertAlmostEqual(rev, 4.8, places=2)
        # 收益低的反而风险调整后更好 2.4 倍
        self.assertGreater(rev / mom, 2.4)

    def test_zero_dd_none(self):
        self.assertIsNone(fx.risk_adjusted_ratio(10, 0))
        self.assertIsNone(fx.risk_adjusted_ratio(None, -10))
        self.assertIsNone(fx.risk_adjusted_ratio(10, None))


class TestCost(unittest.TestCase):
    def test_weighted_avg_cost(self):
        # 老仓 200 @ 11.995，加 100 @ 10.0，费 5 元
        c = fx.weighted_avg_cost(200, 11.995, 100, 10.0, fee=5.0)
        expect = (200 * 11.995 + 1000 + 5) / 300
        self.assertAlmostEqual(c, round(expect, 4), places=4)

    def test_weighted_avg_cost_first_buy(self):
        c = fx.weighted_avg_cost(0, 0, 200, 11.97, fee=5.0)
        self.assertAlmostEqual(c, round((200 * 11.97 + 5) / 200, 4), places=4)

    def test_weighted_avg_cost_invalid(self):
        self.assertIsNone(fx.weighted_avg_cost(100, 10, 0, 10))
        self.assertIsNone(fx.weighted_avg_cost(0, 0, 0, 10))

    def test_breakeven_higher_than_cost(self):
        """涨回成本价不等于回本——必须覆盖卖出费用。"""
        be = fx.breakeven_price(10.0, 100, buy_fee=5.0, sell_fee_rate=0.0008)
        self.assertGreater(be, 10.0)
        self.assertAlmostEqual(be, round((1005) / (100 * (1 - 0.0008)), 4), places=4)

    def test_breakeven_no_fee(self):
        be = fx.breakeven_price(10.0, 100, 0, 0)
        self.assertEqual(be, 10.0)

    def test_stop_take(self):
        st = fx.stop_take_prices(11.995, 8, 15)
        self.assertEqual(st["stop"], 11.04)      # 11.995*0.92=11.0354→11.04
        self.assertEqual(st["take"], 13.79)      # 11.995*1.15=13.794→13.79

    def test_risk_reward(self):
        self.assertEqual(fx.risk_reward_ratio(100, 90, 130), 3.0)
        self.assertIsNone(fx.risk_reward_ratio(100, 100, 130))   # risk<=0

    def test_position_shares_lot_and_cap(self):
        sz = fx.position_shares(10000, 11.93, equity=20000,
                                max_position_pct=25, lot=100)
        # 单票上限 5000 → 5000/11.93=419 → 整手 400
        self.assertEqual(sz["shares"], 400)
        self.assertEqual(sz["binding"], "单票上限")
        self.assertAlmostEqual(sz["amount"], 4772.0, places=2)

    def test_position_shares_cash_binding(self):
        sz = fx.position_shares(1000, 11.93, lot=100)
        self.assertEqual(sz["shares"], 0)        # 1000/11.93=83 股 → 不足一手
        self.assertIn("不足一手", sz["binding"])

    def test_position_shares_star_lot(self):
        sz = fx.position_shares(10000, 50.0, lot=200)
        self.assertEqual(sz["shares"] % 200, 0)


class TestRuleDirection(unittest.TestCase):
    """★ 本次会话的核心踩坑：方向写反＝死规则。"""

    def test_lt_valid_when_price_above(self):
        """配「跌破 11.59」，现价 11.93 在上方 → 有效待触发。"""
        r = fx.check_rule_direction(11.93, 11.59, "lt")
        self.assertTrue(r["ok"])
        self.assertEqual(r["side"], "above")
        self.assertAlmostEqual(r["distance_pct"], 2.93, places=1)

    def test_lt_dead_when_price_below(self):
        """★ 格力真实事故：配「跌破 38.60」，现价 38.52 已在下方 → 死规则。"""
        r = fx.check_rule_direction(38.52, 38.60, "lt")
        self.assertFalse(r["ok"])
        self.assertEqual(r["side"], "below")
        self.assertIn("方向已失效", r["reason"])

    def test_gt_valid_when_price_below(self):
        """配「站上 38.55」，现价 38.52 在下方 → 有效，距离很近。"""
        r = fx.check_rule_direction(38.52, 38.55, "gt")
        self.assertTrue(r["ok"])
        self.assertEqual(r["side"], "below")
        self.assertLess(abs(r["distance_pct"]), 0.1)

    def test_gt_dead_when_price_above(self):
        r = fx.check_rule_direction(40.0, 38.55, "gt")
        self.assertFalse(r["ok"])

    def test_flat(self):
        r = fx.check_rule_direction(38.55, 38.55, "gt")
        self.assertFalse(r["ok"])
        self.assertEqual(r["side"], "flat")

    def test_le_ge(self):
        self.assertTrue(fx.check_rule_direction(11.93, 11.59, "le")["ok"])
        self.assertTrue(fx.check_rule_direction(38.52, 38.55, "ge")["ok"])

    def test_unknown_op(self):
        r = fx.check_rule_direction(10, 9, "xx")
        self.assertFalse(r["ok"])
        self.assertIn("未知操作符", r["reason"])

    def test_missing_price(self):
        self.assertFalse(fx.check_rule_direction(None, 9, "lt")["ok"])


class TestMaDrift(unittest.TestCase):
    """★ 规则价漂移：挂上去就不动，均线却每天在移。"""

    def test_within_threshold(self):
        r = fx.check_ma_drift(11.59, 11.593)
        self.assertFalse(r["need_update"])
        self.assertIsNone(r["suggest"])

    def test_exceed_threshold(self):
        """天山旧值 11.55 vs MA10 11.593：偏 0.37%，未超 1% 但接近。"""
        r = fx.check_ma_drift(11.55, 11.593)
        self.assertFalse(r["need_update"])
        self.assertAlmostEqual(r["drift_pct"], -0.37, places=2)

    def test_over_threshold_suggests(self):
        r = fx.check_ma_drift(11.00, 11.593)
        self.assertTrue(r["need_update"])
        self.assertEqual(r["suggest"], 11.59)
        self.assertIn("需更新", r["reason"])

    def test_none_ma(self):
        r = fx.check_ma_drift(11.55, None)
        self.assertFalse(r["need_update"])
        self.assertIsNone(r["drift_pct"])


class TestRuleSet(unittest.TestCase):
    """批量体检：方向 + 漂移一起跑。"""

    def setUp(self):
        self.rules = [
            {"code": "002532", "price": 11.59, "op": "lt", "label": "天山减仓"},
            {"code": "000651", "price": 38.60, "op": "lt", "label": "格力（方向错）"},
            {"code": "600900", "price": 29.41, "op": "lt", "label": "长电买入"},
        ]
        self.quotes = {"002532": 11.93, "000651": 38.52, "600900": 29.50}

    def test_finds_bad_direction(self):
        rows = fx.check_rule_set(self.rules, self.quotes)
        by_code = {r["code"]: r for r in rows}
        self.assertTrue(by_code["002532"]["ok"])
        self.assertFalse(by_code["000651"]["ok"])
        self.assertTrue(by_code["600900"]["ok"])
        self.assertTrue(any("方向" in p for p in by_code["000651"]["problems"]))

    def test_with_ma_drift(self):
        """声明了 ma_key 才做漂移检查（v2.8.4 语义）。"""
        rules = [{"code": "000651", "price": 38.60, "op": "lt",
                  "ma_key": "ma60", "label": "格力"}]
        ma = {"000651": {"ma60": 38.545}}
        rows = fx.check_rule_set(rules, self.quotes, ma)
        by_code = {r["code"]: r for r in rows}
        # 格力：方向错 + 38.60 vs MA60 38.545 偏 0.14%（未超阈值）
        self.assertFalse(by_code["000651"]["ok"])
        self.assertIsNotNone(by_code["000651"]["drift_pct"])

    def test_fixed_price_rules_skip_ma_drift(self):
        """回归 v2.8.4：固定价规则（-8% 硬止损/前低）绝不参与均线漂移。

        事故背景：早先 `ma_key = r.get("ma_key") or code`，未声明 ma_key 的
        规则会拿 code 键取到**别的**均线值，于是 11.02 的硬止损被建议
        "改成 11.87"——照做就把风控位换成了均线价，保护直接失效。
        """
        rules = [
            {"code": "002532", "price": 11.02, "op": "lt", "label": "-8%硬止损"},
            {"code": "510500", "price": 6.20, "op": "lt", "label": "前低止损"},
        ]
        quotes = {"002532": 12.19, "510500": 6.784}
        ma = {"002532": {"ma20": 11.7625}, "510500": {"ma20": 6.7644}}
        rows = fx.check_rule_set(rules, quotes, ma)
        for row in rows:
            self.assertNotIn("drift_pct", row, "固定价规则不应有漂移结论")
            self.assertIsNone(row.get("suggest_price"),
                              "固定价规则不应被建议改成均线价")
            self.assertTrue(row["ok"], "固定价规则不该因漂移被判为有问题")

    def test_declared_ma_key_is_actually_checked(self):
        """回归 v2.8.4：声明 ma_key 的规则必须**真的**被检查。

        早先 ma_key 被当作 ma_map 的键去查，而 ma_map 的键是股票代码，
        于是查不到 → drift=None → 静默跳过，"均线漂了也不提醒"。
        """
        rules = [{"code": "002532", "price": 11.55, "op": "lt",
                  "ma_key": "ma10", "label": "MA10 减仓"}]
        quotes = {"002532": 12.19}
        ma_nested = {"002532": {"ma10": 11.871, "ma20": 11.7625}}
        row = fx.check_rule_set(rules, quotes, ma_nested)[0]
        self.assertIsNotNone(row.get("drift_pct"), "MA10 规则应被检查到")
        self.assertAlmostEqual(row["drift_pct"], -2.70, places=2)
        self.assertEqual(row["suggest_price"], 11.87)

    def test_pick_ma_flat_and_nested(self):
        """_pick_ma 兼容两种 ma_map 形态。"""
        self.assertEqual(fx._pick_ma({"ma10": 11.87}, "ma10"), 11.87)
        self.assertEqual(fx._pick_ma({"MA10": 11.87}, "ma10"), 11.87)
        self.assertEqual(fx._pick_ma(9.99, "ma10"), 9.99)
        self.assertIsNone(fx._pick_ma(None, "ma10"))
        self.assertIsNone(fx._pick_ma({"ma20": 1.0}, "ma10"))

    def test_missing_quote(self):
        rows = fx.check_rule_set(
            [{"code": "999999", "price": 10, "op": "lt"}], {})
        self.assertFalse(rows[0]["ok"])

    def test_empty(self):
        self.assertEqual(fx.check_rule_set([], {}), [])


class TestSummary(unittest.TestCase):
    def test_full(self):
        closes = [10 + i * 0.1 for i in range(80)]
        out = fx.summary(closes)
        self.assertEqual(out["bars"], 80)
        for key in ("ma5", "ma10", "ma20", "ma60", "rsi14",
                    "volatility_20d", "pos_in_range", "max_drawdown"):
            self.assertIn(key, out)
        self.assertIn("ma_position", out)

    def test_price_override(self):
        """传了 price 就用它算偏离，而不是最后一根收盘。"""
        closes = [10.0] * 30
        out = fx.summary(closes, price=12.0)
        self.assertEqual(out["close"], 12.0)
        self.assertAlmostEqual(out["dev_ma20_pct"], 20.0, places=1)

    def test_empty(self):
        self.assertEqual(fx.summary([]), {"bars": 0})

    def test_short_series_has_none_ma60(self):
        """样本不足时对应均线必须是 None——不强算、也不填 0。"""
        out = fx.summary([10, 11, 12])
        self.assertIsNone(out["ma60"])
        self.assertIsNone(out["ma5"])      # 只有 3 根，MA5 也算不了
        self.assertIsNotNone(fx.summary(list(range(1, 8)))["ma5"])


class TestRealSnapshot20261102(unittest.TestCase):
    """2026-11-02 收盘真实数据回归（数值来自腾讯原始接口 + 脚本复核）。"""

    #: 天山铝业 002532 不复权收盘（31 根，2026-09-11 ~ 11-02）
    TIANSHAN = [13.22, 13.03, 12.96, 12.98, 12.73, 12.81, 12.55, 12.64,
                12.41, 12.11, 11.75, 12.21, 12.18, 12.17, 12.33, 12.00,
                12.11, 12.01, 11.27, 11.50, 11.52, 11.48, 11.27, 11.05,
                11.29, 11.31, 11.92, 11.87, 11.88, 11.93, 11.93]

    def test_tianshan_ma(self):
        self.assertAlmostEqual(fx.sma(self.TIANSHAN, 5), 11.906, places=3)
        self.assertAlmostEqual(fx.sma(self.TIANSHAN, 10), 11.593, places=3)
        self.assertAlmostEqual(fx.sma(self.TIANSHAN, 20), 11.7615, places=3)

    def test_tianshan_price_above_all(self):
        mas = fx.ma_set(self.TIANSHAN, (5, 10, 20))
        self.assertIn("全部上方", fx.ma_position(11.93, mas))


if __name__ == "__main__":
    unittest.main(verbosity=2)
