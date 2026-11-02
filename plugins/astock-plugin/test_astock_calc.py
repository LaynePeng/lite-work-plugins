"""`astock_calc` 工具层单测（v2.4.0）。

**分层原则**：数学正确性在 `test_astock_formula` 里验（纯数字进纯数字出）；
本文件只验**工具层**——参数解析、均线基准选择、渲染与降级。

🚫 红线：零网络。行情与 K 线都用内存替身。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import test_astock_plugin as T                      # noqa: E402

T._install_litework_stubs()

import astock_price_rule as pr                      # noqa: E402


def infer_ma_key(rules):
    """镜像 `_load_plan_rules` 里的 ma_key 推断逻辑（纯函数版，便于单测）。

    规则 label 里常已写明依据哪条均线（"跌破11.55(MA10)"），从里面认出来
    比一律按 MA20 兜底准确得多——后者会对 MA10 规则误报漂移。
    """
    for r in rules:
        if r.get("ma_key"):
            continue
        m = re.search(r"MA\s*(5|10|20|60)", str(r.get("label") or ""),
                      re.IGNORECASE)
        if m:
            r["ma_key"] = f"ma{m.group(1)}"
    return rules


class TestMaKeyInference(unittest.TestCase):
    def test_infer_from_label(self):
        rules, _ = pr.parse_rules([
            {"code": "002532", "price": 11.55, "op": "lt",
             "label": "跌破11.55(MA10) 减100股"},
            {"code": "000651", "price": 38.60, "op": "gt",
             "label": "站上38.60(MA60)"},
            {"code": "600900", "price": 29.41, "op": "lt",
             "label": "跌破29.41(MA20)→可买"},
        ])
        out = infer_ma_key(rules)
        keys = [r.get("ma_key") for r in out]
        self.assertEqual(keys, ["ma10", "ma60", "ma20"])

    def test_explicit_ma_key_wins(self):
        rules, _ = pr.parse_rules([
            {"code": "002532", "price": 11.55, "op": "lt",
             "ma_key": "ma5", "label": "跌破(MA10)"}])
        out = infer_ma_key(rules)
        self.assertEqual(out[0]["ma_key"], "ma5")     # 显式声明不被覆盖

    def test_no_match_left_alone(self):
        rules, _ = pr.parse_rules([
            {"code": "002532", "price": 11.55, "op": "lt",
             "label": "跌破前低减仓"}])
        out = infer_ma_key(rules)
        self.assertNotIn("ma_key", out[0])            # 交给 ma20 兜底

    def test_lowercase_and_spacing(self):
        rules, _ = pr.parse_rules([
            {"code": "1", "price": 1, "op": "lt", "label": "跌破 ma 20 支撑"}])
        out = infer_ma_key(rules)
        self.assertEqual(out[0]["ma_key"], "ma20")


class TestSchemeWhitespace(unittest.TestCase):
    """label 里的括号必须能被认出来——中文全角/半角都要吃。"""

    def test_various_bracket_styles(self):
        cases = [
            ("跌破11.55(MA10)", "ma10"),
            ("跌破11.55（MA10）", "ma10"),          # 全角括号
            ("跌破11.55[MA10]", "ma10"),
            ("跌破11.55 MA10 支撑", "ma10"),
        ]
        for label, expect in cases:
            rules, _ = pr.parse_rules([{"code": "1", "price": 11.55,
                                        "op": "lt", "label": label}])
            self.assertEqual(infer_ma_key(rules)[0].get("ma_key"), expect,
                             f"label={label!r} 未认出 {expect}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
