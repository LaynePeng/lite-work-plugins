# 纯逻辑单测（离线，零网络）：市场规则 / 台账算账 / 提示引擎。
#
# 🚫 红线：不连任何行情接口。数据层用内存替身（`_fetch_stub`）替换，
#   台账一律写到 tempfile 目录，绝不碰用户真实 .astock/ledger.sqlite。
# 运行：python3 -m unittest plugins.astock-plugin.test_astock_plugin
import importlib.util
import os
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 桩
def _install_litework_stubs() -> None:
    """插件模块级 import litework.*；测试环境没有主程序包，按最小契约打桩。"""
    if "litework" in sys.modules:
        return

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

    mods = {
        "litework": types.ModuleType("litework"),
        "litework.core": types.ModuleType("litework.core"),
        "litework.core.types": types.ModuleType("litework.core.types"),
        "litework.tools": types.ModuleType("litework.tools"),
        "litework.tools.plugin": types.ModuleType("litework.tools.plugin"),
    }
    mods["litework.core.types"].ToolDefinition = _ToolDefinition
    mods["litework.tools.plugin"].ToolPlugin = _ToolPlugin
    for k, m in mods.items():
        sys.modules.setdefault(k, m)


_install_litework_stubs()
sys.path.insert(0, _HERE)      # 与宿主一致：插件目录插入 sys.path（裸 import 兄弟模块）

_SPEC = importlib.util.spec_from_file_location(
    "astock_plugin_under_test", os.path.join(_HERE, "plugin.py"))
astock = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(astock)  # type: ignore[union-attr]

mkt = astock.mkt
orders_mod = astock.orders
# 复用 plugin 内部已导入的 astock_notify（同一模块对象）
notify_mod = astock.notify
ledger_mod = astock.Ledger
hints = astock.hints
quant = astock.quant                  # 量化选股引擎（v1.8.0）
quant_tool = astock.quant_tool        # 扫描/缓存/渲染接入层
backtest_mod = astock.backtest        # 回测引擎（v1.9.0）
backtest_tool = astock.backtest_tool  # 回测取数/缓存/卡片接入层
archive_mod = astock.archive_mod      # 本地历史行情存档（v2.0.0）
astock_actions = astock.actions       # 操作指示（先卖后买）（v2.0.0）


# ---------------------------------------------------------------- 市场规则


class TestCodeNormalize(unittest.TestCase):
    def test_plain_and_prefixed(self):
        self.assertEqual(mkt.normalize_code("600000"), ("600000", "sh"))
        self.assertEqual(mkt.normalize_code("sh600000"), ("600000", "sh"))
        self.assertEqual(mkt.normalize_code("SH600000"), ("600000", "sh"))
        self.assertEqual(mkt.normalize_code("000001"), ("000001", "sz"))
        self.assertEqual(mkt.normalize_code("000001.sz"), ("000001", "sz"))
        self.assertEqual(mkt.normalize_code("sz.000001"), ("000001", "sz"))
        self.assertEqual(mkt.normalize_code("430047"), ("430047", "bj"))
        self.assertEqual(mkt.normalize_code("510300"), ("510300", "sh"))

    def test_index_kind_overrides_same_code(self):
        # 000001 既是个股（平安银行，深市）也是上证指数（沪市）
        self.assertEqual(mkt.normalize_code("000001"), ("000001", "sz"))
        self.assertEqual(mkt.normalize_code("000001", "index"), ("000001", "sh"))

    def test_mismatched_venue_rejected(self):
        # sh300750 是典型手误（创业板在深市），必须报错而不是静默取错数据
        with self.assertRaises(ValueError):
            mkt.normalize_code("sh300750")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            mkt.normalize_code("")


class TestLimitUpDown(unittest.TestCase):
    def test_main_board_10pct(self):
        up, down, pct = mkt.limit_prices(10.00, "600000", "浦发银行")
        self.assertEqual(pct, 10.0)
        self.assertAlmostEqual(up, 11.00)
        self.assertAlmostEqual(down, 9.00)

    def test_gem_and_star_20pct(self):
        for code in ("300750", "688981"):
            up, down, pct = mkt.limit_prices(10.00, code, "某股票")
            self.assertEqual(pct, 20.0, code)
            self.assertAlmostEqual(up, 12.00)
            self.assertAlmostEqual(down, 8.00)

    def test_bj_30pct(self):
        up, _, pct = mkt.limit_prices(10.00, "430047", "诺思兰德")
        self.assertEqual(pct, 30.0)
        self.assertAlmostEqual(up, 13.00)

    def test_st_5pct(self):
        for name in ("ST德豪", "*ST海航", "SST前锋", "S*ST天发"):
            up, down, pct = mkt.limit_prices(10.00, "600000", name)
            self.assertEqual(pct, 5.0, name)
            self.assertAlmostEqual(up, 10.50)
            self.assertAlmostEqual(down, 9.50)

    def test_rounding_to_cent(self):
        # 9.53 * 1.1 = 10.483 → 10.48；不能算成 10.49 或 10.483
        up, down, _ = mkt.limit_prices(9.53, "600000", "浦发银行")
        self.assertEqual(up, 10.48)
        self.assertEqual(down, 8.58)      # 9.53*0.9 = 8.577 → 8.58

    def test_no_prev_close(self):
        up, down, pct = mkt.limit_prices(0, "600000", "浦发银行")
        self.assertIsNone(up)
        self.assertIsNone(down)
        self.assertEqual(pct, 10.0)

    def test_index_has_no_limit(self):
        up, down, pct = mkt.limit_prices(3000, "000001", "上证指数", kind="index")
        self.assertEqual(pct, 0.0)
        self.assertIsNone(up)


class TestFees(unittest.TestCase):
    def test_min_commission_applied(self):
        # 小额买入：佣金按最低 5 元
        bd = mkt.calc_fee(1000.0, "buy")
        self.assertAlmostEqual(bd.commission, 5.0)
        self.assertAlmostEqual(bd.stamp_tax, 0.0)       # 买入无印花税

    def test_rate_commission_applied(self):
        bd = mkt.calc_fee(1_000_000.0, "buy")
        self.assertAlmostEqual(bd.commission, 250.0)    # 万 2.5
        self.assertAlmostEqual(bd.transfer_fee, 10.0)   # 0.001%

    def test_sell_has_stamp_tax(self):
        bd = mkt.calc_fee(100_000.0, "sell")
        self.assertAlmostEqual(bd.stamp_tax, 50.0)      # 0.05%
        self.assertAlmostEqual(bd.commission, 25.0)

    def test_etf_no_stamp_no_transfer(self):
        bd = mkt.calc_fee(100_000.0, "sell", board=mkt.BOARD_ETF)
        self.assertAlmostEqual(bd.stamp_tax, 0.0)
        self.assertAlmostEqual(bd.transfer_fee, 0.0)
        self.assertAlmostEqual(bd.commission, 25.0)

    def test_total_is_sum(self):
        bd = mkt.calc_fee(100_000.0, "sell")
        self.assertAlmostEqual(bd.total, bd.commission + bd.stamp_tax +
                               bd.transfer_fee + bd.other, places=6)


class TestLotSize(unittest.TestCase):
    def test_star_200_min(self):
        self.assertEqual(mkt.lot_size("688981"), 200)
        self.assertEqual(mkt.round_lot(350, "688981"), 200)
        self.assertEqual(mkt.round_lot(199, "688981"), 0)

    def test_main_100(self):
        self.assertEqual(mkt.round_lot(199, "600000"), 100)
        self.assertEqual(mkt.round_lot(99, "600000"), 0)

    def test_buy_must_be_lot(self):
        self.assertIsNotNone(mkt.check_order_size(150, "600000", "buy"))
        self.assertIsNone(mkt.check_order_size(200, "600000", "buy"))

    def test_sell_odd_lot_only_when_clearing(self):
        # 持仓 150 股，卖 150 合法（一次性清掉零股）
        self.assertIsNone(mkt.check_order_size(150, "600000", "sell", holding=150))
        # 持仓 250 股，卖 150 不合法（会留下 100 股但本次是零股申报）
        self.assertIsNotNone(mkt.check_order_size(150, "600000", "sell", holding=250))

    def test_sell_more_than_holding_rejected(self):
        self.assertIsNotNone(mkt.check_order_size(500, "600000", "sell", holding=100))


class TestTradingStatus(unittest.TestCase):
    def test_morning_session(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 23, 10, 0)      # 周五 10:00
        st = mkt.trading_status(now)
        self.assertTrue(st["open"])
        self.assertIn("上午", st["phase"])

    def test_lunch_break(self):
        import datetime as dt
        st = mkt.trading_status(dt.datetime(2026, 10, 23, 12, 0))
        self.assertFalse(st["open"])
        self.assertEqual(st["phase"], "午间休市")

    def test_weekend(self):
        import datetime as dt
        st = mkt.trading_status(dt.datetime(2026, 10, 24, 10, 0))   # 周六
        self.assertFalse(st["open"])
        self.assertIn("非交易日", st["phase"])

    def test_after_close(self):
        import datetime as dt
        st = mkt.trading_status(dt.datetime(2026, 10, 23, 15, 30))
        self.assertFalse(st["open"])
        self.assertEqual(st["phase"], "已收盘")


# ---------------------------------------------------------------- 台账


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astock-test-")
        self.path = os.path.join(self.tmp, "ledger.sqlite")
        self.ledger = ledger_mod(self.path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pos(self, code):
        for p in self.ledger.list_positions():
            if p["code"] == code:
                return p
        return None


class TestLedgerPosition(LedgerTestBase):
    def test_buy_creates_position_with_fee_in_cost(self):
        r = self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        p = self.pos("600000")
        self.assertIsNotNone(p)
        self.assertEqual(p["shares"], 1000)
        # 成本含买入费用：金额 10000 + 费用（5 元佣金 + 0.1 元过户费）
        expected = (10000 + r["fee"]) / 1000
        self.assertAlmostEqual(p["cost_price"], expected, places=4)
        self.assertGreater(p["cost_price"], 10.0)      # 费用让成本高于成交价

    def test_weighted_average_cost(self):
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        self.ledger.record_trade("600000", "buy", 12.0, 1000, trade_date="2026-10-20")
        p = self.pos("600000")
        self.assertEqual(p["shares"], 2000)
        # 均价必须在 10 与 12 之间且贴近 11（含费用略高）
        self.assertTrue(10.9 < p["cost_price"] < 11.1, p["cost_price"])

    def test_sell_keeps_cost_price(self):
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        cost_before = self.pos("600000")["cost_price"]
        r = self.ledger.record_trade("600000", "sell", 12.0, 500, trade_date="2026-10-22")
        p = self.pos("600000")
        self.assertEqual(p["shares"], 500)
        self.assertAlmostEqual(p["cost_price"], cost_before, places=4)
        # 已实现盈亏 = (12 - 成本) * 500 - 费用
        expect = (12.0 - cost_before) * 500 - r["fee"]
        self.assertAlmostEqual(r["realized_pnl"], expect, places=2)
        self.assertGreater(r["realized_pnl"], 0)

    def test_full_sell_removes_position(self):
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        self.ledger.record_trade("600000", "sell", 11.0, 1000, trade_date="2026-10-22")
        self.assertIsNone(self.pos("600000"))
        rp = self.ledger.realized_pnl()
        self.assertGreater(rp["realized_pnl"], 0)
        self.assertEqual(rp["sell_count"], 1)

    def test_sell_without_position_rejected(self):
        with self.assertRaises(ledger_mod.__init__.__globals__["LedgerError"]):
            self.ledger.record_trade("600000", "sell", 10.0, 100)


class TestLedgerT1(LedgerTestBase):
    def test_today_buy_not_sellable(self):
        import datetime as dt
        today = dt.date.today().isoformat()
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date=today)
        p = self.pos("600000")
        self.assertEqual(p["available_today"], 0)      # T+1 锁定
        with self.assertRaises(Exception) as ctx:
            self.ledger.record_trade("600000", "sell", 10.5, 500, trade_date=today)
        self.assertIn("T+1", str(ctx.exception))

    def test_yesterday_buy_is_sellable(self):
        import datetime as dt
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        today = dt.date.today().isoformat()
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date=yday)
        p = self.pos("600000")
        self.assertEqual(p["available_today"], 1000)
        r = self.ledger.record_trade("600000", "sell", 10.5, 500, trade_date=today)
        self.assertEqual(r["shares_after"], 500)

    def test_mixed_buy_availability(self):
        import datetime as dt
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        today = dt.date.today().isoformat()
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date=yday)
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date=today)
        p = self.pos("600000")
        self.assertEqual(p["shares"], 2000)
        self.assertEqual(p["available_today"], 1000)   # 只有昨日那笔可卖


class TestLedgerValidation(LedgerTestBase):
    def test_future_date_rejected(self):
        import datetime as dt
        future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        with self.assertRaises(Exception) as ctx:
            self.ledger.record_trade("600000", "buy", 10.0, 100, trade_date=future)
        self.assertIn("未来", str(ctx.exception))

    def test_bad_date_format_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self.ledger.record_trade("600000", "buy", 10.0, 100, trade_date="2026/10/20")
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_buy_not_lot_rejected(self):
        with self.assertRaises(Exception):
            self.ledger.record_trade("600000", "buy", 10.0, 150)

    def test_negative_price_rejected(self):
        with self.assertRaises(Exception):
            self.ledger.record_trade("600000", "buy", -1, 100)

    def test_bad_side_rejected(self):
        with self.assertRaises(Exception):
            self.ledger.record_trade("600000", "short", 10.0, 100)


class TestLedgerRepair(LedgerTestBase):
    def test_delete_trade_rebuilds_position(self):
        r1 = self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        self.ledger.record_trade("600000", "buy", 12.0, 1000, trade_date="2026-10-21")
        rows = self.ledger.trades("600000")
        first_id = min(r["id"] for r in rows)
        self.ledger.delete_trade(first_id)
        p = self.pos("600000")
        self.assertEqual(p["shares"], 1000)
        # 剩下的是 12 元那笔，成本应接近 12（含费用）
        self.assertTrue(11.99 < p["cost_price"] < 12.05, p["cost_price"])

    def test_rebuild_matches_sequential(self):
        self.ledger.record_trade("600000", "buy", 10.0, 1000, trade_date="2026-10-20")
        self.ledger.record_trade("600000", "buy", 11.0, 500, trade_date="2026-10-21")
        before = self.pos("600000")
        self.ledger.rebuild_position("600000")
        after = self.pos("600000")
        self.assertEqual(before["shares"], after["shares"])
        self.assertAlmostEqual(before["cost_price"], after["cost_price"], places=4)


class TestLedgerCashAndWatch(LedgerTestBase):
    def test_cash_set_and_add(self):
        self.assertEqual(self.ledger.get_cash()["balance"], 0.0)
        self.ledger.set_cash(50000)
        self.assertEqual(self.ledger.get_cash()["balance"], 50000.0)
        self.ledger.add_cash(-2000)
        self.assertEqual(self.ledger.get_cash()["balance"], 48000.0)
        with self.assertRaises(Exception):
            self.ledger.set_cash(-1)

    def test_watch_add_remove(self):
        self.ledger.add_watch("600000", "浦发银行", "观察")
        self.assertEqual(len(self.ledger.list_watch()), 1)
        self.assertTrue(self.ledger.remove_watch("600000"))
        self.assertEqual(self.ledger.list_watch(), [])
        self.assertFalse(self.ledger.remove_watch("600000"))

    def test_nothing_recorded_stays_empty(self):
        self.assertEqual(self.ledger.list_positions(), [])
        self.assertEqual(self.ledger.trades(), [])
        self.assertEqual(self.ledger.realized_pnl()["realized_pnl"], 0.0)


# ---------------------------------------------------------------- 提示引擎


def _quote(price, prev_close, name="测试股", limit_up=None, limit_down=None,
           chg=None, vr=None):
    from astock_data import Quote
    q = Quote(code="600000", venue="sh", name=name, price=price,
              prev_close=prev_close, limit_up=limit_up, limit_down=limit_down,
              change_pct=chg if chg is not None else
              (round((price - prev_close) / prev_close * 100, 2) if prev_close else None),
              volume_ratio=vr)
    return q.as_dict()


class TestHints(unittest.TestCase):
    def setUp(self):
        self.cfg = hints.HintsConfig()

    def test_stop_loss_triggered(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(9.0, 9.2), self.cfg)
        kinds = [h.kind for h in hs]
        self.assertIn("stop_loss", kinds)
        sl = next(h for h in hs if h.kind == "stop_loss")
        self.assertEqual(sl.shares, 1000)        # 止损建议清仓
        self.assertIn("%", sl.basis)              # 依据可核对（不是空话）

    def test_no_hint_when_within_range(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(10.2, 10.1), self.cfg)
        self.assertEqual([h.kind for h in hs], [])

    def test_take_profit_suggests_half(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(12.0, 11.9), self.cfg)
        tp = next(h for h in hs if h.kind == "take_profit")
        self.assertEqual(tp.shares, 500)         # 减半

    def test_take_profit_odd_shares_rounded_to_lot(self):
        pos = {"code": "600000", "name": "测试股", "shares": 150, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(12.0, 11.9), self.cfg)
        tp = next(h for h in hs if h.kind == "take_profit")
        # 150 股减半会拆出非法零股（5 股/50 股）——必须给合规数量
        self.assertIn(tp.shares, (100, 150))

    def test_take_profit_odd_shares_suggests_lot(self):
        pos = {"code": "600000", "name": "测试股", "shares": 300, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(12.0, 11.9), self.cfg)
        tp = next(h for h in hs if h.kind == "take_profit")
        self.assertEqual(tp.shares, 100)      # 300/2=150 → 向下取整到 100

    def test_limit_up_short_circuits(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(
            pos, _quote(11.0, 10.0, limit_up=11.0, limit_down=9.0, chg=10.0), self.cfg)
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0].kind, "limit_up")

    def test_limit_down_short_circuits(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(
            pos, _quote(9.0, 10.0, limit_up=11.0, limit_down=9.0, chg=-10.0), self.cfg)
        self.assertEqual(hs[0].kind, "limit_down")
        self.assertEqual(hs[0].severity, "urgent")

    def test_trailing_drawdown(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(10.6, 10.7), self.cfg,
                                        highest_price=12.0)
        self.assertIn("trailing", [h.kind for h in hs])

    def test_position_cap(self):
        pos = {"code": "600000", "name": "测试股", "shares": 5000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(10.1, 10.0), self.cfg,
                                        total_market_value=100000)
        cap = [h for h in hs if h.kind == "position_cap"]
        self.assertTrue(cap)
        # 5 万元 / 10 万元 = 50% > 25% 上限
        self.assertGreater(cap[0].meta["weight_pct"], 25)

    # ---------- 回归：单票占比的分母必须是账户总资产 ----------

    def test_position_cap_uses_total_equity_not_market_value(self):
        """真实报障：只持一只股时占比被算成 100%，正常仓位被误报「超上限」。

        场景：现金 11,423.07 + 格力持仓市值 3,658 → 总资产 15,081.07，
        占比 24.3%，在上限 25% 内。旧口径拿「持仓市值 3,658」当分母，
        占比恒为 100% → 每次都报「单票占比 100% 超上限」并建议减仓。
        """
        pos = {"code": "000651", "name": "格力电器", "shares": 100,
               "cost_price": 36.58}
        hs = hints.build_position_hints(
            pos, _quote(36.58, 36.20, name="格力电器"), self.cfg,
            total_market_value=3658.0, total_equity=15081.07)
        self.assertEqual([h for h in hs if h.kind == "position_cap"], [],
                         "24.3% 在上限 25% 内，不该报超限")

    def test_position_cap_reports_true_weight(self):
        """超限时给出的占比与减仓股数都按总资产口径算。"""
        pos = {"code": "000651", "name": "格力电器", "shares": 800,
               "cost_price": 36.58}
        hs = hints.build_position_hints(
            pos, _quote(36.58, 36.20, name="格力电器"), self.cfg,
            total_market_value=29264.0, total_equity=40687.07)
        cap = [h for h in hs if h.kind == "position_cap"]
        self.assertTrue(cap)
        # 29,264 / 40,687.07 = 71.9%
        self.assertAlmostEqual(cap[0].meta["weight_pct"], 71.92, places=1)
        # 减到 25%：目标市值 10,171.77 → 需减 19,092 / 36.58 ≈ 500 股（整手）
        self.assertEqual(cap[0].shares, 500)

    def test_position_cap_falls_back_to_market_value(self):
        """拿不到总资产（≤0）时退回持仓市值——等价于「全仓无现金」。"""
        pos = {"code": "600000", "name": "测试股", "shares": 5000,
               "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(10.1, 10.0), self.cfg,
                                        total_market_value=100000,
                                        total_equity=0)
        self.assertIn("position_cap", [h.kind for h in hs])

    def test_add_disabled_by_default(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(pos, _quote(9.6, 9.7), self.cfg)
        self.assertNotIn("add_position", [h.kind for h in hs])
        cfg2 = hints.HintsConfig(enable_add=True)
        # 成本 10、现价 9.4 → 浮亏 6%，落在第一档（每跌 5% 一档）
        hs2 = hints.build_position_hints(pos, _quote(9.4, 9.5), cfg2)
        self.assertIn("add_position", [h.kind for h in hs2])

    def test_t1_hint(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000}
        h = hints.build_t1_hint(pos, available=0)
        self.assertIsNotNone(h)
        self.assertIn("T+1", h.title + h.detail)
        self.assertIsNone(hints.build_t1_hint(pos, available=1000))


class TestSuggest(unittest.TestCase):
    def test_buy_respects_cash(self):
        est = hints.suggest_buy("600000", "测试股", 10.0, cash=10000,
                                cfg=hints.HintsConfig())
        self.assertLessEqual(est["suggest_amount"], 10000)
        self.assertEqual(est["suggest_shares"] % 100, 0)
        self.assertGreater(est["suggest_shares"], 0)

    def test_buy_respects_position_cap(self):
        est = hints.suggest_buy("600000", "测试股", 10.0, cash=1_000_000,
                                cfg=hints.HintsConfig(position_cap_pct=25.0),
                                total_market_value=300000)
        # 组合 130 万 × 25% = 32.5 万上限，远小于 100 万现金
        self.assertLessEqual(est["suggest_amount"], 325000)
        self.assertIn("单票占比", est["reason"])

    def test_buy_star_lot_200(self):
        est = hints.suggest_buy("688981", "科创板股", 50.0, cash=100000,
                                cfg=hints.HintsConfig())
        self.assertEqual(est["suggest_shares"] % 200, 0)

    def test_sell_all_and_half(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        all_in = hints.suggest_sell(pos, 12.0, available=1000, ratio=1.0)
        self.assertEqual(all_in["suggest_shares"], 1000)
        half = hints.suggest_sell(pos, 12.0, available=1000, ratio=0.5)
        self.assertEqual(half["suggest_shares"], 500)
        self.assertGreater(half["est_realized_pnl"], 0)

    def test_sell_limited_by_available(self):
        pos = {"code": "600000", "name": "测试股", "shares": 1000, "cost_price": 10.0}
        est = hints.suggest_sell(pos, 12.0, available=0, ratio=1.0)
        self.assertEqual(est["sellable"], 0)
        self.assertNotIn("suggest_shares", est)

    def test_sell_no_position(self):
        with self.assertRaises(ValueError):
            hints.suggest_sell({"code": "600000", "shares": 0}, 10.0, available=0)


# ---------------------------------------------------------------- 插件工具面


class TestPluginSurface(unittest.TestCase):
    def setUp(self):
        self.plugin = astock.AstockPlugin()

    def test_tool_names_match_manifest(self):
        names = {t.name for t in self.plugin.get_tools()}
        self.assertEqual(names, {
            "astock_quote", "astock_kline", "astock_search", "astock_screen",
            "astock_flow", "astock_record", "astock_portfolio", "astock_hint",
            "astock_sentiment", "astock_events", "astock_sync", "astock_watch",
            "astock_pick", "astock_backtest", "astock_archive", "astock_act",
        })

    def test_command_names_are_prefixed(self):
        """斜杠命令必须带 astock- 前缀。

        斜杠命令是宿主全局命名空间，裸名（如 events）易与内置/其他插件的
        命令撞车——前缀化即归属声明（v1.7.0 起）。
        """
        cmds = self.plugin.contributes["commands"]
        self.assertTrue(cmds, "命令声明为空")
        for c in cmds:
            self.assertTrue(c["name"].startswith("astock-"),
                            f"命令未加前缀：{c['name']}")
            self.assertTrue(c.get("description"), c["name"])

    def test_all_tools_have_schema(self):
        for t in self.plugin.get_tools():
            self.assertEqual(t.parameters.get("type"), "object", t.name)
            self.assertIn("properties", t.parameters, t.name)
            self.assertTrue(t.description, t.name)

    def test_unknown_tool_reports_error(self):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(self.plugin.execute("astock_nope", {}))
        finally:
            loop.close()
        self.assertIn("未知工具", out)

    def test_no_order_capability(self):
        """红线回归：工具面不得出现下单/委托类能力（用户自己在券商下单）。"""
        names = [t.name for t in self.plugin.get_tools()]
        for bad in ("order", "trade_submit", "buy", "sell", "cancel"):
            for n in names:
                self.assertNotIn(bad, n.replace("astock_", "").split("_")[0],
                                 f"{n} 疑似含下单能力")

    def test_watch_source_metadata(self):
        w = astock.AStockWatch()
        self.assertEqual(w.source_kind, "astock")
        self.assertTrue(w.display_name)
        self.assertTrue(w.config_schema.get("codes"))
        self.assertIn("stop_loss", w.DEFAULT_KINDS)
        # 旧宿主降级时 describe() 由真实 WatchSource 基类提供，这里只校验自身字段
        if hasattr(w, "describe"):
            self.assertEqual(w.describe()["name"], "astock-watch")


class TestWatchControl(unittest.TestCase):
    """盯盘开关：设置页按钮 + 工具自带 start/stop + 面板状态行。"""

    def setUp(self):
        import tempfile, datetime as dt
        self.tmp = tempfile.mkdtemp(prefix="astock-watchctl-")
        self.path = os.path.join(self.tmp, "l.sqlite")
        lg = ledger_mod(self.path)
        yday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        lg.record_trade("600000", "buy", 10.0, 1000, trade_date=yday, name="浦发银行")
        self.saved = {}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self, watch=True):
        saved = self.saved

        class App:
            workspace = self.tmp
            config = {"astock-plugin": {"ledger_path": self.path,
                                        "watch_enabled": watch,
                                        "watch_only_trading_hours": False}}

            def save_config(self, upd):
                for k, v in upd.items():
                    App.config.setdefault(k, {}).update(v)
                    saved.update(v)
        return App()

    def test_settings_action_toggles(self):
        pl = astock.AstockPlugin()
        app = self._app(watch=True)
        pl._app = app
        res = pl.settings_action("watch_toggle", app.config)
        self.assertTrue(res["ok"])
        self.assertFalse(res["watch_enabled"])
        self.assertIn("已暂停", res["message"])
        # 再点一次恢复
        res2 = pl.settings_action("watch_toggle", app.config)
        self.assertTrue(res2["watch_enabled"])
        self.assertIn("已启动", res2["message"])

    def test_settings_action_rejects_unknown(self):
        pl = astock.AstockPlugin()
        app = self._app()
        pl._app = app
        res = pl.settings_action("nope", app.config)
        self.assertFalse(res["ok"])

    def test_start_message_mentions_host_plan(self):
        """插件无权建宿主计划，必须如实告知用户还要建一次。"""
        pl = astock.AstockPlugin()
        app = self._app(watch=False)
        pl._app = app
        res = pl.settings_action("watch_toggle", app.config)
        self.assertIn("盯梢", res["message"])

    def test_panel_shows_watch_state(self):
        pl = astock.AstockPlugin()
        pl._app = self._app(watch=False)
        pl._snapshot = lambda **kw: {"data": {
            "positions": [], "quotes": {}, "total_market_value": 0.0,
            "equity": 0.0, "cash": 0.0, "float_pnl": 0.0,
            "today_pnl": 0.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        self.assertIn("盯盘已暂停", out)
        self.assertIn("设置", out)      # 告诉用户去哪儿点

    def test_tool_watch_start_stop(self):
        import asyncio
        pl = astock.AstockPlugin()
        app = self._app(watch=True)
        pl._app = app
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(
                pl.execute("astock_watch", {"action": "stop"}))
            self.assertIn("已暂停", out)
            self.assertFalse(app.config["astock-plugin"]["watch_enabled"])
            out2 = loop.run_until_complete(
                pl.execute("astock_watch", {"action": "status"}))
            self.assertIn("盯盘状态", out2)
        finally:
            loop.close()

    def test_probe_respects_disabled_switch(self):
        """暂停后探针直接返回空快照（不唤醒 Agent）。"""
        import asyncio
        w = astock.AStockWatch()
        ctx = types.SimpleNamespace(
            config={"astock-plugin": {"ledger_path": self.path,
                                      "watch_enabled": False}},
            workspace=self.tmp)
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(w.probe({}, ctx))
        finally:
            loop.close()
        self.assertEqual(res.snapshot, "")
        self.assertIn("暂停", res.summary)

    def test_probe_respects_trading_hours_gate(self):
        """开启时段自律时，非交易时段探针不产生信号。

        **必须同时屏蔽盘后报告**：`should_watch_now` 返回"已收盘"时，
        15:05–16:00 窗口内探针会先尝试产出【盘后舆情报告】（那是另一个
        功能）。本测试验证的是"时段闸门"，不是报告；不屏蔽会让它在
        收盘后那段时间里偶发失败（真实踩到过：CI 上午绿、傍晚红）。
        """
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        w = astock.AStockWatch()
        ctx = types.SimpleNamespace(
            config={"astock-plugin": {"ledger_path": self.path,
                                      "watch_enabled": True,
                                      "watch_only_trading_hours": True}},
            workspace=self.tmp)

        async def _no_eod(*a, **k):
            return None

        with patch.object(mkt, "should_watch_now", return_value=(False, "已收盘")), \
                patch.object(w, "_maybe_eod_report", _no_eod):
            loop = asyncio.new_event_loop()
            try:
                res = loop.run_until_complete(w.probe({}, ctx))
            finally:
                loop.close()
        self.assertEqual(res.snapshot, "")
        self.assertIn("不盯盘", res.summary)


class TestPriceRulesInProbe(unittest.TestCase):
    """精确价位规则接入 probe()（v2.1.4，并入原 ruthless-watch）。

    覆盖迁移后最容易出错的两点：
    ① 规则写在**设置页**也要生效（早先只读计划级 cfg，导致整条链路静默无信号）；
    ② `once_per_day` 必须真的去重——价位条件持续满足，不去重就是每 5 分钟
       一封，很快撞上限流上限、然后一整天不再报新信号。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="astock-prule-")
        self.ledger_path = os.path.join(self.tmp, "ledger.sqlite")
        lg = ledger_mod(self.ledger_path)
        import datetime as dt
        yday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        lg.record_trade("000651", "buy", 38.0, 1000, trade_date=yday, name="格力电器")
        self.watch = astock.AStockWatch()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, extra):
        cfg = {"ledger_path": self.ledger_path, "watch_enabled": True,
               "watch_only_trading_hours": False}
        cfg.update(extra)
        return types.SimpleNamespace(
            config={"astock-plugin": cfg}, workspace=self.tmp)

    def _run(self, price=35.5, extra=None, cfg=None):
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        from astock_data import Quote
        q = Quote(code="000651", venue="sz", name="格力电器", price=price,
                  prev_close=36.2, change_pct=-1.93, volume_ratio=1.3)

        def fake(items, use_cache=True):
            return [q]
        with patch.object(d, "fetch_quotes", fake):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    self.watch.probe(cfg or {}, self._ctx(extra or {})))
            finally:
                loop.close()

    RULES = [{"code": "000651", "name": "格力电器", "kind": "stop_loss",
              "price": 36.0, "op": "lt", "label": "跌破前低 36.00 清仓",
              "severity": "urgent"}]

    def test_rule_from_settings_page(self):
        """规则写在设置页（own）也要生效——真实踩过的静默失效。"""
        res = self._run(price=35.5, extra={"rules": self.RULES})
        self.assertIn("000651:stop_loss:36", res.snapshot)
        self.assertIn("跌破前低", res.changed_hint)

    def test_rule_from_plan_config(self):
        """规则写在计划级 probe_config（cfg）优先。"""
        res = self._run(price=35.5, cfg={"rules": self.RULES})
        self.assertIn("000651:stop_loss:36", res.snapshot)

    def test_once_per_day_dedup(self):
        res1 = self._run(price=35.5, extra={"rules": self.RULES})
        self.assertIn("stop_loss", res1.snapshot)
        res2 = self._run(price=35.5, extra={"rules": self.RULES})
        self.assertEqual(res2.snapshot, "", "同一天第二次不该再报")

    def test_not_triggered_when_price_above(self):
        res = self._run(price=37.0, extra={"rules": self.RULES})
        self.assertNotIn("stop_loss", res.snapshot)

    def test_bad_rules_do_not_crash_probe(self):
        """坏配置不能让探针报错停摆（那会连其他持仓的信号一起丢）。"""
        res = self._run(price=35.5, extra={"rules": [
            {"price": 10}, "垃圾", {"code": "000651", "price": "abc"}]})
        self.assertTrue(res.ok)
        self.assertIn("价位规则", res.summary)

    def test_rule_note_in_summary(self):
        res = self._run(price=35.5, extra={"rules": self.RULES})
        self.assertIn("价位规则 1 条", res.summary)
        self.assertIn("命中 1 条", res.summary)


class TestWatchWindow(unittest.TestCase):
    """“什么时候开始盯”：交易时段判断（这是省 token 的关键闸门）。"""

    def _at(self, y, mo, d, h, mi):
        import datetime as dt
        return dt.datetime(y, mo, d, h, mi)

    def test_weekend_never_watches(self):
        ok, why = mkt.should_watch_now(self._at(2026, 10, 24, 10, 30))   # 周六
        self.assertFalse(ok)
        self.assertIn("周末", why)

    def test_before_open_not_watched(self):
        ok, _ = mkt.should_watch_now(self._at(2026, 10, 23, 9, 5))
        self.assertFalse(ok)

    def test_auction_and_continuous_sessions(self):
        for h, m in ((9, 15), (9, 26), (10, 30), (14, 58)):
            ok, _ = mkt.should_watch_now(self._at(2026, 10, 23, h, m))
            self.assertTrue(ok, f"{h}:{m} 应盯盘")

    def test_lunch_break_skipped(self):
        ok, why = mkt.should_watch_now(self._at(2026, 10, 23, 12, 0))
        self.assertFalse(ok)
        self.assertIn("午间", why)

    def test_after_close_skipped(self):
        for h, m in ((15, 0), (15, 3), (20, 0)):
            ok, why = mkt.should_watch_now(self._at(2026, 10, 23, h, m))
            self.assertFalse(ok)
            self.assertIn("已收盘", why)

    def test_next_window_text(self):
        self.assertIn("今天 13:00",
                      mkt.next_watch_window(self._at(2026, 10, 23, 12, 0)))
        # 周五盘后 → 下周一
        self.assertIn("2026-10-26",
                      mkt.next_watch_window(self._at(2026, 10, 23, 16, 0)))


class TestReceiptParse(unittest.TestCase):
    """成交回单解析：这是“买完怎么同步回系统”的入口，宁可少记也不能记错。"""

    def test_csv_with_header(self):
        import astock_receipt as r
        txt = ("证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额,手续费,成交日期\n"
               "600000,浦发银行,买入,9.53,2000,19060.00,5.10,2026-10-24")
        res = r.parse(txt)
        self.assertEqual(res["method"], "csv")
        t = res["usable"][0]
        self.assertEqual((t.code, t.side, t.price, t.shares), ("600000", "buy", 9.53, 2000))
        self.assertAlmostEqual(t.fee, 5.10)
        self.assertEqual(t.trade_date, "2026-10-24")

    def test_line_format_price_vs_amount(self):
        """陷阱：`9.53 2000 19060 5.10` 里 5.10 是费用、19060 是金额，
        不能把它们当成价格/股数（早期实现就错在这里）。"""
        import astock_receipt as r
        res = r.parse("600000 浦发银行 买入 9.53 2000 19060 5.10 2026-10-24")
        t = res["usable"][0]
        self.assertEqual(t.price, 9.53)
        self.assertEqual(t.shares, 2000)

    def test_line_format_sell(self):
        import astock_receipt as r
        res = r.parse("000001 平安银行 卖出 11.20 500 5600.00 5.00")
        t = res["usable"][0]
        self.assertEqual(t.side, "sell")
        self.assertEqual(t.price, 11.20)
        self.assertEqual(t.shares, 500)

    def test_fullwidth_and_thousand_sep(self):
        import astock_receipt as r
        txt = "证券代码\t证券名称\t买卖\t成交价\t数量\n600000\t浦发银行\t买入\t9.53\t2,000"
        t = r.parse(txt)["usable"][0]
        self.assertEqual(t.shares, 2000)

    def test_duplicate_rows_removed(self):
        import astock_receipt as r
        line = "600000 买入 9.53 2000 2026-10-24"
        res = r.parse(line + "\n" + line)
        self.assertEqual(len(res["trades"]), 1)
        self.assertEqual(res["duplicates_removed"], 1)

    def test_unparsed_reported_not_guessed(self):
        """认不出的行必须显式列出——静默丢弃会少记一笔，对账全乱。"""
        import astock_receipt as r
        res = r.parse("600000 买入 9.53\n"          # 缺数量
                      "000001 卖出 11.20 500 2026-10-24")
        self.assertTrue(res["unparsed"])
        self.assertEqual(len(res["usable"]), 1)

    def test_amount_mismatch_flagged(self):
        import astock_receipt as r
        txt = ("证券代码,买卖标志,成交价格,成交数量,成交金额\n"
               "600000,买入,9.53,2000,99999.00")
        res = r.parse(txt)
        bad = [t for t in res["trades"] if not t.ok]
        self.assertTrue(bad)
        self.assertIn("金额校验不符", bad[0].problems[0])

    def test_empty_input(self):
        import astock_receipt as r
        self.assertEqual(r.parse("")["trades"], [])


class TestConditionalOrders(unittest.TestCase):
    """条件单参数：没时间盯盘的人靠这个替代盯盘，算错就是亏钱。"""

    def test_stop_loss_avoids_round_number(self):
        # 10 × (1-10%) = 9.00 → 必须挪开整数关口
        p = orders_mod.stop_loss_price(10.0, 10, "600000")
        self.assertNotEqual(p, 9.00)
        self.assertLess(p, 9.00)

    def test_take_profit_avoids_round_number(self):
        p = orders_mod.take_profit_price(10.0, 10, "600000")
        self.assertNotEqual(p, 11.00)
        self.assertGreater(p, 11.00)

    def test_stop_loss_respects_limit_down(self):
        """止损百分比过大时不能低于跌停价（挂上去当天也成交不了）。"""
        from astock_market import limit_prices
        _up, down, _ = limit_prices(10.0, "600000")
        p = orders_mod.stop_loss_price(10.0, 25, "600000")
        self.assertGreater(p, down)

    def test_take_profit_not_clipped_by_limit_up(self):
        """止盈目标价不按涨停裁剪（涨停只约束当日成交，不约束目标价）。"""
        from astock_market import limit_prices
        up, _d, _ = limit_prices(10.0, "600000")
        p = orders_mod.take_profit_price(10.0, 30, "600000")
        # 13.00 是整数关口 → 避让成 13.01（不是精度问题）
        self.assertAlmostEqual(p, 13.01, places=2)
        self.assertGreater(p, up)          # 高于当日涨停是正常的
        # 但必须被告知"今日不可达"
        ok, why = orders_mod.reachable_today(p, 10.0, "600000")
        self.assertFalse(ok)
        self.assertIn("今日不可达", why)

    def test_buy_order_whole_lots_and_fees(self):
        o = orders_mod.build_buy_order("600000", "浦发银行", 9.53, 50000, 8, 15)
        self.assertEqual(o.shares % 100, 0)
        self.assertGreater(o.shares, 0)
        self.assertLessEqual(o.est_amount, 50000)
        self.assertGreater(o.est_fee, 0)
        self.assertIsNotNone(o.stop_loss)
        self.assertIsNotNone(o.take_profit)

    def test_buy_insufficient_cash(self):
        o = orders_mod.build_buy_order("688256", "寒武纪", 932.2, 5000, 8, 15)
        self.assertEqual(o.shares, 0)
        self.assertTrue(any("资金不足" in n for n in o.notes))

    def test_sell_order_skips_already_breached_take_profit(self):
        """已涨过止盈位时不能再给止盈价——否则条件单会立即成交，
        用户以为是“设置止盈”实际是“立即卖出”（真实踩过的坑）。"""
        o2 = orders_mod.build_sell_order("000001", "平安银行", 12.0, 500, 10.0, 8, 15)
        self.assertIsNone(o2.take_profit)
        self.assertTrue(any("已涨过止盈位" in n for n in o2.notes))

    def test_sell_order_keeps_valid_take_profit(self):
        """成本 10、止盈 15% → 目标 11.50；现价 11.2 尚未到，必须保留。

        注意：11.50 高于当日涨停（11.0×1.1=12.1 时无影响；这里按现价 11.2
        算涨停 12.32 可达），所以不该被裁掉——涨停只约束当日成交价，
        不约束"目标价"这个数字（早期实现把目标价裁到涨停附近导致
        止盈价 < 现价，条件单一挂就成交）。"""
        o = orders_mod.build_sell_order("000001", "平安银行", 11.20, 500, 10.0, 8, 15)
        self.assertIsNotNone(o.take_profit)
        self.assertAlmostEqual(o.take_profit, 11.50, places=2)
        self.assertGreater(o.take_profit, 11.20)

    def test_sell_order_skips_breached_stop_loss(self):
        o = orders_mod.build_sell_order("600000", "浦发银行", 8.5, 1000, 10.0, 8, 15)
        self.assertIsNone(o.stop_loss)
        self.assertTrue(any("已高于现价" in n for n in o.notes))

    def test_order_block_is_copyable(self):
        o = orders_mod.build_buy_order("600000", "浦发银行", 9.53, 50000, 8, 15)
        block = o.order_block()
        self.assertIn("浦发银行", block)
        self.assertIn("止损触发价", block)
        self.assertIn("止盈触发价", block)


class TestPlanStore(unittest.TestCase):
    """建议计划：让成交回单能匹配回当初的建议。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="astock-plan-")
        self.ledger = ledger_mod(os.path.join(self.tmp, "l.sqlite"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_get(self):
        p = self.ledger.save_plan({"code": "600000", "name": "浦发银行",
                                  "side": "buy", "price": 9.53, "shares": 2000,
                                  "stop_loss": 8.77, "take_profit": 10.96})
        self.assertTrue(p["plan_id"].startswith("P"))
        got = self.ledger.get_plan(p["plan_id"])
        self.assertEqual(got["stop_loss"], 8.77)
        self.assertEqual(got["status"], "pending")

    def test_incrementing_ids(self):
        a = self.ledger.save_plan({"code": "600000", "side": "buy"})
        b = self.ledger.save_plan({"code": "000001", "side": "buy"})
        self.assertNotEqual(a["plan_id"], b["plan_id"])

    def test_match_only_same_code_and_side(self):
        p = self.ledger.save_plan({"code": "600000", "side": "buy", "price": 9.5})
        self.assertEqual(self.ledger.match_plan("600000", "buy")["plan_id"],
                         p["plan_id"])
        self.assertIsNone(self.ledger.match_plan("600000", "sell"))
        self.assertIsNone(self.ledger.match_plan("000001", "buy"))

    def test_filled_plan_not_rematched(self):
        p = self.ledger.save_plan({"code": "600000", "side": "buy"})
        self.ledger.mark_plan(p["plan_id"], "filled")
        self.assertIsNone(self.ledger.match_plan("600000", "buy"))

    def test_bad_status_rejected(self):
        p = self.ledger.save_plan({"code": "600000", "side": "buy"})
        with self.assertRaises(Exception):
            self.ledger.mark_plan(p["plan_id"], "whatever")

    def test_bad_side_rejected(self):
        with self.assertRaises(Exception):
            self.ledger.save_plan({"code": "600000", "side": "short"})


class TestWatchOptimizations(unittest.TestCase):
    """盯梢的炒股场景优化：分级 / 条件单去重 / A 股时段信号。"""

    def setUp(self):
        import tempfile, datetime as dt
        self.tmp = tempfile.mkdtemp(prefix="astock-wopt-")
        self.path = os.path.join(self.tmp, "l.sqlite")
        self.ledger = ledger_mod(self.path)
        yday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        self.ledger.record_trade("600000", "buy", 10.0, 1000,
                                 trade_date=yday, name="浦发银行")
        self.watch = astock.AStockWatch()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, extra=None):
        cfg = {"ledger_path": self.path, "watch_enabled": True,
               "watch_only_trading_hours": False}
        if extra:
            cfg.update(extra)
        return types.SimpleNamespace(
            config={"astock-plugin": cfg}, workspace=self.tmp)

    def _quote(self, price=8.8, prev=9.9, open_=9.0, chg=None):
        from astock_data import Quote
        return Quote(code="600000", venue="sh", name="浦发银行", price=price,
                     prev_close=prev, open=open_,
                     limit_up=round(prev * 1.1, 2), limit_down=round(prev * 0.9, 2),
                     change_pct=chg if chg is not None else
                     round((price - prev) / prev * 100, 2))

    def _probe(self, quotes, extra=None):
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        with patch.object(d, "fetch_quotes", lambda items, use_cache=True: quotes):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self.watch.probe({}, self._ctx(extra)))
            finally:
                loop.close()

    # ---------- 条件单去重：已挂的止损不该再催 ----------

    def test_stop_loss_reported_without_plan(self):
        res = self._probe([self._quote()])
        self.assertIn("stop_loss", res.snapshot)

    def test_stop_loss_silenced_when_plan_has_stop(self):
        """挂了止损条件单后，券商会自动执行，插件再喊等于重复催办。"""
        self.ledger.save_plan({"code": "600000", "side": "sell", "price": 9.5,
                               "shares": 1000, "stop_loss": 9.2})
        res = self._probe([self._quote()])
        self.assertNotIn("stop_loss", res.snapshot)

    def test_take_profit_silenced_when_plan_has_tp(self):
        self.ledger.save_plan({"code": "600000", "side": "sell", "price": 9.5,
                               "shares": 1000, "take_profit": 11.5})
        res = self._probe([self._quote(price=11.6, prev=11.5, open_=11.5)])
        self.assertNotIn("take_profit", res.snapshot)

    def test_plan_for_other_code_does_not_silence(self):
        self.ledger.save_plan({"code": "000001", "side": "sell", "price": 10,
                               "shares": 100, "stop_loss": 9.0})
        res = self._probe([self._quote()])
        self.assertIn("stop_loss", res.snapshot)

    # ---------- 分级过滤 ----------

    def test_min_level_urgent_filters_action(self):
        """urgent 档只报紧急信号（止损），普通异动被过滤。"""
        res = self._probe([self._quote()], extra={"min_level": "urgent"})
        self.assertIn("stop_loss", res.snapshot)
        self.assertNotIn("event", res.snapshot)

    def test_min_level_info_reports_all(self):
        res = self._probe([self._quote()], extra={"min_level": "info"})
        self.assertIn("stop_loss", res.snapshot)
        self.assertIn("event", res.snapshot)

    # ---------- A 股时段信号 ----------

    def test_auction_signal_only_in_window(self):
        from unittest.mock import patch
        import datetime as dt
        with patch.object(mkt, "now_cn",
                          return_value=dt.datetime(2026, 10, 23, 9, 20)):
            res = self._probe([self._quote(price=10.1, prev=9.9)])
        self.assertIn("auction", res.snapshot)

    def test_auction_not_reported_outside_window(self):
        from unittest.mock import patch
        import datetime as dt
        with patch.object(mkt, "now_cn",
                          return_value=dt.datetime(2026, 10, 23, 11, 0)):
            res = self._probe([self._quote(price=10.1, prev=9.9)])
        self.assertNotIn("auction", res.snapshot)

    def test_tail_signal_has_upper_bound(self):
        """尾盘信号不能越过 15:00——否则收盘后、深夜还报"尾盘异动"（踩过）。"""
        from unittest.mock import patch
        import datetime as dt
        cases = [(dt.datetime(2026, 10, 23, 14, 40), True),
                 (dt.datetime(2026, 10, 23, 14, 59), True),
                 (dt.datetime(2026, 10, 23, 15, 5), False),
                 (dt.datetime(2026, 10, 23, 20, 0), False)]
        for when, expect in cases:
            with patch.object(mkt, "now_cn", return_value=when):
                res = self._probe([self._quote(price=8.8, prev=9.9)])
            self.assertEqual("tail" in res.snapshot, expect, f"{when} 期望 {expect}")

    def test_gap_signal_detected_at_open(self):
        from unittest.mock import patch
        import datetime as dt
        with patch.object(mkt, "now_cn",
                          return_value=dt.datetime(2026, 10, 23, 9, 40)):
            res = self._probe([self._quote(price=9.0, prev=9.9, open_=9.0)])
        self.assertIn("gap", res.snapshot)

    def test_kinds_filter_excludes_session_signal(self):
        from unittest.mock import patch
        import datetime as dt
        with patch.object(mkt, "now_cn",
                          return_value=dt.datetime(2026, 10, 23, 9, 20)):
            res = self._probe([self._quote(price=10.1, prev=9.9)],
                              extra={"kinds": "stop_loss"})
        self.assertNotIn("auction", res.snapshot)

    # ---------- 指纹稳定性 ----------

    def test_same_signals_same_fingerprint(self):
        a = self._probe([self._quote()])
        b = self._probe([self._quote()])
        self.assertEqual(a.fingerprint(), b.fingerprint())


class TestPanelRender(unittest.TestCase):

    def setUp(self):
        import tempfile
        import datetime as dt
        self.tmp = tempfile.mkdtemp(prefix="astock-panel-")
        self.path = os.path.join(self.tmp, "ledger.sqlite")
        lg = ledger_mod(self.path)
        yday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        lg.record_trade("600000", "buy", 10.0, 1000, trade_date=yday, name="浦发银行")
        lg.set_cash(30000)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plugin(self, style="markdown"):
        pl = astock.AstockPlugin()
        pl._app = types.SimpleNamespace(
            workspace=self.tmp,
            config={"astock-plugin": {"panel_style": style}})
        return pl

    def _snapshot_stub(self, pl):
        """替换 _snapshot，避免联网（面板逻辑才是测试目标）。"""
        quote = {"price": 9.53, "prev_close": 9.62, "change_pct": -0.94,
                 "low": 9.40, "high": 9.80,
                 "limit_up": 10.58, "limit_down": 8.66}
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.005, "available_today": 1000, "board": "main",
                "cost_amount": 10005.0, "opened_at": "2026-10-20",
                "updated_at": "2026-10-20", "note": ""}]
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": quote}, "watch_quotes": {},
            "watch_quotes": {},
            "total_market_value": 9530.0, "equity": 39530.0, "cash": 30000.0,
            "float_pnl": -475.1, "today_pnl": -90.0, "realized_pnl": 0.0}}
        return pl

    def test_unknown_panel_returns_empty(self):
        self.assertEqual(self._plugin().panel_content("nope", {}), "")

    def test_markdown_is_richbox_kpi_and_progress(self):
        """markdown 形态：富围栏版——头部 ```kpi 指标卡 + 每股 ```progress 条。

        宿主面板（lite-work 2.0.4+）已支持富围栏渲染；面板输出从纯文本短行
        升级为「KPI 卡 + 进度条」结构。旧宿主不识别时围栏降级为代码块，
        数字仍在 JSON 里可读。
        """
        import json as _json
        pl = self._snapshot_stub(self._plugin("markdown"))
        out = pl.panel_content("portfolio", {})
        self.assertIn("浦发银行", out)
        self.assertNotIn("lite-tree", out)
        self.assertNotIn("| 名称 | 股数 |", out)
        # kpi 围栏：可解析、4 张卡
        self.assertIn("```kpi", out)
        kpi_json = out.split("```kpi\n", 1)[1].split("\n```", 1)[0]
        kpi = _json.loads(kpi_json)
        self.assertEqual(len(kpi["items"]), 4)
        labels = [it["label"] for it in kpi["items"]]
        self.assertIn("总资产", labels)
        self.assertIn("可用资金", labels)
        # progress 围栏：仓位空间 + 距止损余量两条（v1.7.2 改为「安全余量」语义）
        self.assertIn("```progress", out)
        pr_json = out.split("```progress\n", 1)[1].split("\n```", 1)[0]
        pr = _json.loads(pr_json)
        plabels = [it["label"] for it in pr["items"]]
        self.assertTrue(any("仓位空间" in x for x in plabels))
        self.assertTrue(any("距止损余量" in x for x in plabels))
        # 数值在 0-100 区间
        for it in pr["items"]:
            self.assertGreaterEqual(it["value"], 0)
            self.assertLessEqual(it["value"], 100)

    def test_tree_is_valid_litetree_json(self):
        import json
        pl = self._snapshot_stub(self._plugin("tree"))
        out = pl.panel_content("portfolio", {})
        data = json.loads(out)               # 必须是合法 JSON，否则前端降级成代码块
        self.assertEqual(data["type"], "lite-tree")
        self.assertEqual(data["total"], 1)
        card = data["trees"][0]
        self.assertIn("浦发银行", card["title"])
        # 卡片必须带"当前建议动作"，且不伪造概率（单选项恒 1.0）
        # 第 1 条＝当前建议动作（prob 1.0 / chosen）；其余是可视化刻度（≤3 条）
        self.assertEqual(card["options"][0]["prob"], 1.0)
        self.assertTrue(card["options"][0]["chosen"])
        self.assertLessEqual(len(card["options"]), 3)
        self.assertGreaterEqual(len(card["options"]), 2)
        for extra in card["options"][1:]:
            self.assertFalse(extra["chosen"])
            self.assertGreaterEqual(extra["prob"], 0.0)
            self.assertLessEqual(extra["prob"], 1.0)

    def test_tree_card_shows_triggered_rule(self):
        import json
        pl = self._plugin("tree")
        quote = {"price": 8.0, "prev_close": 9.5, "change_pct": -15.8,
                 "limit_up": 10.45, "limit_down": 8.55}
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.0, "available_today": 1000}]
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": quote}, "watch_quotes": {},
            "total_market_value": 8000.0, "equity": 38000.0, "cash": 30000.0,
            "float_pnl": -2000.0, "today_pnl": -1500.0, "realized_pnl": 0.0}}
        data = json.loads(pl.panel_content("portfolio", {}))
        card = data["trees"][0]
        # 浮亏 20% 触发止损 → 卡片应是 danger 且写明止损
        self.assertEqual(card["options"][0]["tone"], "danger")
        self.assertIn("止损", card["question"])

    def test_empty_portfolio_message(self):
        pl = self._plugin("tree")
        pl._snapshot = lambda **kw: {"data": {
            "positions": [], "quotes": {}, "total_market_value": 0.0,
            "equity": 0.0, "cash": 0.0, "float_pnl": 0.0,
            "today_pnl": 0.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        self.assertIn("空仓", out)

    def test_broken_ledger_degrades_gracefully(self):
        pl = self._plugin()
        pl._snapshot = lambda **kw: (_ for _ in ()).throw(RuntimeError("台账炸了"))
        out = pl.panel_content("portfolio", {})
        self.assertIn("台账读取失败", out)     # 不能把面板打空白或抛异常
        self.assertIn("台账炸了", out)

    def test_status_section_separated_from_data(self):
        """盯盘/舆情各自成块不粘连：分割线 + 小标题 + 块间空行。

        回归背景（真实报障两轮）：① 状态行直接 append 到末尾与自选表
        粘成一片；② v1.7.0 用自造的 `──` 文本当分隔线，markdown 渲染器
        不认，且盯盘/舆情之间只有单换行——渲染时三行全合并成一行。
        v1.7.1 改为标准 `---` 分割线 + **盯盘**/**舆情** 各自成块。
        """
        pl = self._snapshot_stub(self._plugin("markdown"))
        # 面板渲染已拆到 astock_panel（v2.1.2），所以打桩要打在模块函数上，
        # 不是插件实例的同名方法——后者已不存在，打桩会静默无效。
        import unittest.mock as _mock
        import astock_panel as _panel
        with _mock.patch.object(
                _panel, "render_sentiment_flag",
                lambda d: "🔴 **浦发银行 舆情背离**：研报偏多但融资余额连降"):
            out = pl.panel_content("portfolio", {})
        # 标准 markdown 分割线（不是自造 ──）
        self.assertIn("\n---\n", out)
        self.assertNotIn("──", out)
        # 两块各有小标题，且顺序：盯盘在前、舆情在后
        self.assertIn("**盯盘**", out)
        self.assertIn("**舆情**", out)
        self.assertLess(out.index("**盯盘**"), out.index("**舆情**"))
        # 块与块之间必须空行分隔（单换行会被渲染器合并成一行）
        m = __import__("re").search(r"\*\*盯盘\*\*\n\n(.+?)\n\n\*\*舆情\*\*", out, __import__("re").S)
        self.assertTrue(m, "盯盘块与舆情块之间未用空行分隔")
        # 数据区在分割线之前：数据在上、状态在下
        self.assertLess(out.index("浦发银行"), out.index("\n---\n"))

    def test_progress_is_safety_headroom_not_consumed(self):
        """进度条语义必须是「安全余量」而非「已消耗量」（v1.7.2）。

        回归背景（真实报障）：宿主 progress 色阶固定 <40 红 / ≥80 绿
        （为健康度设计）。此前喂「已消耗量」：浮亏 -8.06% 触到 8% 止损线
        → 100% 满条**绿色**——看着最健康实际最危险，语义反转。
        修复后：触线 = 0（红短条），安全 = 100（绿满条）。
        """
        import json as _json
        pl = self._plugin("markdown")
        quote = {"price": 8.0, "prev_close": 9.5, "change_pct": -15.8,
                 "limit_up": 10.45, "limit_down": 8.55}
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.0, "available_today": 1000}]
        # 注意：这里刻意用「全仓无现金」（现金 0、总资产=持仓市值 8000），
        # 这样 100% 占比在**新口径（占总资产）**下同样真超限。
        # 早先 fixture 写了 cash=30000/equity=38000，实际只占 21%，
        # 是旧 bug（拿持仓市值当分母）才让它显示 100% 超限——
        # 那种"靠 bug 制造超限"的 fixture 会把修复本身变成测试失败。
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": quote}, "watch_quotes": {},
            "total_market_value": 8000.0, "equity": 8000.0, "cash": 0.0,
            "float_pnl": -2000.0, "today_pnl": -1500.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        pr_json = out.split("```progress\n", 1)[1].split("\n```", 1)[0]
        pr = _json.loads(pr_json)
        stop = next(it for it in pr["items"] if "距止损余量" in it["label"])
        # 浮亏 20% 超过 8% 止损线 → 余量应为 0（红色短条），不是 100
        self.assertEqual(stop["value"], 0)
        # 仓位 100% 超过 25% 上限 → 空间应为 0
        room = next(it for it in pr["items"] if "仓位空间" in it["label"])
        self.assertEqual(room["value"], 0)
        self.assertIn("超限", room["note"])

    def test_progress_weight_uses_total_equity_not_market_value(self):
        """回归（真实报障）：持仓 8000 + 现金 30000 的账户，单票占 21%，
        不该被算成「100% 超限」。

        旧口径用持仓市值当分母 → 只持一只股时恒为 100%，仓位条永远画成
        红色「已超限」，并持续建议减仓（用户会做出多余的卖出操作）。
        """
        import json as _json
        pl = self._plugin("markdown")
        quote = {"price": 8.0, "prev_close": 9.5, "change_pct": -15.8,
                 "limit_up": 10.45, "limit_down": 8.55}
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.0, "available_today": 1000}]
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": quote}, "watch_quotes": {},
            "total_market_value": 8000.0, "equity": 38000.0, "cash": 30000.0,
            "float_pnl": -2000.0, "today_pnl": -1500.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        pr_json = out.split("```progress\n", 1)[1].split("\n```", 1)[0]
        pr = _json.loads(pr_json)
        room = next(it for it in pr["items"] if "仓位空间" in it["label"])
        # 8000 / 38000 = 21.05% → 余量 (25-21.05)/25 = 15.8%，未超限
        self.assertGreater(room["value"], 0)
        self.assertNotIn("超限", room["note"])
        self.assertIn("21.1%", room["note"])

    def test_watch_quotes_renders_table_when_ok(self):
        """自选行情正常时必须画出表格（防回归：v1.7.2 曾把表格代码掉进
        elif 分支，行情正常时只出标题不出表——真实报障「自选观察没有了」）。"""
        pl = self._plugin("markdown")
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.0, "available_today": 1000}]
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": {"price": 9.5, "change_pct": -1.2}},
            "watch_quotes": {"600036": {"name": "招商银行", "price": 42.5,
                                           "change_pct": 1.75, "low": 41.4, "high": 42.5}},
            "watch_count": 1,
            "total_market_value": 9500.0, "equity": 39500.0, "cash": 30000.0,
            "float_pnl": -500.0, "today_pnl": -100.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        self.assertIn("自选观察", out)
        self.assertIn("招商银行（600036）", out)
        self.assertIn("42.50", out)
        self.assertIn("| 名称 |", out)   # 表格本身必须在，不能只出标题

    def test_watch_fetch_failure_shows_notice_not_silence(self):
        """自选行情拉取失败不得静默消失（v1.7.2）。

        回归背景（真实报障）：拉挂时 `if wq:` 跳过整节，用户无从分辨
        「没自选」还是「拉挂了」。修复后 watch_count>0 且无行情时显式提示。
        """
        pl = self._plugin("markdown")
        pos = [{"code": "600000", "name": "浦发银行", "shares": 1000,
                "cost_price": 10.0, "available_today": 1000}]
        pl._snapshot = lambda **kw: {"data": {
            "positions": pos, "quotes": {"600000": {"price": 9.5, "change_pct": -1.2}},
            "watch_quotes": {}, "watch_count": 5,
            "total_market_value": 9500.0, "equity": 39500.0, "cash": 30000.0,
            "float_pnl": -500.0, "today_pnl": -100.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        self.assertIn("自选观察", out)
        self.assertIn("5 只行情未取到", out)
        self.assertIn("重试", out)

    def test_sentiment_failure_does_not_blank_panel(self):
        """舆情接口挂了，面板仍要显示持仓（舆情只是附加信息）。"""
        from unittest.mock import patch
        import astock_sentiment_data as sd
        pl = self._snapshot_stub(self._plugin("markdown"))
        with patch.object(sd, "collect", side_effect=RuntimeError("舆情挂了")):
            out = pl.panel_content("portfolio", {})
        self.assertIn("浦发银行", out)
        self.assertNotIn("舆情挂了", out)


class TestWatchProbe(unittest.TestCase):
    """盯梢源 probe()：此前从未验证过（漏测风险最高的一处）。

    全程离线：行情用内存替身替换 `astock_data.fetch_quotes`，台账写临时目录。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="astock-watch-")
        self.ledger_path = os.path.join(self.tmp, "ledger.sqlite")
        # 建一只持仓（昨日买入 → 可卖）
        lg = ledger_mod(self.ledger_path)
        import datetime as dt
        yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        lg.record_trade("600000", "buy", 10.0, 1000, trade_date=yday, name="浦发银行")
        lg.add_watch("688256", "寒武纪")
        self.watch = astock.AStockWatch()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, config=None):
        # watch_only_trading_hours=False：这些用例测的是**信号识别**，
        # 若受交易时段闸门影响，跑测试时（常常是夜间/周末）会全部变"无异动"。
        # 时段闸门本身由 TestWatchWindow 专门覆盖。
        cfg = {"ledger_path": self.ledger_path,
               "watch_enabled": True, "watch_only_trading_hours": False}
        if config:
            cfg.update((config.get("astock-plugin") or {}))
        return types.SimpleNamespace(
            config={"astock-plugin": cfg}, workspace=self.tmp)

    def _run(self, quotes, cfg=None, config=None):
        import asyncio
        from unittest.mock import patch
        import astock_data as d

        def fake_fetch(items, use_cache=True):
            return [q for q in quotes
                    if any(q.code == c for c, _v, _k in items)]
        with patch.object(d, "fetch_quotes", fake_fetch):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    self.watch.probe(cfg or {}, self._ctx(config)))
            finally:
                loop.close()

    def _quote(self, code, price, prev, limit_up=None, limit_down=None,
               chg=None):
        from astock_data import Quote
        return Quote(code=code, venue="sh", name="测试", price=price,
                     prev_close=prev, limit_up=limit_up, limit_down=limit_down,
                     change_pct=chg if chg is not None else
                     round((price - prev) / prev * 100, 2))

    def test_no_positions_reports_error(self):
        """空台账要明确报"先录入"，而不是静默 ok。"""
        import tempfile
        empty = os.path.join(tempfile.mkdtemp(), "e.sqlite")
        ledger_mod(empty)  # 建空库
        res = self._run([], config={"astock-plugin": {"ledger_path": empty}})
        self.assertFalse(res.ok)
        self.assertIn("没有持仓或自选股", res.error)

    def test_no_change_gives_empty_snapshot(self):
        """无异动时 snapshot 必须为空——否则每次探测都被判为"有变化"。"""
        res = self._run([self._quote("600000", 10.1, 10.0),
                         self._quote("688256", 100.0, 100.0)])
        self.assertTrue(res.ok)
        self.assertEqual(res.snapshot, "")
        self.assertIn("无异动", res.summary)

    def test_stop_loss_signal_detected(self):
        # 成本 10，现价 8.8 → 浮亏 12% 触发止损
        res = self._run([self._quote("600000", 8.8, 9.0)])
        self.assertTrue(res.ok)
        self.assertIn("600000:stop_loss", res.snapshot)
        self.assertIn("止损", res.changed_hint)

    def test_price_change_alone_is_not_a_signal(self):
        """关键回归：只有价格变、没触发规则 → 不得产生变化（否则高频误唤醒）。"""
        a = self._run([self._quote("600000", 10.05, 10.0)])
        b = self._run([self._quote("600000", 10.08, 10.0)])
        self.assertEqual(a.snapshot, b.snapshot)
        self.assertEqual(a.snapshot, "")

    def test_watchlist_limit_up_flagged(self):
        """自选股（无持仓）也要能报涨跌停。"""
        res = self._run([self._quote("688256", 11.0, 10.0, limit_up=11.0,
                                     limit_down=9.0, chg=10.0)])
        self.assertTrue(res.ok)
        self.assertIn("688256:limit_up", res.snapshot)

    def test_kinds_filter(self):
        res = self._run([self._quote("600000", 8.8, 9.0)],
                        cfg={"kinds": "limit_up"})
        self.assertEqual(res.snapshot, "")     # 只要涨停，止损被过滤

    def test_extra_codes_from_config(self):
        res = self._run([self._quote("000001", 12.0, 11.0, limit_up=12.1,
                                     limit_down=9.9, chg=9.09)],
                        cfg={"codes": "000001"})
        self.assertIn("000001", res.snapshot + res.summary)

    def test_quote_failure_returns_error_not_change(self):
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        with patch.object(d, "fetch_quotes",
                          side_effect=d.DataError("模拟网络故障")):
            loop = asyncio.new_event_loop()
            try:
                res = loop.run_until_complete(self.watch.probe({}, self._ctx()))
            finally:
                loop.close()
        self.assertFalse(res.ok)
        self.assertEqual(res.snapshot, "")
        self.assertIn("行情获取失败", res.error)

    def test_snapshot_is_stable_for_same_signals(self):
        """同一组信号多次探测 → 指纹必须一致（否则盯梢永远在触发）。"""
        q = [self._quote("600000", 8.8, 9.0)]
        a = self._run(q)
        b = self._run(q)
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_string_false_is_not_true(self):
        """config 里写字符串 "false" 不能被当成 True（bool() 陷阱）。"""
        self.assertFalse(astock._as_bool("false"))
        self.assertFalse(astock._as_bool("0"))
        self.assertFalse(astock._as_bool(""))
        self.assertFalse(astock._as_bool(None))
        self.assertTrue(astock._as_bool("true"))
        self.assertTrue(astock._as_bool(True))
        self.assertTrue(astock._as_bool(1))


class TestWatchOnlyHints(unittest.TestCase):
    """自选股（无持仓）的提示：只出与仓位无关的信号。"""

    def setUp(self):
        self.cfg = hints.HintsConfig()

    def test_limit_up_without_position(self):
        hs = hints.build_position_hints(
            {}, {"code": "600000", "name": "测试", "price": 11.0,
                 "prev_close": 10.0, "change_pct": 10.0,
                 "limit_up": 11.0, "limit_down": 9.0}, self.cfg)
        self.assertEqual([h.kind for h in hs], ["limit_up"])

    def test_no_pnl_hints_without_position(self):
        """无持仓不该出现止损/止盈/加仓/仓位占比（那些要有成本才有意义）。"""
        hs = hints.build_position_hints(
            {}, {"code": "600000", "name": "测试", "price": 11.0,
                 "prev_close": 10.0, "change_pct": 10.0,
                 "limit_up": 12.0, "limit_down": 9.0}, self.cfg)
        kinds = [h.kind for h in hs]
        for bad in ("stop_loss", "take_profit", "add_position", "reduce",
                    "position_cap", "trailing"):
            self.assertNotIn(bad, kinds)

    def test_drop_event_without_position(self):
        hs = hints.build_position_hints(
            {}, {"code": "600000", "name": "测试", "price": 9.0,
                 "prev_close": 10.0, "change_pct": -10.0,
                 "limit_up": 11.0, "limit_down": 9.0}, self.cfg)
        self.assertIn("event", [h.kind for h in hs])

    def test_holding_still_gets_all_hints(self):
        pos = {"code": "600000", "name": "测试", "shares": 1000, "cost_price": 10.0}
        hs = hints.build_position_hints(
            pos, {"code": "600000", "name": "测试", "price": 8.8,
                  "prev_close": 9.0, "change_pct": -2.2,
                  "limit_up": 9.9, "limit_down": 8.1}, self.cfg)
        self.assertIn("stop_loss", [h.kind for h in hs])


class TestSentiment(unittest.TestCase):

    @staticmethod
    def _ev(cat, title, direction="unknown", as_of="2026-10-20",
            reliability="hard_fact", meta=None):
        from astock_sentiment_data import Evidence
        return Evidence(category=cat, title=title, reliability=reliability,
                        direction=direction, as_of=as_of, source="test",
                        meta=meta or {})

    def test_direction_keywords(self):
        from astock_sentiment_data import judge_direction
        self.assertEqual(judge_direction("公司公告：拟增持股份"), "positive")
        self.assertEqual(judge_direction("收到证监会立案告知书"), "negative")
        self.assertEqual(judge_direction("关于召开股东大会的通知"), "unknown")
        # 正负同现 → neutral（保守，不硬判）
        self.assertEqual(judge_direction("拟增持但存在诉讼风险"), "neutral")

    def test_news_relevance(self):
        """标题为准：只在正文提到的公司名不算直接相关（踩过的坑）。"""
        from astock_sentiment_data import _news_relevance
        self.assertEqual(
            _news_relevance("浦发银行菏泽分行被罚30万", "", "浦发银行"), "direct")
        self.assertEqual(
            _news_relevance("银行行业资金流出榜：招商银行等8股", "浦发银行也在列",
                            "浦发银行"), "industry")
        self.assertEqual(
            _news_relevance("各大银行围猎iPhone", "提及浦发银行", "浦发银行"), "industry")
        self.assertEqual(
            _news_relevance("营收翻倍！688256遭减持", "", "寒武纪", "688256"), "direct")
        self.assertEqual(_news_relevance("无关的新闻", "", "浦发银行"), "weak")

    def test_unknown_dominates_category(self):
        """10 条公告里 1 条含负面词，不能总结成"公告整体偏负面"。"""
        from astock_sentiment import summarize
        items = [self._ev("announce", f"常规公告 {i}") for i in range(9)]
        items.append(self._ev("announce", "关于终止某合同的公告", "negative"))
        rep = summarize(items, "600000", "测试")
        ann = next(c for c in rep.categories if c.category == "announce")
        self.assertEqual(ann.direction, "unknown")
        self.assertEqual(ann.negative, 1)

    def test_expired_event_not_counted(self):
        """过期的事件型证据不进方向判断，但要留在 stale_items 里。"""
        from astock_sentiment import summarize
        items = [
            self._ev("lhb", "2026-04-30 上榜", "positive", as_of="2026-04-30"),
            self._ev("margin", "融资余额下降", "negative", as_of="2026-10-23"),
        ]
        rep = summarize(items, "600000", "测试")
        cats = {c.category for c in rep.categories}
        self.assertIn("margin", cats)
        self.assertNotIn("lhb", cats)      # 过期 → 不计入归集
        self.assertTrue(rep.stale_items)   # 但要说出来

    def test_conflict_report_vs_margin(self):
        from astock_sentiment import summarize
        items = [
            self._ev("report", "买入评级：业绩高增", "positive",
                     reliability="official_view"),
            self._ev("margin", "融资余额下降 5%", "negative",
                     meta={"trend": {"delta_pct": -5.0, "window": "a → b"}}),
            self._ev("lhb", "净买入", "positive"),
        ]
        rep = summarize(items, "600000", "测试")
        kinds = [c.kind for c in rep.conflicts]
        self.assertIn("report_vs_margin", kinds)
        high = [c for c in rep.conflicts if c.severity == "high"]
        self.assertTrue(high)
        self.assertEqual(rep.tone, "conflicted")
        self.assertIn("客观数据", rep.tone_text)

    def test_thin_evidence(self):
        from astock_sentiment import summarize
        rep = summarize([self._ev("news", "一条新闻", "positive",
                                  reliability="media")], "600000", "测试")
        self.assertEqual(rep.tone, "thin")
        self.assertIn("不足以形成舆情判断", rep.tone_text)

    def test_aligned_positive(self):
        from astock_sentiment import summarize
        items = [self._ev("report", f"买入评级 {i}", "positive",
                          reliability="official_view") for i in range(2)]
        items += [self._ev("news", f"业绩增长 {i}", "positive", reliability="media")
                  for i in range(3)]
        rep = summarize(items, "600000", "测试")
        self.assertEqual(rep.tone, "aligned_positive")
        self.assertGreater(rep.weight_score, 0)

    def test_hot_vs_flow_conflict(self):
        from astock_sentiment import summarize
        items = [
            self._ev("hot", "人气榜第 3 名", "positive",
                     reliability="crowd", meta={"rank": 3, "heat": 100000}),
            self._ev("margin", "融资余额下降", "negative",
                     meta={"trend": {"delta_pct": -3.0, "window": "a → b"}}),
        ]
        rep = summarize(items, "600000", "测试")
        self.assertIn("hot_vs_flow", [c.kind for c in rep.conflicts])

    def test_render_has_disclaimer_and_facts_first(self):
        from astock_sentiment import summarize
        import astock_sentiment_tool as tool
        items = [
            self._ev("announce", "季度报告", as_of="2026-10-20"),
            self._ev("report", "买入评级", "positive", reliability="official_view"),
            self._ev("margin", "融资余额下降", "negative",
                     meta={"trend": {"delta_pct": -5.0, "window": "a → b"}}),
        ]
        rep = summarize(items, "600000", "测试股", {"holder": "接口不可用"})
        text = tool.render(rep, items, {})
        self.assertIn("不构成投资建议", text)
        self.assertIn("信号背离", text)
        self.assertIn("数据质量", text)          # 缺数据必须显式列出
        self.assertIn("接口不可用", text)
        self.assertIn("【事实】", text)          # 每条证据带可靠度标签

    def test_brief_render_is_short(self):
        from astock_sentiment import summarize
        import astock_sentiment_tool as tool
        items = [self._ev("margin", "融资余额下降", "negative",
                          meta={"trend": {"delta_pct": -5.0, "window": "a → b"}}),
                 self._ev("report", "买入", "positive", reliability="official_view")]
        rep = summarize(items, "600000", "测试股")
        brief = tool.render_brief(rep)
        self.assertIn("测试股", brief)
        self.assertLess(len(brief), 200)

    def test_collect_never_raises_on_failure(self):
        """任一类接口失败都必须降级，而不是抛异常把工具打挂。"""
        import astock_sentiment_data as sd
        from unittest.mock import patch

        def boom(*a, **k):
            raise sd.DataError("模拟接口故障")
        with patch.object(sd, "_http_get_json", boom), \
             patch.object(sd, "_http_get", boom):
            got = sd.collect("600000", "浦发银行")
        self.assertIsInstance(got["items"], list)
        self.assertTrue(got["errors"])       # 失败要如实记录


class TestFormatting(unittest.TestCase):
    def test_number_formatting_handles_none(self):
        self.assertEqual(astock._f(None), "-")
        self.assertEqual(astock._money(None), "-")
        self.assertEqual(astock._money(2_500_000_000), "25.00亿")
        self.assertEqual(astock._money(35_000), "3.50万")

    def test_table_escapes_pipe(self):
        out = astock._table(["A"], [["x|y"]])
        self.assertIn("x\\|y", out)

    def test_ratio_has_no_sign(self):
        """换手率/占比不是涨跌幅——带 + 号会让人误读方向（踩过）。"""
        self.assertEqual(astock._ratio(10.66), "10.66%")
        self.assertEqual(astock._pct(10.66), "+10.66%")
        self.assertEqual(astock._ratio(None), "-")


class TestDataLayerOffline(unittest.TestCase):
    """数据层纯逻辑（不联网）：限速器、缓存、解析函数、相关性判定。"""

    def test_rate_limiter_bounds(self):
        from astock_data import RateLimiter
        rl = RateLimiter(rate_per_sec=1000, burst=2)
        self.assertTrue(rl.acquire())
        self.assertTrue(rl.acquire())

    def test_ttl_cache_expiry(self):
        from astock_data import TTLCache
        import time as _t
        c = TTLCache()
        c.set("k", "v")
        self.assertEqual(c.get("k", 10), "v")
        self.assertIsNone(c.get("k", 0.0))     # TTL=0 → 立刻过期
        c.set("k2", "v2")
        _t.sleep(0.02)
        self.assertIsNone(c.get("k2", 0.001))

    def test_parse_tencent_line(self):
        from astock_data import _parse_tencent_line
        fields = ["1", "浦发银行", "600000", "9.53", "9.62", "9.69", "755926",
                  "393482", "362445", "9.52"] + ["0"] * 20 + \
                 ["20261023161458", "-0.09", "-0.94", "9.73", "9.52",
                  "9.53/755926/726344396", "755926", "72634", "0.23", "6.20",
                  "", "9.73", "9.52", "2.18", "3174.05", "3174.05", "0.42",
                  "10.58", "8.66", "1.20"]
        q = _parse_tencent_line('v_sh600000="' + "~".join(fields) + '";', "sh")
        self.assertIsNotNone(q)
        self.assertEqual(q.code, "600000")
        self.assertEqual(q.name, "浦发银行")
        self.assertAlmostEqual(q.price, 9.53)
        self.assertAlmostEqual(q.change_pct, -0.94)
        self.assertAlmostEqual(q.limit_up, 10.58)
        self.assertAlmostEqual(q.limit_down, 8.66)
        self.assertEqual(q.trade_time, "2026-10-23 16:14:58")

    def test_parse_tencent_line_rejects_garbage(self):
        from astock_data import _parse_tencent_line
        self.assertIsNone(_parse_tencent_line("v_sh600000=\"\";", "sh"))
        self.assertIsNone(_parse_tencent_line("garbage", "sh"))

    def test_parse_sina_line(self):
        from astock_data import _parse_sina_line
        parts = ["浦发银行", "9.690", "9.620", "9.530", "9.730", "9.520"] + \
                ["9.520", "9.530", "75592632", "726344396.000"] + ["0"] * 20 + \
                ["2026-10-23", "15:00:00"]
        q = _parse_sina_line('var hq_str_sh600000="' + ",".join(parts) + '";',
                            "600000", "sh")
        self.assertIsNotNone(q)
        self.assertEqual(q.source, "sina")
        self.assertAlmostEqual(q.price, 9.53)
        self.assertAlmostEqual(q.change_pct, -0.94, places=2)

    def test_sina_screen_normalizes_mcap_unit(self):
        """新浪 mktcap 是万元，必须换算成元，否则市值过滤会差 1 万倍。"""
        from astock_data import _screen_via_sina
        import astock_data as d
        from unittest.mock import patch
        fake = ('[{"symbol":"sh600000","code":"600000","name":"浦发银行",'
                '"trade":"9.53","pricechange":"-0.09","changepercent":"-0.94",'
                '"open":"9.69","high":"9.73","low":"9.52","settlement":"9.62",'
                '"volume":"75592632","amount":"726344396","turnoverratio":"0.23",'
                '"per":"6.2","pb":"0.42","mktcap":"31740500","nmc":"30000000"}]')

        def fake_get(url, headers=None, encoding="utf-8", retries=2, backoff=0.6):
            return '"5576"' if "StockCount" in url else fake
        with patch.object(d, "_http_get", fake_get):
            rows, total = _screen_via_sina("all", "change_pct", False, 10,
                                           None, None, None, None, None, None,
                                           None, None, True)
        self.assertEqual(total, 5576)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["total_mcap_yuan"], 31740500 * 1e4)
        self.assertEqual(rows[0]["code"], "600000")

    def test_sina_flow_leaves_unknown_fields_none(self):
        """备源没有的字段必须是 None——不能塞 0（会让 Agent 讲成"零流入"）。"""
        import astock_data as d
        from unittest.mock import patch
        fake = ('[{"opendate":"2026-10-23","netamount":"30867750.62",'
                '"r0_net":"-39701734.84"}]')
        with patch.object(d, "_http_get",
                          lambda *a, **k: fake):
            rows = d._flow_via_sina("sh600000", 5)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["main_net"])
        self.assertIsNone(rows[0]["large_net"])
        self.assertEqual(rows[0]["source"], "sina")

    def test_to_float_handles_placeholders(self):
        from astock_market import to_float
        self.assertIsNone(to_float("", None))
        self.assertIsNone(to_float("-", None))
        self.assertIsNone(to_float(None, None))
        self.assertEqual(to_float("1,234.5", None), 1234.5)
        self.assertIsNone(astock.mkt.to_float("abc", None))


class TestEmailNotify(unittest.TestCase):
    """高风险邮件通知：复用 email-plugin 的发信能力（不自建 SMTP）。"""

    def setUp(self):
        import astock_notify as n
        self.n = n
        self._saved = sys.modules.get(n._EMAIL_MODULE_KEY, "MISSING")
        self.sent: list = []

    def tearDown(self):
        if self._saved == "MISSING":
            sys.modules.pop(self.n._EMAIL_MODULE_KEY, None)
        else:
            sys.modules[self.n._EMAIL_MODULE_KEY] = self._saved

    def _fake(self, auth_err=""):
        sent = self.sent
        m = types.ModuleType(self.n._EMAIL_MODULE_KEY)

        def _auth_material(app):
            if auth_err:
                return "", "", "", auth_err
            return "me@test.com", "secret-token", "oauth2", ""

        m._auth_material = _auth_material
        m._own_cfg = lambda app: {"email_from": ""}
        # 真实 email-plugin 用 _oauth_conf() 展开服务商预设
        # （主机/端口/加密方式）；astock 必须调它，不能读配置原始键
        m._oauth_conf = lambda app: {"smtp_host": "smtp.test.com",
                                     "smtp_port": 465, "smtp_tls": "ssl"}
        m._build_msg = (lambda frm, to, cc, bcc, subj, body, html, att, headers=None:
                        {"to": list(to), "subject": subj, "body": body})
        m._smtp_send = (lambda h, p, u, s, f, msg, auth_mode="basic", smtp_tls="ssl":
                        sent.append({"user": u, "msg": msg, "auth_mode": auth_mode}))
        return m

    def test_unavailable_without_plugin(self):
        import unittest.mock as mock
        sys.modules.pop(self.n._EMAIL_MODULE_KEY, None)
        with mock.patch.object(self.n, "_find_email_plugin", return_value=None):
            self.assertFalse(self.n.module_available())

    def test_send_uses_email_plugin(self):
        sys.modules[self.n._EMAIL_MODULE_KEY] = self._fake()
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        ok, msg = self.n.Notifier().send(cfg, None, "主题", "正文", "<p>h</p>")
        self.assertTrue(ok, msg)
        self.assertEqual(self.sent[0]["user"], "me@test.com")
        self.assertEqual(self.sent[0]["msg"]["to"], ["a@x.com"])

    def test_reuses_oauth2_auth_mode(self):
        """OAuth2 账号必须能发（自建 SMTP 会丢掉这条能力）。"""
        sys.modules[self.n._EMAIL_MODULE_KEY] = self._fake()
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        ok, _ = self.n.Notifier().send(cfg, None, "s", "b")
        self.assertTrue(ok)
        self.assertEqual(self.sent[0]["auth_mode"], "oauth2")

    def test_fallback_to_self(self):
        sys.modules[self.n._EMAIL_MODULE_KEY] = self._fake()
        cfg = self.n.MailConfig(enabled=True, mail_to=[], fallback_to_self=True)
        ok, _ = self.n.Notifier().send(cfg, None, "s", "b")
        self.assertTrue(ok)
        self.assertEqual(self.sent[0]["msg"]["to"], ["me@test.com"])

    def test_missing_credentials_actionable(self):
        sys.modules[self.n._EMAIL_MODULE_KEY] = self._fake(
            auth_err="邮箱账号未配置：请在设置页填写")
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        ok, msg = self.n.Notifier().send(cfg, None, "s", "b")
        self.assertFalse(ok)
        self.assertIn("凭据不可用", msg)

    def test_dedup_and_reset(self):
        nt = self.n.Notifier()
        self.assertTrue(nt.should_send("600000", "stop_loss")[0])
        nt.mark_sent("600000", "stop_loss")
        ok, why = nt.should_send("600000", "stop_loss")
        self.assertFalse(ok)
        self.assertIn("已提醒过", why)
        nt.reset_signal("600000", "stop_loss")
        self.assertTrue(nt.should_send("600000", "stop_loss")[0])

    def test_hourly_cap(self):
        nt = self.n.Notifier()
        for i in range(self.n.MAX_MAILS_PER_HOUR):
            nt.mark_sent("c%d" % i, "event")
        ok, why = nt.should_send("zzz", "event")
        self.assertFalse(ok)
        self.assertIn("上限", why)

    def test_build_mail_config(self):
        cfg = self.n.build_mail_config({
            "email_notify_enabled": True, "mail_to": "a@x.com，b@y.com",
            "email_min_level": "ACTION"})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.mail_to, ["a@x.com", "b@y.com"])
        self.assertEqual(cfg.min_level, "action")

    def test_render_subject_and_disclaimer(self):
        entries = [{"code": "600000", "name": "浦发银行", "severity": "urgent",
                    "title": "触发止损线", "kind": "stop_loss"}]
        subject, body = self.n.render_mail(entries, "2026-10-25 10:00")
        self.assertIn("紧急", subject)
        self.assertIn("浦发银行", subject)
        self.assertIn("不构成投资建议", body)
        self.assertIn("计划外", body)

    def test_render_html_escapes(self):
        entries = [{"code": "600000", "name": "<script>x</script>",
                    "severity": "urgent", "title": "t", "kind": "stop_loss"}]
        html = self.n.render_mail_html(entries, "now")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestNotifyIntegration(unittest.TestCase):
    """盯梢 → 邮件通知 联动（注入假 email-plugin，离线验证）。"""

    def setUp(self):
        import astock_notify as n
        self.n = n
        self._saved = sys.modules.get(n._EMAIL_MODULE_KEY, "MISSING")
        self.sent: list = []
        sys.modules[n._EMAIL_MODULE_KEY] = self._fake_module()
        import tempfile, datetime as dt
        self.tmp = tempfile.mkdtemp(prefix="astock-notify-")
        self.path = os.path.join(self.tmp, "l.sqlite")
        lg = ledger_mod(self.path)
        yday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        lg.record_trade("600000", "buy", 10.0, 1000, trade_date=yday, name="浦发银行")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._saved == "MISSING":
            sys.modules.pop(self.n._EMAIL_MODULE_KEY, None)
        else:
            sys.modules[self.n._EMAIL_MODULE_KEY] = self._saved

    def _fake_module(self):
        sent = self.sent
        m = types.ModuleType(self.n._EMAIL_MODULE_KEY)
        m._auth_material = lambda app: ("me@test.com", "tok", "basic", "")
        m._own_cfg = lambda app: {}
        m._oauth_conf = lambda app: {"smtp_host": "smtp.test.com",
                                     "smtp_port": 465, "smtp_tls": "ssl"}
        m._build_msg = (lambda frm, to, cc, bcc, subj, body, html, att, headers=None:
                        {"to": list(to), "subject": subj, "body": body})
        m._smtp_send = (lambda h, p, u, s, f, msg, auth_mode="basic", smtp_tls="ssl":
                        sent.append(msg))
        return m

    def _cfg(self, **over):
        cfg = {"ledger_path": self.path, "watch_enabled": True,
               "watch_only_trading_hours": False,
               "email_notify_enabled": True, "mail_to": "boss@x.com",
               "email_min_level": "urgent"}
        cfg.update(over)
        return {"astock-plugin": cfg}

    def _probe(self, quotes, cfg=None, watch=None):
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        w = watch or astock.AStockWatch()
        config = cfg or self._cfg()
        with patch.object(d, "fetch_quotes", lambda items, use_cache=True: quotes):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(w.probe(
                    {}, types.SimpleNamespace(config=config, workspace=self.tmp)))
            finally:
                loop.close()

    def _quote(self, price=8.8, prev=9.9):
        from astock_data import Quote
        return Quote(code="600000", venue="sh", name="浦发银行", price=price,
                     prev_close=prev, open=price,
                     limit_up=round(prev * 1.1, 2), limit_down=round(prev * 0.9, 2),
                     change_pct=round((price - prev) / prev * 100, 2))

    def test_urgent_signal_sends_mail(self):
        res = self._probe([self._quote()])
        self.assertIn("stop_loss", res.snapshot)
        self.assertEqual(len(self.sent), 1, "未触发邮件发送")
        self.assertIn("boss@x.com", self.sent[0]["to"])

    def test_no_app_attribute_regression(self):
        """回归：AStockWatch 没有 self._app，早先误用会让通知静默失效
        （异常被 except 吞掉，只在日志留一行 warning，最难发现）。"""
        w = astock.AStockWatch()
        self.assertFalse(hasattr(w, "_app"))
        self.assertTrue(hasattr(w, "_notifier"))     # 状态必须挂在本类
        res = self._probe([self._quote()], watch=w)
        self.assertTrue(res.ok)
        self.assertEqual(len(self.sent), 1)

    def test_disabled_switch_sends_nothing(self):
        self._probe([self._quote()], cfg=self._cfg(email_notify_enabled=False))
        self.assertEqual(self.sent, [])

    def test_action_signal_silent_at_urgent(self):
        """action 级信号在 urgent 阈值下不该发信。

        **必须屏蔽时段信号**：14:30–15:00 的这个跌幅还会额外触发「尾盘跳水」
        （urgent 级），那会让本测试在下午跑时莫名失败——它验证的是
        min_level 分级，不是时段规则。真实报障过一次：CI 上午绿、傍晚红。
        """
        self._probe([self._quote(price=9.4, prev=9.9)],
                    cfg=self._cfg(email_min_level="urgent",
                                  kinds="stop_loss,take_profit,trailing,"
                                        "limit_up,limit_down,event"))
        self.assertEqual(self.sent, [])

    def test_action_signal_sent_at_action(self):
        self._probe([self._quote(price=9.4, prev=9.9)],
                    cfg=self._cfg(email_min_level="action"))
        self.assertGreaterEqual(len(self.sent), 1)

    def test_same_signal_deduped_across_probes(self):
        w = astock.AStockWatch()
        cfg = self._cfg()
        first = self._probe([self._quote()], cfg=cfg, watch=w)
        second = self._probe([self._quote()], cfg=cfg, watch=w)
        self.assertIn("stop_loss", first.snapshot)
        self.assertIn("stop_loss", second.snapshot)
        self.assertEqual(len(self.sent), 1, "重复探测不应重发邮件")

    def test_recipient_fallback_to_self(self):
        self._probe([self._quote()], cfg=self._cfg(mail_to=""))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["to"], ["me@test.com"])


class TestSettingsConsistency(unittest.TestCase):
    """设置页声明的键必须与实际读取的键一致。

    回归背景：astock 的邮件通知从"自建 SMTP"改为"复用 email-plugin"后，
    设置页残留了 smtp_host / smtp_password 等 6 项——它们已不再被任何代码
    读取，用户填了却不生效，比"没有这个选项"更糟（会误以为已配好）。
    这类"声明与实现脱节"无法靠跑功能测出来，只能用结构性检查锁住。
    """

    def setUp(self):
        import re
        with open(os.path.join(_HERE, "plugin.py"), encoding="utf-8") as _f:
            self.src = _f.read()
        m = re.search(r'"settings": \[(.*?)\n        \],\n        "panels"',
                      self.src, re.S)
        self.block = m.group(1) if m else ""
        self.declared = re.findall(r'"key": "(\w+)"', self.block)

    def test_settings_block_found(self):
        self.assertTrue(self.declared, "未解析到 settings 声明块")

    def test_no_stale_smtp_keys(self):
        """自建 SMTP 时代的键必须彻底移除（改复用 email-plugin 后已无用途）。"""
        stale = [k for k in ("smtp_host", "smtp_port", "smtp_security",
                             "smtp_username", "smtp_password", "mail_from",
                             "email_reuse_email_plugin")
                 if k in self.declared]
        self.assertEqual(stale, [], f"设置页残留失效项：{stale}")

    def test_declared_keys_are_actually_read(self):
        """声明的键必须至少在一处被读取。

        **为什么不能只查字面量**：阈值类键（stop_loss_pct 等）由
        `hints_config()` 迭代 dataclass 字段 + `_num(app, key)` 动态读取，
        源码里不会出现 `"stop_loss_pct"` 字面量。因此这里分两步：
          ① 字面量出现的直接算命中；
          ② 其余要求能在 HintsConfig 的字段名里找到（说明是动态读取）。
        """
        import astock_hints as h
        body = self.src.replace(self.block, "")       # 排除声明块本身
        # **扫描整个插件目录**：设置项会被任意模块读取（notify / panel / plan …）。
        # v2.1.2 拆分后仍只拼 notify 一个模块，会让 panel_style 这类
        # "挪到面板模块去读"的键被误判成"无人读取"。
        parts = [body]
        for fn in sorted(os.listdir(_HERE)):
            if not fn.endswith(".py") or fn.startswith("test_"):
                continue
            if fn in ("plugin.py", "astock_hints.py"):
                continue        # plugin 已由 self.src 提供；hints 是纯算法层
            with open(os.path.join(_HERE, fn), encoding="utf-8") as _f:
                parts.append(_f.read())
        combined = "\n".join(parts)
        hint_fields = set(h.HintsConfig().__dataclass_fields__)
        unused = [k for k in self.declared
                  if f'"{k}"' not in combined and k not in hint_fields]
        self.assertEqual(unused, [], f"声明了但无人读取的键：{unused}")

    def test_read_keys_are_declared(self):
        """代码读取的 email_/watch_ 前缀键必须有声明（否则用户改不了）。"""
        import astock_notify as n
        import re as _re
        with open(os.path.join(_HERE, "astock_notify.py"), encoding="utf-8") as _f:
            notify_src = _f.read()
        read = set(_re.findall(r'own\.get\("(\w+)"\)', notify_src))
        read |= set(_re.findall(r'pick\("(\w+)"', self.src))
        missing = [k for k in sorted(read)
                   if k.startswith(("email_", "watch_")) and k not in self.declared]
        self.assertEqual(missing, [], f"代码读取但未声明的键：{missing}")


class TestSettingsActions(unittest.TestCase):
    """设置页动作按钮：这两个 action 是用户唯一能点的插件控件，必须不抛异常。

    回归背景（真实报障）：把邮件通知从"自建 SMTP"改为"复用 email-plugin"后，
    `_action_test_mail` 漏改——它仍在调用已删除的
    `notify.email_plugin_credentials()`，并访问新 MailConfig 里不存在的
    smtp_host/username 字段。用户在设置页一点「发送测试邮件」就 500：
      module 'astock_notify' has no attribute 'email_plugin_credentials'
    这类"只改一半"的重构，功能测试测不出来（因为动作入口没被覆盖）。
    """

    def setUp(self):
        self._saved = sys.modules.get(notify_mod._EMAIL_MODULE_KEY, "MISSING")
        self.sent: list = []

        class App:
            workspace = tempfile.mkdtemp(prefix="astock-act-")
            config = {"astock-plugin": {}}

            def save_config(self, upd):
                for k, v in upd.items():
                    App.config.setdefault(k, {}).update(v)

        self.App = App
        self.plugin = astock.AstockPlugin()
        self.plugin._app = App()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.App.workspace, ignore_errors=True)
        if self._saved == "MISSING":
            sys.modules.pop(notify_mod._EMAIL_MODULE_KEY, None)
        else:
            sys.modules[notify_mod._EMAIL_MODULE_KEY] = self._saved

    def _inject_email_plugin(self, auth_err=""):
        sent = self.sent
        m = types.ModuleType(notify_mod._EMAIL_MODULE_KEY)

        def _auth_material(app):
            if auth_err:
                return "", "", "", auth_err
            return "me@test.com", "tok", "basic", ""

        m._auth_material = _auth_material
        m._own_cfg = lambda app: {}
        m._oauth_conf = lambda app: {"smtp_host": "smtp-mail.outlook.com",
                                     "smtp_port": 587, "smtp_tls": "starttls"}
        m._build_msg = (lambda frm, to, cc, bcc, subj, body, html, att, headers=None:
                        {"to": list(to), "subject": subj})
        m._smtp_send = (lambda h, p, u, s, f, msg, auth_mode="basic", smtp_tls="ssl":
                        sent.append(msg))
        sys.modules[notify_mod._EMAIL_MODULE_KEY] = m

    # ---------- 发送测试邮件 ----------

    def test_test_mail_does_not_raise(self):
        """真实报障的回归：无论如何都不能抛 AttributeError（会变成 500）。"""
        self._inject_email_plugin()
        res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertIsInstance(res, dict)
        self.assertIn("ok", res)
        self.assertIn("message", res)

    def test_test_mail_sends_when_configured(self):
        self._inject_email_plugin()
        res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(len(self.sent), 1)

    def test_test_mail_ignores_main_switch(self):
        """测试发送无视「高风险邮件通知」主开关（先验证再启用是正常流程）。"""
        self._inject_email_plugin()
        self.App.config["astock-plugin"]["email_notify_enabled"] = False
        res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertTrue(res["ok"], res.get("message"))

    def test_test_mail_reports_missing_plugin(self):
        """email-plugin 不可用时给可操作提示，而不是抛异常。"""
        sys.modules.pop(notify_mod._EMAIL_MODULE_KEY, None)
        import unittest.mock as mock
        with mock.patch.object(notify_mod, "_find_email_plugin", return_value=None):
            res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertFalse(res["ok"])
        self.assertIn("email-plugin", res["message"])

    def test_test_mail_reports_bad_credentials(self):
        self._inject_email_plugin(auth_err="邮箱账号未配置：请在设置页填写")
        res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertFalse(res["ok"])
        self.assertIn("凭据不可用", res["message"])

    def test_test_mail_recipient_uses_email_plugin_sender(self):
        """收件人留空 → 发给 email-plugin 的发件邮箱自己。"""
        self._inject_email_plugin()
        res = self.plugin.settings_action("email_test_send", self.App.config)
        self.assertTrue(res["ok"])
        self.assertEqual(self.sent[0]["to"], ["me@test.com"])

    # ---------- 盯盘开关 ----------

    def test_watch_toggle_does_not_raise(self):
        res = self.plugin.settings_action("watch_toggle", self.App.config)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["ok"])
        self.assertFalse(res["watch_enabled"])          # 默认开 → 点一下关

    def test_unknown_action_returns_error(self):
        res = self.plugin.settings_action("no_such_action", self.App.config)
        self.assertFalse(res["ok"])
        self.assertIn("未知动作", res["message"])


class TestSmtpParamsFromEmailPlugin(unittest.TestCase):
    """SMTP 参数必须由 email-plugin 解析，不能直接读配置原始键。

    回归背景（真实报障）：用户用 Hotmail（OAuth2）时发信报
      SMTPServerDisconnected: Connection unexpectedly closed
    根因：email-plugin 的配置里存的是「服务商标识」（provider=outlook）
    与 OAuth 令牌，具体的 smtp_host/smtp_port/smtp_tls 由它的预设表展开
    （_oauth_conf()）。astock 原先直接读 own["smtp_host"]，拿到空值后回退到
    "通用默认"（smtp.qq.com:465+ssl）——对 Hotmail 就是连错地址+用错协议，
    服务器直接断开。
    """

    def setUp(self):
        import astock_notify as n
        self.n = n
        self._saved = sys.modules.get(n._EMAIL_MODULE_KEY, "MISSING")
        self.calls: list = []

    def tearDown(self):
        if self._saved == "MISSING":
            sys.modules.pop(self.n._EMAIL_MODULE_KEY, None)
        else:
            sys.modules[self.n._EMAIL_MODULE_KEY] = self._saved

    def _inject(self, oauth_conf, own_cfg=None):
        calls = self.calls
        m = types.ModuleType(self.n._EMAIL_MODULE_KEY)
        m._auth_material = lambda app: ("me@test.com", "tok", "oauth2", "")
        m._own_cfg = lambda app: dict(own_cfg or {})
        m._oauth_conf = lambda app: dict(oauth_conf)
        m._build_msg = (lambda frm, to, cc, bcc, subj, body, html, att, headers=None:
                        {"to": list(to), "subject": subj})
        m._smtp_send = (lambda h, p, u, s, f, msg, auth_mode="basic", smtp_tls="ssl":
                        calls.append({"host": h, "port": p, "tls": smtp_tls}))
        sys.modules[self.n._EMAIL_MODULE_KEY] = m
        return m

    def test_uses_oauth_conf_not_raw_keys(self):
        """配置里没有 smtp_host（只有 provider），也要拿到正确的服务器。"""
        self._inject({"smtp_host": "smtp-mail.outlook.com", "smtp_port": 587,
                      "smtp_tls": "starttls"}, own_cfg={"provider": "outlook"})
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        ok, msg = self.n.Notifier().send(cfg, None, "s", "b")
        self.assertTrue(ok, msg)
        self.assertEqual(self.calls[0]["host"], "smtp-mail.outlook.com")
        self.assertEqual(self.calls[0]["port"], 587)
        self.assertEqual(self.calls[0]["tls"], "starttls")

    def test_never_falls_back_to_generic_default(self):
        """关键回归：不能回退到 smtp.qq.com/465/ssl（那正是报障原因）。"""
        self._inject({"smtp_host": "smtp-mail.outlook.com", "smtp_port": 587,
                      "smtp_tls": "starttls"})
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        self.n.Notifier().send(cfg, None, "s", "b")
        self.assertNotEqual(self.calls[0]["host"], "smtp.qq.com")
        self.assertNotEqual(self.calls[0]["tls"], "ssl")

    def test_missing_host_reports_actionable(self):
        """解析不出服务器时给可操作提示，而不是硬连默认地址。"""
        self._inject({"smtp_host": "", "smtp_port": 587, "smtp_tls": "starttls"})
        cfg = self.n.MailConfig(enabled=True, mail_to=["a@x.com"])
        ok, msg = self.n.Notifier().send(cfg, None, "s", "b")
        self.assertFalse(ok)
        self.assertIn("SMTP 服务器", msg)
        self.assertEqual(self.calls, [])

    def test_source_does_not_read_raw_smtp_keys(self):
        """结构性检查：源码里不得再直接读 own 的 smtp_host/port/tls。"""
        with open(os.path.join(_HERE, "astock_notify.py"), encoding="utf-8") as _f:
            src = _f.read()
        self.assertIn("_oauth_conf", src)
        for bad in ('own.get("smtp_host")', "own.get('smtp_host')",
                    'own.get("smtp_port")', 'own.get("smtp_tls")'):
            self.assertNotIn(bad, src, f"仍在直接读配置原始键：{bad}")


class TestBoardRender(unittest.TestCase):
    """`astock_board.build_board` —— 此前**零测试覆盖**，而它是聊天内看板的核心。

    看板是用户最常看到的"扫一眼"视图（`astock_portfolio` 默认就渲染它），
    却有字段上限、分栏规则、tone 着色三层逻辑从未被验证。
    """

    def setUp(self):
        import astock_board as bd
        import astock_hints as hs
        self.bd, self.hs = bd, hs
        self.cfg = hs.HintsConfig()
        self.data = {
            "positions": [{"code": "600000", "name": "浦发银行", "shares": 1000,
                           "cost_price": 10.0, "available_today": 1000}],
            "quotes": {"600000": {"price": 9.53, "change_pct": -0.94}},
            "total_market_value": 9530.0, "cash": 30000.0,
            "float_pnl": -470.0, "today_pnl": -90.0,
        }

    def _build(self, data=None, watch=()):
        def no_t1(pos, avail):
            return None
        return self.bd.build_board(data or self.data, self.cfg,
                                   self.hs.build_position_hints, no_t1,
                                   watch_codes=list(watch))

    def test_no_content_returns_none(self):
        empty = {"positions": [], "quotes": {},
                 "total_market_value": 0.0, "cash": 0.0,
                 "float_pnl": 0.0, "today_pnl": 0.0}
        self.assertIsNone(self._build(empty))

    def test_respects_host_field_limits(self):
        """title≤80 / desc≤200 / tag≤20 —— 超了前端会静默硬截成半句话。"""
        spec = self._build()
        for col in spec["columns"]:
            self.assertLessEqual(len(col["name"]), 30)
            for card in col["cards"]:
                self.assertLessEqual(len(card["title"]), 80)
                self.assertLessEqual(len(card["desc"] or ""), 200)
                self.assertLessEqual(len(card["tag"] or ""), 20)

    def test_tone_only_host_supported_values(self):
        """宿主只认 good/bad/neutral 三色，其它值会被前端丢弃成无色。"""
        spec = self._build()
        for col in spec["columns"]:
            for card in col["cards"]:
                self.assertIn(card["tone"], ("good", "bad", "neutral"))

    def test_watch_without_quote_is_honestly_marked(self):
        """自选股没有行情时如实标「未取到」，不假装有数据。"""
        spec = self._build(watch=["600519"])
        idle = [c for col in spec["columns"] if col["name"] == "无需关注"
                for c in col["cards"]]
        self.assertTrue(any("未取到" in (c["desc"] or "") for c in idle))

    def test_watch_with_quote_shows_price_not_placeholder(self):
        """修复：自选股有行情时必须显示价格，而不是「（行情未取到）」。

        回归点：`_snapshot` 曾完全不取自选行情，导致看板里每只自选都是空卡片。
        """
        data = dict(self.data)
        data["watch_quotes"] = {"600519": {"code": "600519", "name": "贵州茅台",
                                           "price": 1342.0, "change_pct": 0.52}}
        spec = self._build(data, watch=["600519"])
        self.assertIsNotNone(spec)
        titles = [c["title"] for col in spec["columns"] for c in col["cards"]]
        self.assertIn("贵州茅台", "\n".join(titles))
        # 有行情就不该出现"未取到"占位
        descs = "\n".join(c["desc"] or "" for col in spec["columns"]
                           for c in col["cards"])
        self.assertNotIn("未取到", descs)

    def test_columns_always_three(self):
        spec = self._build()
        self.assertEqual([c["name"] for c in spec["columns"]],
                         ["需处理", "持有中", "无需关注"])




class TestSnapshotIncludesWatch(unittest.TestCase):
    """`_snapshot(include_watch=True)` 必须拉自选行情（空卡片 bug 的根因）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astock-snap-")
        # **显式 ledger_path 指到临时目录**：默认路径已改为全局
        # （~/.lite-work/astock-data/），测试若走默认会**读写用户真实台账**
        # （曾把 set_cash(1000) 写进真实数据）。显式配置是测试隔离的唯一
        # 正确方式——不要再用 default_ledger_path(workspace) 语义。
        self.path = os.path.join(self.tmp, "ledger.sqlite")
        lg = ledger_mod(self.path)
        lg.add_watch("600519", "贵州茅台")
        lg.set_cash(1000)
        pl = astock.AstockPlugin()
        pl._app = types.SimpleNamespace(
            workspace=self.tmp,
            config={"astock-plugin": {"ledger_path": self.path}})
        self.pl = pl

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_watch_quotes_present_when_requested(self):
        import astock_data as d
        seen = {}

        def fake_fetch(items, use_cache=True):
            seen["items"] = list(items)
            return []

        orig = d.fetch_quotes
        d.fetch_quotes = fake_fetch
        try:
            snap = self.pl._snapshot(include_watch=True)
        finally:
            d.fetch_quotes = orig
        self.assertIn("watch_quotes", snap["data"])
        codes = [c for c, _, _ in seen.get("items", [])]
        self.assertIn("600519", codes, "自选股未被请求行情 → 看板会出现空卡片")

    def test_watch_not_fetched_by_default(self):
        import astock_data as d
        called = {"n": 0}

        def fake_fetch(items, use_cache=True):
            called["n"] += 1
            return []

        orig = d.fetch_quotes
        d.fetch_quotes = fake_fetch
        try:
            snap = self.pl._snapshot()
        finally:
            d.fetch_quotes = orig
        self.assertEqual(called["n"], 0, "默认不该为自选打行情请求")
        self.assertEqual(snap["data"]["watch_quotes"], {})


# ---------------------------------------------------------------- 量化选股（v1.8.0）


def _mk_bars(base: float, trend: float, n: int = 120,
             vol_base: float = 2000.0) -> list:
    """构造合成日 K（升序）。trend>1 上升、<1 下降、=1 横盘。"""
    bars = []
    p = base
    for i in range(n):
        p *= trend
        bars.append({"close": p, "high": p * 1.01, "low": p * 0.99,
                     "volume_lot": vol_base + i})
    return bars


def _mk_scored(code: str, name: str, bars: list, *, pe=20.0, pb=2.0,
               mcap=5e10, amount=3e8, turnover=3.0, chg=1.0,
               main_net=1e7) -> "quant.Scored":
    tech = quant.compute_technical(bars)
    price = bars[-1]["close"] if bars else 0.0
    row = {"code": code, "name": name, "price": price, "change_pct": chg,
           "pe": pe, "pb": pb, "total_mcap_yuan": mcap, "amount_yuan": amount,
           "turnover_pct": turnover, "main_net_inflow_yuan": main_net}
    return quant.Scored(code=code, name=name, price=price, change_pct=chg,
                        tech=tech, row=row)


class TestQuantTechnical(unittest.TestCase):
    """技术因子计算：口径必须能被手算核对。"""

    def test_uptrend_alignment_and_momentum(self):
        t = quant.compute_technical(_mk_bars(10.0, 1.004))
        self.assertEqual(t["bars"], 120)
        self.assertEqual(t["ma_alignment"], 3, "持续上涨应满足 close>MA5>MA10>MA20")
        self.assertGreater(t["mom20"], 0)
        self.assertGreater(t["mom60"], 0)
        self.assertGreater(t["pos_in_range"], 90, "接近区间高点")

    def test_downtrend(self):
        t = quant.compute_technical(_mk_bars(30.0, 0.996))
        self.assertLess(t["mom20"], 0)
        self.assertLess(t["pos_in_range"], 10, "接近区间低位")
        self.assertLess(t["rsi14"], 50)

    def test_short_history_is_none_not_zero(self):
        """数据不足时必须给 None，不能给 0。

        0 在「低波动」因子里是最优点 → 数据不足的标的会被推到第一名。
        这是最难发现的静默错误之一，必须锁住。
        """
        t = quant.compute_technical(_mk_bars(10.0, 1.01, n=2))
        self.assertIsNone(t.get("volatility_20d"))
        self.assertIsNone(t.get("ma20"))
        self.assertIsNone(t.get("mom20"))

    def test_empty_and_bad_bars(self):
        self.assertEqual(quant.compute_technical([])["bars"], 0)
        self.assertEqual(quant.compute_technical([{"close": 0}])["bars"], 0)

    def test_rsi_bounds(self):
        up = quant.compute_technical(_mk_bars(10.0, 1.01, n=40))
        self.assertGreater(up["rsi14"], 90, "单边上涨 RSI 应接近 100")
        self.assertLessEqual(up["rsi14"], 100.0)
        down = quant.compute_technical(_mk_bars(10.0, 0.99, n=40))
        self.assertLess(down["rsi14"], 10)
        self.assertGreaterEqual(down["rsi14"], 0.0)

    def test_rsi_flat_is_neutral(self):
        bars = [{"close": 10.0, "high": 10.0, "low": 10.0, "volume_lot": 100}
                for _ in range(40)]
        self.assertEqual(quant.compute_technical(bars)["rsi14"], 50.0)


class TestQuantPercentiles(unittest.TestCase):
    """百分位是打分的地基，必须精确。"""

    def test_basic_ascending(self):
        p = quant.percentiles({"a": 1, "b": 2, "c": 3}, direction=1)
        self.assertEqual(p, {"a": 0.0, "b": 0.5, "c": 1.0})

    def test_direction_flip(self):
        p = quant.percentiles({"a": 1, "b": 2, "c": 3}, direction=-1)
        self.assertEqual(p["a"], 1.0)
        self.assertEqual(p["c"], 0.0)

    def test_ties_get_average_rank(self):
        """并列必须同分——否则同值股票会因排序不稳定而分数抖动。"""
        p = quant.percentiles({"a": 1, "b": 2, "c": 2, "d": 3}, direction=1)
        self.assertEqual(p["b"], p["c"])
        self.assertAlmostEqual(p["b"], (1 + 2) / 2 / 3)

    def test_single_element_is_full_score(self):
        """单元素池给 1.0 而非 0：只选一只时得 0 分看着像出错。"""
        self.assertEqual(quant.percentiles({"a": 5}, direction=1), {"a": 1.0})

    def test_empty(self):
        self.assertEqual(quant.percentiles({}, direction=1), {})


class TestQuantRanking(unittest.TestCase):
    """多因子打分：策略方向相反，同一批股票排序应相反。"""

    def setUp(self):
        self.rising = _mk_scored("600001", "强趋势", _mk_bars(10.0, 1.008))
        self.flat = _mk_scored("600002", "横盘", _mk_bars(15.0, 1.0))
        self.falling = _mk_scored("600003", "下跌", _mk_bars(20.0, 0.997))

    def test_momentum_ranks_rising_first(self):
        ranked = quant.rank_candidates(
            [self.rising, self.flat, self.falling], quant.STRATEGIES["momentum"])
        self.assertEqual(ranked[0].code, "600001")

    def test_reversal_ranks_falling_first(self):
        ranked = quant.rank_candidates(
            [self.rising, self.flat, self.falling], quant.STRATEGIES["reversal"])
        self.assertEqual(ranked[0].code, "600003")

    def test_scores_within_0_100(self):
        ranked = quant.rank_candidates(
            [self.rising, self.flat, self.falling], quant.STRATEGIES["momentum"])
        for c in ranked:
            self.assertGreaterEqual(c.score, 0.0)
            self.assertLessEqual(c.score, 100.0 + 1e-6)

    def test_contributions_sum_to_score(self):
        """贡献之和必须等于总分——这是"可核对"的硬前提。"""
        ranked = quant.rank_candidates(
            [self.rising, self.flat, self.falling], quant.STRATEGIES["momentum"])
        for c in ranked:
            if c.contributions:
                self.assertAlmostEqual(sum(c.contributions.values()), c.score,
                                       places=1)

    def test_missing_factor_not_treated_as_best(self):
        """缺数据（如亏损股 PE<=0）不得被当成"极度便宜"。

        做法：该因子从这只票的分母里剔除。若实现成"缺失→给 0 分"，
        它会被压到末位；若实现成"缺失→给满分"，它会窜到首位。
        正确行为是：只按它有数据的因子打分（得分介于两者之间）。
        """
        rich = _mk_scored("600010", "有PE", _mk_bars(10.0, 1.004), pe=10.0)
        poor = _mk_scored("600011", "亏损", _mk_bars(10.0, 1.004), pe=-5.0)
        strat = quant.STRATEGIES["quality"]
        ranked = quant.rank_candidates([rich, poor], strat)
        by_code = {c.code: c for c in ranked}
        self.assertIn("pe_inv", by_code["600011"].missing_factors)
        self.assertNotIn("pe_inv", by_code["600011"].factor_pcts)
        self.assertGreater(by_code["600011"].score, 0.0,
                           "有其它有效因子时不应是 0 分")

    def test_empty_input(self):
        self.assertEqual(quant.rank_candidates([], quant.STRATEGIES["momentum"]), [])

    def test_weight_override(self):
        only_mom = quant.rank_candidates(
            [self.rising, self.flat, self.falling],
            quant.STRATEGIES["momentum"], weights={"mom20": 1.0})
        self.assertEqual(only_mom[0].code, "600001")
        self.assertEqual(set(only_mom[0].contributions), {"mom20"})

    def test_bad_factor_value_does_not_crash(self):
        """单只票的脏数据不应炸掉整轮选股。"""
        bad = _mk_scored("600099", "脏数据", _mk_bars(10.0, 1.004))
        bad.row["pe"] = "abc"           # 非数值
        ranked = quant.rank_candidates(
            [self.rising, bad], quant.STRATEGIES["quality"])
        self.assertEqual(len(ranked), 2)


class TestQuantFilters(unittest.TestCase):
    """硬门槛：不通过的要给出人能读懂的原因。"""

    def setUp(self):
        self.strat = quant.STRATEGIES["momentum"]
        self.scored = _mk_scored("600001", "测试", _mk_bars(10.0, 1.004))

    def test_normal_passes(self):
        ok, why = quant.passes_filters(self.scored.row, self.scored.tech, self.strat)
        self.assertTrue(ok, why)

    def test_st_rejected(self):
        row = dict(self.scored.row, name="ST测试")
        ok, why = quant.passes_filters(row, self.scored.tech, self.strat)
        self.assertFalse(ok)
        self.assertIn("ST", why)

    def test_low_liquidity_rejected(self):
        row = dict(self.scored.row, amount_yuan=1e6)
        ok, why = quant.passes_filters(row, self.scored.tech, self.strat)
        self.assertFalse(ok)
        self.assertIn("成交额", why)

    def test_limit_up_rejected(self):
        """涨停不追：成交不确定。"""
        row = dict(self.scored.row, change_pct=10.0)
        ok, why = quant.passes_filters(row, self.scored.tech, self.strat)
        self.assertFalse(ok)
        self.assertIn("涨停", why)

    def test_no_price_rejected(self):
        row = dict(self.scored.row, price=0)
        ok, why = quant.passes_filters(row, self.scored.tech, self.strat)
        self.assertFalse(ok)
        self.assertIn("停牌", why)

    def test_insufficient_bars_rejected(self):
        tech = quant.compute_technical(_mk_bars(10.0, 1.0, n=10))
        ok, why = quant.passes_filters(self.scored.row, tech, self.strat)
        self.assertFalse(ok)
        self.assertIn("历史数据不足", why)


class TestQuantSizing(unittest.TestCase):
    """仓位分配：股数必须整手、不超预算、不超单票上限。"""

    def setUp(self):
        self.ranked = quant.rank_candidates(
            [_mk_scored(f"60000{i}", f"股{i}", _mk_bars(10.0 + i, 1.004))
             for i in range(1, 6)],
            quant.STRATEGIES["momentum"])

    def test_shares_are_whole_lots(self):
        taken, _s, _sum = quant.size_positions(
            self.ranked, cash=1e5, equity=3e5, cfg=quant.SizingConfig())
        self.assertTrue(taken)
        for a in taken:
            self.assertEqual(a.shares % 100, 0, f"{a.code} 不是整手")

    def test_never_exceeds_budget(self):
        taken, _s, summary = quant.size_positions(
            self.ranked, cash=1e5, equity=3e5, cfg=quant.SizingConfig())
        spent = sum(a.amount + a.fee for a in taken)
        self.assertLessEqual(spent, summary["budget"] + 1e-6)

    def test_never_exceeds_position_cap(self):
        cfg = quant.SizingConfig(position_cap_pct=10.0)
        taken, _s, _sum = quant.size_positions(
            self.ranked, cash=1e6, equity=1e6, cfg=cfg)
        for a in taken:
            self.assertLessEqual(a.amount, 1e6 * 0.10 + 1.0,
                                 f"{a.code} 超出单票上限")

    def test_max_positions_respected(self):
        cfg = quant.SizingConfig(max_positions=2)
        taken, _s, _sum = quant.size_positions(
            self.ranked, cash=1e6, equity=1e6, cfg=cfg)
        self.assertLessEqual(len(taken), 2)

    def test_insufficient_cash_reports_reason(self):
        """钱不够买一手 → 跳过并说明原因，不能静默消失。"""
        expensive = quant.rank_candidates(
            [_mk_scored("600999", "高价股", _mk_bars(5000.0, 1.004))],
            quant.STRATEGIES["momentum"])
        taken, skipped, _sum = quant.size_positions(
            expensive, cash=1000, equity=1000, cfg=quant.SizingConfig())
        self.assertEqual(taken, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("一手", skipped[0].skip_reason)

    def test_cash_usage_pct(self):
        cfg = quant.SizingConfig(cash_usage_pct=50.0)
        _t, _s, summary = quant.size_positions(
            self.ranked, cash=1e5, equity=1e5, cfg=cfg)
        self.assertAlmostEqual(summary["budget"], 5e4, places=2)

    def test_cash_reserve_floor(self):
        cfg = quant.SizingConfig(cash_usage_pct=100.0, cash_reserve_yuan=20000)
        _t, _s, summary = quant.size_positions(
            self.ranked, cash=1e5, equity=1e5, cfg=cfg)
        self.assertAlmostEqual(summary["budget"], 8e4, places=2)

    def test_zero_cash_all_skipped(self):
        taken, skipped, _s = quant.size_positions(
            self.ranked, cash=0, equity=0, cfg=quant.SizingConfig())
        self.assertEqual(taken, [])
        self.assertEqual(len(skipped), len(self.ranked[:5]))

    def test_idle_budget_is_reallocated(self):
        """买不起一手的标的留下的预算必须回收，不能躺在账上。

        回归背景：实测 8 万预算只投出 5 万、剩 3 万闲置——原因是有一只
        高价股买不起一手，它那份预算没人接手。用户看到的是"算出该买 8 万
        却只买了 5 万"，而原因藏在细节里，几乎没人会去算。
        """
        cheap = quant.Scored(code="600001", name="便宜", price=8.0,
                             change_pct=1.0, score=90.0)
        pricey = quant.Scored(code="600002", name="高价", price=5000.0,
                              change_pct=1.0, score=80.0)
        taken, skipped, summary = quant.size_positions(
            [cheap, pricey], cash=100000, equity=1000000,
            cfg=quant.SizingConfig(max_positions=2))
        self.assertEqual(len(skipped), 1, "高价股应买不起一手")
        self.assertEqual(len(taken), 1)
        # 便宜的这只应吃到远超"一半"的预算（回收了高价股那份）
        self.assertGreater(summary["allocated_amount"], 60000,
                           f"预算未回收，只投出 {summary['allocated_amount']}")

    def test_idle_cash_stays_within_budget(self):
        """回收之后仍然不能超预算——上限是被反复校验的。"""
        cheap = quant.Scored(code="600001", name="便宜", price=8.0,
                             change_pct=1.0, score=90.0)
        pricey = quant.Scored(code="600002", name="高价", price=5000.0,
                              change_pct=1.0, score=80.0)
        taken, _sk, summary = quant.size_positions(
            [cheap, pricey], cash=100000, equity=1e9,
            cfg=quant.SizingConfig(max_positions=2))
        spent = sum(a.amount + a.fee for a in taken)
        self.assertLessEqual(spent, summary["budget"] + 1e-6)

    def test_stop_loss_below_price(self):
        taken, _s, _sum = quant.size_positions(
            self.ranked, cash=1e5, equity=3e5, cfg=quant.SizingConfig())
        for a in taken:
            self.assertLess(a.stop_loss, a.price)
            self.assertGreater(a.take_profit, a.price)

    def test_star_board_lot_is_200(self):
        bars = _mk_bars(10.0, 1.004)
        star = quant.Scored(code="688001", name="科创", price=bars[-1]["close"],
                            change_pct=1.0,
                            tech=quant.compute_technical(bars),
                            row={"code": "688001", "name": "科创",
                                 "price": bars[-1]["close"], "amount_yuan": 3e8})
        taken, _s, _sum = quant.size_positions(
            [star], cash=1e6, equity=1e6, cfg=quant.SizingConfig())
        if taken:
            self.assertEqual(taken[0].shares % 200, 0, "科创板应 200 股一手")


class TestQuantRendering(unittest.TestCase):
    """渲染：必须带上免责口径与"分数不是概率"。"""

    def test_report_contains_shares_and_disclaimer(self):
        ranked = quant.rank_candidates(
            [_mk_scored("600001", "甲", _mk_bars(10.0, 1.004))],
            quant.STRATEGIES["momentum"])
        taken, skipped, summary = quant.size_positions(
            ranked, cash=1e5, equity=2e5, cfg=quant.SizingConfig())
        text = quant.format_pick_report(quant.STRATEGIES["momentum"],
                                        taken, skipped, summary,
                                        scanned=5000, candidates=20)
        self.assertIn("买入清单", text)
        self.assertIn("股", text)
        self.assertIn("不构成投资建议", text)
        self.assertIn("止损", text)

    def test_empty_result_explains_why(self):
        text = quant.format_pick_report(
            quant.STRATEGIES["momentum"], [], [],
            {"cash": 0, "budget": 0, "equity": 0,
             "allocated_amount": 0, "allocated_fee": 0,
             "remaining_cash": 0, "position_cap_amount": 0,
             "position_cap_pct": 25.0, "position_count": 0,
             "skipped_count": 0, "budget_source": "x"},
            scanned=5000, candidates=0)
        self.assertIn("没有产生可执行的买入计划", text)
        self.assertIn("不构成投资建议", text)

    def test_panel_without_cache_prompts_action(self):
        import astock_quant_tool as qt
        text = qt.render_panel(None, "momentum")
        self.assertIn("尚未生成", text)
        self.assertIn("面板只读缓存", text)

    def test_explain_is_human_readable(self):
        """成交额/PE 等在内部做了 log/倒数变换，展示时必须还原。"""
        c = _mk_scored("600001", "甲", _mk_bars(10.0, 1.004), pe=10.0)
        ranked = quant.rank_candidates([c], quant.STRATEGIES["quality"])
        text = ranked[0].explain(5)
        self.assertNotIn("e-", text)
        self.assertIn("贡献", text)


class TestQuantStrategyRegistry(unittest.TestCase):
    """策略表自身的完整性——新增因子时最容易漏掉引用。"""

    def test_three_strategies(self):
        self.assertEqual(set(quant.STRATEGIES),
                         {"momentum", "reversal", "quality"})

    def test_all_weights_reference_known_factors(self):
        for key, s in quant.STRATEGIES.items():
            for f in s.weights:
                self.assertIn(f, quant.FACTORS, f"{key} 引用了未知因子 {f}")
            self.assertGreater(sum(s.weights.values()), 0)

    def test_opposite_directions_documented(self):
        """趋势策略看高的因子，回归策略应看低——否则两套策略是同一件事。"""
        mom = quant.STRATEGIES["momentum"].weights
        rev = quant.STRATEGIES["reversal"].weights
        self.assertIn("mom20", mom)
        self.assertNotIn("mom20", rev, "回归策略不该追动量")
        self.assertIn("low_pos", rev)
        self.assertNotIn("low_pos", mom)

    def test_default_strategy_exists(self):
        self.assertIn(quant.DEFAULT_STRATEGY, quant.STRATEGIES)


# ---------------------------------------------------------------- 一次采集多套策略


class TestScanAll(unittest.TestCase):
    """`scan_all`：一次数据采集产出三套策略结果。

    这是 v2.1.0 的核心优化。此前看三套策略要各扫一次（3× 采集成本，
    约 18 秒 + 90 次 K 线请求），而三套策略的差异只在 filters/weights
    （纯 CPU）——采集结果完全共用。
    """

    def setUp(self):
        import astock_data as d
        import astock_quant_tool as qt
        self.d, self.qt = d, qt
        qt.clear_cache()
        self.calls = {"screen": 0, "kline": 0}
        # 造一批覆盖三种策略口径的标的：上涨趋势（动量）/ 下跌（回归）/
        # 低估值大盘（价值）。用同一份数据供三套策略评估。
        self.rows = [
            {"code": "600001", "name": "上涨股", "price": 20.0, "change_pct": 1.0,
             "pe": 30.0, "pb": 3.0, "total_mcap_yuan": 8e10,
             "amount_yuan": 5e8, "turnover_pct": 3.0,
             "main_net_inflow_yuan": 1e7, "_trend": 1.004},
            {"code": "600002", "name": "下跌股", "price": 9.0, "change_pct": -1.0,
             "pe": 15.0, "pb": 1.2, "total_mcap_yuan": 6e10,
             "amount_yuan": 4e8, "turnover_pct": 2.0,
             "main_net_inflow_yuan": 5e6, "_trend": 0.996},
            {"code": "600003", "name": "价值股", "price": 40.0, "change_pct": 0.2,
             "pe": 6.0, "pb": 0.8, "total_mcap_yuan": 3e11,
             "amount_yuan": 6e8, "turnover_pct": 1.0,
             "main_net_inflow_yuan": 2e7, "_trend": 1.001},
            {"code": "600004", "name": "小额股", "price": 15.0, "change_pct": 0.0,
             "pe": 25.0, "pb": 2.0, "total_mcap_yuan": 1e10,
             "amount_yuan": 1e6, "turnover_pct": 0.5,
             "main_net_inflow_yuan": 1e5, "_trend": 1.000},
        ]
        self._orig = (d.screen, d.fetch_klines)

        def fake_screen(**kw):
            self.calls["screen"] += 1
            public = [{k: v for k, v in r.items() if not k.startswith("_")}
                      for r in self.rows]
            return {"rows": public, "universe_total": 5000}

        def fake_klines(code, period="day", limit=120, **kw):
            self.calls["kline"] += 1
            row = next(r for r in self.rows if r["code"] == code)
            from astock_data import Bar
            bars = []
            p = row["price"] / (row["_trend"] ** 120)
            for _ in range(120):
                p *= row["_trend"]
                bars.append(Bar(date="2026-01-01", open=p, close=p,
                                high=p * 1.01, low=p * 0.99, volume_lot=2000.0))
            return bars

        d.screen, d.fetch_klines = fake_screen, fake_klines

    def tearDown(self):
        self.d.screen, self.d.fetch_klines = self._orig
        self.qt.clear_cache()

    def _run(self, **kw):
        return self.qt.scan_all(cash=100000.0, equity=200000.0,
                                sizing=quant.SizingConfig(max_positions=2),
                                use_cache=False, **kw)

    def test_returns_all_strategies_in_one_collect(self):
        out = self._run()
        self.assertEqual(set(out), set(quant.STRATEGIES))
        # 关键：K 线只拉了「标的数」次，不是「标的数 × 策略数」次
        self.assertEqual(self.calls["kline"], len(self.rows),
                         "三套策略必须共用一次数据采集")
        self.assertEqual(self.calls["screen"], 1)

    def test_each_result_is_independent(self):
        """三套策略的过滤条件不同 → 候选池/结果必须各自独立。

        `quant.rank_candidates` 会原地写候选对象，若共用同一批 Scored
        实例，后一套会覆盖前一套的打分痕迹（得分全变成最后一套的）。
        """
        out = self._run()
        # 结果里 strategy 字段必须各自正确，不能全变成同一个
        for key, res in out.items():
            self.assertEqual(res["strategy"], key)
        # 各策略的候选数应允许不同（filters 不同）；至少策略标签不同
        labels = {res["strategy_label"] for res in out.values()}
        self.assertEqual(len(labels), len(quant.STRATEGIES))
        # 同一只票在不同策略下的得分不应完全一致（权重方向相反）
        def codes_scores(res):
            return {s.code: s.score for s in res.get("ranked") or []}
        mom, qual = codes_scores(out["momentum"]), codes_scores(out["quality"])
        shared = set(mom) & set(qual)
        if len(shared) >= 2:
            self.assertNotEqual([mom[c] for c in sorted(shared)],
                                [qual[c] for c in sorted(shared)],
                                "动量与价值的打分不应完全相同")

    def test_no_network_when_cache_hit(self):
        """缓存命中时必须零请求（面板 5 秒刷新会踩到这条路径）。"""
        self._run()
        self.calls.update(screen=0, kline=0)
        out = self.qt.scan_all(cash=100000.0, equity=200000.0,
                               sizing=quant.SizingConfig(max_positions=2),
                               use_cache=True)
        self.assertEqual(set(out), set(quant.STRATEGIES))
        self.assertEqual(self.calls["screen"], 0, "缓存命中不该再打快照")
        self.assertEqual(self.calls["kline"], 0, "缓存命中不该再拉 K 线")
        for res in out.values():
            self.assertTrue(res["cached"])

    def test_partial_cache_only_collects_pending(self):
        """只有部分策略有缓存时，仍只采集一次（补齐缺失的那些）。"""
        self.qt.scan("momentum", cash=100000.0, equity=200000.0,
                     sizing=quant.SizingConfig(max_positions=2),
                     candidate_limit=30, use_cache=False)
        self.calls.update(screen=0, kline=0)
        out = self.qt.scan_all(cash=100000.0, equity=200000.0,
                               sizing=quant.SizingConfig(max_positions=2),
                               use_cache=True)
        self.assertTrue(out["momentum"]["cached"])
        self.assertFalse(out["quality"]["cached"])
        self.assertEqual(self.calls["screen"], 1, "补齐时仍应只采集一次")

    def test_single_scan_still_consistent_with_scan_all(self):
        """兼容性：单策略 `scan` 与 `scan_all` 取同一策略，结论必须一致。"""
        import astock_quant_tool as qt
        one = qt.scan("quality", cash=100000.0, equity=200000.0,
                      sizing=quant.SizingConfig(max_positions=2),
                      use_cache=False)
        qt.clear_cache()
        many = self._run()["quality"]
        self.assertEqual([s.code for s in one["ranked"]],
                         [s.code for s in many["ranked"]])
        self.assertEqual(one["candidates"], many["candidates"])

    def test_bad_strategy_rejected(self):
        import astock_quant_tool as qt
        with self.assertRaises(ValueError):
            qt.scan("nope", cash=1.0, equity=1.0, use_cache=False)


class TestQuantPanelTabs(unittest.TestCase):
    """右栏面板：v2.1.0 起是「持仓 / 选股」两个 tab，选股 tab 内做三策略对比。

    历史：此前是「持仓 / 动量 / 回归 / 价值」四个 tab。三 tab 的硬伤是
    面板只读、不能触发扫描，而设置页按钮一次只跑一套策略 → 另两个 tab
    永远是「尚未生成」。合并后少了两次 tab 切换，还能直接看出三套分歧。
    """

    def setUp(self):
        self.plugin = astock.AstockPlugin()
        import astock_quant_tool as qt
        qt.clear_cache()

    def test_two_panels_only(self):
        ids = [p["id"] for p in self.plugin.contributes["panels"]]
        self.assertEqual(ids, ["portfolio", "pick"],
                         "tab 应收敛为「持仓 / 选股」两个")

    def test_panel_ids_unique_and_titled(self):
        panels = self.plugin.contributes["panels"]
        ids = [p["id"] for p in panels]
        self.assertEqual(len(ids), len(set(ids)))
        for p in panels:
            self.assertTrue(p.get("title"), p["id"])

    def test_compare_panel_renders_without_cache(self):
        text = self.plugin.panel_content("pick", {})
        self.assertTrue(text.strip())
        self.assertIn("量化选股", text)
        self.assertIn("尚未生成", text)

    def test_legacy_strategy_panels_still_work(self):
        """旧 id（pick-momentum 等）保留可用——已安装用户的书签不失效。"""
        for key in quant.STRATEGIES:
            text = self.plugin.panel_content(f"pick-{key}", {})
            self.assertTrue(text.strip(), key)
            self.assertIn("量化选股", text)

    def test_compare_shows_all_three_strategies_from_cache(self):
        """三套都有缓存时，对比视图必须把三套都列出来（不是只显示默认那套）。"""
        import astock_quant_tool as qt

        class _A:
            def __init__(self, code, name, price=10.0, score=60.0, shares=100):
                self.code, self.name, self.price, self.score = code, name, price, score
                self.shares = shares

        import time as _t
        stamp = _t.time()          # 必须是"现在"，否则 cache_age 会算出天文数字
        for key in quant.STRATEGIES:
            qt._CACHE[qt._cache_key(key, "main", 30,
                                    quant.SizingConfig(), 1e5, 2e5, None)] = (
                stamp,
                {"strategy": key, "strategy_label": quant.STRATEGIES[key].label,
                 "allocations": [_A(f"60000{len(key)}", quant.STRATEGIES[key].label + "股")],
                 "skipped": [], "summary": {}, "scanned": 5000,
                 "candidates": 3, "generated_at": "2026-10-25 10:00:00",
                 "cache_age_sec": 5.0})
        text = self.plugin.panel_content("pick", {})
        for key in quant.STRATEGIES:
            self.assertIn(quant.STRATEGIES[key].label, text)

    def test_compare_flags_multi_strategy_overlap(self):
        """同一只票被多套选中时要显式标出来（这是对比视图的核心价值）。"""
        import astock_quant_tool as qt

        class _A:
            def __init__(self, code, name, price=10.0, score=60.0, shares=100):
                self.code, self.name, self.price, self.score = code, name, price, score
                self.shares = shares

        import time as _t
        stamp = _t.time()
        for key in quant.STRATEGIES:
            qt._CACHE[qt._cache_key(key, "main", 30,
                                    quant.SizingConfig(), 1e5, 2e5, None)] = (
                stamp,
                {"strategy": key, "strategy_label": quant.STRATEGIES[key].label,
                 "allocations": [_A("600000", "共识股")],
                 "skipped": [], "summary": {}, "scanned": 5000,
                 "candidates": 1, "generated_at": "2026-10-25 10:00:00",
                 "cache_age_sec": 5.0})
        text = self.plugin.panel_content("pick", {})
        self.assertIn("多套同时选中", text)
        self.assertIn("共识股", text)

    def test_unknown_panel_is_empty(self):
        self.assertEqual(self.plugin.panel_content("pick-nope", {}), "")
        self.assertEqual(self.plugin.panel_content("nope", {}), "")

    def test_pick_compare_arg_runs_all_three_once(self):
        """对话里 compare=true 必须一次采集出三套，且三套都出现在输出里。"""
        import asyncio
        from unittest.mock import patch

        class _App:
            workspace = "/tmp"
            config = {"astock-plugin": {"backtest_auto_grade": False}}

            def save_config(self, _u):
                pass

        pl = astock.AstockPlugin()
        pl._app = _App()
        fake = {k: {"strategy": k,
                    "strategy_label": quant.STRATEGIES[k].label,
                    "allocations": [], "skipped": [], "summary": {},
                    "scanned": 5000, "candidates": 0,
                    "generated_at": "2026-10-27 14:00:00",
                    "cache_age_sec": 1.0} for k in quant.STRATEGIES}

        async def _no_grade(_s, fresh=False):      # 推荐率会联网，这里屏蔽
            return ""

        with patch.object(pl, "_quant_scan_all", return_value=fake), \
                patch.object(pl, "_attach_grade", _no_grade):
            loop = asyncio.new_event_loop()
            try:
                out = loop.run_until_complete(pl._tool_pick({"compare": True}))
            finally:
                loop.close()
        for key in quant.STRATEGIES:
            self.assertIn(quant.STRATEGIES[key].label, out)
        self.assertIn("三策略对比", out)

    def test_pick_default_does_not_scan_three(self):
        """默认「帮我选股」仍只跑一套——不该悄悄把成本翻三倍。"""
        import asyncio
        from unittest.mock import patch

        class _App:
            workspace = "/tmp"
            config = {"astock-plugin": {"backtest_auto_grade": False}}

            def save_config(self, _u):
                pass

        pl = astock.AstockPlugin()
        pl._app = _App()
        called = {"all": 0}

        def boom(args=None):
            called["all"] += 1
            raise AssertionError("默认路径不该走三套")

        one = {"strategy": "momentum", "strategy_label": "趋势动量",
               "allocations": [], "skipped": [], "summary": {},
               "scanned": 5000, "candidates": 0,
               "generated_at": "2026-10-27 14:00:00", "cache_age_sec": 1.0}

        with patch.object(pl, "_quant_scan_all", boom), \
                patch.object(pl, "_quant_scan", return_value=one):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(pl._tool_pick({}))
            finally:
                loop.close()
        self.assertEqual(called["all"], 0)

    def test_panel_never_scans_network(self):
        """面板 5 秒刷新 × 30 只 K 线 = 打死接口。渲染路径必须零网络。"""
        import astock_data as d
        import astock_quant_tool as qt
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("面板渲染不应触发行情请求")

        orig_screen, orig_kline = d.screen, d.fetch_klines
        d.screen, d.fetch_klines = boom, boom
        qt.clear_cache()
        try:
            self.plugin.panel_content("pick", {})
            for key in quant.STRATEGIES:
                self.plugin.panel_content(f"pick-{key}", {})
        finally:
            d.screen, d.fetch_klines = orig_screen, orig_kline
        self.assertEqual(called["n"], 0)


class TestQuantSettingsAndAction(unittest.TestCase):
    """设置页动作：按钮要可用，且"钱不够"不能报成错误。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="astock-pick-")
        self.plugin = astock.AstockPlugin()

        class _App:
            workspace = self.tmp
            config = {"astock-plugin": {
                "ledger_path": os.path.join(self.tmp, "l.sqlite"),
                "pick_strategy": "momentum",
            }}

            def save_config(self, _u):
                pass

        self.plugin._app = _App()

    def test_default_strategy_read_from_config(self):
        self.assertEqual(self.plugin._default_strategy(), "momentum")

    def test_bad_strategy_falls_back(self):
        self.plugin._app.config["astock-plugin"]["pick_strategy"] = "nonsense"
        self.assertEqual(self.plugin._default_strategy(),
                         quant.DEFAULT_STRATEGY)

    def test_sizing_cfg_clamps_absurd_values(self):
        cfg = self.plugin._sizing_cfg({"max_positions": 999,
                                       "cash_usage_pct": 500})
        self.assertLessEqual(cfg.max_positions, 20)
        self.assertLessEqual(cfg.cash_usage_pct, 100.0)

    def test_action_reports_error_gracefully(self):
        """接口失败要给人话，不能抛栈。

        注意打桩的是 `scan_all`（v2.1.0 起设置页按钮一次跑三套策略），
        不再是 `scan`——打错目标会让测试真的去联网，既慢又不稳。
        """
        import astock_quant_tool as qt
        orig = qt.scan_all

        def boom(*a, **k):
            raise RuntimeError("接口炸了")

        self.plugin._app.config["astock-plugin"]["backtest_auto_grade"] = False
        qt.scan_all = boom
        try:
            res = self.plugin.settings_action("pick_run", self.plugin._app.config)
        finally:
            qt.scan_all = orig
        self.assertFalse(res["ok"])
        self.assertIn("接口炸了", res["message"])
        self.assertIn("限流", res["message"])

    def test_action_reports_insufficient_cash_as_ok(self):
        """钱不够买一手是**资金约束**，不是错误——ok 必须为 True。"""
        import astock_quant_tool as qt
        orig = qt.scan_all

        def fake(**k):
            out = {}
            for key in quant.STRATEGIES:
                out[key] = {
                    "strategy": key, "strategy_label": "趋势动量",
                    "allocations": [], "skipped": [
                        quant.Allocation(code="600001", name="甲", price=5000.0,
                                         score=50.0, skipped=True,
                                         skip_reason="预算 100 元不足一手")],
                    "summary": {"cash": 100.0, "budget": 80.0},
                    "scanned": 5000, "candidates": 3}
            return out

        self.plugin._app.config["astock-plugin"]["backtest_auto_grade"] = False
        qt.scan_all = fake
        try:
            res = self.plugin.settings_action("pick_run", self.plugin._app.config)
        finally:
            qt.scan_all = orig
        self.assertTrue(res["ok"], res)
        self.assertIn("资金不足", res["message"])

    def test_pick_run_covers_all_strategies(self):
        """点一次「立即跑一轮选股」必须把三套策略都算出来并各自汇报。

        这是 v2.1.0 的核心体验修正：此前只跑设置页选中的那一套，
        另外两个 tab 永远是「尚未生成结果」，要来回改设置再点三次。
        """
        import astock_quant_tool as qt
        orig = qt.scan_all
        got = {}

        def fake(**k):
            out = {}
            for key in quant.STRATEGIES:
                out[key] = {
                    "strategy": key,
                    "strategy_label": quant.STRATEGIES[key].label,
                    "allocations": [], "skipped": [],
                    "summary": {"cash": 1e5, "budget": 8e4},
                    "scanned": 5000, "candidates": 0}
            got.update(out)
            return out

        # 关掉「自动附推荐率」：那是 25 只 K 线的回测，属于另一个测试的范围，
        # 开着会让本测试真的联网（慢且不稳）。
        self.plugin._app.config["astock-plugin"]["backtest_auto_grade"] = False
        qt.scan_all = fake
        try:
            res = self.plugin.settings_action("pick_run", self.plugin._app.config)
        finally:
            qt.scan_all = orig
        self.assertTrue(res["ok"], res)
        self.assertEqual(set(got), set(quant.STRATEGIES))
        for key in quant.STRATEGIES:
            self.assertIn(quant.STRATEGIES[key].label, res["message"])


# ---------------------------------------------------------------- 回测与推荐率（v1.9.0）


def _bt_bars(trend_fn, n=300, start="2024-01-01"):
    """构造回测用日 K（含 open/close）。trend_fn(i) 返回当日涨幅因子。"""
    import datetime as _dt
    bars = []
    p = 10.0
    d = _dt.date.fromisoformat(start)
    for i in range(n):
        p *= trend_fn(i)
        d += _dt.timedelta(days=1)
        bars.append({"date": d.isoformat(), "open": p, "close": p,
                     "high": p * 1.01, "low": p * 0.99, "volume_lot": 10000.0})
    return bars


def _bt_flat_bench(n=300):
    return [{"date": b["date"], "open": 10.0, "close": 10.0}
            for b in _bt_bars(lambda i: 1.0, n)]


class TestBacktestNoLookahead(unittest.TestCase):
    """防未来函数 —— 回测可信与否的分水岭。"""

    def test_early_periods_do_not_see_late_boom(self):
        """构造「前 250 天横盘、最后 50 天暴涨」的股票。

        若回测正确：早期调仓期它不该被选中（当时它还不涨），
        早期收益应接近 0（仅扣费）。
        若用了未来数据：早期就会选中它并赚大钱 —— 这是最常见的自欺方式。
        """
        boom = {"code": "A", "name": "晚涨",
                "bars": _bt_bars(lambda i: 1.03 if i >= 250 else 1.0)}
        flat1 = {"code": "B", "name": "横1", "bars": _bt_bars(lambda i: 1.0)}
        flat2 = {"code": "C", "name": "横2", "bars": _bt_bars(lambda i: 1.0)}
        cfg = backtest_mod.BacktestConfig(rebalance_days=10, top_k=1,
                                         hold_days=10)
        r = backtest_mod.run_backtest("momentum", [boom, flat1, flat2],
                                      _bt_flat_bench(), cfg=cfg)
        early = r.periods[:5]
        self.assertTrue(early)
        for p in early:
            self.assertLess(p.net_return_pct, 1.0,
                            f"第 {p.date} 期收益 {p.net_return_pct}% 异常高——"
                            f"疑似提前用到了未来数据")

    def test_late_periods_capture_the_boom(self):
        """暴涨段应被捕捉到（否则说明因子/回测根本没生效）。"""
        boom = {"code": "A", "name": "晚涨",
                "bars": _bt_bars(lambda i: 1.03 if i >= 250 else 1.0)}
        flat1 = {"code": "B", "name": "横1", "bars": _bt_bars(lambda i: 1.0)}
        cfg = backtest_mod.BacktestConfig(rebalance_days=10, top_k=1,
                                         hold_days=10)
        r = backtest_mod.run_backtest("momentum", [boom, flat1],
                                      _bt_flat_bench(), cfg=cfg)
        self.assertGreater(max(p.net_return_pct for p in r.periods), 5.0,
                           "暴涨段未被捕捉，因子可能失效")

    def test_entry_is_next_day_open_not_signal_close(self):
        """买入价必须是信号日**次日开盘**——当日收盘时你还没下单。"""
        bars = _bt_bars(lambda i: 1.005)
        r = backtest_mod.run_backtest(
            "momentum",
            [{"code": "A", "name": "a", "bars": bars},
             {"code": "B", "name": "b", "bars": list(bars)}],
            _bt_flat_bench(), cfg=backtest_mod.BacktestConfig(top_k=1))
        for p in r.periods:
            self.assertNotEqual(p.date, p.entry_date,
                                "信号日与买入日相同 → 用了当日价买入")


class TestBacktestMetrics(unittest.TestCase):
    """指标口径：每个数字都要能被手算核对。"""

    def setUp(self):
        up = {"code": "A", "name": "涨", "bars": _bt_bars(lambda i: 1.006)}
        down = {"code": "B", "name": "跌", "bars": _bt_bars(lambda i: 0.997)}
        self.r = backtest_mod.run_backtest(
            "momentum", [up, down], _bt_flat_bench(),
            cfg=backtest_mod.BacktestConfig(rebalance_days=10, top_k=1,
                                            hold_days=10))

    def test_periods_exist(self):
        self.assertGreater(self.r.period_count, 10)

    def test_win_rate_matches_periods(self):
        """胜率必须等于「跑赢期数 / 总期数」，不能另算一套。"""
        beats = sum(1 for p in self.r.periods if p.beat)
        self.assertAlmostEqual(self.r.win_rate,
                               round(beats / self.r.period_count * 100, 1),
                               places=1)

    def test_excess_equals_net_minus_bench(self):
        for p in self.r.periods:
            self.assertAlmostEqual(p.excess_pct,
                                   round(p.net_return_pct - p.benchmark_pct, 2),
                                   places=1)

    def test_beat_flag_consistent(self):
        for p in self.r.periods:
            self.assertEqual(p.beat, p.excess_pct > 0)

    def test_costs_reduce_returns(self):
        """扣费后收益必须低于毛收益——否则费用没扣。"""
        for p in self.r.periods:
            self.assertLess(p.net_return_pct, p.gross_return_pct)

    def test_max_drawdown_is_non_positive(self):
        self.assertLessEqual(self.r.max_drawdown_pct, 0.0)

    def test_as_dict_serializable(self):
        import json
        json.dumps(self.r.as_dict(), ensure_ascii=False)

    def test_insufficient_universe_raises(self):
        with self.assertRaises(ValueError):
            backtest_mod.run_backtest(
                "momentum", [{"code": "A", "name": "a",
                              "bars": _bt_bars(lambda i: 1.0, n=30)}],
                _bt_flat_bench(), cfg=backtest_mod.BacktestConfig(top_k=3))

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            backtest_mod.run_backtest("nope", [], [],
                                      cfg=backtest_mod.BacktestConfig())


class TestRecommendationGrading(unittest.TestCase):
    """推荐率分档：宁可说"测不准"，也不给好看的假结论。"""

    def _mk(self, win_rate, samples):
        r = backtest_mod.BacktestResult(strategy="momentum",
                                        strategy_label="趋势动量")
        beats = int(round((win_rate or 0) / 100.0 * samples))
        for i in range(samples):
            r.periods.append(backtest_mod.PeriodResult(
                date=f"2024-01-{i + 1:02d}", entry_date=f"2024-02-{i + 1:02d}",
                picks=["A"], names=["甲"], gross_return_pct=1.0,
                net_return_pct=1.0, benchmark_pct=0.0,
                excess_pct=(0.5 if i < beats else -0.5),
                beat=i < beats))
        return r

    def test_high_win_rate_with_enough_samples_is_a(self):
        rec = backtest_mod.grade_recommendation(self._mk(70.0, 20))
        self.assertEqual(rec.grade, "A")
        self.assertEqual(rec.label, "推荐")

    def test_mid_win_rate_is_b(self):
        rec = backtest_mod.grade_recommendation(self._mk(55.0, 20))
        self.assertEqual(rec.grade, "B")

    def test_low_win_rate_is_c_not_a(self):
        rec = backtest_mod.grade_recommendation(self._mk(40.0, 20))
        self.assertEqual(rec.grade, "C")

    def test_bad_win_rate_is_d(self):
        rec = backtest_mod.grade_recommendation(self._mk(20.0, 20))
        self.assertEqual(rec.grade, "D")

    def test_few_samples_never_gets_a_grade(self):
        """样本极少时哪怕胜率 100% 也不能给 A —— 2 期样本毫无统计意义。

        这是本功能最重要的防误导规则：不做这个降级，
        用户会看到「胜率 100% · 推荐」然后重仓。
        """
        rec = backtest_mod.grade_recommendation(self._mk(100.0, 3))
        self.assertEqual(rec.grade, "D")
        self.assertEqual(rec.label, "样本不足")
        self.assertIn("不足以支撑", rec.reason)

    def test_confidence_scales_with_samples(self):
        self.assertEqual(
            backtest_mod.grade_recommendation(self._mk(60.0, 30)).confidence,
            "较充分")
        # 阈值定义：>=24 较充分 / >=12 一般 / >=5 偏少 / 其余过少
        self.assertEqual(
            backtest_mod.grade_recommendation(self._mk(60.0, 12)).confidence,
            "一般")
        self.assertEqual(
            backtest_mod.grade_recommendation(self._mk(60.0, 5)).confidence,
            "偏少")
        self.assertEqual(
            backtest_mod.grade_recommendation(self._mk(60.0, 2)).confidence,
            "过少")

    def test_no_samples_returns_d(self):
        r = backtest_mod.BacktestResult(strategy="momentum", strategy_label="x")
        rec = backtest_mod.grade_recommendation(r)
        self.assertEqual(rec.grade, "D")
        self.assertIsNone(rec.win_rate)

    def test_caveats_always_mention_survivorship(self):
        """幸存者偏差必须每次都提——它是最大的系统性高估来源。"""
        rec = backtest_mod.grade_recommendation(self._mk(70.0, 20))
        self.assertTrue(any("幸存者偏差" in c for c in rec.caveats))

    def test_incomplete_strategy_flagged(self):
        r = self._mk(70.0, 20)
        r.unavailable_factors = ["pe_inv", "pb_inv"]
        rec = backtest_mod.grade_recommendation(r)
        self.assertFalse(r.complete)
        self.assertTrue(any("不完整" in c for c in rec.caveats))


class TestBacktestRendering(unittest.TestCase):
    """渲染：结论先说、限制必写、措辞不能把策略胜率说成个股概率。"""

    def setUp(self):
        up = {"code": "A", "name": "涨", "bars": _bt_bars(lambda i: 1.006)}
        mid = {"code": "B", "name": "平", "bars": _bt_bars(lambda i: 1.0005)}
        self.r = backtest_mod.run_backtest(
            "momentum", [up, mid], _bt_flat_bench(),
            cfg=backtest_mod.BacktestConfig(rebalance_days=10, top_k=1))
        self.rec = backtest_mod.grade_recommendation(self.r)

    def test_report_has_win_rate_and_drawdown(self):
        text = backtest_mod.render_backtest(self.r, self.rec)
        self.assertIn("推荐率", text)
        self.assertIn("最大回撤", text)
        self.assertIn("样本", text)

    def test_report_mentions_survivorship(self):
        text = backtest_mod.render_backtest(self.r, self.rec)
        self.assertIn("幸存者偏差", text)

    def test_report_explains_it_is_strategy_not_stock(self):
        text = backtest_mod.render_backtest(self.r, self.rec)
        self.assertIn("不是", text)
        self.assertIn("策略", text)

    def test_detail_periods_rendered(self):
        text = backtest_mod.render_backtest(self.r, self.rec, detail_periods=3)
        self.assertIn("逐期明细", text)


class TestBacktestPanelCards(unittest.TestCase):
    """右栏卡片看板（需求：像炒股软件那样）。"""

    def _picks(self):
        return [quant.Allocation(code="600001", name="甲", price=10.0,
                                 score=80.0, shares=1000, amount=10000.0,
                                 fee=5.0, stop_loss=9.2, take_profit=11.5,
                                 weight_pct=10.0)]

    def test_cards_are_valid_json_lite_tree(self):
        import json
        rec = backtest_mod.grade_recommendation(
            backtest_mod.BacktestResult(strategy="momentum",
                                        strategy_label="趋势动量"))
        raw = backtest_tool.build_recommendation_cards(
            self._picks(), None, "趋势动量")
        payload = json.loads(raw)
        self.assertEqual(payload["type"], "lite-tree")
        self.assertEqual(payload["total"], 1)
        card = payload["trees"][0]
        self.assertIn("甲", card["title"])
        self.assertIn("1000 股", card["context"])
        self.assertTrue(card["options"])

    def test_win_rate_rendered_as_progress(self):
        """推荐率必须进 prob 通道（宿主唯一会渲染成进度条的字段）。"""
        import json
        r = backtest_mod.BacktestResult(strategy="momentum",
                                        strategy_label="趋势动量")
        for i in range(20):
            r.periods.append(backtest_mod.PeriodResult(
                date=f"2024-01-{i + 1:02d}", entry_date="2024-02-01",
                picks=["A"], names=["甲"], gross_return_pct=1.0,
                net_return_pct=1.0, benchmark_pct=0.0,
                excess_pct=(0.5 if i < 13 else -0.5), beat=i < 13))
        rec = backtest_mod.grade_recommendation(r)
        payload = json.loads(backtest_tool.build_recommendation_cards(
            self._picks(), rec, "趋势动量"))
        probs = [o["prob"] for o in payload["trees"][0]["options"]]
        self.assertTrue(any(abs(p - 0.65) < 0.02 for p in probs),
                        f"推荐率未体现在进度条上: {probs}")

    def test_status_carries_grade(self):
        import json
        r = backtest_mod.BacktestResult(strategy="momentum",
                                        strategy_label="趋势动量")
        for i in range(20):
            r.periods.append(backtest_mod.PeriodResult(
                date=f"2024-01-{i + 1:02d}", entry_date="2024-02-01",
                picks=["A"], names=["甲"], gross_return_pct=1.0,
                net_return_pct=1.0, benchmark_pct=0.0,
                excess_pct=(0.5 if i < 15 else -0.5), beat=i < 15))
        rec = backtest_mod.grade_recommendation(r)
        payload = json.loads(backtest_tool.build_recommendation_cards(
            self._picks(), rec, "趋势动量"))
        self.assertTrue(payload["status"]["chips"])


class TestBacktestSurface(unittest.TestCase):
    """工具与设置面：声明与实际一致。"""

    def setUp(self):
        self.plugin = astock.AstockPlugin()

    def test_backtest_tool_registered(self):
        names = {t.name for t in self.plugin.get_tools()}
        self.assertIn("astock_backtest", names)

    def test_settings_keys_exist(self):
        keys = {s["key"] for s in self.plugin.contributes["settings"]}
        for k in ("pick_panel_style", "backtest_run", "backtest_auto_grade"):
            self.assertIn(k, keys)

    def test_backtest_command_exists(self):
        cmds = {c["name"] for c in self.plugin.contributes["commands"]}
        self.assertIn("astock-backtest", cmds)

    def test_unknown_strategy_reports_error(self):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(
                self.plugin.execute("astock_backtest", {"strategy": "nope"}))
        finally:
            loop.close()
        self.assertIn("未知策略", out)


# ---------------------------------------------------------------- 本地存档（v2.0.0）


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="astock-arc-")
        self.arc = archive_mod.Archive(os.path.join(self.tmp, "arc.sqlite"))

    @staticmethod
    def _bars(prefix="2024-01-", n=5, base=10.0):
        return [{"date": f"{prefix}{i:02d}", "open": base + i,
                 "close": base + i + 0.5, "high": base + i + 1,
                 "low": base + i - 1, "volume_lot": 1000,
                 "amount_yuan": 1e7} for i in range(1, n + 1)]


class TestArchiveRawPriority(ArchiveTestBase):
    """不复权优先 —— 存档可信度的地基。"""

    def test_save_and_load(self):
        info = self.arc.save_bars("600000", self._bars(), adjust="raw")
        self.assertEqual(info["saved"], 5)
        bars = self.arc.load_bars("600000", adjust="raw")
        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0]["date"], "2024-01-01")



    def test_qfq_does_not_affect_raw(self):
        """两个口径**各自独立**：写 qfq 不得改变 raw 的值。

        回归背景：前复权价会随每次分红重算整段历史。若 qfq 能覆盖 raw，
        存档里同一天的价格会随分红变动，跨期拼接后自相矛盾——
        而且数据看着还是「有」，属于最难发现的静默错误。
        """
        self.arc.save_bars("600000", self._bars(base=10.0), adjust="raw")
        self.arc.save_bars("600000", self._bars(base=99.0), adjust="qfq")
        raw = self.arc.load_bars("600000", adjust="raw")
        qfq = self.arc.load_bars("600000", adjust="qfq")
        self.assertEqual(raw[0]["close"], 11.5, "raw 被 qfq 污染了")
        self.assertEqual(qfq[0]["close"], 100.5, "qfq 应独立保存")
        self.assertEqual(len(raw), 5)
        self.assertEqual(len(qfq), 5)

    def test_raw_and_qfq_coexist_per_date(self):
        """同一日期两个口径并存：这是主键设计的意图，不是 bug。"""
        self.arc.save_bars("600000", self._bars(n=3, base=10.0), adjust="raw")
        self.arc.save_bars("600000", self._bars(n=3, base=99.0), adjust="qfq")
        self.assertEqual(len(self.arc.load_bars("600000", adjust="raw")), 3)
        self.assertEqual(len(self.arc.load_bars("600000", adjust="qfq")), 3)



    def test_raw_write_after_qfq_is_independent(self):
        """先存 qfq、后存 raw → raw 是独立一份，不受 qfq 影响。"""
        self.arc.save_bars("600000", self._bars(base=99.0), adjust="qfq")
        self.arc.save_bars("600000", self._bars(base=10.0), adjust="raw")
        raw = self.arc.load_bars("600000", adjust="raw")
        self.assertEqual(raw[0]["close"], 11.5)
        qfq = self.arc.load_bars("600000", adjust="qfq")
        self.assertEqual(qfq[0]["close"], 100.5)

    def test_same_adjust_is_idempotent(self):
        """同口径重复写入应幂等（不产生重复行）。"""
        self.arc.save_bars("600000", self._bars(), adjust="raw")
        self.arc.save_bars("600000", self._bars(), adjust="raw")
        self.assertEqual(len(self.arc.load_bars("600000", adjust="raw")), 5)

    def test_bad_rows_skipped(self):
        bad = [{"date": "", "close": 10}, {"date": "2024-01-02", "close": 0},
               {"date": "2024-01-03"}] + self._bars(n=2)
        info = self.arc.save_bars("600000", bad, adjust="raw")
        self.assertEqual(info["saved"], 2)

    def test_adjust_isolation(self):
        """两个口径各自独立，条数互不影响。"""
        self.arc.save_bars("600000", self._bars(n=3), adjust="raw")
        self.arc.save_bars("600000", self._bars(n=5, base=50), adjust="qfq")
        self.assertEqual(len(self.arc.load_bars("600000", adjust="raw")), 3)
        self.assertEqual(len(self.arc.load_bars("600000", adjust="qfq")), 5)


class TestArchiveQuery(ArchiveTestBase):
    def test_date_range_filter(self):
        self.arc.save_bars("600000", self._bars(n=10), adjust="raw")
        got = self.arc.load_bars("600000", start="2024-01-03",
                                 end="2024-01-05", adjust="raw")
        self.assertEqual([b["date"] for b in got],
                         ["2024-01-03", "2024-01-04", "2024-01-05"])

    def test_limit_returns_latest_in_ascending_order(self):
        """limit 取**最近** N 根，但返回仍应升序（调用方按时间正序用）。"""
        self.arc.save_bars("600000", self._bars(n=10), adjust="raw")
        got = self.arc.load_bars("600000", limit=3, adjust="raw")
        self.assertEqual([b["date"] for b in got],
                         ["2024-01-08", "2024-01-09", "2024-01-10"])

    def test_stats(self):
        self.arc.save_bars("600000", self._bars(n=5), adjust="raw")
        self.arc.save_bars("000001", self._bars(n=3), adjust="raw")
        st = self.arc.stats()
        self.assertEqual(st["codes"], 2)
        self.assertEqual(st["bars"], 8)
        self.assertTrue(st["first_date"])
        self.assertIn("B", st["size_human"])

    def test_empty_stats(self):
        st = self.arc.stats()
        self.assertEqual(st["codes"], 0)
        self.assertEqual(st["bars"], 0)

    def test_coverage(self):
        self.arc.save_bars("600000", self._bars(n=5), adjust="raw")
        cov = self.arc.coverage(["600000", "999999"])
        self.assertEqual(cov["600000"]["bar_count"], 5)
        self.assertEqual(cov["999999"]["bar_count"], 0)


class TestArchivePrune(ArchiveTestBase):
    def test_refuses_to_wipe_everything(self):
        """无条件的 prune 必须拒绝——几年存档不能被误点清空。"""
        self.arc.save_bars("600000", self._bars(), adjust="raw")
        with self.assertRaises(ValueError):
            self.arc.prune()
        self.assertEqual(len(self.arc.load_bars("600000")), 5)

    def test_prune_by_date(self):
        self.arc.save_bars("600000", self._bars(n=10), adjust="raw")
        res = self.arc.prune(before="2024-01-06")
        self.assertEqual(res["deleted"], 5)
        self.assertEqual(len(self.arc.load_bars("600000")), 5)

    def test_prune_by_code(self):
        self.arc.save_bars("600000", self._bars(), adjust="raw")
        self.arc.save_bars("000001", self._bars(), adjust="raw")
        self.arc.prune(codes=["600000"])
        self.assertEqual(len(self.arc.load_bars("600000")), 0)
        self.assertEqual(len(self.arc.load_bars("000001")), 5)


class TestArchiveMerge(unittest.TestCase):
    """存档与在线数据的合并：按日期去重、存档优先。"""

    def test_merged_prefers_archive(self):
        archived = [{"date": "2024-01-01", "close": 10.0}]
        online = [{"date": "2024-01-01", "close": 99.0},
                  {"date": "2024-01-02", "close": 11.0}]
        merged = archive_mod.merged_bars_dicts(archived, online)
        self.assertEqual(len(merged), 2, "同一天不应出现两行")
        self.assertEqual(merged[0]["close"], 10.0, "存档应优先")

    def test_merged_sorted(self):
        archived = [{"date": "2024-01-03", "close": 1.0}]
        online = [{"date": "2024-01-01", "close": 2.0},
                  {"date": "2024-01-02", "close": 3.0}]
        dates = [b["date"] for b in
                 archive_mod.merged_bars_dicts(archived, online)]
        self.assertEqual(dates, ["2024-01-01", "2024-01-02", "2024-01-03"])

    def test_merged_handles_empty(self):
        self.assertEqual(archive_mod.merged_bars_dicts([], []), [])
        one = [{"date": "2024-01-01", "close": 1.0}]
        self.assertEqual(len(archive_mod.merged_bars_dicts(one, [])), 1)
        self.assertEqual(len(archive_mod.merged_bars_dicts([], one)), 1)


class TestArchiveUpdate(ArchiveTestBase):
    def test_update_collects_failures_without_aborting(self):
        """单只失败不应中断整批（接口限流是常态）。"""
        def fetch(code, _adj, _lim):
            if code == "BAD":
                raise RuntimeError("模拟接口失败")
            return self._bars()

        res = archive_mod.update_from_provider(
            self.arc, ["600000", "BAD", "000001"], fetch)
        self.assertEqual(len(res["updated"]), 2)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["code"], "BAD")

    def test_update_empty_result_is_failure(self):
        res = archive_mod.update_from_provider(
            self.arc, ["600000"], lambda c, a, l: [])
        self.assertEqual(len(res["failed"]), 1)

    def test_update_writes_raw_only(self):
        archive_mod.update_from_provider(
            self.arc, ["600000"], lambda c, a, l: self._bars(), adjust="raw")
        self.assertEqual(len(self.arc.load_bars("600000", adjust="raw")), 5)
        self.assertEqual(len(self.arc.load_bars("600000", adjust="qfq")), 0)


class TestArchiveSurface(unittest.TestCase):
    def setUp(self):
        self.plugin = astock.AstockPlugin()

    def test_tool_registered(self):
        names = {t.name for t in self.plugin.get_tools()}
        self.assertIn("astock_archive", names)

    def test_settings_keys(self):
        keys = {s["key"] for s in self.plugin.contributes["settings"]}
        for k in ("archive_path", "archive_auto_update", "archive_run"):
            self.assertIn(k, keys)

    def test_archive_path_follows_ledger_dir(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="astock-arcpath-")

        class _App:
            workspace = tmp
            config = {"astock-plugin": {
                "ledger_path": os.path.join(tmp, "l.sqlite")}}

            def save_config(self, _u):
                pass

        self.plugin._app = _App()
        path = self.plugin._archive_path()
        self.assertEqual(os.path.dirname(path), tmp)
        self.assertTrue(path.endswith("archive.sqlite"))

    def test_custom_archive_path_respected(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="astock-arcpath2-")
        target = os.path.join(tmp, "custom", "a.sqlite")

        class _App:
            workspace = tmp
            config = {"astock-plugin": {"archive_path": target}}

            def save_config(self, _u):
                pass

        self.plugin._app = _App()
        self.assertEqual(self.plugin._archive_path(), os.path.abspath(target))


class TestArchiveBrandNew(unittest.TestCase):
    """全新存档的冷启动：不该崩，且状态要说清"还没有数据"。"""

    def test_status_on_empty_archive(self):
        import asyncio
        import tempfile
        tmp = tempfile.mkdtemp(prefix="astock-arcnew-")
        plugin = astock.AstockPlugin()

        class _App:
            workspace = tmp
            config = {"astock-plugin": {
                "ledger_path": os.path.join(tmp, "l.sqlite"),
                "archive_path": os.path.join(tmp, "a.sqlite")}}

            def save_config(self, _u):
                pass

        plugin._app = _App()
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(
                plugin.execute("astock_archive", {"action": "status"}))
        finally:
            loop.close()
        self.assertIn("存档", out)
        self.assertIn("存档为空", out)

    def test_prune_without_args_is_rejected(self):
        import asyncio
        import tempfile
        tmp = tempfile.mkdtemp(prefix="astock-arcnew2-")
        plugin = astock.AstockPlugin()

        class _App:
            workspace = tmp
            config = {"astock-plugin": {
                "ledger_path": os.path.join(tmp, "l.sqlite"),
                "archive_path": os.path.join(tmp, "a.sqlite")}}

            def save_config(self, _u):
                pass

        plugin._app = _App()
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(
                plugin.execute("astock_archive", {"action": "prune"}))
        finally:
            loop.close()
        self.assertIn("必须指定", out)


# ---------------------------------------------------------------- 操作指示（v2.0.0）


class _Cfg:
    stop_loss_pct = 8.0
    take_profit_pct = 15.0
    position_cap_pct = 25.0


class _Alloc:
    """测试用 Allocation 替身（避免依赖 quant 内部字段）。"""

    def __init__(self, code, name, price, shares, score=60.0,
                 amount=10000.0, fee=5.0, stop_loss=8.0, take_profit=11.0,
                 skipped=False, weight_pct=10.0):
        self.code, self.name, self.price, self.shares = code, name, price, shares
        self.score, self.amount, self.fee = score, amount, fee
        self.stop_loss, self.take_profit = stop_loss, take_profit
        self.skipped, self.weight_pct = skipped, weight_pct


class TestActionSells(unittest.TestCase):
    """卖出判定：只依据明确规则，且必须尊重 T+1。"""

    def _pos(self, code="600000", shares=2000, avail=2000, cost=10.0):
        return {"code": code, "name": code, "shares": shares,
                "available_today": avail, "cost_price": cost}

    def test_stop_loss_triggers_full_sell(self):
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 9.0}}      # -10% 破 -8% 止损线
        sells = astock_actions.decide_sells(pos, quotes, [], _Cfg())
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].shares, 2000)
        self.assertEqual(sells[0].urgency, "urgent")

    def test_take_profit_sells_half(self):
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 12.0}}     # +20% 过 +15% 止盈线
        sells = astock_actions.decide_sells(pos, quotes, [], _Cfg())
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].shares, 1000, "应减半锁利")

    def test_t1_locked_never_sells_more_than_available(self):
        """T+1 锁定的股份不能出现在卖出指示里——那是废单。"""
        pos = [self._pos(shares=2000, avail=0, cost=10.0)]
        quotes = {"600000": {"price": 8.0}}      # 触发止损
        sells = astock_actions.decide_sells(pos, quotes, [], _Cfg())
        self.assertEqual(sells, [], "今日可卖为 0 时不该给卖出指示")

    def test_partial_t1_limited(self):
        pos = [self._pos(shares=2000, avail=600, cost=10.0)]
        quotes = {"600000": {"price": 8.0}}
        sells = astock_actions.decide_sells(pos, quotes, [], _Cfg())
        self.assertEqual(len(sells), 1)
        self.assertLessEqual(sells[0].shares, 600)

    def test_no_trigger_no_sell(self):
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 10.2}}     # 小幅浮盈，不触发任何规则
        sells = astock_actions.decide_sells(pos, quotes, ["600000"], _Cfg())
        self.assertEqual(sells, [])

    def test_switch_out_when_not_picked_and_losing(self):
        """不在选股名单 + 浮亏 → 建议换仓（腾资金给更好的标的）。"""
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 9.7}}      # -3%，未到止损线
        sells = astock_actions.decide_sells(pos, quotes, ["600519"], _Cfg())
        self.assertEqual(len(sells), 1)
        self.assertIn("换仓", sells[0].reason)

    def test_picked_and_losing_is_held(self):
        """在选股名单里即使浮亏也不换仓（策略仍然看好它）。"""
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 9.7}}
        sells = astock_actions.decide_sells(pos, quotes, ["600000"], _Cfg())
        self.assertEqual(sells, [])

    def test_position_cap_reduces(self):
        pos = [self._pos(shares=10000, cost=10.0)]
        quotes = {"600000": {"price": 10.0}}
        sells = astock_actions.decide_sells(
            pos, quotes, ["600000"], _Cfg(), total_market_value=200000.0)
        self.assertTrue(sells)
        self.assertIn("超上限", sells[0].reason)

    def test_zero_price_skipped(self):
        pos = [self._pos()]
        sells = astock_actions.decide_sells(pos, {"600000": {"price": 0}}, [], _Cfg())
        self.assertEqual(sells, [])

    def test_missing_quote_skipped(self):
        sells = astock_actions.decide_sells([self._pos()], {}, [], _Cfg())
        self.assertEqual(sells, [])


class TestActionPlan(unittest.TestCase):
    """资金闭环：cash_after 必须等于 现金 + 卖出净额 - 买入成本。"""

    def _pos(self, code="600000", shares=2000, avail=2000, cost=10.0):
        return {"code": code, "name": code, "shares": shares,
                "available_today": avail, "cost_price": cost}

    def test_cash_identity_holds(self):
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 8.0}}     # 止损
        plan = astock_actions.build_action_plan(
            pos, quotes, [_Alloc("600519", "甲", 100.0, 100, amount=10000.0)],
            cash=5000.0, cfg=_Cfg())
        expect = plan.cash_before + plan.sell_proceeds - plan.buy_cost
        self.assertAlmostEqual(plan.cash_after, expect, places=2)

    def test_sell_before_buy_note_added(self):
        pos = [self._pos(cost=10.0)]
        quotes = {"600000": {"price": 8.0}}
        plan = astock_actions.build_action_plan(
            pos, quotes, [_Alloc("600519", "甲", 100.0, 100)], cash=1000.0,
            cfg=_Cfg())
        self.assertTrue(any("先卖出、再买入" in n for n in plan.notes))

    def test_insufficient_funds_flagged(self):
        """买入额超过「现金+卖出」时必须明确告警，不能默默放过。"""
        plan = astock_actions.build_action_plan(
            [], {}, [_Alloc("600519", "甲", 100.0, 1000, amount=100000.0)],
            cash=1000.0, cfg=_Cfg())
        self.assertTrue(plan.blocked)
        self.assertIn("超过可用资金", " ".join(plan.notes))

    def test_nothing_to_do(self):
        plan = astock_actions.build_action_plan([], {}, [], cash=1000.0,
                                               cfg=_Cfg())
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.sells, [])
        self.assertEqual(plan.buys, [])

    def test_t1_lock_noted(self):
        pos = [{"code": "600000", "name": "甲", "shares": 1000,
                "available_today": 0, "cost_price": 10.0}]
        plan = astock_actions.build_action_plan(
            pos, {"600000": {"price": 11.0}}, [], cash=1000.0, cfg=_Cfg())
        self.assertTrue(any("T+1" in n for n in plan.notes))

    def test_never_claims_to_trade(self):
        """红线：输出必须明确"不连接券商、不下单"。"""
        plan = astock_actions.build_action_plan(
            [], {"600000": {"price": 10.0}},
            [_Alloc("600519", "甲", 100.0, 100)], cash=50000.0, cfg=_Cfg())
        text = astock_actions.render_action_plan(plan)
        self.assertIn("不连接", text)
        self.assertIn("不下单", text)

    def test_skipped_allocation_excluded(self):
        plan = astock_actions.build_action_plan(
            [], {}, [_Alloc("600519", "甲", 100.0, 0, skipped=True)],
            cash=50000.0, cfg=_Cfg())
        self.assertEqual(plan.buys, [])

    def test_render_has_two_steps(self):
        pos = [{"code": "600000", "name": "甲", "shares": 2000,
                "available_today": 2000, "cost_price": 10.0}]
        plan = astock_actions.build_action_plan(
            pos, {"600000": {"price": 8.0}},
            [_Alloc("600519", "乙", 100.0, 100)], cash=50000.0, cfg=_Cfg())
        text = astock_actions.render_action_plan(plan)
        self.assertIn("第一步：卖出", text)
        self.assertIn("第二步：买入", text)


class TestActionSurface(unittest.TestCase):
    def setUp(self):
        self.plugin = astock.AstockPlugin()

    def test_tool_registered(self):
        names = {t.name for t in self.plugin.get_tools()}
        self.assertIn("astock_act", names)

    def test_command_registered(self):
        cmds = {c["name"] for c in self.plugin.contributes["commands"]}
        self.assertIn("astock-act", cmds)

    def test_all_tools_declared_in_manifest(self):
        import json
        with open(os.path.join(_HERE, "..", "..", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        entry = [p for p in man["plugins"] if p["name"] == "astock-plugin"][0]
        declared = set(entry["tools"])
        actual = {t.name for t in self.plugin.get_tools()}
        self.assertEqual(declared, actual,
                         "manifest 与实际工具面不一致（CI 会因此报错）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
