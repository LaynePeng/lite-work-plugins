# 扩展数据面单测（离线，零网络）：筹码分布算法 / 归一化 / 分发契约
#
# 🚫 红线：不连任何行情接口。所有取数函数在测试里被内存替身替换。
# 运行：python3 -m unittest plugins.astock-plugin.test_astock_extra
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _install_litework_stubs() -> None:
    """补齐 litework.* 最小契约（discover 时别的测试模块可能已建了一部分）。"""
    class _Plugin:
        name = "plugin"
        contributes = {}

        def install(self, kernel):
            pass

    class _ToolPlugin(_Plugin):
        version = ""
        description = ""
        removed_tools = []

        def get_tools(self):
            return []

        async def execute(self, name, args):
            return ""

    class _ToolDefinition:
        def __init__(self, name="", description="", parameters=None):
            self.name = name
            self.description = description
            self.parameters = parameters or {}

    def mod(name):
        m = types.ModuleType(name)
        sys.modules.setdefault(name, m)
        return sys.modules[name]

    mod("litework")
    mod("litework.core")
    core_types = mod("litework.core.types")
    if not hasattr(core_types, "ToolDefinition"):
        core_types.ToolDefinition = _ToolDefinition
    mod("litework.tools")
    tools_plugin = mod("litework.tools.plugin")
    if not hasattr(tools_plugin, "ToolPlugin"):
        tools_plugin.ToolPlugin = _ToolPlugin


_install_litework_stubs()

import astock_extra_data as ex  # noqa: E402
from astock_mixin_extra import ExtraToolsMixin  # noqa: E402


# ---------------------------------------------------------------- 纯函数


class NormalizeTest(unittest.TestCase):
    """日期 / 数值 / 涨停时分的归一化（缺数据一律 None，不用 0 顶替）。"""

    def test_day_parses_both_formats(self):
        self.assertEqual(ex._day("2026-11-04 00:00:00"), "2026-11-04")
        self.assertEqual(ex._day("20261031"), "2026-10-31")
        self.assertEqual(ex._day("2026-11-04"), "2026-11-04")

    def test_day_returns_none_for_garbage(self):
        """认不出的日期必须返回 None —— 返回 '' 会让下游排序/比较静默错位。"""
        for raw in (None, "", "  ", "not-a-date", "2026/11/04"):
            self.assertIsNone(ex._day(raw), raw)

    def test_num_none_instead_of_zero(self):
        """空值必须是 None：0 会被当成"确实是零"参与均值/求和，结论就歪了。"""
        for raw in (None, "", "-", "--", "N/A", "null"):
            self.assertIsNone(ex._num(raw), raw)

    def test_num_parses_with_thousands_separator(self):
        self.assertEqual(ex._num("1,234.50"), 1234.5)
        self.assertEqual(ex._num(3), 3.0)

    def test_fmt_zt_time(self):
        """涨停池的时间是 HHMMSS 整数：92500 要补成 09:25:00。"""
        self.assertEqual(ex._fmt_zt_time(92500), "09:25:00")
        self.assertEqual(ex._fmt_zt_time(144500), "14:45:00")
        self.assertEqual(ex._fmt_zt_time(None), "")


# ---------------------------------------------------------------- 筹码分布


def _bars(rows):
    """构造 K 线行（含 turnover_pct，筹码算法必需）。"""
    return [{"date": d, "open": o, "close": c, "high": h, "low": lo,
             "vol_dummy": 0, "turnover_pct": t}
            for d, o, c, h, lo, t in rows]


class TriangularWeightTest(unittest.TestCase):
    """三角分布权重：峰值在均价、面积归一、退化情形不产生全零。"""

    def test_weights_sum_to_one(self):
        grid = [10.0 + i * 0.1 for i in range(21)]     # 10.0 ~ 12.0
        w = ex._triangular_weights(grid, 10.5, 11.5, 11.0)
        self.assertAlmostEqual(sum(w), 1.0, places=9)

    def test_peak_at_average(self):
        grid = [10.0 + i * 0.1 for i in range(21)]
        w = ex._triangular_weights(grid, 10.5, 11.5, 11.0)
        peak = grid[w.index(max(w))]
        self.assertAlmostEqual(peak, 11.0, delta=0.15)

    def test_flat_limit_up_day_piles_on_one_price(self):
        """一字板（最高=最低）必须把权重全压在那一档，否则换手衰减会摊到全区间。"""
        grid = [10.0 + i * 0.1 for i in range(21)]
        w = ex._triangular_weights(grid, 11.0, 11.0, 11.0)
        self.assertAlmostEqual(sum(w), 1.0, places=9)
        self.assertEqual(w.count(max(w)), 1)

    def test_narrow_range_still_normalized(self):
        """当日振幅窄于网格步长时，不能返回全零（否则那天的换手衰减凭空丢失）。"""
        grid = [10.0 + i * 0.5 for i in range(5)]
        w = ex._triangular_weights(grid, 10.001, 10.002, 10.0015)
        self.assertAlmostEqual(sum(w), 1.0, places=9)

    def test_invalid_range_returns_zeros(self):
        grid = [10.0, 11.0]
        self.assertEqual(ex._triangular_weights(grid, 12.0, 10.0, 11.0), [0.0, 0.0])


class ChipDistributionTest(unittest.TestCase):
    """筹码分布：获利比例 / 平均成本 / 成本区间 / 集中度。"""

    def _single_price_bars(self, price, days, turnover):
        return _bars([[f"2026-07-{i + 1:02d}", price, price, price, price, turnover]
                      for i in range(days)])

    def test_all_chips_at_current_price(self):
        """价格完全不动时：获利比例≈100%、平均成本≈现价、集中度≈0。"""
        res = ex.chip_distribution(self._single_price_bars(10.0, 5, 20.0))
        self.assertAlmostEqual(res["price"], 10.0, places=6)
        self.assertGreater(res["profit_ratio"], 0.99)
        self.assertAlmostEqual(res["avg_cost"], 10.0, delta=0.05)
        self.assertLess(res["concentration_90"], 0.01)

    def test_losing_position_has_low_profit_ratio(self):
        """一路下跌：现价低于绝大多数筹码成本 → 获利比例应偏低（不是 100%）。"""
        rows = []
        price = 20.0
        for i in range(10):
            rows.append([f"2026-07-{i + 1:02d}", price, price, price, price, 15.0])
            price -= 1.0
        res = ex.chip_distribution(_bars(rows))
        self.assertLess(res["profit_ratio"], 0.20)
        self.assertGreater(res["avg_cost"], res["price"])

    def test_cost_bands_are_ordered(self):
        """90% 区间必须包住 70% 区间，否则分位插值写反了。"""
        rows = []
        for i in range(20):
            base = 10.0 + (i % 5)
            rows.append([f"2026-07-{i + 1:02d}", base, base + 0.2,
                         base + 0.5, base - 0.5, 8.0])
        res = ex.chip_distribution(_bars(rows))
        c70, c90 = res["cost_70"], res["cost_90"]
        self.assertLessEqual(c90[0], c70[0])
        self.assertGreaterEqual(c90[1], c70[1])

    def test_requires_turnover(self):
        """缺换手率的 K 线必须报错，而不是静默按 0 换手算出"全是首日筹码"。"""
        bars = [{"date": "2026-07-01", "open": 10, "close": 10,
                 "high": 10.5, "low": 9.5}]
        with self.assertRaises(ValueError):
            ex.chip_distribution(bars)

    def test_empty_bars_raises(self):
        with self.assertRaises(ValueError):
            ex.chip_distribution([])

    def test_unsorted_input_is_sorted_internally(self):
        """倒序传入（常见写法"最新在前"）结果必须与升序一致——衰减有方向。"""
        asc = self._single_price_bars(10.0, 4, 25.0)
        res_asc = ex.chip_distribution(asc)
        res_desc = ex.chip_distribution(list(reversed(asc)))
        self.assertAlmostEqual(res_asc["avg_cost"], res_desc["avg_cost"], places=6)


# ---------------------------------------------------------------- 情绪速算


class LimitSentimentTest(unittest.TestCase):
    """炸板率/连板梯队是纯计算，用假数据钉口径。"""

    def setUp(self):
        self._orig = ex.limit_pool

    def tearDown(self):
        ex.limit_pool = self._orig

    def _stub(self, zt, zb, dt):
        def fake(kind, date=None):
            return {"zt": zt, "zb": zb, "dt": dt}[kind]
        ex.limit_pool = fake

    def test_break_rate_denominator_includes_broken(self):
        """炸板率 = 炸板/(涨停+炸板)。只用涨停当分母会算出 >100%。"""
        self._stub([{"limit_days": 2}, {"limit_days": 1}], [{}, {}, {}], [])
        res = ex.limit_sentiment("20261104")
        self.assertEqual(res["zt_count"], 2)
        self.assertEqual(res["zb_count"], 3)
        self.assertEqual(res["break_rate_pct"], 60.0)

    def test_ladder_and_max_streak(self):
        self._stub([{"limit_days": 1}, {"limit_days": 1}, {"limit_days": 3}], [], [])
        res = ex.limit_sentiment("20261104")
        self.assertEqual(res["ladder"], {1: 2, 3: 1})
        self.assertEqual(res["max_streak"], 3)

    def test_empty_pools_give_none_break_rate(self):
        """三张池子全空（非交易日）时炸板率是 None 而不是 0——
        0 会被读成"今天没有一只炸板"，与"根本没数据"是两件事。"""
        self._stub([], [], [])
        res = ex.limit_sentiment("20261104")
        self.assertIsNone(res["break_rate_pct"])
        self.assertEqual(res["zt_count"], 0)

    def test_non_trading_day_marker(self):
        """push2ex 返回 data=null 时 limit_pool 必须给空列表（上层据此提示非交易日）。"""
        orig = ex._http_get_json
        ex._http_get_json = lambda url, headers=None: {"rc": 0, "data": None}
        try:
            self.assertEqual(ex.limit_pool("zt", "20261101"), [])
        finally:
            ex._http_get_json = orig


# ---------------------------------------------------------------- 分发契约


class _StubPlugin(ExtraToolsMixin):
    """只为跑分发逻辑：所有取数函数被换掉，实例不需要宿主。"""


class ActionDispatchTest(unittest.TestCase):
    """`_tool_extra` 的 action 路由与错误文案。"""

    def setUp(self):
        self.plugin = _StubPlugin()
        self._saved = {}

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(ex, name, fn)

    def _patch(self, name, fn):
        self._saved.setdefault(name, getattr(ex, name))
        setattr(ex, name, fn)

    def _run(self, args):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.plugin._tool_extra(args))
        finally:
            loop.close()

    def test_missing_action_lists_all_actions(self):
        out = self._run({})
        self.assertIn("[Error]", out)
        for action in ("limit_sentiment", "board_flow", "chips", "docs", "macro"):
            self.assertIn(action, out)

    def test_unknown_action_is_rejected(self):
        out = self._run({"action": "nope"})
        self.assertIn("[Error]", out)
        self.assertIn("nope", out)

    def test_unknown_action_lists_all_available(self):
        out = self._run({"action": "nope"})
        for action in ("limit_sentiment", "strong", "hot", "industry", "board_flow",
                       "st", "margin", "block_trade", "holders", "dividend",
                       "lockup", "lhb", "concepts", "chips", "events", "docs",
                       "macro"):
            self.assertIn(action, out, action)

    def test_code_required_for_margin(self):
        out = self._run({"action": "margin"})
        self.assertIn("[Error]", out)
        self.assertIn("code", out)

    def test_code_required_for_chips(self):
        self.assertIn("[Error]", self._run({"action": "chips"}))
        self.assertIn("[Error]", self._run({"action": "docs"}))

    def test_events_unknown_type_explains_choices(self):
        out = self._run({"action": "events", "event": "nope"})
        self.assertIn("[Error]", out)
        self.assertIn("forecast", out)

    def test_macro_unknown_explains_choices(self):
        out = self._run({"action": "macro", "macro": "nope"})
        self.assertIn("[Error]", out)
        for key in ("lpr", "pmi", "yield", "repo", "calendar"):
            self.assertIn(key, out)

    def test_docs_finance_rejects_bad_report_type(self):
        out = self._run({"action": "docs", "doc": "finance",
                         "code": "600519", "report_type": "xxx"})
        self.assertIn("[Error]", out)

    def test_non_trading_day_message_is_explicit(self):
        """三张池子全空时必须说"多半不是交易日"，不能渲染成"零涨停"。"""
        self._patch("limit_sentiment", lambda date=None: {
            "date": "20261101", "zt_count": 0, "zb_count": 0, "dt_count": 0,
            "break_rate_pct": None, "max_streak": 0, "ladder": {},
            "zt_stocks": [], "zb_stocks": [], "dt_stocks": []})
        out = self._run({"action": "limit_sentiment"})
        self.assertIn("不是交易日", out)
        self.assertIn("零涨停", out)

    def test_sentiment_renders_break_rate_and_ladder(self):
        self._patch("limit_sentiment", lambda date=None: {
            "date": "20261104", "zt_count": 2, "zb_count": 3, "dt_count": 1,
            "break_rate_pct": 60.0, "max_streak": 3, "ladder": {1: 1, 3: 1},
            "zt_stocks": [{"code": "000001", "name": "平安银行", "price": 10.0,
                           "change_pct": 10.0, "streak_boards": 3,
                           "seal_fund_yuan": 1e8, "first_seal": "09:30:00"}],
            "zb_stocks": [], "dt_stocks": []})
        self._patch("ths_limit_reason", lambda date=None: [])
        out = self._run({"action": "limit_sentiment"})
        self.assertIn("60.0%", out)
        self.assertIn("3 板", out)
        self.assertIn("平安银行", out)

    def test_chips_states_window_turnover_caveat(self):
        """累计换手 <100% 时必须显式说明"窗口前筹码仍在"，否则会被当成真实持仓成本。"""
        self._patch("kline_with_turnover", lambda *a, **k: _bars(
            [["2026-07-01", 10, 10, 10.5, 9.5, 10.0]]))
        out = self._run({"action": "chips", "code": "600519"})
        self.assertIn("累计换手", out)
        self.assertIn("模型估算", out)

    def test_board_flow_labels_period(self):
        self._patch("board_fund_flow", lambda b, p, n: {
            "board_type": b, "period": p, "total": 1,
            "rows": [{"rank": 1, "name": "半导体", "code": "BK1036",
                      "change_pct": 2.0, "main_net_yuan": 1e8,
                      "main_pct": 3.0, "leader": "某龙头"}]})
        out = self._run({"action": "board_flow", "board_type": "concept",
                         "period": "5d"})
        self.assertIn("概念", out)
        self.assertIn("5日", out)
        self.assertIn("半导体", out)

    def test_industry_renders_leader_name(self):
        """领涨股必须是名称不是代码（历史上 f140/f128 取反过）。"""
        self._patch("industry_rank", lambda n: {
            "total": 2,
            "top": [{"rank": 1, "name": "光伏辅材", "change_pct": 9.94,
                     "up_count": 18, "down_count": 0, "leader": "永臻股份",
                     "leader_code": "603381", "leader_change_pct": 9.98}],
            "bottom": []})
        out = self._run({"action": "industry"})
        self.assertIn("永臻股份", out)
        self.assertIn("光伏辅材", out)


class SchemaSurfaceTest(unittest.TestCase):
    """工具 schema 与实现必须一致（action 枚举不能有"声明了却没实现"的项）。"""

    def test_all_declared_actions_dispatch(self):
        import astock_tools as decl
        tool = next(t for t in decl.build_tools() if t.name == "astock_extra")
        declared = set(tool.parameters["properties"]["action"]["enum"])
        plugin = _StubPlugin()
        import inspect
        src = inspect.getsource(plugin._tool_extra)
        for action in declared:
            self.assertIn(f'"{action}"', src, f"schema 声明了 {action} 但分发里没有")

    def test_action_is_required(self):
        import astock_tools as decl
        tool = next(t for t in decl.build_tools() if t.name == "astock_extra")
        self.assertEqual(tool.parameters.get("required"), ["action"])


if __name__ == "__main__":
    unittest.main()
