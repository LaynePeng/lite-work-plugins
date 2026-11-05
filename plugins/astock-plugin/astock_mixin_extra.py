"""`astock_extra` 工具层：一个 action 分发 + 渲染器，不重复写 30 个工具。

**为什么做成「一个工具 + action 枚举」而不是 30 个独立工具**：

1. **工具面已经 18 个**，再塞 30 个会让模型在选工具时明显变慢、也更容易
   选错（实测工具数超过 ~25 后选择准确率下降）；
2. 这些端点的**调用形态高度一致**（都是「给我某只票/某天的某类数据」），
   拆成独立工具只会把同一段取数→渲染逻辑复制 30 遍；
3. 分组语义清晰：`打板 / 资金筹码 / 事件 / 资料 / 宏观 / 筹码分布`，
   模型只需按用户意图选 action，不会把 `astock_flow`（个股资金流）
   和 `astock_extra action=board_flow`（板块资金流）搞混——
   后者的帮助文案里明确写了区别。

**同理不做的两件事**：
  · 不在这一层做判断（不喊单、不给概率、不合成情绪分）；
  · 不把多路结果拼成"综合评分"——可靠度差着数量级，合成只会掩盖冲突。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

import astock_extra_data as extra
import astock_market as mkt
from astock_common import _f, _money, _pct, _ratio, _table

logger = logging.getLogger("litework.astock.extra")


def _dash(value: Any, digits: int = 2, dash: str = "-") -> str:
    """数值格式化（缺数据一律破折号，绝不显示 0）。"""
    return _f(value, digits, dash)


def _wan(value: Any) -> str:
    """万元单位的数值 → 人类可读（接口给的就是万元时用）。"""
    return _money(value * 1e4) if isinstance(value, (int, float)) else "-"


class ExtraToolsMixin:
    """扩展数据工具（`astock_extra`）。

    由 `AstockPlugin` 继承；mixin 单独不可用（依赖宿主的 `_fetch_quotes` /
    `_get_ledger` 与设置页配置）。
    """

    # ------------------------------------------------------------ 打板

    async def _x_sentiment(self, args: Dict[str, Any]) -> str:
        """打板情绪：涨停/炸板/跌停家数 + 炸板率 + 连板梯队 + 涨停原因。"""
        date = str(args.get("date") or "").strip() or None
        snap = await asyncio.to_thread(extra.limit_sentiment, date)
        day = snap["date"]
        readable = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day
        if not snap["zt_count"] and not snap["zb_count"] and not snap["dt_count"]:
            return (f"{readable} 三张池子全为空。**多半不是交易日**（周末/节假日），"
                    f"而不是「零涨停」——东方财富涨停池在非交易日返回 data=null。\n"
                    f"若确认是交易日，则说明接口未更新，稍后再试。")

        lines = [f"**打板情绪**（{readable}）", ""]
        ladder = snap["ladder"]
        ladder_txt = " / ".join(f"{k}板 {v} 家" for k, v in sorted(ladder.items(),
                                                                  reverse=True)) or "-"
        lines.append(_table(["指标", "数值"], [
            ["涨停家数", str(snap["zt_count"])],
            ["炸板家数", str(snap["zb_count"])],
            ["跌停家数", str(snap["dt_count"])],
            ["炸板率", f"{snap['break_rate_pct']}%" if snap["break_rate_pct"] is not None else "-"],
            ["最高连板", f"{snap['max_streak']} 板" if snap["max_streak"] else "-"],
            ["连板梯队", ladder_txt],
        ]))

        # 涨停原因题材（同花顺独家）；拿不到不影响上面的正式结论
        reason_map: Dict[str, Dict[str, Any]] = {}
        try:
            for item in await asyncio.to_thread(extra.ths_limit_reason, day):
                if item.get("code"):
                    reason_map[item["code"]] = item
        except Exception as exc:       # noqa: BLE001 - 辅助源失败不阻断主结论
            logger.info("[astock] 同花顺涨停揭秘失败: %s", exc)

        zt = snap["zt_stocks"][:15]
        if zt:
            rows = []
            for s in zt:
                r = reason_map.get(s["code"]) or {}
                rows.append([
                    f"{s['name']}({s['code']})",
                    _f(s.get("price")),
                    _pct(s.get("change_pct")),
                    f"{s.get('streak_boards') or '-'}板",
                    _money(s.get("seal_fund_yuan")),
                    str(s.get("first_seal") or "-"),
                    r.get("reason") or "-",
                ])
            lines += ["", f"**涨停前 {len(rows)} 只**（按首次封板时间）", "",
                      _table(["名称(代码)", "现价", "涨跌幅", "连板", "封板资金",
                              "首封", "涨停原因(同花顺)"], rows)]
            if not reason_map:
                lines += ["", "注：同花顺涨停揭秘当前不可用，涨停原因留空（不是没有原因）。"]

        if snap["dt_stocks"]:
            lines += ["", "**跌停**　" + "、".join(
                f"{s['name']}({s['code']})" for s in snap["dt_stocks"][:10])]
        lines += ["", "注：炸板率 = 炸板 / (涨停 + 炸板)；只统计当日封过板的标的。"
                      "以上为当日情绪数据，不预测次日。"]
        return "\n".join(lines)

    async def _x_strong(self, args: Dict[str, Any]) -> str:
        """同花顺当日强势股 + 题材归因（独家题材标签）。"""
        date = str(args.get("date") or "").strip() or None
        limit = max(1, min(int(args.get("limit") or 25), 100))
        try:
            rows = await asyncio.to_thread(extra.ths_strong, date)
        except extra.DataError as exc:
            return f"[Error] 同花顺强势股获取失败：{exc}"
        if not rows:
            return "同花顺强势股当日无数据（非交易日或接口未更新）。"
        rows = rows[:limit]
        lines = [f"**当日题材归因**（同花顺热点，{len(rows)} 只）", "",
                 _table(["名称(代码)", "题材归因"],
                        [[f"{r['name']}({r['code']})", r["reason"] or "-"] for r in rows])]
        lines += ["", "注：该接口只给题材标签，不含价格/涨幅——"
                      "要价格请用 `astock_quote`（可批量传 codes）。"
                      "题材标签是媒体归纳，不是投资建议。"]
        return "\n".join(lines)

    async def _x_hot(self, args: Dict[str, Any]) -> str:
        """同花顺热榜（人气值 + 概念标签 + 人气排名变化）。"""
        period = str(args.get("period") or "hour").lower()
        if period not in ("hour", "day"):
            period = "hour"
        limit = max(1, min(int(args.get("limit") or 20), 100))
        rows = await asyncio.to_thread(extra.ths_hot_list, period)
        if not rows:
            return "同花顺热榜当前无数据。"
        rows = rows[:limit]
        lines = [f"**人气热榜**（同花顺{'小时' if period == 'hour' else '日'}榜，"
                 f"前 {len(rows)}）", "",
                 _table(["排名", "名称(代码)", "人气值", "涨跌幅", "排名变化", "概念", "标签"],
                        [[str(r["rank"] or "-"), f"{r['name']}({r['code']})",
                          _f(r.get("heat"), 0), _pct(r.get("change_pct")),
                          ("+" if isinstance(r.get("rank_change"), int)
                           and r["rank_change"] > 0 else "")
                          + str(r.get("rank_change") if r.get("rank_change") is not None else "-"),
                          "、".join(r.get("concepts") or []) or "-",
                          r.get("tag") or "-"] for r in rows])]
        lines += ["", "注：人气值是散户关注度（crowd 级证据），热度高不等于该涨。"]
        return "\n".join(lines)

    # ------------------------------------------------------------ 板块

    async def _x_industry(self, args: Dict[str, Any]) -> str:
        """全行业涨跌幅排名（东财行业板块）。"""
        top_n = max(3, min(int(args.get("top_n") or 10), 30))
        res = await asyncio.to_thread(extra.industry_rank, top_n)

        def rows_of(items: List[Dict[str, Any]]) -> List[List[str]]:
            return [[str(r["rank"]), r["name"], _pct(r.get("change_pct")),
                     f"{r.get('up_count') or 0}涨/{r.get('down_count') or 0}跌",
                     (f"{r['leader']}({_pct(r.get('leader_change_pct'))})"
                      if r.get("leader") else "-")] for r in items]

        headers = ["排名", "行业", "涨跌幅", "涨跌家数", "领涨股"]
        lines = [f"**行业板块排名**（共 {res['total']} 个行业）", "",
                 "涨幅榜 TOP：", "",
                 _table(headers, rows_of(res["top"])), "",
                 "跌幅榜 TOP：", "",
                 _table(headers, rows_of(res["bottom"][::-1]))]
        lines += ["", "注：板块涨跌幅是该板块成分股加权结果，用于定位当日资金偏好，"
                      "不代表板块内每只个股同向。"]
        return "\n".join(lines)

    async def _x_board_flow(self, args: Dict[str, Any]) -> str:
        """板块资金流向（行业/概念/地域 × 今日/5日/10日）。"""
        btype = str(args.get("board_type") or "industry").lower()
        period = str(args.get("period") or "today").lower()
        top_n = max(3, min(int(args.get("top_n") or 15), 50))
        try:
            res = await asyncio.to_thread(extra.board_fund_flow, btype, period, top_n)
        except ValueError as exc:
            return f"[Error] {exc}"
        labels = {"industry": "行业", "concept": "概念", "region": "地域"}
        plabel = {"today": "今日", "5d": "5日", "10d": "10日"}
        if not res["rows"]:
            return f"{labels.get(btype, btype)}板块 {plabel.get(period, period)} 资金流为空。"
        lines = [f"**{labels.get(btype, btype)}板块资金流**（{plabel.get(period, period)}，"
                 f"共 {res['total']} 个，按主力净额降序）", "",
                 _table(["排名", "板块", "涨跌幅", "主力净额", "主力净占比", "领涨股"],
                        [[str(r["rank"]), r["name"], _pct(r.get("change_pct")),
                          _money(r.get("main_net_yuan")),
                          _ratio(r.get("main_pct")) if r.get("main_pct") is not None else "-",
                          r.get("leader") or "-"] for r in res["rows"]])]
        lines += ["", "注：主力净额 = 超大单 + 大单，单位已换算；"
                      "该数值是结果而非原因，流入不等于后续上涨。"
                      "板块级只有 今日/5日/10日（3 日档个股级才有）。"]
        return "\n".join(lines)

    async def _x_st(self, args: Dict[str, Any]) -> str:
        """全市场 ST / *ST 名单（风险警示 + 涨跌停 5% 口径）。"""
        rows = await asyncio.to_thread(extra.st_stocks)
        market = str(args.get("market") or "").strip().lower()
        if market in ("sh", "sz", "bj"):
            rows = [r for r in rows if r.get("market") == market]
        limit = max(1, min(int(args.get("limit") or 50), 200))
        shown = rows[:limit]
        if not shown:
            return f"ST 名单里没有{'该市场' if market else ''}的记录。"
        lines = [f"**ST / *ST 名单**（{'全部' if not market else market.upper()}，"
                 f"共 {len(rows)} 只，显示前 {len(shown)}）", "",
                 _table(["名称(代码)", "市场", "类型", "现价", "涨跌幅"],
                        [[f"{r['name']}({r['code']})", r["market"].upper(),
                          r["st_type"], _f(r.get("price")), _pct(r.get("change_pct"))]
                         for r in shown])]
        lines += ["", "注：ST / *ST 涨跌停限制为 ±5%（主板口径），"
                      "`astock_hint` 计算止损止盈时会按此口径。"
                      "名单随风险警示变动，非交易日与盘后可能滞后。"]
        return "\n".join(lines)

    # ------------------------------------------------------------ 资金 / 筹码

    async def _x_margin(self, args: Dict[str, Any]) -> str:
        """融资融券余额趋势（杠杆资金态度）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, _venue = mkt.normalize_code(code)
        days = max(2, min(int(args.get("days") or 10), 60))
        rows = await asyncio.to_thread(extra.margin_trading, norm, days)
        if not rows:
            return (f"{norm} 没有两融数据（可能不是两融标的、或该市场不覆盖）。"
                    f"两融标的名录可查交易所公告。")
        rows = sorted(rows, key=lambda r: r.get("date") or "")
        latest = rows[-1]
        trend_pct = None
        if len(rows) >= 2 and latest.get("fin_balance_yuan") and rows[0].get("fin_balance_yuan"):
            trend_pct = mkt.pct_change(latest["fin_balance_yuan"],
                                       rows[0]["fin_balance_yuan"])
        lines = [f"**{norm} 两融余额**（{len(rows)} 个交易日）", ""]
        lines.append(_table(["项目", "数值"], [
            ["最新日期", latest.get("date") or "-"],
            ["融资余额", _money(latest.get("fin_balance_yuan"))],
            ["融券余额", _money(latest.get("sec_balance_yuan"))],
            ["两融余额合计", _money(latest.get("total_balance_yuan"))],
            ["区间融资余额变化", _pct(trend_pct)],
        ]))
        lines += ["", _table(["日期", "融资余额", "融资买入", "融资净买", "融券余额"],
                             [[r.get("date") or "-", _money(r.get("fin_balance_yuan")),
                               _money(r.get("fin_buy_yuan")),
                               _money(r.get("fin_net_buy_yuan")),
                               _money(r.get("sec_balance_yuan"))] for r in reversed(rows)])]
        lines += ["", "注：两融余额反映杠杆资金持仓意愿，数据 T+1 披露（当日盘后才会出现今天的数据）。"]
        return "\n".join(lines)

    async def _x_block_trade(self, args: Dict[str, Any]) -> str:
        """大宗交易记录（溢价/折价成交）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, _venue = mkt.normalize_code(code)
        rows = await asyncio.to_thread(extra.block_trade, norm,
                                       max(1, min(int(args.get("limit") or 15), 50)))
        if not rows:
            return f"{norm} 近期没有大宗交易记录（不是数据缺失）。"
        lines = [f"**{norm} 大宗交易**（{len(rows)} 笔）", "",
                 _table(["日期", "成交价", "当日收盘", "溢价率", "成交量(股)", "成交额", "买方", "卖方"],
                        [[r.get("date") or "-", _f(r.get("price")), _f(r.get("close")),
                          _pct(r.get("premium_pct")), _f(r.get("volume"), 0),
                          _money(r.get("amount_yuan")),
                          (r.get("buyer") or "-")[:14], (r.get("seller") or "-")[:14]]
                         for r in rows])]
        lines += ["", "注：折价（负溢价）成交常见于大股东/机构减持，但单笔大宗不构成方向判断依据。"]
        return "\n".join(lines)

    async def _x_holders(self, args: Dict[str, Any]) -> str:
        """股东户数变化（筹码集中度）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, _venue = mkt.normalize_code(code)
        rows = await asyncio.to_thread(extra.holder_count, norm,
                                        max(2, min(int(args.get("limit") or 8), 20)))
        if not rows:
            return f"{norm} 没有股东户数数据。"
        rows = sorted(rows, key=lambda r: r.get("end_date") or "")
        lines = [f"**{norm} 股东户数**（{len(rows)} 期）", "",
                 _table(["报告期", "股东户数", "变化户数", "环比", "户均持仓市值"],
                        [[r.get("end_date") or "-", _f(r.get("holders"), 0),
                          _f(r.get("change_num"), 0), _pct(r.get("change_pct")),
                          _money(r.get("avg_hold_value_yuan"))] for r in reversed(rows)])]
        latest = rows[-1]
        chg = latest.get("change_pct")
        if chg is not None:
            verdict = ("户数下降 → 筹码趋于集中（通常偏正面）" if chg < 0
                       else "户数上升 → 筹码分散、散户增多（通常偏负面）" if chg > 0
                       else "户数持平")
            lines += ["", f"最新一期（{latest.get('end_date')}）：{verdict}。"]
        lines += ["", "注：股东户数按季披露（季末后数周），滞后性很强，"
                      "不能当短期信号用。"]
        return "\n".join(lines)

    async def _x_dividend(self, args: Dict[str, Any]) -> str:
        """分红送转历史（每股派息 / 每 10 股送转）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, _venue = mkt.normalize_code(code)
        rows = await asyncio.to_thread(extra.dividend_history, norm,
                                       max(1, min(int(args.get("limit") or 10), 30)))
        if not rows:
            return f"{norm} 没有分红送转记录。"
        lines = [f"**{norm} 分红送转**（{len(rows)} 次）", "",
                 _table(["除权除息日", "报告期", "每股派息(税前)", "每10股送股",
                         "每10股转增", "进度"],
                        [[r.get("ex_date") or "-", r.get("report_date") or "-",
                          _f(r.get("bonus_rmb_per_share"), 3),
                          _f(r.get("bonus_per_10")), _f(r.get("transfer_per_10")),
                          r.get("progress") or "-"] for r in rows])]
        lines += ["", "注：每股派息是**税前**口径，实际到账要扣红利税（持股期限不同税率不同）。"
                      "除权除息日当天股价会按分红金额下调，不是「白跌」。"]
        return "\n".join(lines)

    async def _x_lockup(self, args: Dict[str, Any]) -> str:
        """限售解禁日历（历史 + 未来 N 天待解禁）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, _venue = mkt.normalize_code(code)
        days = max(7, min(int(args.get("days") or 90), 365))
        res = await asyncio.to_thread(extra.lockup_expiry, norm, days)
        lines = [f"**{norm} 限售解禁**（未来窗口 {res['window']}）", ""]
        up = res["upcoming"]
        if up:
            lines += [f"⚠️ **未来 {len(up)} 批待解禁**", "",
                      _table(["解禁日", "类型", "解禁股数(万股)", "实际可流通(万股)", "占总股本"],
                             [[r.get("date") or "-", r.get("type") or "-",
                               _f(r.get("shares_10k"), 1), _f(r.get("able_shares_10k"), 1),
                               _ratio((r.get("ratio") or 0) * 100)
                               if r.get("ratio") is not None else "-"] for r in up])]
        else:
            lines.append(f"未来 {days} 天内无待解禁记录。")
        hist = res["history"]
        if hist:
            lines += ["", f"**历史解禁**（最近 {min(len(hist), 8)} 批）", "",
                      _table(["解禁日", "类型", "解禁股数(万股)", "实际可流通(万股)"],
                             [[r.get("date") or "-", (r.get("type") or "-")[:20],
                               _f(r.get("shares_10k"), 1),
                               _f(r.get("able_shares_10k"), 1)] for r in hist[:8]])]
        lines += ["", "注：「实际可流通」比「解禁股数」更贴近真实抛压——"
                      "公告的解禁数量常含仍未流通的部分。解禁≠立刻卖出。"]
        return "\n".join(lines)

    async def _x_lhb(self, args: Dict[str, Any]) -> str:
        """龙虎榜：给 code 看个股席位明细，不给看全市场榜单。"""
        code = str(args.get("code") or "").strip()
        if code:
            norm, _venue = mkt.normalize_code(code)
            res = await asyncio.to_thread(extra.lhb_stock, norm,
                                          max(30, min(int(args.get("days") or 180), 720)))
            rec = res["records"]
            if not rec:
                return (f"{norm} 近 {res['window_days']} 天无龙虎榜记录"
                        f"（不是数据缺失——多数标的本来就不常上榜）。")
            lines = [f"**{norm} 龙虎榜**（近 {res['window_days']} 天，上榜 {len(rec)} 次）", "",
                     _table(["日期", "上榜原因", "净买入(万)", "换手率", "涨跌幅"],
                            [[r.get("date") or "-", (r.get("reason") or "-")[:26],
                              _f(r.get("net_buy_wan"), 1), _ratio(r.get("turnover_pct")),
                              _pct(r.get("change_pct"))] for r in rec[:12]])]
            latest = res["latest_date"]
            if res["seats"]["buy"] or res["seats"]["sell"]:
                lines += ["", f"**{latest} 买入席位 TOP5**", "",
                          _table(["营业部", "买入(万)", "卖出(万)", "净额(万)"],
                                 [[s["name"][:22], _f(s["buy_wan"], 1),
                                   _f(s["sell_wan"], 1), _f(s["net_wan"], 1)]
                                  for s in res["seats"]["buy"]]),
                          "", f"**{latest} 卖出席位 TOP5**", "",
                          _table(["营业部", "买入(万)", "卖出(万)", "净额(万)"],
                                 [[s["name"][:22], _f(s["buy_wan"], 1),
                                   _f(s["sell_wan"], 1), _f(s["net_wan"], 1)]
                                  for s in res["seats"]["sell"]])]
            inst = res["institution"]
            if inst["buy_wan"] or inst["sell_wan"]:
                lines += ["", f"机构专用席位：买 {_f(inst['buy_wan'], 1)} 万 / "
                              f"卖 {_f(inst['sell_wan'], 1)} 万 / "
                              f"净 {_f(inst['net_wan'], 1)} 万"]
            lines += ["", "注：营业部席位含游资与量化，不必然是机构；"
                          "「机构专用」代码为 0 的才是机构。"]
            return "\n".join(lines)

        res = await asyncio.to_thread(extra.lhb_market,
                                      str(args.get("date") or "") or None,
                                      args.get("min_net_wan"))
        if not res["stocks"]:
            return (f"{res['date']} 龙虎榜无记录（非交易日，或盘后数据尚未更新）。"
                    f"原始记录数 {res['raw_records']}。")
        limit = max(5, min(int(args.get("limit") or 20), 100))
        stocks = res["stocks"][:limit]
        lines = [f"**全市场龙虎榜**（{res['date']}，{res['total']} 条，显示前 {len(stocks)}）", "",
                 _table(["名称(代码)", "上榜原因", "收盘", "涨跌幅", "净买入(万)",
                         "买入(万)", "卖出(万)"],
                        [[f"{s['name']}({s['code']})", (s["reason"] or "-")[:24],
                          _f(s.get("close")), _pct(s.get("change_pct")),
                          _f(s.get("net_buy_wan"), 1), _f(s.get("buy_wan"), 1),
                          _f(s.get("sell_wan"), 1)] for s in stocks])]
        lines += ["", "注：龙虎榜是交易所披露的异动个股成交席位，"
                      "净买入为正代表榜内席位合计买入更多，不等于后续会涨。"]
        return "\n".join(lines)

    async def _x_concepts(self, args: Dict[str, Any]) -> str:
        """个股所属板块/概念归属（一次请求拿全）。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, venue = mkt.normalize_code(code)
        res = await asyncio.to_thread(extra.concept_blocks, norm, venue)
        if not res["boards"]:
            return f"{norm} 没有板块归属数据。"
        lines = [f"**{norm} 板块/概念归属**（共 {res['total']} 个）", "",
                 _table(["板块/概念", "板块代码", "板块涨跌幅", "板块龙头"],
                        [[b["name"], b["code"], _pct(b.get("change_pct")),
                          b.get("leader") or "-"] for b in res["boards"]])]
        lines += ["", "注：混合行业/概念/地域三类，板块名自解释。"
                      "「被归到某个热门概念」不等于主营相关——题材炒作常见牵强挂靠。"]
        return "\n".join(lines)

    async def _x_chips(self, args: Dict[str, Any]) -> str:
        """筹码分布 CYQ：获利比例 / 平均成本 / 成本区间 / 集中度 / 筹码峰。"""
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, venue = mkt.normalize_code(code)
        days = max(30, min(int(args.get("days") or 150), 640))
        decay = float(args.get("decay") or 1.0)
        bars = await asyncio.to_thread(extra.kline_with_turnover, norm, venue, days,
                                       str(args.get("adjust") or "qfq"))
        if not bars:
            return f"{norm} 没有可用 K 线，无法计算筹码分布。"
        res = await asyncio.to_thread(extra.chip_distribution, bars, 300, decay)
        c90 = res["cost_90"]
        c70 = res["cost_70"]
        lines = [f"**{norm} 筹码分布**（{res['bars']} 个交易日，"
                 f"{res['first_date']} → {res['last_date']}，衰减系数 {decay}）", "",
                 _table(["指标", "数值"], [
                     ["现价", _f(res["price"], 3)],
                     ["获利比例", f"{res['profit_ratio'] * 100:.1f}%"],
                     ["平均成本", _f(res["avg_cost"], 3)],
                     ["90% 成本区间", f"{_f(c90[0], 2)} ~ {_f(c90[1], 2)}"],
                     ["70% 成本区间", f"{_f(c70[0], 2)} ~ {_f(c70[1], 2)}"],
                     ["90% 集中度", (f"{res['concentration_90'] * 100:.2f}%"
                                    if res["concentration_90"] is not None else "-")],
                     ["70% 集中度", (f"{res['concentration_70'] * 100:.2f}%"
                                    if res["concentration_70"] is not None else "-")],
                     ["筹码峰", _f(res["peak_price"], 3)],
                     ["窗口累计换手", _ratio(res["cum_turnover_pct"])],
                 ])]
        # 口径提示是这个指标最容易误用的地方，必须显式写出来
        cum = res["cum_turnover_pct"]
        lines += ["", "**口径提示（重要）**：", "",
                  f"- 本窗口累计换手 {cum:.1f}%"
                  + ("，**不足 100%** —— 窗口之前的筹码仍大量存在，"
                     "平均成本与获利比例只代表「按本窗口成交推算的分布」，"
                     "不代表全市场真实持仓成本。窗口越短，这个偏差越大："
                     "60 日窗口累计换手往往只有十几个百分点，"
                     "此时「获利比例」几乎只反映窗口内那段行情。"
                     if cum < 100
                     else "，已覆盖一轮完整换手，分布相对可靠。"),
                  "- 筹码分布是按日 K 的 (最高+最低+收盘)/3 与换手率递推的**模型估算**，"
                  "不是交易所披露的真实持仓数据。",
                  "- 集中度越低说明筹码越分散；筹码峰在现价上方意味着上方套牢盘较重。"]
        return "\n".join(lines)

    # ------------------------------------------------------------ 事件驱动

    async def _x_events_data(self, args: Dict[str, Any]) -> str:
        """事件数据：业绩预告 / 机构调研 / 股东增减持 / 回购 / 质押 / 新股 / 可转债。"""
        what = str(args.get("event") or "forecast").lower()
        code = str(args.get("code") or "").strip()
        days = max(1, min(int(args.get("days") or 30), 365))
        limit = max(1, min(int(args.get("limit") or 20), 100))
        norm = ""
        if code:
            norm, _venue = mkt.normalize_code(code)

        if what == "forecast":
            rows = await asyncio.to_thread(extra.earnings_forecast, norm or None,
                                           str(args.get("report_date") or "") or None,
                                           limit)
            if not rows:
                return "没有业绩预告记录（非披露季或该股未披露）。"
            return "\n".join([
                f"**业绩预告**（{len(rows)} 条{'，' + norm if norm else '，全市场最新'}）", "",
                _table(["名称(代码)", "公告日", "报告期", "指标", "类型",
                        "预计下限", "预计上限", "同比区间"],
                       [[f"{r['name']}({r['code']})", r.get("notice_date") or "-",
                         r.get("report_date") or "-", r.get("indicator") or "-",
                         r.get("type") or "-", _money(r.get("amount_lower_yuan")),
                         _money(r.get("amount_upper_yuan")),
                         (f"{_pct(r.get('change_pct_lower'))} ~ {_pct(r.get('change_pct_upper'))}"
                          if r.get("change_pct_lower") is not None else "-")]
                        for r in rows]),
                "", "注：一次预告会拆成多行（`指标` 不同）；金额单位为元。"
                    "预告是公司自己给的区间，不等于最终财报。"])

        if what == "survey":
            rows = await asyncio.to_thread(extra.institution_survey, norm or None,
                                           days, limit)
            if not rows:
                return f"近 {days} 天没有机构调研记录。"
            return "\n".join([
                f"**机构调研**（近 {days} 天，{len(rows)} 场）", "",
                _table(["名称(代码)", "公告日", "接待日", "机构家数", "方式", "接待人"],
                       [[f"{r['name']}({r['code']})", r.get("notice_date") or "-",
                         r.get("survey_date") or "-", _f(r.get("org_count"), 0),
                         (r.get("way") or "-")[:16], (r.get("receptionist") or "-")[:20]]
                        for r in rows]),
                "", "注：调研家数多说明机构关注度高，但调研本身不含任何买卖动作。"])

        if what == "holder_trade":
            direction = str(args.get("direction") or "").strip() or None
            rows = await asyncio.to_thread(extra.holder_trades, norm or None,
                                           direction, days, limit)
            if not rows:
                return f"近 {days} 天没有股东增减持记录。"
            return "\n".join([
                f"**股东增减持**（近 {days} 天，{len(rows)} 条"
                f"{'，' + direction if direction else ''}）", "",
                _table(["名称(代码)", "股东", "方向", "变动股数(万股)", "变动后占比",
                        "均价", "方式", "公告日"],
                       [[f"{r['name']}({r['code']})", (r.get("holder") or "-")[:18],
                         r.get("direction") or "-", _f(r.get("change_shares_10k"), 2),
                         _ratio(r.get("after_pct_total")), _f(r.get("avg_price")),
                         r.get("channel") or "-", r.get("notice_date") or "-"]
                        for r in rows]),
                "", "注：股数单位万股，减持为负；`变动后占比` 是该股东持股占总股本 %。"
                    "均价很多公告不披露，留空不是 0。"])

        if what in ("buyback", "pledge"):
            if what == "buyback":
                rows = await asyncio.to_thread(extra.share_buyback, norm or None,
                                               None, limit)
                if not rows:
                    return f"{norm or '全市场'} 没有回购记录。"
                return "\n".join([
                    f"**{norm or '全市场'} 股票回购**（{len(rows)} 个方案）", "",
                    _table(["进度", "方案起", "方案止", "价格上限", "金额下限", "金额上限",
                            "已回购股数", "已回购金额", "最新公告"],
                           [[r.get("progress") or f"进度码{r.get('progress_code')}",
                             r.get("plan_start") or "-", r.get("plan_end") or "-",
                             _f(r.get("price_cap")), _money(r.get("amount_lower_yuan")),
                             _money(r.get("amount_upper_yuan")),
                             _f(r.get("done_shares"), 0), _money(r.get("done_amount_yuan")),
                             r.get("latest_notice") or "-"] for r in rows]),
                    "", "注：「已回购」为实施进度，方案金额区间是计划而非承诺。"
                        "回购不必然注销（也可能用于股权激励），对每股价值影响不同。"])
            rows = await asyncio.to_thread(extra.equity_pledge, norm or None, limit)
            if not rows:
                return (f"{norm or '全市场'} 没有质押统计记录"
                        f"（可能是北交所——中国结算质押只覆盖沪深）。")
            return "\n".join([
                f"**{norm or '最近统计日全市场'} 股权质押**（{len(rows)} 条）", "",
                _table(["统计日", "名称(代码)", "质押比例", "质押股数(万股)",
                        "质押市值(万元)", "质押笔数"],
                       [[r.get("date") or "-", f"{r.get('name') or '-'}({r.get('code') or '-'})",
                         _ratio(r.get("pledge_ratio_pct")),
                         _f(r.get("pledged_shares_10k"), 1),
                         _f(r.get("pledged_mktcap_10k"), 1),
                         _f(r.get("pledge_count"), 0)] for r in rows]),
                "", "注：中国结算按周统计（通常周五）。质押比例高（>50%）意味着大股东平仓风险，"
                    "但质押比例低不代表没有风险——要看具体质押价。"])

        if what == "ipo":
            rows = await asyncio.to_thread(extra.ipo_calendar, limit)
            if not rows:
                return "新股申购日历为空。"
            return "\n".join([
                f"**新股申购日历**（{len(rows)} 条，按申购日倒序）", "",
                _table(["名称(代码)", "申购代码", "板块", "申购日", "上市日",
                        "发行价", "发行PE", "行业PE", "中签率"],
                       [[f"{r['name']}({r['code']})", r.get("apply_code") or "-",
                         r.get("board") or "-", r.get("apply_date") or "-",
                         r.get("listing_date") or "-", _f(r.get("issue_price")),
                         _f(r.get("issue_pe")), _f(r.get("industry_pe")),
                         _ratio(r.get("win_rate_pct"))] for r in rows]),
                "", "注：发行价在定价前为空（不是 0）；中签率是网上申购中签率 %。"])

        if what == "convertible":
            rows = await asyncio.to_thread(
                extra.convertible_bonds, bool(args.get("include_delisted")), 1000)
            if norm:
                rows = [r for r in rows if r.get("stock_code") == norm
                        or r.get("code") == norm]
            if not rows:
                return f"没有可转债记录{'（正股 ' + norm + '）' if norm else ''}。"
            rows = sorted(rows, key=lambda r: (r.get("premium_pct") is None,
                                               r.get("premium_pct") or 0))[:limit]
            return "\n".join([
                f"**可转债**（{len(rows)} 只，按溢价率升序）", "",
                _table(["转债(代码)", "正股", "状态", "评级", "债价", "正股价",
                        "转股价", "转股价值", "溢价率"],
                       [[f"{r['name']}({r['code']})",
                         f"{r.get('stock_name') or '-'}({r.get('stock_code') or '-'})",
                         r.get("status") or "-", r.get("rating") or "-",
                         _f(r.get("bond_price")), _f(r.get("stock_price")),
                         _f(r.get("convert_price")), _f(r.get("convert_value")),
                         _pct(r.get("premium_pct"))] for r in rows]),
                "", "注：转股价值 = 100 / 转股价 × 正股价；溢价率 = 债价 / 转股价值 − 1。"
                    "溢价率为负代表转股后立刻卖出比持有转债更划算（需考虑流动性）。"])

        return (f"[Error] 未知 event={what}。可用：forecast 业绩预告 / survey 机构调研 / "
                f"holder_trade 增减持 / buyback 回购 / pledge 质押 / ipo 新股 / "
                f"convertible 可转债")

    # ------------------------------------------------------------ 研究资料

    async def _x_docs(self, args: Dict[str, Any]) -> str:
        """研究资料：巨潮公告 / 个股研报 / 财报三表。"""
        what = str(args.get("doc") or "announce").lower()
        code = str(args.get("code") or "").strip()
        if not code:
            return "[Error] 需要 code"
        norm, venue = mkt.normalize_code(code)
        limit = max(1, min(int(args.get("limit") or 15), 50))

        if what == "announce":
            rows = await asyncio.to_thread(extra.cninfo_announcements, norm, limit)
            if not rows:
                return f"{norm} 近期没有巨潮公告记录。"
            return "\n".join([
                f"**{norm} 巨潮公告**（最近 {len(rows)} 条）", "",
                _table(["披露日", "标题", "类型"],
                       [[r.get("date") or "-", (r.get("title") or "-")[:60],
                         (r.get("type") or "-")[:16]] for r in rows]),
                "", "注：巨潮资讯是证监会指定披露平台，公告属**法定披露事实**"
                    "（可靠度最高的一档证据）。标题即原文标题，未做解读——"
                    "要点开原文看具体内容与金额。"])

        if what == "report":
            months = max(1, min(int(args.get("months") or 12), 60))
            rows = await asyncio.to_thread(extra.eastmoney_reports, norm, months, limit)
            if not rows:
                return f"{norm} 近 {months} 个月没有研报覆盖（小市值标的常见）。"
            return "\n".join([
                f"**{norm} 个股研报**（近 {months} 个月，{len(rows)} 篇）", "",
                _table(["发布日期", "机构", "评级", "标题", "今年EPS", "明年EPS", "今年PE"],
                       [[r.get("date") or "-", r.get("org") or "-",
                         r.get("rating") or "-", (r.get("title") or "-")[:34],
                         _f(r.get("eps_this_year")), _f(r.get("eps_next_year")),
                         _f(r.get("pe_this_year"))] for r in rows]),
                "", "注：研报是**卖方观点**（official_view 档），不是事实。"
                    "评级集中度高是行业惯例，别把「全是买入」当成安全信号。"]) 

        if what == "finance":
            rtype = str(args.get("report_type") or "lrb").lower()
            if rtype not in ("fzb", "lrb", "llb"):
                return "[Error] report_type 只能是 fzb(资产负债) / lrb(利润) / llb(现金流)"
            rows = await asyncio.to_thread(extra.sina_financial_report, norm, venue,
                                           rtype, limit)
            if not rows:
                return f"{norm} 没有财报数据。"
            names = {"fzb": "资产负债表", "lrb": "利润表", "llb": "现金流量表"}
            lines = [f"**{norm} {names[rtype]}**（最近 {len(rows)} 期）", ""]
            for rec in rows:
                items = rec["items"]
                pick = [k for k in items if k in (
                    "营业总收入", "营业收入", "净利润", "归属于母公司所有者的净利润",
                    "资产总计", "负债合计", "经营活动产生的现金流量净额",
                    "货币资金", "应收账款")]
                if not pick:
                    pick = list(items)[:8]
                lines += [f"**{rec['period']}**", "",
                          _table(["科目", "金额", "同比"],
                                 [[k, str(items.get(k) or "-"),
                                   str(rec["yoy"].get(k) or "-")] for k in pick[:10]]), ""]
            lines.append("注：新浪口径，金额单位随科目（多为元）。"
                         "同比字段接口不一定给，留空不是 0。")
            return "\n".join(lines)

        return ("[Error] 未知 doc。可用：announce 巨潮公告 / report 研报 / "
                "finance 财报三表")

    # ------------------------------------------------------------ 宏观 / 利率

    async def _x_macro(self, args: Dict[str, Any]) -> str:
        """宏观与利率：LPR / PMI / 国债收益率 / 回购定盘利率 / 交易日历。"""
        what = str(args.get("macro") or "lpr").lower()

        if what == "lpr":
            rows = await asyncio.to_thread(extra.lpr_history,
                                           max(1, min(int(args.get("days") or 24), 200)))
            latest = rows[-1]
            lines = [f"**LPR 贷款市场报价利率**（最近 {len(rows)} 次报价）", "",
                     _table(["报价日", "1 年期", "5 年期以上"],
                            [[r["date"], _ratio(r.get("lpr_1y")), _ratio(r.get("lpr_5y"))]
                             for r in reversed(rows)])]
            lines += ["", f"最新（{latest['date']}）：1 年期 {latest.get('lpr_1y')}%、"
                          f"5 年期以上 {latest.get('lpr_5y') or '-'}%。",
                      "", "注：LPR 每月 20 日报价；5 年期品种 2019-08-20 才设立，"
                          "更早的行留空不是 0。"]
            return "\n".join(lines)

        if what == "pmi":
            res = await asyncio.to_thread(extra.nbs_pmi)
            def mark(v: Optional[float]) -> str:
                if v is None:
                    return "-"
                return f"{v:.1f}（{'扩张' if v >= 50 else '收缩'}）"
            return "\n".join([
                f"**PMI 采购经理指数**（{res.get('period') or '-'}）", "",
                _table(["指标", "数值"], [
                    ["制造业 PMI", mark(res.get("manufacturing_pmi"))],
                    ["非制造业商务活动", mark(res.get("non_manufacturing_pmi"))],
                    ["综合 PMI 产出", mark(res.get("composite_pmi"))],
                    ["大型企业", mark(res.get("pmi_large"))],
                    ["中型企业", mark(res.get("pmi_medium"))],
                    ["小型企业", mark(res.get("pmi_small"))],
                ]),
                "", "注：50 是荣枯线，≥50 为扩张。国家统计局每月末发布，"
                    "数据来自官方发布稿正文。"])

        if what == "yield":
            curve = str(args.get("curve") or "treasury").lower()
            res = await asyncio.to_thread(extra.chinabond_yield,
                                          max(1, min(int(args.get("days") or 30), 360)),
                                          curve)
            rows = res["rows"]
            latest_by_curve: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                latest_by_curve[r["curve"]] = r
            lines = [f"**中债收益率曲线**（{res['start']} ~ {res['end']}，"
                     f"{len(rows)} 条记录）", "",
                     _table(["曲线", "日期", "3月", "6月", "1年", "3年", "5年", "7年",
                             "10年", "30年"],
                            [[r["curve"], r["date"]]
                             + [_ratio(r.get(k)) for k in extra._CB_TENORS]
                             for r in sorted(latest_by_curve.values(),
                                             key=lambda x: x["curve"])])]
            lines += ["", "注：单位 %。中债不公布债券市场交易日历，"
                          "整天缺失（非交易日）无法与「接口漏数据」区分，"
                          "区间内 7 天以上 0 行才报错。"]
            return "\n".join(lines)

        if what == "repo":
            kind = str(args.get("kind") or "FR").upper()
            rows = await asyncio.to_thread(extra.repo_fixing, kind)
            tail = rows[-max(1, min(int(args.get("days") or 10), 60)):]
            k = kind.lower()
            return "\n".join([
                f"**银行间回购定盘利率 {kind}**（最近 {len(tail)} 个交易日，%）", "",
                _table(["日期", f"{kind}001(隔夜)", f"{kind}007(7天)", f"{kind}014(14天)"],
                       [[r["date"], _ratio(r.get(f"{k}001")), _ratio(r.get(f"{k}007")),
                         _ratio(r.get(f"{k}014"))] for r in reversed(tail)]),
                "", f"注：来源为中国货币网/外汇交易中心官方。"
                    f"FR 是全市场口径，FDR 是银银间口径（约近 1 年数据）。"])

        if what == "calendar":
            year = int(args.get("year") or mkt.now_cn().year)
            month = int(args.get("month") or mkt.now_cn().month)
            res = await asyncio.to_thread(extra.trading_calendar, year, month)
            closed = [d["date"] for d in res["days"] if not d["is_open"]]
            return "\n".join([
                f"**{year} 年 {month} 月交易日历**（深交所官方，共 {len(res['days'])} 天）", "",
                f"交易日 **{res['open_count']}** 天；非交易日 {len(closed)} 天：",
                "", "、".join(closed) if closed else "（无）",
                "", "注：周末调休按官方口径，不按「周末必休」推断。"
                    "沪深交易日一致，此日历可用于全市场排期。"])

        return ("[Error] 未知 macro。可用：lpr 利率 / pmi 采购经理指数 / "
                "yield 国债收益率曲线 / repo 回购定盘利率 / calendar 交易日历")

    # ------------------------------------------------------------ 分发

    async def _tool_extra(self, args: Dict[str, Any]) -> str:
        """`astock_extra` 统一入口：按 action 分到上面各分组。"""
        action = str(args.get("action") or "").strip().lower()
        if not action:
            return ("[Error] 需要 action。打板情绪=limit_sentiment / 题材归因=strong / "
                    "热榜=hot / 行业排名=industry / 板块资金流=board_flow / ST 名单=st / "
                    "两融=margin / 大宗=block_trade / 股东户数=holders / 分红=dividend / "
                    "解禁=lockup / 龙虎榜=lhb / 板块归属=concepts / 筹码分布=chips / "
                    "事件数据=events / 研究资料=docs / 宏观利率=macro")
        table: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "limit_sentiment": self._x_sentiment,
            "strong": self._x_strong,
            "hot": self._x_hot,
            "industry": self._x_industry,
            "board_flow": self._x_board_flow,
            "st": self._x_st,
            "margin": self._x_margin,
            "block_trade": self._x_block_trade,
            "holders": self._x_holders,
            "dividend": self._x_dividend,
            "lockup": self._x_lockup,
            "lhb": self._x_lhb,
            "concepts": self._x_concepts,
            "chips": self._x_chips,
            "events": self._x_events_data,
            "docs": self._x_docs,
            "macro": self._x_macro,
        }
        handler = table.get(action)
        if handler is None:
            return (f"[Error] 未知 action={action}。可用："
                    + " / ".join(table))
        return await handler(args)

