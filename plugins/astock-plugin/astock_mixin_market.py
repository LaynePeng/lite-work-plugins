"""行情类工具：实时行情 / K 线 / 检索 / 筛选 / 资金流。

**mixin 而非独立函数**：这批方法都通过 `self._fetch_quotes` 统一限流与
批量取数，并读 `self._app` 的配置（速率上限、缓存 TTL）。改成函数需要
传一长串依赖，而 mixin 只是"搬家"——`self.` 全部照旧、行为零变化。

⚠️ **mixin 不是独立可用的类**：它假定宿主类提供 `_fetch_quotes` / `_app`，
只能与 `AstockPlugin` 组合使用。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import astock_data as data
import astock_market as mkt
from astock_common import _f, _money, _pct, _ratio, _table

logger = logging.getLogger("litework.astock.mixin")


class MarketToolsMixin:
    """行情类工具：实时行情 / K 线 / 检索 / 筛选 / 资金流。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _fetch_quotes(self, codes: List[str], kind: str = "stock",
                            use_cache: bool = True) -> List[data.Quote]:
        items: List[Tuple[str, str, str]] = []
        for raw in codes:
            raw = str(raw or "").strip()
            if not raw:
                continue
            code, venue = mkt.normalize_code(raw, kind)
            items.append((code, venue, kind))
        if not items:
            return []
        if len(items) > MAX_QUOTE_BATCH:
            items = items[:MAX_QUOTE_BATCH]
        return await asyncio.to_thread(data.fetch_quotes, items, use_cache)

    async def _tool_quote(self, args: Dict[str, Any]) -> str:
        kind = str(args.get("kind") or "stock")
        codes = [str(c) for c in (args.get("codes") or []) if str(c).strip()]
        if not codes:
            ledger = self._get_ledger()
            codes = [p["code"] for p in await asyncio.to_thread(ledger.list_positions)]
            for w in await asyncio.to_thread(ledger.list_watch):
                if w["code"] not in codes:
                    codes.append(w["code"])
            if not codes:
                return ("台账里没有持仓或自选股。请直接指定 codes（如 [\"600000\"]），"
                        "或先用 astock_watch 添加自选股。")
        quotes = await self._fetch_quotes(codes, kind,
                                          use_cache=not bool(args.get("no_cache")))
        ctx = hints.market_context()
        rows = []
        for q in quotes:
            d = q.as_dict()
            rows.append([
                f"{d.get('name') or '-'}({q.code})",
                _f(d.get("price")), _pct(d.get("change_pct")),
                f"{_f(d.get('limit_down'))} / {_f(d.get('limit_up'))}",
                _f(d.get("volume_ratio")), _ratio(d.get("turnover_pct")),
                _money(d.get("amount_yuan")), _money(d.get("total_mcap_yuan")),
                d.get("trade_time") or "-",
            ])
        out = [f"**{len(quotes)} 只行情**（{ctx['text']}）", ""]
        out.append(_table(["名称(代码)", "现价", "涨跌幅", "跌停/涨停", "量比",
                           "换手", "成交额", "总市值", "时间"], rows))
        halted = [q for q in quotes if q.extra.get("price_is_prev_close")]
        if halted:
            out += ["", "⏸ 无成交价（停牌/未开盘）：" +
                    "、".join(f"{q.name}({q.code})" for q in halted)]
        if not ctx["open"]:
            out += ["", f"注：{ctx['text']}"]
        return "\n".join(out)

    async def _tool_kline(self, args: Dict[str, Any]) -> str:
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        period = str(args.get("period") or "day")
        limit = int(args.get("limit") or 120)
        adjust = str(args.get("adjust") or "qfq")
        bars = await asyncio.to_thread(data.fetch_klines, code, period, limit, adjust)
        if not bars:
            return f"[Error] {code} 没有 {period} K 线数据"
        closes = [b.close for b in bars if b.close]
        vols = [b.volume_lot for b in bars if b.volume_lot]

        def ma(n: int) -> Optional[float]:
            if len(closes) < n:
                return None
            return round(sum(closes[-n:]) / n, 3)

        last = bars[-1]
        first = bars[0]
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        total_chg = mkt.pct_change(last.close, first.close)
        recent = vols[-5:]
        prev = vols[-20:-5] if len(vols) >= 20 else vols[:-5]
        vol_ratio = (round((sum(recent) / len(recent)) / (sum(prev) / len(prev)), 2)
                     if recent and prev and sum(prev) > 0 else None)
        pos = mkt.pct_change(last.close, lo)
        out = [
            f"**{code} {period} K 线**（{len(bars)} 根，{first.date} → {last.date}，"
            f"复权：{('不复权' if period.endswith('m') else adjust)}）",
            "",
            _table(["项目", "数值"], [
                ["最新收盘", _f(last.close)],
                ["区间涨跌幅", _pct(total_chg)],
                ["区间最高 / 最低", f"{_f(hi)} / {_f(lo)}"],
                ["现价距区间低点", _pct(pos)],
                ["MA5 / MA10 / MA20 / MA60",
                 " / ".join(_f(ma(n), 3) for n in (5, 10, 20, 60))],
                ["近 5 根均量较前 5 根",
                 f"{_f(vol_ratio)} 倍" if vol_ratio else "-"],
            ]),
        ]
        # 均线相对位置（给 Agent 判断趋势位置的锚点，而不是让模型自己看图）
        mx = ma(20)
        if mx:
            rel = "上方" if last.close > mx else "下方"
            out += ["", f"现价位于 MA20 {rel}（MA20={mx:.3f}，现价 {last.close:.3f}），"
                        f"偏离 {_pct(mkt.pct_change(last.close, mx))}。"]
        if args.get("with_bars"):
            out += ["", "最近 10 根：", _table(
                ["日期", "开", "高", "低", "收", "量(手)"],
                [[b.date, _f(b.open), _f(b.high), _f(b.low), _f(b.close), _f(b.volume_lot, 0)]
                 for b in bars[-10:]])]
        out += ["", "注：以上为历史统计，不预测未来走势。"]
        return "\n".join(out)

    async def _tool_search(self, args: Dict[str, Any]) -> str:
        kw = str(args.get("keyword") or "").strip()
        if not kw:
            return "[Error] 需要 keyword"
        rows = await asyncio.to_thread(data.search, kw, int(args.get("limit") or 10))
        # 只保留可交易/可跟踪的品种：债券、权证等与炒股无关，过滤掉免误导
        rows = [r for r in rows if r.get("kind") in ("stock", "index", "etf")]
        if not rows:
            return f"没找到与「{kw}」匹配的股票/ETF/指数（债券、基金净值等已过滤）。"
        return _table(["代码", "名称", "类型", "板块", "symbol"],
                      [[r["code"], r["name"], r["market"], r["board"], r["symbol"]]
                       for r in rows])

    async def _tool_screen(self, args: Dict[str, Any]) -> str:
        def pick(key: str) -> Optional[float]:
            v = args.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        result = await asyncio.to_thread(
            data.screen,
            universe=str(args.get("universe") or "all"),
            sort_by=str(args.get("sort_by") or "change_pct"),
            ascending=bool(args.get("ascending")),
            limit=int(args.get("limit") or 20),
            min_change=pick("min_change"), max_change=pick("max_change"),
            min_amount=pick("min_amount"), max_amount=pick("max_amount"),
            min_turnover=pick("min_turnover"), max_turnover=pick("max_turnover"),
            min_mcap=pick("min_mcap"), max_mcap=pick("max_mcap"),
            exclude_st=bool(args.get("exclude_st", True)),
        )
        rows = result["rows"]
        source_note = ""
        if result["filters"].get("source") == "sina":
            source_note = ("\n\n注：东财接口当前被限流，已自动切换新浪排行备源——"
                           "**不含主力净流入与量比**（备源没有该字段，不是 0）。")
        if not rows:
            return ("没有符合条件的标的。可放宽过滤条件（如 min_amount 调小、"
                    "或去掉涨跌幅区间）。" + source_note)
        table = _table(
            ["代码", "名称", "现价", "涨跌幅", "换手", "量比", "成交额", "总市值", "主力净额"],
            [[r["code"], r["name"], _f(r["price"]), _pct(r["change_pct"]),
              _ratio(r["turnover_pct"]), _f(r["volume_ratio"]),
              _money(r["amount_yuan"]), _money(r["total_mcap_yuan"]),
              _money(r["main_net_inflow_yuan"])] for r in rows])
        return (f"**条件选股**（{result['universe']}，全市场 {result['universe_total']} 只，"
                f"命中 {result['total_matched']}）\n\n{table}\n\n"
                f"排序：{result['filters']['sort_by']}；"
                f"筛选结果仅作研究起点，不构成推荐。{source_note}")

    async def _tool_flow(self, args: Dict[str, Any]) -> str:
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        rows = await asyncio.to_thread(data.fetch_money_flow, code,
                                      int(args.get("days") or 10))
        if not rows:
            return f"{code} 没有资金流数据（可能是指数/ETF 或数据源未覆盖）。"
        total_main = sum(r["main_net"] or 0 for r in rows)
        table = _table(
            ["日期", "主力净额", "超大单", "大单", "中单", "小单"],
            [[r["date"], _money(r["main_net"]), _money(r["super_net"]),
              _money(r["large_net"]), _money(r["mid_net"]), _money(r["small_net"])]
             for r in rows])
        # 数据源口径不同，必须如实说明——否则 Agent 会把「新浪净额」
        # 当成东财的「主力（超大单+大单）」去讲，结论就错了
        source = rows[0].get("source")
        if source == "sina":
            note = ("注：东财资金流接口当前被限流，已切新浪备源。新浪的"
                    "「主力净额」口径为当日资金净流入、与东财「超大单+大单」不完全一致；"
                    "大单/中单/小单无对应字段故留空（不是 0）。")
        else:
            note = "注：主力=超大单+大单口径，资金流是结果而非原因。"
        return (f"**{code} 近 {len(rows)} 日资金流**（合计主力净额 {_money(total_main)}）\n\n"
                f"{table}\n\n{note}")
