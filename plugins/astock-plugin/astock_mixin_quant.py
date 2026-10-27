"""量化选股 / 回测 / 操作指示 / 本地存档。

- `astock_pick`：选股 + 换算股数；`compare=true` 一次采集跑三套并对比。
- `astock_backtest`：策略回测出推荐率。
- `astock_act`：把选股与持仓合成"照着做"的清单（先卖后买）。
- `astock_archive`：日 K 本地存档（突破在线接口 2.6 年上限的唯一办法）。

⚠️ **mixin 单独不可用**：假定宿主类提供 `_snapshot` / `_quant_scan` /
`_get_ledger` / `_get_archive` / `_hints_cfg`。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import astock_actions as actions
import astock_archive as archive_mod
import astock_backtest as backtest
import astock_backtest_tool as backtest_tool
import astock_data as data
import astock_quant as quant
import astock_quant_tool as quant_tool
from astock_common import _bool, _span_years

logger = logging.getLogger("litework.astock.mixin")


class QuantToolsMixin:
    """量化选股 / 回测 / 操作指示 / 本地存档。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _tool_pick(self, args: Dict[str, Any]) -> str:
        """量化选股：全市场扫描 → 打分 → 换算股数（可带推荐率）。

        `compare=true` 时一次采集同时产出三套并做对比（v2.1.0）——数据采集
        只做一次，成本与单策略几乎相同。默认仍只跑一套：多数人问「帮我选股」
        只想看一个结论，三套一起给反而要他自己挑。**要看分歧时**明确说
        「三套对比一下」，模型会带上这个参数。
        """
        if args.get("compare"):
            return await self._tool_pick_compare(args)
        try:
            result = await asyncio.to_thread(self._quant_scan, None, args)
        except data.DataError as exc:
            return f"[Error] 选股扫描失败：{exc}"
        if args.get("rank_only"):
            n = int(args.get("top_n") or 10)
            return quant_tool.render_ranking(result, max(1, min(n, 20)))
        text = quant_tool.render_scan(result)
        # 推荐率：默认附上该策略的回测结论（只读缓存，不额外拉网络）。
        # 为什么默认附：用户要的是"该不该买"的判断，只给得分仍要他自己解读。
        if not args.get("no_grade", False) \
                and _bool(self._app, "backtest_auto_grade", True):
            grade = await self._attach_grade(result.get("strategy") or "")
            if grade:
                text = grade + "\n\n---\n\n" + text
        return text

    async def _tool_pick_compare(self, args: Dict[str, Any]) -> str:
        """三策略对比：选股与回测**都只取一次数据**。

        两条链路原本各有一份重复取数，这里一并收掉：
          ① 选股：`scan_all` 一次采集跑三套（v2.1.0）；
          ② 回测：`run_all` 一次取 25 只历史 K 线 + 基准跑三套（v2.1.1）。
        否则「对比一下」会变成 30 只 K 线 ×1 + 25 只 K 线 ×3，等一分钟以上。
        """
        try:
            results = await asyncio.to_thread(self._quant_scan_all, args)
        except data.DataError as exc:
            return f"[Error] 选股扫描失败：{exc}"

        # 推荐率：只读缓存；缺的用 run_all 一次补齐（不在关键路径上阻塞过久）
        recs: Dict[str, Any] = {}
        for key in quant.STRATEGIES:
            try:
                cached = backtest_tool.get_cached(key)
                if cached:
                    recs[key] = cached.get("recommendation")
            except Exception:
                logger.debug("[astock] 读回测缓存失败: %s", key, exc_info=True)
        missing = [k for k in quant.STRATEGIES if k not in recs]
        if missing and not args.get("no_grade", False) \
                and _bool(self._app, "backtest_auto_grade", True):
            try:
                got = await asyncio.to_thread(
                    backtest_tool.run_all, strategies=missing,
                    use_cache=True)
                for key, val in got.items():
                    rec = val.get("recommendation")
                    if rec is not None:
                        recs[key] = rec
            except Exception as exc:
                logger.info("[astock] 对比模式的推荐率回测失败（不影响选股）: %s", exc)

        head = quant_tool.render_compare(results, recs=recs, max_rows=5,
                                           with_advice=False)
        # 「该用哪套」的完整版（含理由与风险提示）——这才是"哪个效果好"的答案，
        # 面板里只放得下结论，这里把完整论证给出来。
        advice = ""
        try:
            adv = advise.advise(recs)
            advice = advise.render_advice(adv)
        except Exception:
            logger.debug("[astock] 策略建议生成失败", exc_info=True)
        grades: List[str] = []
        for key in quant.STRATEGIES:
            if key in recs:
                grades.append(quant_tool.render_grade_line(recs[key]))
        tail = ("\n\n---\n\n### 各策略推荐率（历史表现，不是上涨概率）\n\n"
                + "\n\n".join([g for g in grades if g])) if grades else ""
        mid = (f"\n\n---\n\n### 该用哪套\n\n{advice}") if advice else ""
        return head + mid + tail

    async def _attach_grade(self, strategy: str,
                            fresh: bool = False) -> str:
        """取该策略的回测推荐率摘要（优先缓存；无缓存时跑一轮）。"""
        if not strategy:
            return ""
        try:
            cached = backtest_tool.get_cached(strategy)
            if cached is None or fresh:
                cached = await asyncio.to_thread(
                    backtest_tool.run, strategy, use_cache=not fresh)
        except Exception as exc:
            logger.info("[astock] 推荐率获取失败（不影响选股结果）: %s", exc)
            return ("> ⚪ **推荐率暂不可用**：回测取数失败（可能接口限流）。"
                    "可稍后单独问「回测一下这个策略」。")
        rec = cached.get("recommendation")
        if rec is None:
            return ""
        mark = {"A": "🟢", "B": "🔵", "C": "🟠", "D": "🔴"}.get(rec.grade, "⚪")
        if rec.win_rate is None:
            return f"> {mark} **推荐率无法判定**：{rec.reason}"
        return (f"> {mark} **策略推荐率 {rec.win_rate:.0f}%**"
                f"（{rec.grade} 档 · {rec.label}；历史 {rec.sample} 期中 "
                f"{int(round(rec.win_rate / 100 * rec.sample))} 期跑赢沪深300，"
                f"样本可信度 {rec.confidence}）\n"
                f"> 这是**策略**的历史表现，不是这几只票各自上涨的概率。")

    async def _tool_backtest(self, args: Dict[str, Any]) -> str:
        """策略回测：出推荐率与风险指标。"""
        strategy = str(args.get("strategy") or "").strip().lower() \
            or self._default_strategy()
        if strategy not in quant.STRATEGIES:
            return (f"[Error] 未知策略 {strategy}；可选："
                    f"{'、'.join(quant.STRATEGIES)}")
        cfg = backtest.BacktestConfig()
        for key in ("rebalance_days", "top_k", "hold_days"):
            v = args.get(key)
            if v not in (None, ""):
                try:
                    setattr(cfg, key, int(float(v)))
                except (TypeError, ValueError):
                    pass
        cfg.rebalance_days = max(2, min(cfg.rebalance_days, 60))
        cfg.top_k = max(1, min(cfg.top_k, 10))
        cfg.hold_days = max(1, min(cfg.hold_days, 60))
        pool = int(args.get("pool_size") or backtest_tool.DEFAULT_POOL)
        try:
            out = await asyncio.to_thread(
                backtest_tool.run, strategy,
                pool_size=max(6, min(pool, 60)), cfg=cfg,
                use_cache=not bool(args.get("fresh")))
        except data.DataError as exc:
            return f"[Error] 回测取数失败：{exc}"
        except ValueError as exc:
            return f"[Error] {exc}"
        detail = int(args.get("detail_periods") or 0)
        return backtest_tool.render(
            out["result"], out["recommendation"],
            detail_periods=max(0, min(detail, 30)),
            fetch_failed=out.get("fetch_failed") or 0)

    async def _tool_act(self, args: Dict[str, Any]) -> str:
        """生成操作指示：先卖后买 + 资金闭环。"""
        try:
            scan = await asyncio.to_thread(self._quant_scan, None, args)
        except data.DataError as exc:
            return f"[Error] 选股失败，无法生成操作指示：{exc}"

        snap = self._snapshot()
        d = snap["data"]
        positions = d.get("positions") or []
        quotes = d.get("quotes") or {}
        strategy_key = scan.get("strategy") or self._default_strategy()
        strat = quant.STRATEGIES.get(strategy_key)

        total_mv = float(d.get("total_market_value") or 0)
        plan = actions.build_action_plan(
            positions=positions,
            quotes=quotes,
            allocations=scan.get("allocations") or [],
            cash=float(d.get("cash") or 0),
            cfg=self._hints_cfg(),
            strategy=strat,
            total_market_value=total_mv,
            total_equity=float(d.get("equity") or 0) or (
                total_mv + float(d.get("cash") or 0)),
            fee_cfg=self._get_ledger().fee)

        text = actions.render_action_plan(
            plan, strat.label if strat else "")
        # 推荐率一并给出：让用户知道"这套指示背后的策略靠不靠谱"
        grade = await self._attach_grade(strategy_key)
        if grade:
            text = grade + "\n\n" + text
        return text

    async def _tool_archive(self, args: Dict[str, Any]) -> str:
        """本地历史行情存档：更新 / 概览 / 清理。"""
        action = str(args.get("action") or "status").strip().lower()
        archive = self._get_archive()

        if action == "status":
            st = await asyncio.to_thread(archive.stats)
            lines = ["**本地历史行情存档**", ""]
            lines.append("| 项目 | 数值 |")
            lines.append("|---|---|")
            lines.append(f"| 文件 | `{st['path']}` |")
            lines.append(f"| 已存标的 | {st['codes']} 只 |")
            lines.append(f"| 总 K 线根数 | {st['bars']:,} 根 |")
            lines.append(f"| 时间跨度 | {st['first_date'] or '-'} ~ "
                         f"{st['last_date'] or '-'} |")
            lines.append(f"| 占用空间 | {st['size_human']} |")
            lines.append("")
            if st["detail"]:
                lines.append("### 覆盖最多的标的（前 10）")
                lines.append("")
                lines.append("| 代码 | 口径 | 根数 | 起 | 止 |")
                lines.append("|---|---|---|---|---|")
                for d in st["detail"][:10]:
                    lines.append(f"| {d['code']} | {d['adjust']} | "
                                 f"{d['bar_count']} | {d['first_date']} | "
                                 f"{d['last_date']} |")
                lines.append("")
            if st["bars"] == 0:
                lines.append("**存档为空**。说一句「更新 K 线存档」开始累积。")
            else:
                years = _span_years(st["first_date"], st["last_date"])
                lines.append(f"> 当前覆盖约 {years:.1f} 年。回测会自动优先用存档数据"
                             f"——**存得越久，回测窗口越长**。建议每天跑一次更新。")
            lines.append("")
            lines.append("_存档只存不复权原始价：前复权会随分红重算整段历史，"
                         "存下来会自相矛盾。_")
            return "\n".join(lines)

        if action == "prune":
            before = str(args.get("before") or "").strip()
            codes = [str(c).strip() for c in (args.get("codes") or []) if str(c).strip()]
            if not before and not codes:
                return ("[Error] prune 必须指定 before 日期或 codes，"
                        "否则会清空全部存档。这是刻意的保护——"
                        "几年数据一次误点就没了。")
            res = await asyncio.to_thread(archive.prune, before, codes or None)
            return (f"✅ 已清理 {res['deleted']} 根 K 线"
                    + (f"（{', '.join(codes)}）" if codes else "")
                    + (f"（{before} 之前）" if before else ""))

        if action != "update":
            return f"[Error] 未知 action {action}；可选 update/status/prune"

        # update：默认标的 = 持仓 + 自选
        codes = [str(c).strip() for c in (args.get("codes") or []) if str(c).strip()]
        if not codes:
            ledger = self._get_ledger()
            try:
                codes = [p["code"] for p in await asyncio.to_thread(
                    ledger.list_positions)]
                codes += [w["code"] for w in await asyncio.to_thread(
                    ledger.list_watch)]
            except LedgerError as exc:
                return f"[Error] 读取台账失败：{exc}"
        codes = list(dict.fromkeys(c for c in codes if c))
        if not codes:
            return ("台账里没有持仓或自选股，没东西可存。"
                    "先用 astock_record 录入持仓，或 astock_watch 添加自选，"
                    "或用 codes 参数指定要存档的代码。")

        limit = int(args.get("limit") or 640)
        limit = max(60, min(limit, 2000))

        def fetch_fn(code: str, _adjust: str, _limit: int) -> List[Dict[str, Any]]:
            # 存档一律取**不复权**（见 astock_archive 模块注释①）
            bars = data.fetch_klines(code, period="day", limit=limit, adjust="none")
            return [b.as_dict() for b in bars]

        result = await asyncio.to_thread(
            archive_mod.update_from_provider, archive, codes, fetch_fn,
            "raw", limit)
        ok = len(result["updated"])
        fail = len(result["failed"])
        lines = [f"✅ 存档已更新：成功 {ok} 只、失败 {fail} 只、"
                 f"新增/刷新 {result['total_saved']:,} 根", ""]
        if result["updated"]:
            lines.append("| 代码 | 写入 | 起 | 止 |")
            lines.append("|---|---|---|---|")
            for u in result["updated"][:15]:
                lines.append(f"| {u['code']} | {u['saved']} | "
                             f"{u['first_date']} | {u['last_date']} |")
            lines.append("")
        if result["failed"]:
            lines.append("**失败明细**（通常是接口限流，稍后重试即可）：")
            lines.append("")
            for f in result["failed"][:10]:
                lines.append(f"- {f['code']}：{f['error']}")
            lines.append("")
        lines.append("_提示：存档是随时间累积的，今天存的覆盖约 2.6 年，"
                     "**一年后才会多出一年**。建议每天跑一次。_")
        return "\n".join(lines)
