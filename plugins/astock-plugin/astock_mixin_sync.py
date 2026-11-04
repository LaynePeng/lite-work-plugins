"""成交回单同步 + 投资舆情分析。

- `astock_sync`：粘贴券商回单 → 解析多笔 → 匹配建议计划 → 校价 → 落账。
  `dry_run` 预览与落账走**同一套解析**，避免两处口径分叉。
- `astock_sentiment`：七类证据归集 + 背离提示（不合成情绪分）。
  支持 `code=portfolio` 跑持仓+自选的全量报告（复用 astock_report）。

⚠️ **mixin 单独不可用**：假定宿主类提供 `_get_ledger` / `_snapshot`。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import astock_business as business
import astock_data as data
import astock_market as mkt
import astock_receipt as receipt
import astock_report as report
import astock_sentiment as senti
import astock_sentiment_data as senti_data
import astock_sentiment_tool as senti_tool
from astock_common import _f, _table

logger = logging.getLogger("litework.astock.mixin")


class SyncToolsMixin:
    """成交回单同步 + 投资舆情分析。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _tool_sync(self, args: Dict[str, Any]) -> str:
        """把券商成交回单同步进台账（解析 → 匹配计划 → 校价 → 落账）。

        这是"我自己买了怎么同步回系统"的答案：不用逐笔手抄，把回单**粘贴**
        或把 CSV 内容传进来即可，一次处理一整天的成交。
        """
        text = str(args.get("text") or args.get("receipt") or "").strip()
        if not text:
            return ("[Error] 需要 text（券商成交回单）。可直接粘贴 App 里的成交明细，"
                    "或传导出的 CSV/TSV 内容。一行一笔也可以。")
        parsed = receipt.parse(text)
        if not parsed["trades"]:
            return ("[Error] 没能从这段内容里识别出成交记录。\n"
                    "支持的格式：\n"
                    "· 带表头的 CSV/TSV（粘贴券商导出文件内容）\n"
                    "· 每行一笔：`600000 买入 9.53 2000 2026-10-24`\n"
                    "也可以只用 astock_record 手工录单笔。")
        dry = bool(args.get("dry_run", True))
        entries = parsed["usable"]
        if not entries:
            return self._render_sync_preview(parsed, [], [], dry=True,
                                            reason="所有行都缺少关键字段")

        ledger = self._get_ledger()
        results: List[Dict[str, Any]] = []
        for t in entries:
            date = t.trade_date or dt.date.today().isoformat()
            match = await asyncio.to_thread(ledger.match_plan, t.code, t.side)
            item: Dict[str, Any] = {"trade": t, "plan": match, "status": "ok",
                                    "message": ""}
            # 校价：与计划建议价比较（只提示，不阻断——成交价由市场决定）
            if match and match.get("price"):
                plan_price = float(match["price"])
                if plan_price > 0 and t.price:
                    dev = (t.price - plan_price) / plan_price * 100.0
                    item["deviation_pct"] = round(dev, 2)
                    limit = float(_num(self._app, "sync_price_tolerance_pct", 2.0))
                    if abs(dev) > limit:
                        item["status"] = "warn"
                        item["message"] = (f"成交价 {t.price:.2f} 与计划价 "
                                           f"{plan_price:.2f} 偏差 {dev:+.2f}%"
                                           f"（阈值 ±{limit:g}%）")
                    shares_plan = int(match.get("shares") or 0)
                    if shares_plan and t.shares and t.shares != shares_plan:
                        item["status"] = "warn"
                        item["message"] = (item["message"] + "；" if item["message"] else "") \
                            + f"数量与计划不符（计划 {shares_plan} 股，成交 {t.shares} 股）"
            if not dry:
                try:
                    rec = await asyncio.to_thread(
                        ledger.record_trade,
                        code=t.code, side=t.side, price=float(t.price or 0),
                        shares=int(t.shares or 0), trade_date=date,
                        name=t.name or "", note=f"回单同步({t.confidence})",
                        fee_override=t.fee)
                    item["recorded"] = rec
                    if match:
                        await asyncio.to_thread(
                            ledger.mark_plan, match["plan_id"], "filled",
                            f"回单同步：成交 {t.shares} 股 @ {t.price}")
                except LedgerError as exc:
                    item["status"] = "error"
                    item["message"] = str(exc)
            results.append(item)

        return self._render_sync_preview(parsed, results, [], dry)

    def _render_sync_preview(self, parsed: Dict[str, Any],
                             results: List[Dict[str, Any]],
                             _unused: List[Any], dry: bool,
                             reason: str = "") -> str:
        trades = parsed["trades"]
        ok_n = sum(1 for r in results if r["status"] == "ok")
        warn_n = sum(1 for r in results if r["status"] == "warn")
        err_n = sum(1 for r in results if r["status"] == "error")
        head = ("**成交回单预览**（未落账）" if dry else "**成交已同步**")
        lines = [head, ""]
        lines.append(f"识别 {len(trades)} 笔"
                     + (f"（格式：{parsed['method']}）" if parsed.get("method") else "")
                     + (f"｜重复去掉 {parsed['duplicates_removed']} 笔"
                        if parsed.get("duplicates_removed") else ""))
        if reason:
            lines += ["", f"⚠️ {reason}"]
        lines.append("")
        rows = []
        for r in results:
            t = r["trade"]
            plan = r.get("plan") or {}
            dev = r.get("deviation_pct")
            rows.append([
                t.code, (t.name or "-")[:8],
                "买入" if t.side == "buy" else "卖出",
                _f(t.price), t.shares or "-",
                _f(t.fee) if t.fee else "自动算",
                t.trade_date or "今天",
                plan.get("plan_id") or "无对应计划",
                f"{dev:+.2f}%" if dev is not None else "-",
                {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(r["status"], "?"),
            ])
        if rows:
            lines.append(_table(
                ["代码", "名称", "方向", "成交价", "数量", "费用", "日期",
                 "匹配计划", "校价", "状态"], rows))
            lines.append("")
        warns = [r for r in results if r["message"]]
        if warns:
            lines.append("**需要你确认**：")
            for r in warns:
                mark = "⚠️" if r["status"] == "warn" else "❌"
                t = r["trade"]
                lines.append(f"- {mark} {t.code} {t.name or ''}：{r['message']}")
            lines.append("")
        if parsed.get("unparsed"):
            lines.append(f"**未能识别（{len(parsed['unparsed'])} 笔，未计入）**：")
            for raw in parsed["unparsed"][:8]:
                lines.append(f"- `{raw[:80]}`")
            lines.append("")
        if dry:
            if err_n:
                lines.append("❌ 有解析/校验错误，修正后重试（或改用 astock_record 手工录）。")
            else:
                lines.append(f"确认无误后用 `dry_run=false` 重新调用即落账"
                             f"（✅{ok_n} / ⚠️{warn_n}）。")
        else:
            lines.append(f"落账结果：成功 {ok_n} 笔"
                         + (f"，警告 {warn_n} 笔" if warn_n else "")
                         + (f"，失败 {err_n} 笔" if err_n else "") + "。")
            lines.append("对应计划已标记为已执行；台账成本价与 T+1 可卖已同步更新。")
        return "\n".join(lines)

    # -------------------------------------------------- 舆情
    async def _tool_sentiment(self, args: Dict[str, Any]) -> str:
        code_raw = str(args.get("code") or "").strip()
        if not code_raw:
            return ("[Error] 需要 code。舆情按标的维度分析——"
                    "要查行业/宏观事件请传 scope 含 feed 并给出可关联的标的代码。"
                    "跑持仓+自选全量请传 code=portfolio。")
        # 组合模式：持仓+自选全量舆情报告（与盘后自动报告同一实现）
        if code_raw.lower() == "portfolio":
            return await report.build_report(
                self._get_ledger(), manual=True,
                company_resolver=self._company_in_text)
        code, _ = mkt.normalize_code(code_raw)
        scope = args.get("scope") or []
        if isinstance(scope, str):
            scope = [s.strip() for s in scope.replace("，", ",").split(",") if s.strip()]
        days = int(args.get("days") or 7)

        name = ""
        try:
            q = await self._fetch_quotes([code])
            if q:
                name = q[0].name
        except data.DataError:
            name = ""
        if not name:
            try:
                rows = await asyncio.to_thread(data.search, code, 1)
                if rows:
                    name = rows[0]["name"]
            except data.DataError:
                name = ""

        try:
            collected = await asyncio.to_thread(
                senti_data.collect, code, name, tuple(scope), days)
        except Exception as exc:      # 舆情采集不该把工具打挂
            logger.exception("[astock] 舆情采集失败")
            return (f"[Error] 舆情数据采集失败：{type(exc).__name__}: {exc}。"
                    f"可先用 astock_quote / astock_kline 看行情，稍后重试舆情。")

        # ⚠️ 局部变量不能叫 report：本函数顶部 `import astock_report as report`，
        # 一旦在函数内给 report 赋值，Python 会把它当作**整个函数的局部变量**，
        # 于是组合模式（code=portfolio）走 `report.build_report(...)` 时
        # 抛出 UnboundLocalError —— 这就是 11-04 组合舆情失败的真实原因。
        rep = senti.summarize(collected["items"], code, name,
                              collected["errors"], collected["extras"])
        if args.get("brief"):
            line = senti_tool.render_brief(rep)
            conflicts = senti_tool.render_conflict_lines(rep)
            out = [line]
            if conflicts:
                out.append("")
                out.append("**信号背离**：")
                out += [f"- {c}" for c in conflicts]
            if collected["errors"]:
                out += ["", "数据缺口：" + "；".join(
                    f"{k}={v}" for k, v in collected["errors"].items())]
            return "\n".join(out)
        return senti_tool.render(rep, collected["items"], collected["extras"])

    # -------------------------------------------------- 主营构成（门槛②）
    async def _tool_business(self, args: Dict[str, Any]) -> str:
        """主营构成 + 门槛②（相关业务收入占营收 ≥5%）自动判定。

        这是「选股方法论第②道门槛」的落地：此前工具链没有分部财报字段，
        这道门槛被登记成永久缺口，导致所有标的都只能写「②无法核实 → 只观察」。
        现在直接读东财 F10 主营构成，把占比算出来。

        **四种结论必须分清**（见 astock_business 的模块注释）：
        pass / below / absent / na，另有 unknown。
        尤其 `absent`（分部表里没这块业务）是**否定证据**，不是数据缺失——
        分部披露有重要性门槛，进不了表就意味着远低于 5%。
        """
        event_text = str(args.get("event") or "").strip()
        keywords = args.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]

        # **批量模式必须先判**（2026-11-04 踩到）：
        # 早先先校验 code 再判 codes，导致只传 codes 时被「需要 code」挡回去，
        # 批量分支永远走不到——参数都声明了却用不了，属于典型的顺序错。
        codes_raw = args.get("codes") or []
        if isinstance(codes_raw, str):
            codes_raw = [c.strip() for c in codes_raw.replace("，", ",").split(",") if c.strip()]
        if codes_raw:
            return await self._business_batch(list(codes_raw), event_text, keywords)

        code_raw = str(args.get("code") or "").strip()
        if not code_raw:
            return ("[Error] 需要 code（如 002273）或 codes 数组。这是按标的查分部收入构成的工具，"
                    "用于判定选股方法论第②道门槛「相关业务收入占营收 ≥5%」。"
                    "单只传 code，批量核持仓/候选池传 codes。")

        code, _ = mkt.normalize_code(code_raw)
        name = ""
        try:
            q = await self._fetch_quotes([code])
            if q:
                name = q[0].name
        except Exception:
            name = ""

        res = await asyncio.to_thread(
            business.check, code, event_text, name, tuple(keywords))

        lines = [f"## 主营构成 · 门槛②判定：{name or code}（{code}）", ""]
        lines.append(f"**结论：{res.verdict}**")
        lines.append("")
        if res.reason:
            lines.append(f"> {res.reason}")
            lines.append("")
        if res.report_date:
            lines.append(f"报告期：**{res.report_date}**（分部数据按中报/年报披露，不是实时）")
            lines.append("")

        if res.candidates:
            lines.append("| 分部 | 收入占比 | 毛利率 |")
            lines.append("|---|---|---|")
            for seg in res.candidates[:8]:
                g = f"{seg.gross_pct:.1f}%" if seg.gross_pct is not None else "-"
                lines.append(f"| {seg.item} | **{seg.ratio_pct:.2f}%** | {g} |")
            lines.append("")

        # 判定说明：把"为什么是这个结论"讲清，避免把 absent 读成"没查到"
        note = {
            business.PASS: "占比达标，该业务变好会进报表。",
            business.BELOW: "有这块业务但占比不足 5%，即使翻倍对报表影响也在四舍五入范围。",
            business.ABSENT: ("**分部表里没有这块业务**——分部披露有重要性门槛，"
                              "进不了表意味着占比远低于 5%。这是**否定证据**，不是数据缺失。"),
            business.NA: ("事件属资本运作（回购/增持/分红），不涉及某块业务收入占比。"
                          "**门槛② 不适用**，不能据此否决——应以门槛③资金、门槛④止损为准。"),
            business.UNKNOWN: ("数据取不到（北交所/ETF/接口异常）。"
                               "**这不等于通过**，只是一条未核实的门槛。"),
        }.get(res.result, "")
        if note:
            lines.append(f"**怎么读这个结论**：{note}")
            lines.append("")

        if res.keywords:
            lines.append(f"匹配关键词：{'、'.join(res.keywords)}")
            lines.append("")

        lines.append("> 数据源：东财 F10 主营构成。只做数据归集与门槛判定，不构成买入建议。")
        return "\n".join(lines)

    async def _business_batch(self, codes: List[str], event_text: str,
                              keywords: Sequence[str]) -> str:
        """批量核门槛②（持仓/候选池体检）。"""
        rows: List[str] = []
        stats = {business.PASS: 0, business.BELOW: 0, business.ABSENT: 0,
                 business.NA: 0, business.UNKNOWN: 0}
        no_event = 0          # 未指定业务，只列了分部构成（不算任何判定结果）
        for raw in codes[:20]:
            try:
                code, _ = mkt.normalize_code(raw)
            except Exception:
                continue
            name = ""
            try:
                q = await self._fetch_quotes([code])
                if q:
                    name = q[0].name
            except Exception:
                name = ""
            res = await asyncio.to_thread(
                business.check, code, event_text, name, tuple(keywords))
            if res.result == business.UNKNOWN and not event_text and not keywords:
                # 没给 event/keywords 时没法判门槛②，但分部构成本身仍然有用：
                # 直接把主要业务列出来，让人一眼看到"这家靠什么赚钱"，
                # 比一整列「未核实」有意义得多。
                top = "、".join(f"{s.item} {s.ratio_pct:.1f}%"
                                for s in res.candidates[:3])
                rows.append(f"| {name or code}（{code}）| ➖ 未指定业务 | {top or '—'} |")
                no_event += 1
                continue
            stats[res.result] = stats.get(res.result, 0) + 1
            detail = (f"{res.matched} {res.ratio_pct:.1f}%" if res.ratio_pct is not None
                      else (res.reason[:34] + ("…" if len(res.reason) > 34 else "")))
            rows.append(f"| {name or code}（{code}）| {res.verdict} | {detail} |")

        total = sum(stats.values()) + no_event
        lines = [f"## 门槛②批量体检（{total} 只）", ""]
        judged = sum(stats.values())
        if judged:
            lines.append(f"**过 {stats[business.PASS]} 只 · 不过 {stats[business.BELOW] + stats[business.ABSENT]} 只"
                         f" · 不适用 {stats[business.NA]} 只 · 未核实 {stats[business.UNKNOWN]} 只**")
        if no_event:
            lines.append(f"**{no_event} 只未指定业务，只列主营构成**"
                         f"（传 event 或 keywords 才判门槛②）")
        lines.append("")
        lines.append("| 标的 | 结论 | 命中分部/原因 |")
        lines.append("|---|---|---|")
        lines += rows
        lines.append("")
        lines.append("> 「不适用」= 事件属回购/增持等资本运作，门槛② 本就不涉及业务占比；"
                     "「未核实」= 数据取不到（北交所/ETF），**不等于通过**。")
        return "\n".join(lines)
