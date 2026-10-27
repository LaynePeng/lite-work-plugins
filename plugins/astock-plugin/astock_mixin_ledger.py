"""台账类工具：录成交（含 dry-run 校验）+ 持仓总览（含聊天看板）。

**mixin 而非独立函数**：`_tool_portfolio` 要调 `_snapshot` 与
`_render_board_text`，`_tool_record` 要调 `_preview_trade` /
`_format_record_result`，且都通过 `self._get_ledger` 拿台账句柄。
改函数要动多处调用点，mixin 只是搬家。

⚠️ **mixin 单独不可用**：假定宿主类提供 `_get_ledger` / `_snapshot` /
`_hints_cfg`，只能与 `AstockPlugin` 组合。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import astock_board as board
import astock_data as data
import astock_hints as hints
import astock_market as mkt
from astock_ledger import Ledger
from astock_common import _f, _table

logger = logging.getLogger("litework.astock.mixin")


class LedgerToolsMixin:
    """台账类工具：录成交（含 dry-run 校验）+ 持仓总览（含聊天看板）。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _tool_record(self, args: Dict[str, Any]) -> str:
        side = str(args.get("side") or "").strip().lower()
        code = str(args.get("code") or "").strip()
        price = args.get("price")
        shares = args.get("shares")
        if side not in ("buy", "sell"):
            return "[Error] side 只能是 buy 或 sell"
        if not code:
            return "[Error] 需要 code"
        try:
            price_f = float(price)
            shares_i = int(float(shares))
        except (TypeError, ValueError):
            return "[Error] price / shares 必须是数字"
        dry = bool(args.get("dry_run"))

        def do() -> Dict[str, Any]:
            ledger = self._get_ledger()
            norm, venue = mkt.normalize_code(code)
            name = str(args.get("name") or "").strip()
            if not name:
                try:
                    q = data.fetch_quotes([(norm, venue, "stock")], use_cache=True)
                    if q:
                        name = q[0].name
                except data.DataError:
                    name = ""
            if dry:
                return self._preview_trade(ledger, norm, name, side, price_f,
                                          shares_i, str(args.get("trade_date") or ""),
                                          args.get("fee"))
            return ledger.record_trade(
                code=norm, side=side, price=price_f, shares=shares_i,
                trade_date=str(args.get("trade_date") or ""), name=name,
                note=str(args.get("note") or ""),
                fee_override=(float(args["fee"]) if args.get("fee") not in (None, "") else None),
            )

        result = await asyncio.to_thread(do)
        if dry:
            return result["text"]
        return self._format_record_result(result, dry=False)

    def _preview_trade(self, ledger: Ledger, code: str, name: str, side: str,
                       price: float, shares: int, trade_date: str,
                       fee_override: Any) -> Dict[str, Any]:
        """预览：完整跑一遍校验与算账，但**不落账**（用内存里的持仓副本）。"""
        pos = next((p for p in ledger.list_positions() if p["code"] == code), None)
        held = int(pos["shares"]) if pos else 0
        cost = float(pos["cost_price"]) if pos else 0.0
        available = int(pos["available_today"]) if pos else 0
        amount = round(price * shares, 2)
        board = mkt.board_of(code)
        fee_bd = mkt.calc_fee(amount, side, ledger.fee, board)
        fee = fee_bd.total if fee_override in (None, "") else float(fee_override)

        problems: List[str] = []
        err = mkt.check_order_size(shares, code, side, held)
        if err:
            problems.append(err)
        if side == "sell":
            if held <= 0:
                problems.append(f"{code} 当前无持仓，无法录入卖出")
            elif shares > available:
                problems.append(f"今日可卖仅 {available} 股（T+1），本次想卖 {shares} 股")
        if price <= 0:
            problems.append("成交价必须大于 0")
        if trade_date:
            try:
                import datetime as _d
                if _d.date.fromisoformat(trade_date) > _d.date.today():
                    problems.append("成交日期不能晚于今天")
            except ValueError:
                problems.append(f"成交日期格式应为 YYYY-MM-DD（收到 {trade_date!r}）")

        if side == "buy":
            new_shares = held + shares
            new_cost = round((cost * held + amount + fee) / new_shares, 4) if new_shares else 0
            cash_delta = -(amount + fee)
            realized = None
        else:
            new_shares = max(0, held - shares)
            new_cost = round(cost, 4) if new_shares else 0.0
            cash_delta = amount - fee
            realized = round((price - cost) * shares - fee, 2)

        lines = [f"**预览（未落账）**：{'买入' if side == 'buy' else '卖出'} "
                 f"{name or code}({code}) {shares} 股 @ {price:.2f}",
                 "",
                 _table(["项目", "数值"], [
                     ["成交金额", f"{amount:,.2f} 元"],
                     ["费用明细", f"佣金 {fee_bd.commission:.2f} / 印花税 "
                                  f"{fee_bd.stamp_tax:.2f} / 过户费 {fee_bd.transfer_fee:.2f}"],
                     ["费用合计", f"{fee:.2f} 元"],
                     ["资金变动", f"{cash_delta:+,.2f} 元"],
                     ["持仓变化", f"{held} → {new_shares} 股"],
                     ["成本价变化", f"{cost:.4f} → {new_cost:.4f}" if held else f"— → {new_cost:.4f}"],
                     ["已实现盈亏", f"{realized:+,.2f} 元" if realized is not None else "—"],
                     ["今日可卖", f"{available} 股"],
                 ])]
        if problems:
            lines += ["", "⚠️ **校验未通过，落账会被拒绝**："] + [f"- {p}" for p in problems]
        else:
            lines += ["", "✅ 校验通过。确认无误后用 dry_run=false 重新调用即可落账。"]
        return {"text": "\n".join(lines), "problems": problems}

    def _format_record_result(self, r: Dict[str, Any], dry: bool) -> str:
        side_cn = "买入" if r["side"] == "buy" else "卖出"
        lines = [f"✅ 已录入{side_cn}：{r['name'] or r['code']}({r['code']}) "
                 f"{r['shares']} 股 @ {r['price']:.2f}（{r['trade_date']}）",
                 "",
                 _table(["项目", "数值"], [
                     ["成交金额", f"{r['amount']:,.2f} 元"],
                     ["费用", f"{r['fee']:.2f} 元"],
                     ["资金变动", f"{r['cash_delta']:+,.2f} 元"],
                     ["持仓", f"{r['shares_after']} 股"],
                     ["成本价", f"{r['cost_price_before']} → {r['cost_price_after']}"
                      if r.get("cost_price_before") else f"{r['cost_price_after']}"],
                     ["已实现盈亏", f"{r['realized_pnl']:+,.2f} 元"
                      if r.get("realized_pnl") is not None else "—"],
                 ])]
        lines += ["", "提示：台账里的可用资金不会自动扣减（出入金不可见），"
                      "需要时用 astock_portfolio action=set_cash 手动校准。"]
        return "\n".join(lines)

    async def _tool_portfolio(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "list")
        ledger_snapshot = await asyncio.to_thread(self._snapshot)
        if action == "set_cash":
            cash = args.get("cash")
            if cash is None:
                return "[Error] action=set_cash 需要 cash 参数"
            ledger = self._get_ledger()
            res = await asyncio.to_thread(ledger.set_cash, float(cash))
            return f"✅ 可用资金已设为 {res['balance']:,.2f} 元（{res['updated_at']}）"
        if action == "trades":
            ledger = self._get_ledger()
            rows = await asyncio.to_thread(
                ledger.trades, str(args.get("code") or ""),
                int(args.get("limit") or 30))
            if not rows:
                return "暂无成交记录。用 astock_record 录入你在券商的实际成交。"
            return _table(
                ["id", "日期", "代码", "名称", "方向", "价格", "数量", "费用", "已实现"],
                [[r["id"], r["trade_date"], r["code"], r["name"],
                  "买入" if r["side"] == "buy" else "卖出",
                  _f(r["price"]), r["shares"], _f(r["fee"]),
                  _f(r["realized"]) if r["realized"] is not None else "—"]
                 for r in rows])
        if action == "delete_trade":
            tid = args.get("trade_id")
            if tid is None:
                return "[Error] action=delete_trade 需要 trade_id（先用 action=trades 查 id）"
            ledger = self._get_ledger()
            res = await asyncio.to_thread(ledger.delete_trade, int(tid))
            return (f"✅ 已删除流水 {res['deleted']}（{res['code']}），"
                    f"并按剩余流水重算：持仓 {res['shares']} 股，"
                    f"成本价 {res['cost_price']:.4f}")
        # list / summary 都返回总览（summary 只是语义别名，便于模型表达意图）
        # 看板版式：聊天区宽度足够三栏，且含 ```board 围栏的结果会被宿主
        # 自动展开渲染（右栏 331px 只够 1 栏，故右栏保持简表）。
        if action in ("list", "summary"):
            board_text = await self._render_board_text()
            if board_text:
                return board_text
        return ledger_snapshot["text"]

    async def _render_board_text(self) -> str:
        """持仓 → 聊天内三栏看板（含空列占位与文字兜底）。"""
        try:
            snap = await asyncio.to_thread(self._snapshot, include_watch=True)
            data_ = snap["data"]
            ledger = self._get_ledger()
            watch = await asyncio.to_thread(ledger.list_watch)
            spec = board.build_board(
                data_, self._hints_cfg(),
                hints.build_position_hints, hints.build_t1_hint,
                watch_codes=[w["code"] for w in watch])
        except Exception:
            logger.warning("[astock] 看板渲染失败，回退文字版", exc_info=True)
            return ""
        if not spec:
            return ""
        lines = [board.render_board_json(spec), ""]
        # 看板是"扫一眼"的视图，明细数字仍需可核对 → 附一行口径说明
        lines.append(
            f"共 {len(data_['positions'])} 只持仓、"
            f"{len(watch)} 只自选；卡片首行＝现价/涨跌幅，次行为"
            f"股数·成本·浮盈·今日可卖，箭头后为该做什么。"
            f"完整数值用 `astock_portfolio action=trades` 或右栏「持仓」面板核对。")
        return "\n".join(lines)
