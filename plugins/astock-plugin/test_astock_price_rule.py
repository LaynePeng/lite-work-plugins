"""精确价位规则单测：并入 ruthless-watch 后的语义等价性。

**为什么必须逐条对拍**：这不是新功能，是**迁移**。迁移最怕"看起来能跑、
但边界变了"——比如 `<` 变成 `<=`、量能条件从"必须满足"变成"有就用"。
所以这里的用例直接照着原插件的 `match_rule` 行为写，确保搬过来没走样。

🚫 红线：零网络、零真实台账。全部是纯函数输入输出。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import astock_price_rule as pr                    # noqa: E402


def Q(price, vr=None, chg=0.0, name="格力电器", code="000651"):
    return {"code": code, "name": name, "price": price,
            "volume_ratio": vr, "change_pct": chg}


class TestParseRules(unittest.TestCase):
    def test_normal(self):
        rules, errs = pr.parse_rules([
            {"code": "000651", "price": 36.0, "op": "lt", "kind": "stop_loss"}])
        self.assertEqual(len(rules), 1)
        self.assertEqual(errs, [])
        self.assertEqual(rules[0]["price"], 36.0)
        self.assertEqual(rules[0]["op"], "lt")

    def test_empty(self):
        for v in (None, "", []):
            rules, errs = pr.parse_rules(v)
            self.assertEqual(rules, [])
            self.assertEqual(errs, [])

    def test_single_dict_tolerated(self):
        """单条写成对象也认（用户在计划编辑器里少写一对 [] 很常见）。"""
        rules, errs = pr.parse_rules(
            {"code": "000651", "price": 36.0, "op": "lt"})
        self.assertEqual(len(rules), 1)

    def test_bad_entries_skipped_not_crash(self):
        """一条写错不该让整个探针停摆——那会连其他持仓的信号一起丢掉。"""
        rules, errs = pr.parse_rules([
            {"code": "000651", "price": 36.0, "op": "lt"},   # 好
            {"price": 10.0},                                  # 缺 code
            {"code": "000002", "price": "abc"},               # price 非法
            "not-a-dict",
        ])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["code"], "000651")
        self.assertEqual(len(errs), 3)

    def test_unknown_op_falls_back_to_lt_with_warning(self):
        rules, errs = pr.parse_rules(
            [{"code": "1", "price": 10, "op": "below"}])
        self.assertEqual(rules[0]["op"], "lt")
        self.assertTrue(any("op=" in e for e in errs))

    def test_non_list(self):
        rules, errs = pr.parse_rules("000651 36.0")
        self.assertEqual(rules, [])
        self.assertTrue(errs)


class TestMatchRule(unittest.TestCase):
    def _r(self, **kw):
        base = {"code": "000651", "name": "格力电器", "kind": "stop_loss",
                "price": 36.0, "op": "lt", "label": "跌破前低"}
        base.update(kw)
        return base

    def test_lt(self):
        self.assertIsNotNone(pr.match_rule(self._r(), Q(35.99)))
        self.assertIsNone(pr.match_rule(self._r(), Q(36.0)),
                          "lt 是严格小于，等于不触发")
        self.assertIsNone(pr.match_rule(self._r(), Q(36.01)))

    def test_le_gt_ge(self):
        self.assertIsNotNone(pr.match_rule(self._r(op="le"), Q(36.0)))
        self.assertIsNotNone(pr.match_rule(self._r(op="gt", price=37.11),
                                          Q(37.12)))
        self.assertIsNone(pr.match_rule(self._r(op="gt", price=37.11),
                                        Q(37.11)))
        self.assertIsNotNone(pr.match_rule(self._r(op="ge", price=37.11),
                                           Q(37.11)))

    def test_vol_ratio_gt_required(self):
        """'放量站上'必须真的放量：量比不够就不算触发。"""
        r = self._r(op="gt", price=37.0, vol_ratio_gt=1.2, kind="rebound")
        self.assertIsNotNone(pr.match_rule(r, Q(37.5, vr=1.5)))
        self.assertIsNone(pr.match_rule(r, Q(37.5, vr=1.1)))

    def test_vol_ratio_lt_required(self):
        """'缩量站稳'：量比过高说明不是缩量。"""
        r = self._r(op="gt", price=37.0, vol_ratio_lt=1.0)
        self.assertIsNotNone(pr.match_rule(r, Q(37.5, vr=0.8)))
        self.assertIsNone(pr.match_rule(r, Q(37.5, vr=1.5)))

    def test_missing_volume_ratio_does_not_block(self):
        """行情没带量比时不该因为'无法验证'就永远不报。

        与原插件行为一致（`if cond is not None and vr is not None`）：
        拿不到量比就不做量能过滤，而不是判为未触发。
        """
        r = self._r(op="gt", price=37.0, vol_ratio_gt=1.2)
        self.assertIsNotNone(pr.match_rule(r, Q(37.5, vr=None)))

    def test_missing_price(self):
        self.assertIsNone(pr.match_rule(self._r(), {"price": None}))

    def test_output_shape_matches_hint_fields(self):
        """输出字段要与 astock_hints.Hint 对齐，才能复用邮件与去重链路。"""
        sig = pr.match_rule(self._r(), Q(35.5, vr=1.3, chg=-2.1))
        for k in ("kind", "code", "name", "severity", "title", "detail",
                  "suggestion", "target", "op"):
            self.assertIn(k, sig)
        self.assertIn("现价 35.50", sig["title"])
        self.assertIn("跌破", sig["title"])
        self.assertIn("量比 1.30", sig["title"])

    def test_title_uses_label_when_given(self):
        sig = pr.match_rule(self._r(label="跌破前低 36.00 清仓"), Q(35.5))
        self.assertIn("跌破前低 36.00 清仓", sig["title"])

    def test_title_falls_back(self):
        sig = pr.match_rule(self._r(label=""), Q(35.5))
        self.assertIn("格力电器", sig["title"])
        self.assertIn("36", sig["title"])

    def test_severity_validated(self):
        sig = pr.match_rule(self._r(severity="URGENT"), Q(35.5))
        self.assertEqual(sig["severity"], "urgent")
        sig2 = pr.match_rule(self._r(severity="什么鬼"), Q(35.5))
        self.assertEqual(sig2["severity"], "action")


class TestOncePerDay(unittest.TestCase):
    def test_fires_once_then_suppressed(self):
        """价位条件是**持续满足**的：不去重就会每 5 分钟报一次。"""
        st = pr.PriceRuleState()
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0,
                                    "op": "lt", "kind": "stop_loss"}])
        quotes = {"000651": Q(35.0)}
        fired1, tags1 = pr.evaluate(rules, quotes, state=st)
        self.assertEqual(len(fired1), 1)
        self.assertEqual(tags1, ["000651:stop_loss:36"])
        fired2, tags2 = pr.evaluate(rules, quotes, state=st)
        self.assertEqual(fired2, [], "同一天第二次不该再报")
        self.assertEqual(tags2, [])

    def test_resets_next_day(self):
        st = pr.PriceRuleState()
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0,
                                    "op": "lt", "kind": "stop_loss"}])
        quotes = {"000651": Q(35.0)}
        d1 = dt.datetime(2026, 11, 3, 10, 0)
        d2 = dt.datetime(2026, 11, 4, 10, 0)
        self.assertEqual(len(pr.evaluate(rules, quotes, state=st, now=d1)[0]), 1)
        self.assertEqual(len(pr.evaluate(rules, quotes, state=st, now=d1)[0]), 0)
        self.assertEqual(len(pr.evaluate(rules, quotes, state=st, now=d2)[0]), 1,
                         "跨日应重新允许提醒")

    def test_once_per_day_off_repeats(self):
        st = pr.PriceRuleState()
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0,
                                    "op": "lt"}])
        quotes = {"000651": Q(35.0)}
        for _ in range(3):
            fired, _t = pr.evaluate(rules, quotes, state=st, once_per_day=False)
            self.assertEqual(len(fired), 1)

    def test_different_targets_are_independent(self):
        """同一只票的两条价位（36.0 / 34.0）互不干扰。"""
        st = pr.PriceRuleState()
        rules, _ = pr.parse_rules([
            {"code": "000651", "price": 36.0, "op": "lt", "kind": "watch"},
            {"code": "000651", "price": 34.0, "op": "lt", "kind": "watch"},
        ])
        quotes = {"000651": Q(33.5)}      # 两条都满足
        fired, tags = pr.evaluate(rules, quotes, state=st)
        self.assertEqual(len(fired), 2)
        self.assertEqual(len(set(tags)), 2)


class TestEvaluate(unittest.TestCase):
    def test_snapshot_tags_contain_no_price(self):
        """snapshot 只放信号标签，不放价格。

        宿主按 snapshot 的 sha256 判断"有无变化"，价格放进去等于每次探测都
        判为变化 → 每 5 分钟白烧一次 LLM。
        """
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0,
                                    "op": "lt"}])
        _fired, tags = pr.evaluate(rules, {"000651": Q(35.0)}, once_per_day=False)
        self.assertEqual(tags, ["000651:watch:36"])
        self.assertNotIn("35", tags[0], "标签里不该出现现价")

    def test_missing_quote_skipped(self):
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0, "op": "lt"}])
        fired, tags = pr.evaluate(rules, {}, once_per_day=False)
        self.assertEqual(fired, [])
        self.assertEqual(tags, [])

    def test_not_triggered(self):
        rules, _ = pr.parse_rules([{"code": "000651", "price": 36.0, "op": "lt"}])
        fired, _ = pr.evaluate(rules, {"000651": Q(40.0)}, once_per_day=False)
        self.assertEqual(fired, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
