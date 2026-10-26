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
            "astock_sentiment", "astock_sync", "astock_watch",
        })

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
        pl._snapshot = lambda: {"data": {
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
        """开启时段自律时，非交易时段探针不产生信号。"""
        import asyncio
        from unittest.mock import patch
        import astock_data as d
        w = astock.AStockWatch()
        ctx = types.SimpleNamespace(
            config={"astock-plugin": {"ledger_path": self.path,
                                      "watch_enabled": True,
                                      "watch_only_trading_hours": True}},
            workspace=self.tmp)
        with patch.object(mkt, "should_watch_now", return_value=(False, "已收盘")):
            loop = asyncio.new_event_loop()
            try:
                res = loop.run_until_complete(w.probe({}, ctx))
            finally:
                loop.close()
        self.assertEqual(res.snapshot, "")
        self.assertIn("不盯盘", res.summary)


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
            "positions": pos, "quotes": {"600000": quote},
            "watch_quotes": {},
            "total_market_value": 9530.0, "equity": 39530.0, "cash": 30000.0,
            "float_pnl": -475.1, "today_pnl": -90.0, "realized_pnl": 0.0}}
        return pl

    def test_unknown_panel_returns_empty(self):
        self.assertEqual(self._plugin().panel_content("nope", {}), "")

    def test_markdown_is_compact_and_has_range_bar(self):
        """markdown 形态：每股紧凑 2 行 + 位置刻度条，不再是 6 列宽表。

        宽表在 331px 右栏里会被挤成竖条（数值读不出来），故改为短行块；
        刻度条用 `├─●─┤` 表达现价位置，成本线落在区间内时以 `◇` 标出。
        """
        pl = self._snapshot_stub(self._plugin("markdown"))
        out = pl.panel_content("portfolio", {})
        self.assertIn("浦发银行", out)
        self.assertNotIn("lite-tree", out)
        # 不再用 6 列宽表；每行都要短
        self.assertNotIn("| 名称 | 股数 |", out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 60, f"行过长会挤压窄栏: {line!r}")
        # 刻度条：区间端点 + 现价位置点
        self.assertIn("├", out)
        self.assertIn("●", out)
        self.assertNotIn("（区间数据缺失）", out)
        # 成本 10.005 在当日区间 9.40-9.80 之外 → 不画成本线（画了反而是错的）
        self.assertNotIn("◇", out)

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
        pl._snapshot = lambda: {"data": {
            "positions": pos, "quotes": {"600000": quote},
            "total_market_value": 8000.0, "equity": 38000.0, "cash": 30000.0,
            "float_pnl": -2000.0, "today_pnl": -1500.0, "realized_pnl": 0.0}}
        data = json.loads(pl.panel_content("portfolio", {}))
        card = data["trees"][0]
        # 浮亏 20% 触发止损 → 卡片应是 danger 且写明止损
        self.assertEqual(card["options"][0]["tone"], "danger")
        self.assertIn("止损", card["question"])

    def test_empty_portfolio_message(self):
        pl = self._plugin("tree")
        pl._snapshot = lambda: {"data": {
            "positions": [], "quotes": {}, "total_market_value": 0.0,
            "equity": 0.0, "cash": 0.0, "float_pnl": 0.0,
            "today_pnl": 0.0, "realized_pnl": 0.0}}
        out = pl.panel_content("portfolio", {})
        self.assertIn("空仓", out)

    def test_broken_ledger_degrades_gracefully(self):
        pl = self._plugin()
        pl._snapshot = lambda: (_ for _ in ()).throw(RuntimeError("台账炸了"))
        out = pl.panel_content("portfolio", {})
        self.assertIn("台账读取失败", out)     # 不能把面板打空白或抛异常
        self.assertIn("台账炸了", out)

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
        # 现价 9.4 / 昨收 9.9 → 当日 -5.05%：触发 event(action)，未到止损(-8%)
        self._probe([self._quote(price=9.4, prev=9.9)],
                    cfg=self._cfg(email_min_level="urgent"))
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
        # 通知类键由 astock_notify.build_mail_config 读取（另一个模块）
        with open(os.path.join(_HERE, "astock_notify.py"), encoding="utf-8") as _f:
            notify_src = _f.read()
        combined = body + notify_src
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


class TestPanelRangeBar(unittest.TestCase):
    """位置刻度条：把"现价在区间哪个位置"画成一行，替代被挤变形的宽表。"""

    def setUp(self):
        import plugin as pl
        self.rb = pl._range_bar

    def test_position_ratio_and_ends(self):
        out = self.rb(10.0, 20.0, 15.0)
        self.assertIn("10", out)
        self.assertIn("20", out)
        self.assertIn("●", out)
        self.assertIn("50%", out)

    def test_clamps_out_of_range_price(self):
        """价格超出区间时不越界画点（宁可靠边也不画到框外）。"""
        low = self.rb(10.0, 20.0, 5.0)
        high = self.rb(10.0, 20.0, 99.0)
        self.assertIn("0%", low)
        self.assertIn("100%", high)

    def test_cost_marker_only_when_inside_range(self):
        inside = self.rb(10.0, 20.0, 15.0, cost=12.0)
        outside = self.rb(10.0, 20.0, 15.0, cost=99.0)
        self.assertIn("◇", inside)
        self.assertNotIn("◇", outside)

    def test_missing_data_says_so(self):
        """数据缺失时明说，而不是画一条错的图。"""
        self.assertIn("缺失", self.rb(None, None, 10.0))
        self.assertIn("缺失", self.rb(20.0, 10.0, 15.0))   # high<=low 视为坏数据
        self.assertIn("缺失", self.rb("x", "y", "z"))


class TestSnapshotIncludesWatch(unittest.TestCase):
    """`_snapshot(include_watch=True)` 必须拉自选行情（空卡片 bug 的根因）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astock-snap-")
        # 走默认路径（<workspace>/.astock/ledger.sqlite），确保插件的 _ledger_path
        # 与测试写入的是同一个库——之前写到 tmp/ledger.sqlite 会读不到自选。
        lg = ledger_mod(astock.default_ledger_path(self.tmp))
        lg.add_watch("600519", "贵州茅台")
        lg.set_cash(1000)
        pl = astock.AstockPlugin()
        pl._app = types.SimpleNamespace(workspace=self.tmp, config={})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
