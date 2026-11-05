"""计算公式工具（`astock_calc`）：把 `astock_formula` 的纯函数接到行情上。

────────────────────────────────────────────────────────────────────────
为什么要单开一个工具，而不是让公式库自己取数
────────────────────────────────────────────────────────────────────────
`astock_formula` 是**零网络纯函数**，好处是能被单测、能被 quant 复用、
能在临时排查时直接 import 算。但它自己不知道该看哪只票、哪段 K 线。

本 mixin 只做三件事，不含任何数学：
  ① 取数（复用宿主的 `_fetch_quotes` / `data.fetch_klines`，自动享受
     限流、缓存、主备源切换）
  ② 调公式
  ③ 渲染成表

────────────────────────────────────────────────────────────────────────
支持的 mode（一次调用只跑一种，避免输出过长）
────────────────────────────────────────────────────────────────────────
    summary     全套技术指标（均线/偏离/RSI/波动率/动量/回撤/区间位置）
    ma          只算均线 + 偏离 + 位置 + 多头排列（盘后重算规则价用这个）
    rule_check  价位规则体检（方向可达性 + 均线漂移）——**本次会话三次踩坑的直接产物**
    cost        成本/盈亏平衡/止损止盈/盈亏比/可买股数（下单前算账用）
    adjust      除权影响估算（判断不复权算均线会虚高多少）
    ratio       收益/回撤比（选策略用：先看回撤再看收益）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import astock_data as data
import astock_formula as fx
import astock_market as mkt
from astock_common import _f, _pct, _ratio, _table

logger = logging.getLogger("litework.astock.calc")


class CalcToolsMixin:
    """计算公式工具。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _tool_calc(self, args: Dict[str, Any]) -> str:
        mode = str(args.get("mode") or "summary").strip().lower()
        if mode == "rule_check":
            return await self._calc_rule_check(args)
        if mode == "cost":
            return self._calc_cost(args)
        if mode == "adjust":
            return self._calc_adjust(args)
        if mode == "ratio":
            return self._calc_ratio(args)
        if mode in ("summary", "ma"):
            return await self._calc_series(args, mode)
        return (f"[Error] 未知 mode={mode!r}。支持：summary / ma / rule_check / "
                f"cost / adjust / ratio")

    # ---------------------------------------------------------------- 序列类

    async def _calc_series(self, args: Dict[str, Any], mode: str) -> str:
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        period = str(args.get("period") or "day")
        limit = int(args.get("limit") or 120)
        # 默认 qfq：均线看复权是硬规则（见 astock_formula.ADJUST_POLICY）。
        adjust = str(args.get("adjust") or "qfq")
        bars = await asyncio.to_thread(data.fetch_klines, code, period, limit,
                                       adjust)
        if not bars:
            return f"[Error] {code} 没有 {period} K 线数据"
        closes = [b.close for b in bars if b.close]
        if not closes:
            return f"[Error] {code} K 线没有有效收盘价"

        # 现价：优先实时行情（盘中）；取不到则用最后一根收盘
        price = None
        try:
            quotes = await self._fetch_quotes([code])
            if quotes:
                price = quotes[0].price
        except Exception:
            logger.debug("[astock] calc 取实时价失败，退回收盘价", exc_info=True)

        info = fx.summary(closes, price)
        last_date = bars[-1].date
        name = code
        try:
            quotes = quotes if 'quotes' in dir() else []
        except Exception:
            quotes = []

        head = [f"**{name}({code}) 技术指标**"
                f"（{period} · 复权={adjust} · 最新 {last_date} · {len(closes)} 根）",
                ""]

        if mode == "ma":
            rows = [
                ["均线", "MA5", "MA10", "MA20", "MA60"],
                ["数值", _f(info.get("ma5"), 3), _f(info.get("ma10"), 3),
                 _f(info.get("ma20"), 3), _f(info.get("ma60"), 3)],
                ["偏离", _pct(info.get("dev_ma5_pct")),
                 _pct(info.get("dev_ma10_pct")),
                 _pct(info.get("dev_ma20_pct")),
                 _pct(info.get("dev_ma60_pct"))],
            ]
            out = head + [_table(rows[0], rows[1:]), ""]
            out.append(f"- 收盘/现价：**{_f(info.get('close'))}**")
            out.append(f"- 位置：{info.get('ma_position')}")
            align = info.get("alignment")
            if align is not None:
                out.append(f"- 均线多头排列：{align}/3"
                           + ("（完全多头）" if align == 3 else ""))
            out += ["", "> 口径：均线用**前复权**（qfq）算，成交价用原始价。"
                        "含除权的标的不复权算长周期均线会**虚高**，见 mode=adjust。"]
            return "\n".join(out)

        # summary：全套
        rows = [
            ["均线", "MA5", "MA10", "MA20", "MA60"],
            ["数值", _f(info.get("ma5"), 3), _f(info.get("ma10"), 3),
             _f(info.get("ma20"), 3), _f(info.get("ma60"), 3)],
            ["偏离", _pct(info.get("dev_ma5_pct")),
             _pct(info.get("dev_ma10_pct")),
             _pct(info.get("dev_ma20_pct")),
             _pct(info.get("dev_ma60_pct"))],
        ]
        out = head + [_table(rows[0], rows[1:]), ""]
        out.append(f"| 指标 | 数值 |\n|---|---|")
        out.append(f"| 收盘/现价 | **{_f(info.get('close'))}** |")
        out.append(f"| 位置 | {info.get('ma_position')} |")
        out.append(f"| 多头排列 | {info.get('alignment')}/3 |")
        out.append(f"| RSI14 | {_f(info.get('rsi14'))} |")
        out.append(f"| 年化波动率(20日) | {_ratio(info.get('volatility_20d'))} |")
        if info.get("mom5") is not None:
            out.append(f"| 动量 5/20/60 日 | {_pct(info.get('mom5'))} / "
                       f"{_pct(info.get('mom20'))} / {_pct(info.get('mom60'))} |")
        dd = info.get("max_drawdown") or {}
        if dd.get("pct") is not None:
            out.append(f"| 区间最大回撤 | **{_pct(dd.get('pct'))}** "
                       f"(峰 {_f(dd.get('peak'))} → 谷 {_f(dd.get('trough'))}) |")
        out.append(f"| 60 日区间 | {_f(info.get('low_window'))} ~ "
                   f"{_f(info.get('high_window'))} |")
        out.append(f"| 区间位置 | {_f(info.get('pos_in_range'))}%（0=最低 100=最高） |")
        out.append(f"| 距区间高点 | {_pct(info.get('drawdown_from_high'))} |")
        out += ["", "> 以上为历史统计，不预测未来。"]
        return "\n".join(out)

    # ---------------------------------------------------------------- 规则体检

    async def _calc_rule_check(self, args: Dict[str, Any]) -> str:
        """体检盯盘规则：方向是否可达 + 均线是否漂移。

        不传 rules 时自动读当前盯梢计划里的 rules（默认行为，覆盖最常见的
        "帮我看看现在的规则有没有失效"）。
        """
        rules: List[Dict[str, Any]] = list(args.get("rules") or [])
        source = "入参 rules"
        if not rules:
            rules, source = await self._load_plan_rules()
            if not rules:
                return ("没有可体检的规则：入参没给 rules，自动读取盯梢计划也"
                        "没读到（计划可能未创建或 probe_config.rules 为空）。"
                        "请用 rules=[{code,price,op,label}] 显式传入。")

        codes = sorted({str(r.get("code") or "") for r in rules if r.get("code")})
        quotes: Dict[str, float] = {}
        names: Dict[str, str] = {}
        try:
            got = await self._fetch_quotes(codes)
            for q in got:
                quotes[q.code] = q.price
                names[q.code] = q.name
        except Exception as exc:
            logger.info("[astock] 规则体检取行情失败: %s", exc)
            return f"[Error] 取行情失败，无法体检（方向校验必须有现价）：{exc}"

        # 均线：只为**声明了 ma_key** 的规则取数（v2.8.4）。
        # 早先给每只标的都按"第一条带 ma_key 的规则"取一条均线，再让
        # check_rule_set 用 `r.get("ma_key") or code` 去查——于是：
        #   · 同标的有多条不同周期规则的，后几条拿到错误周期；
        #   · 固定价规则（-8% 硬止损/前低）也去查 code 键，被要求"改成均线价"。
        # 现在按 (code, ma_key) 组合精确取，且只给需要漂移检查的规则取。
        ma_map: Dict[str, Dict[str, Optional[float]]] = {}
        want_ma = args.get("use_ma", True)
        if want_ma:
            need: Dict[str, set] = {}
            for r in rules:
                code = str(r.get("code") or "")
                ma_key = str(r.get("ma_key") or "").strip()
                if code and ma_key:
                    need.setdefault(code, set()).add(ma_key)
            for code, keys in need.items():
                per_code: Dict[str, Optional[float]] = {}
                for ma_key in sorted(keys):
                    try:
                        n = int(str(ma_key).replace("ma", "") or 20)
                    except ValueError:
                        n = 20
                    try:
                        bars = await asyncio.to_thread(
                            data.fetch_klines, code, "day", max(n + 5, 70), "qfq")
                        closes = [b.close for b in bars if b.close]
                        per_code[ma_key] = fx.sma(closes, n)
                    except Exception:
                        logger.debug("[astock] %s %s 均线取数失败",
                                     code, ma_key, exc_info=True)
                        per_code[ma_key] = None
                ma_map[code] = per_code

        rows = fx.check_rule_set(rules, quotes, ma_map if want_ma else None)

        lines = [f"**规则体检**（{len(rows)} 条 · 来源：{source}）", ""]
        bad = [r for r in rows if not r["ok"]]
        lines.append(f"结论：**{'⚠️ ' + str(len(bad)) + ' 条有问题' if bad else '✅ 全部有效'}**")
        lines.append("")
        table_rows = []
        for r in rows:
            mk = str(r.get("ma_key") or "") if want_ma else ""
            table_rows.append([
                names.get(r["code"], r["code"]),
                f"{r['rule_price']}",
                str(r["op"]).upper(),
                _f(r["price"]),
                _pct(r["distance_pct"]),
                "✅" if r["direction_ok"] else "❌ 方向失效",
                (mk.upper() if mk else "-"),
                _pct(r.get("drift_pct")) if r.get("drift_pct") is not None else "-",
                (f"{r['suggest_price']}" if r.get("suggest_price") else "-"),
            ])
        lines.append(_table(
            ["标的", "规则价", "方向", "现价", "距离", "方向可达",
             "均线基准", "漂移", "建议价"],
            table_rows))
        if bad:
            lines += ["", "### 需要处理", ""]
            for r in bad:
                for p in r["problems"]:
                    lines.append(f"- **{names.get(r['code'], r['code'])}** "
                                 f"{r['rule_price']}：{p}")
        lines += ["", "> 方向可达 = 规则的 op 与现价位置是否**还能按预期触发**。"
                      "写成「跌破 38.60」但现价 38.52 已在下方，就是一条死规则——"
                      "不报错、只是永远不响。"]
        return "\n".join(lines)

    async def _load_plan_rules(self):
        """从盯梢计划读 rules（读不到返回空，不抛）。

        走 `plan_ctl.find_plans` + `plan_ctl._plan_probe_config`——这是插件内
        读取**计划级** probe_config 的既有通路（`astock_watch action=plan` 也用
        它）。**不用 `astock_watch status`**：那个只出条数与摘要，拿不到 op 字段，
        而方向校验恰恰只能靠 op + 现价一起算。
        """
        try:
            import astock_plan as plan_ctl
            import astock_price_rule as pr

            def _read() -> Dict[str, Any]:
                plans = plan_ctl.find_plans(self._app)
                if not plans:
                    return {}
                return plan_ctl._plan_probe_config(plans[0]) or {}

            cfg = await asyncio.to_thread(_read)
            rules, _errs = pr.parse_rules((cfg or {}).get("rules"))
            # 自动推断均线基准：**仅当 label 明确写了 MAxx** 才认。
            # 没写的一律不推断 → 视为固定价规则，不做漂移检查（v2.8.4）。
            # 早先"没声明就按 ma20 兜底"，把 -8% 硬止损、前低价也拉去
            # 和 MA20 比，报出"建议改为 6.76"这种会毁掉风控的建议。
            import re as _re
            for r in rules:
                if r.get("ma_key"):
                    continue
                m = _re.search(r"MA\s*(5|10|20|60)", str(r.get("label") or ""),
                               _re.IGNORECASE)
                if m:
                    r["ma_key"] = f"ma{m.group(1)}"
            return rules, "当前盯梢计划"
        except Exception:
            logger.debug("[astock] 读计划 rules 失败", exc_info=True)
            return [], ""

    # ---------------------------------------------------------------- 算账

    def _calc_cost(self, args: Dict[str, Any]) -> str:
        """成本 / 盈亏平衡 / 止损止盈 / 盈亏比 / 可买股数。"""
        try:
            entry = float(args.get("entry_price") or args.get("cost") or 0)
            shares = int(args.get("shares") or 0)
        except (TypeError, ValueError):
            return "[Error] entry_price / shares 需要是数字"
        if entry <= 0 or shares <= 0:
            return "[Error] 需要 entry_price（成本价）与 shares（股数）"

        buy_fee = float(args.get("buy_fee") or 0.0)
        sell_rate = float(args.get("sell_fee_rate") or
                          (mkt.FeeConfig().commission_rate
                           + mkt.FeeConfig().stamp_tax_rate
                           + mkt.FeeConfig().transfer_fee_rate))
        lines = [f"**成本与盈亏计算**（{shares} 股 @ {entry}）", ""]

        be = fx.breakeven_price(entry, shares, buy_fee, sell_rate)
        price_rows = [["项目", "数值"]]
        price_rows.append(["成本价", f"{entry}"])
        price_rows.append(["盈亏平衡价", f"**{_f(be, 4)}**"])
        if be:
            price_rows.append(["平衡价溢价", _pct((be - entry) / entry * 100.0)])
        price_rows.append(["卖出费率合计", _ratio(sell_rate * 100.0)])

        stop_pct = float(args.get("stop_pct") or 8.0)
        take_pct = float(args.get("take_pct") or 15.0)
        st = fx.stop_take_prices(entry, stop_pct, take_pct)
        price_rows.append([f"止损价（-{stop_pct}%）", f"{_f(st['stop'])}"])
        price_rows.append([f"止盈价（+{take_pct}%）", f"{_f(st['take'])}"])
        lines.append(_table(price_rows[0], price_rows[1:]))

        if st["stop"] and st["take"]:
            rr = fx.risk_reward_ratio(entry, st["stop"], st["take"])
            lines += ["", f"- **盈亏比**：{_f(rr)}"
                          + ("（<1，赚得比亏得少，位置不划算）" if rr and rr < 1 else "")]

        # 可买股数（给了现金才算）
        cash = args.get("cash")
        if cash is not None:
            equity = args.get("equity")
            max_pos = args.get("max_position_pct")
            lot = mkt.lot_size(str(args.get("code") or "600000"))
            sz = fx.position_shares(
                float(cash), float(args.get("price") or entry),
                equity=float(equity) if equity is not None else None,
                max_position_pct=float(max_pos) if max_pos is not None else None,
                lot=lot,
                cash_usage_pct=float(args.get("cash_usage_pct") or 100.0))
            lines += ["", "**可买股数**", ""]
            lines.append(f"- {sz['shares']} 股（约 {_f(sz['amount'])} 元），"
                         f"受限于「{sz['binding']}」，买入后剩余现金 "
                         f"{_f(sz['cash_left'])} 元")
            if sz["shares"] <= 0:
                lines.append("- ⚠️ 买不到一手：现金或单票上限不足。")

        lines += ["", "> 平衡价的含义：**涨回成本价不等于回本**，还要覆盖来回手续费。"
                      "实际费用以券商为准，本表按主流费率估算。"]
        return "\n".join(lines)

    def _calc_adjust(self, args: Dict[str, Any]) -> str:
        """除权对均线的影响估算。"""
        try:
            prev_close = float(args.get("prev_close") or 0)
            dividend = float(args.get("dividend_per_share") or 0)
            n = int(args.get("ma_period") or 60)
        except (TypeError, ValueError):
            return "[Error] prev_close / dividend_per_share / ma_period 需要是数字"
        if prev_close <= 0:
            return "[Error] need args"

        factor = fx.dividend_adjust_factor(prev_close, dividend)
        distortion = fx.ma_distortion_estimate(prev_close, dividend, n)
        lines = [f"**除权影响估算**（除权前收盘 {prev_close}，每股派现 {dividend}）", ""]
        rows = [["项目", "数值"],
                ["前复权调整因子", f"{factor}"],
                ["除权前价格应打折扣", _ratio((1 - factor) * 100.0)]]
        if distortion is not None:
            rows.append([f"不复权算 MA{n} 约虚高", f"**{_ratio(abs(distortion))}**"])
        lines.append(_table(rows[0], rows[1:]))
        lines += ["", "**结论**：",
                  f"- 用**前复权（qfq）**算均线才是真实的级别；不复权会让"
                  f"除权前的价格偏高，均线整体**虚高**。",
                  f"- 但**止损价、成交价必须用原始价**——券商申报的就是那个数。",
                  "", "> 两者不可混用：**均线看复权，价位看原始**。"]
        return "\n".join(lines)

    def _calc_ratio(self, args: Dict[str, Any]) -> str:
        """收益/回撤比（选策略用）。"""
        try:
            ret = float(args.get("total_return_pct"))
            dd = float(args.get("max_drawdown_pct"))
        except (TypeError, ValueError):
            return ("[Error] 需要 total_return_pct（累计收益%，可为负）与 "
                    "max_drawdown_pct（最大回撤%，填负数）")
        r = fx.risk_adjusted_ratio(ret, dd)
        lines = ["**收益 / 回撤比**（选策略先看回撤，再看收益）", ""]
        rows = [["项目", "数值"],
                ["累计收益", _pct(ret)],
                ["最大回撤", _pct(dd)],
                ["收益/回撤比", f"**{_f(r)}**" if r is not None else "-"]]
        lines.append(_table(rows[0], rows[1:]))
        if r is None:
            lines += ["", "回撤缺失或为 0，无法计算（不能除以 0 说「无穷好」）。"]
        else:
            lines += ["", "**怎么用**：这个数越大，说明每承担 1 单位回撤换来的"
                          "收益越多。回撤大的策略即使累计收益更高，"
                          "**你可能在中途就扛不住卖掉**，实盘拿不到回测的数。"]
        return "\n".join(lines)
