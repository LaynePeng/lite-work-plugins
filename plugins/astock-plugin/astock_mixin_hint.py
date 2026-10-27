"""下单提示类工具：`astock_hint`（该不该动/动多少/条件单参数）。

**mixin 而非独立函数**：一条提示要串起行情、台账、历史最高价、舆情速查
与条件单落库（`_build_order_plans` 会写 plans 表），彼此调用密集且都靠
`self` 拿句柄。mixin 只是搬家，不改调用点。

⚠️ **mixin 单独不可用**：假定宿主类提供 `_snapshot` / `_fetch_quotes` /
`_get_ledger` / `_hints_cfg` / `_sentiment_flags` 等。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import astock_data as data
import astock_hints as hints
import astock_market as mkt
import astock_orders as orders
import astock_sentiment as senti
import astock_sentiment_data as senti_data
from astock_common import _f, _pct, _table

logger = logging.getLogger("litework.astock.mixin")


class HintToolsMixin:
    """下单提示类工具：`astock_hint`（该不该动/动多少/条件单参数）。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _tool_hint(self, args: Dict[str, Any]) -> str:
        only = str(args.get("code") or "").strip()
        want_buy = bool(args.get("buy"))
        sell_ratio = args.get("sell_ratio")
        snap = await asyncio.to_thread(self._snapshot)
        cfg = self._hints_cfg()
        ledger = self._get_ledger()
        ctx = hints.market_context()

        codes: List[str] = []
        pos_by_code = {p["code"]: p for p in snap["data"]["positions"]}
        if only:
            code, _ = mkt.normalize_code(only)
            codes = [code]
        else:
            codes = [p["code"] for p in snap["data"]["positions"]]
            for w in await asyncio.to_thread(ledger.list_watch):
                if w["code"] not in codes:
                    codes.append(w["code"])
        if not codes:
            return ("台账里没有持仓或自选股，没有可提示的对象。"
                    "可以先用 astock_record 录入持仓，或 astock_watch 添加自选股。")

        quotes: Dict[str, Dict[str, Any]] = dict(snap["data"]["quotes"])
        missing = [c for c in codes if c not in quotes]
        if missing:
            try:
                fetched = await self._fetch_quotes(missing)
                for q in fetched:
                    quotes[q.code] = q.as_dict()
            except data.DataError as exc:
                return f"[Error] 行情获取失败，无法生成提示：{exc}"

        highest = await asyncio.to_thread(self._highest_prices, list(quotes))
        lines = [f"**下单提示**（{ctx['text']}）", ""]
        all_hints: List[hints.Hint] = []
        total_mv = snap["data"]["total_market_value"] or 0
        # 单票占比的分母必须是**账户总资产**（持仓市值 + 可用现金）。
        # 曾误用 total_mv（持仓市值）→ 只持一只股时占比恒为 100%，
        # 24% 的正常仓位也会被报成「超上限」并建议减仓。
        total_equity = float(snap["data"].get("equity") or 0) or (
            float(total_mv) + float(snap["data"].get("cash") or 0))

        for code in codes:
            q = quotes.get(code)
            if not q:
                continue
            pos = pos_by_code.get(code)
            if pos:
                hs = hints.build_position_hints(pos, q, cfg,
                                               highest_price=highest.get(code),
                                               total_market_value=total_mv,
                                               total_equity=total_equity)
                t1 = hints.build_t1_hint(pos, int(pos.get("available_today") or 0))
                if t1:
                    hs.append(t1)
                all_hints.extend(hs)
                if hs:
                    lines.append(f"**{pos.get('name') or code}({code})**")
                    for h in hs:
                        lines.append(f"- {h.title}｜{h.suggestion}（依据：{h.basis}）")
                    lines.append("")
                else:
                    pnl = mkt.pct_change(q.get("price") or 0, pos["cost_price"])
                    lines.append(f"**{pos.get('name') or code}({code})**："
                                 f"现价 {_f(q.get('price'))}，浮盈 {_pct(pnl)}，"
                                 f"未触发任何规则区间（止损 -{cfg.stop_loss_pct}% / "
                                 f"止盈 +{cfg.take_profit_pct}%）。")
                    lines.append("")
            else:
                # 只盯不持有：watch_only 只产出与仓位无关的信号（涨跌停/异动/放量）
                hs = hints.build_position_hints({"code": code}, q, cfg,
                                                watch_only=True)
                if hs:
                    lines.append(f"**{q.get('name') or code}({code})**（自选）")
                    for h in hs:
                        lines.append(f"- {h.title}｜{h.suggestion}")
                    lines.append("")
            # 卖出数量估算（有持仓时）
            if pos and sell_ratio is not None:
                try:
                    ratio = float(sell_ratio)
                except (TypeError, ValueError):
                    ratio = 1.0
                est = hints.suggest_sell(pos, q.get("price") or 0,
                                        int(pos.get("available_today") or 0),
                                        ratio=ratio, fee_cfg=ledger.fee)
                if est.get("suggest_shares"):
                    lines.append(_table(
                        ["卖出估算", "数值"], [
                            ["建议卖出", f"{est['suggest_shares']} 股（剩余 {est['remain_shares']}）"],
                            ["预计金额", f"{est['suggest_amount']:,.2f} 元"],
                            ["预计费用", f"{est['est_fee']:.2f} 元"],
                            ["预计净收入", f"{est['est_net_proceeds']:,.2f} 元"],
                            ["预计已实现盈亏", f"{est['est_realized_pnl']:+,.2f} 元"],
                        ]))
                    lines.append("")
            # 买入数量估算
            if want_buy and q.get("price"):
                pos_shares = int(pos["shares"]) if pos else 0
                est = hints.suggest_buy(
                    code, q.get("name") or code, q["price"],
                    cash=snap["data"]["cash"] + 0.0,
                    cfg=cfg, existing_shares=pos_shares,
                    existing_cost=float(pos["cost_price"]) if pos else 0.0,
                    total_market_value=total_mv, fee_cfg=ledger.fee)
                lines.append(_table(
                    ["买入估算", "数值"], [
                        ["参考价", f"{est['price']:.2f}"],
                        ["建议股数", f"{est['suggest_shares']} 股（{est['reason']}）"],
                        ["预计金额", f"{est['suggest_amount']:,.2f} 元"],
                        ["预计费用", f"{est['est_fee']:.2f} 元"],
                        ["预计总成本", f"{est['est_total_cost']:,.2f} 元"],
                        ["买入后成本价", _f(est.get("avg_cost_after"), 3)],
                        ["涨停 / 跌停价", f"{_f(est['limit_up'])} / {_f(est['limit_down'])}"],
                    ]))
                lines.append(f"注：{est['note']}")
                lines.append("")

        if not all_hints and not only:
            lines.insert(2, "本次扫描未触发任何规则信号（止损/止盈/回撤/涨跌停/异动）。\n")
        # 舆情背离：只在"有持仓或指定标的"且用户没关时才查（省接口与时间）
        if not args.get("no_sentiment"):
            flag_codes = [c for c in codes if c in pos_by_code] or codes[:1]
            flags = await self._sentiment_flags(flag_codes)
            if flags:
                lines.append("")
                lines.append("### 舆情参考（背离信号优先）")
                lines += flags
                lines.append("")
        # 条件单参数块：没时间盯盘的人真正需要的部分——照着挂一次，白天不用看
        plan_lines, plan_ids = await self._build_order_plans(
            codes, pos_by_code, quotes, snap["data"], want_buy)
        if plan_lines:
            lines.append("")
            lines.append("### 可照抄的条件单（挂一次，不用盯盘）")
            lines += plan_lines
            if plan_ids:
                lines.append("")
                lines.append(f"_计划号：{'、'.join(plan_ids)}。成交后用 astock_sync "
                             f"把回单粘回来即可自动匹配这条计划。_")
        lines.append(hints.DISCLAIMER)
        return "\n".join(lines)

    async def _build_order_plans(self, codes: List[str],
                                 pos_by_code: Dict[str, Dict[str, Any]],
                                 quotes: Dict[str, Dict[str, Any]],
                                 data_: Dict[str, Any],
                                 want_buy: bool) -> Tuple[List[str], List[str]]:
        """为持仓/自选生成条件单参数，并落库为"建议计划"。

        只对**已经触发信号**或用户明确问买入的标的生成——不然每只持仓都挂一张
        计划会把 plans 表塞满，反而找不到该执行哪一条。
        """
        cfg = self._hints_cfg()
        fee = self._get_ledger().fee
        cash = float(data_.get("cash") or 0)
        total_mv = float(data_.get("total_market_value") or 0)
        cap_amount = ((total_mv + cash) * cfg.position_cap_pct / 100.0
                      if (total_mv + cash) > 0 else 0)
        out: List[str] = []
        ids: List[str] = []
        ledger = self._get_ledger()

        for code in codes[:6]:
            q = quotes.get(code) or {}
            price = q.get("price")
            if not price:
                continue
            pos = pos_by_code.get(code)
            name = q.get("name") or (pos or {}).get("name") or code
            order = None
            if pos:
                # 有持仓 → 卖出侧条件单（止损/止盈），数量=今日可卖
                avail = int(pos.get("available_today") or 0)
                order = orders.build_sell_order(
                    code, name, float(price), avail,
                    float(pos.get("cost_price") or 0),
                    cfg.stop_loss_pct, cfg.take_profit_pct, cfg.trailing_pct,
                    fee_cfg=fee)
            elif want_buy:
                order = orders.build_buy_order(
                    code, name, float(price), cash, cfg.stop_loss_pct,
                    cfg.take_profit_pct, cfg.trailing_pct,
                    cap_amount=cap_amount, fee_cfg=fee)
            if order is None or order.shares <= 0:
                if order is not None and order.notes:
                    out.append(order.order_block())
                continue
            # 落库：让成交回单能匹配回这条建议
            try:
                plan = await asyncio.to_thread(
                    ledger.save_plan, {
                        "code": code, "name": name, "side": order.side,
                        "price": order.price, "shares": order.shares,
                        "stop_loss": order.stop_loss,
                        "take_profit": order.take_profit,
                        "trailing": order.trailing_pct,
                        "reason": "条件单参数（由 astock_hint 生成）",
                        "kind": "conditional_order",
                    })
                pid = plan.get("plan_id") or ""
            except Exception:
                logger.warning("[astock] 计划落库失败 %s", code, exc_info=True)
                pid = ""
            block = order.order_block()
            if pid:
                block += f"\n  计划号  {pid}"
                ids.append(pid)
            out.append(block)
        return out, ids

    async def _sentiment_flags(self, codes: List[str],
                              limit: int = 3) -> List[str]:
        """给下单提示挂一段舆情背离摘要。

        只取**背离**（高/中优先级），不铺完整证据清单——下单那一刻用户需要的是
        "有没有反方向的证据",而不是读完七类公告。取不到就静默降级（行情本身
        仍可用），但会在结果里说"舆情未取到"，避免用户以为查过没问题。
        """
        if not codes:
            return []
        out: List[str] = []
        failed: List[str] = []
        for code in codes[:limit]:
            try:
                name = ""
                try:
                    q = data.fetch_quotes(
                        [(code, mkt.normalize_code(code)[1], "stock")])
                    if q:
                        name = q[0].name
                except data.DataError:
                    pass
                collected = senti_data.collect(code, name)
                report = senti.summarize(collected["items"], code, name,
                                        collected["errors"], collected["extras"])
            except Exception:
                logger.warning("[astock] 舆情速查失败 %s", code, exc_info=True)
                failed.append(code)
                continue
            if not report.conflicts:
                continue
            top = [c for c in report.conflicts if c.severity in ("high", "medium")][:2]
            if not top:
                continue
            out.append(f"**{name or code}({code}) 舆情背离**")
            for c in top:
                mark = "🔴" if c.severity == "high" else "🟠"
                out.append(f"- {mark} {c.title}：{c.detail}")
                if c.advice:
                    out.append(f"  → {c.advice}")
        if failed:
            out.append("_（" + "、".join(failed) +
                       " 舆情未取到，结论仅基于行情与规则）_")
        return out

    def _highest_prices(self, codes: List[str]) -> Dict[str, float]:
        """取持仓期最高价（用近 60 日最高价近似——台账记录开仓日期后
        可精确到开仓日，这里先用区间最高价，避免为每只股打一次接口）。"""
        out: Dict[str, float] = {}
        for code in codes[:20]:
            try:
                bars = data.fetch_klines(code, "day", limit=60, adjust="qfq")
                if bars:
                    out[code] = max(b.high for b in bars)
            except data.DataError:
                continue
        return out
