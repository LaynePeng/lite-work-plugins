"""台账类工具：录成交（含 dry-run 校验）、持仓总览（含聊天看板），
以及数据迁移（台账 JSON/成交 CSV 导出导入，v2.10.0 与 dsh-astock 互通）。

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
import astock_maintenance as maint
import astock_market as mkt
from astock_ledger import Ledger
from astock_common import _as_bool, _f, _table

logger = logging.getLogger("litework.astock.mixin")


def cash_hint_text(ledger: Ledger, cash_delta: float,
                   ledger_after: Any = None) -> str:
    """录成交后的「可用资金未自动扣减」提示（带可执行的具体金额）。

    为什么需要带数字：`set_cash()` 是用户手工录入（银行转账/出入金对插件
    不可见），所以**每笔成交都不会自动扣现金**。此前只有一句泛泛的
    「需要时用 set_cash 手动校准」——用户看完仍不知道该填多少，实际后果是
    账面现金虚高：真实事故里一笔 6,770 元买入后 3 分钟，用户手工把 cash
    设成了**买入前**的余额，虚高 6,779 元，直接让后续 astock_hint /
    astock_act 的股数估算多算一手。

    这里只**建议**、绝不自动扣：插件看不见这段时间里有没有转账，自动扣同
    样会算错。给「若此后再无出入金，应为 X」这个参考值，是让用户一眼能判断
    对不对，而不是替他决定。

    :param ledger: 台账（读当前账面余额与上次校准时间）
    :param cash_delta: 本次资金变动（买入为负，卖出为正，已含费用）
    :param ledger_after: 预留：要展示的「变动后参考值」直接由账面 + delta 算，
        传 None 即可（保留参数便于将来接入多笔汇总）。
    """
    try:
        cur = ledger.get_cash()
        balance = float(cur.get("balance") or 0.0)
        updated = str(cur.get("updated_at") or "")
    except Exception:                       # noqa: BLE001 - 提示失败不该阻断录单
        logger.debug("[astock] 读取资金余额失败，用简化提示", exc_info=True)
        return ("提示：台账里的可用资金不会自动扣减（出入金不可见），"
                "请用 astock_portfolio action=set_cash 按券商实际余额校准。")
    ref = round(balance + float(cash_delta or 0.0), 2)
    when = f"上次校准于 {updated}" if updated else "尚未校准过"
    return "\n".join([
        f"⚠️ 可用资金未自动扣减 —— 本次资金变动 {cash_delta:+,.2f} 元。",
        f"   账面可用资金 {balance:,.2f} 元（{when}），"
        f"若此后再无出入金，真实可用应为 {ref:,.2f} 元。",
        "   请核对券商 App 后校准："
        "`astock_portfolio action=set_cash cash=<真实余额>`",
        "   （插件看不到你的银行转账/出入金，所以不会自动改余额。）",
    ])


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
        lines += ["", cash_hint_text(self._get_ledger(), r.get("cash_delta") or 0.0)]
        return "\n".join(lines)

    async def _tool_portfolio(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "list")
        # 快照（含实时行情）**只给 list/summary**：导出/导入/设资金/查流水/删
        # 流水都是纯台账操作，没必要为了显示浮盈去打一轮行情接口——
        # 导入几千笔流水时那轮请求既慢又毫无用途。
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
            ledger_snapshot = await asyncio.to_thread(self._snapshot)
            board_text = await self._render_board_text()
            if board_text:
                return board_text
            return ledger_snapshot["text"]
        return f"[Error] 未知 action：{action}"

    # -------------------------------------------------- 数据迁移（导出 / 导入）
    #
    # 独立成 `astock_migrate` 工具而不是塞进 astock_portfolio 的 action：
    # `astock_portfolio` 在 `read_only_tools` 里（无人值守简报要用它读持仓），
    # 而导出/导入会写文件、改台账。把写能力挂在只读工具上，等于给无人值守
    # 任务留了一个能改账户数据的后门——工具清单是按**工具名**过滤的，
    # 宿主看不到 action 级别的意图。

    async def _tool_migrate(self, args: Dict[str, Any]) -> str:
        """数据迁移入口：导出 / 导入台账 JSON / 导入成交 CSV。"""
        action = str(args.get("action") or "").strip().lower()
        if action == "export":
            return await asyncio.to_thread(
                self._export_data, str(args.get("kind") or "ledger"))
        if action == "import":
            return await self._import_ledger_action(args)
        if action == "import_trades":
            return await self._import_trades_action(args)
        return (f"[Error] 未知 action：{action or '(空)'}"
                f"（支持 export / import / import_trades）")

    def _export_data(self, kind: str) -> str:
        """导出台账 / 成交 / 自选到台账同目录的 `exports/`，返回可执行路径。"""
        try:
            ledger = self._get_ledger()
            res = maint.write_export(ledger, kind)
        except Exception as exc:              # noqa: BLE001 - 导出失败要说人话
            logger.warning("[astock] 导出失败: %s", exc)
            return f"[Error] 导出失败：{type(exc).__name__}: {exc}"
        if not res.get("ok"):
            return f"[Error] {res.get('message') or '导出失败'}"
        summary = res.get("summary") or {}
        path = res.get("path") or ""
        actual = res.get("kind") or "ledger"
        if actual == "trades":
            head = f"✅ 已导出成交 CSV：{summary.get('trades', 0)} 笔"
            tip = ("用 Excel 双击打开即可（中文表头 + BOM，不乱码）；"
                   "也可以在 dsh-astock 的「维护 → 导入 → 导入成交」里粘贴这份 CSV 内容。")
        elif actual == "watchlist":
            head = f"✅ 已导出自选 JSON：{summary.get('watchlist', 0)} 只"
            tip = "把文件内容粘贴到 dsh-astock 的「维护 → 导入 → 自选」即可。"
        else:
            head = (f"✅ 已导出台账 JSON：{summary.get('trades', 0)} 笔流水 / "
                    f"{summary.get('positions', 0)} 只持仓 / "
                    f"{summary.get('watchlist', 0)} 只自选 / "
                    f"资金 {float(summary.get('cash') or 0.0):,.2f} 元")
            tip = ("把文件内容粘贴到 dsh-astock 的「维护 → 导入 → 从 JSON 恢复台账」"
                   "即可（它认这个格式；反过来它的导出文件也能用 "
                   "`astock_migrate action=import` 读回）。")
        return "\n".join([head, "", f"文件：`{path}`",
                          "（格式与 DSH 版 A 股助手 dsh-astock 一致）", "", f"下一步：{tip}"])

    async def _import_ledger_action(self, args: Dict[str, Any]) -> str:
        src = await asyncio.to_thread(
            maint.read_import_text, str(args.get("text") or ""),
            str(args.get("path") or ""))
        if not src.get("ok"):
            return f"[Error] {src.get('message')}"
        text = src["text"]
        scope = maint.normalize_scope(args.get("scope"))
        dry = _as_bool(args.get("dry_run"), True)
        ledger = self._get_ledger()
        if dry:
            res = await asyncio.to_thread(maint.preview_ledger_import,
                                          ledger, text, scope)
            return self._render_ledger_import_preview(res)
        applied = await asyncio.to_thread(maint.import_ledger, ledger, text, scope)
        return self._render_ledger_import_result(applied)

    async def _import_trades_action(self, args: Dict[str, Any]) -> str:
        src = await asyncio.to_thread(
            maint.read_import_text, str(args.get("text") or ""),
            str(args.get("path") or ""))
        if not src.get("ok"):
            return f"[Error] {src.get('message')}"
        dry = _as_bool(args.get("dry_run"), True)
        ledger = self._get_ledger()
        res = await asyncio.to_thread(maint.import_trades, ledger, src["text"],
                                      not dry)
        return self._render_trades_import(res, dry=dry)

    def _render_ledger_import_preview(self, res: Dict[str, Any]) -> str:
        """台账 JSON 导入预览（不落账）：差异 + 范围 + 资金变化一眼看清。"""
        if not res.get("ok"):
            errs = res.get("errors") or []
            detail = ("\n".join(f"- {e}" for e in errs[:5]) if errs else "")
            return f"[Error] {res.get('message') or '解析失败'}" + (
                f"\n{detail}" if detail else "")
        scope_cn = {"all": "全量", "watch_cash": "仅资金+自选",
                    "trades": "仅成交"}[res.get("scope") or "all"]
        pos = res.get("positions") or {}
        tr = res.get("trades") or {}
        watch = res.get("watchlist") or {}
        cash = res.get("cash") or {}
        lines = [
            f"**台账导入预览**（未落账）：{scope_cn}",
            "",
            _table(["项目", "结果"], [
                ["持仓", f"+{len(pos.get('add') or [])} / 改 "
                         f"{len(pos.get('update') or [])} / 保留 "
                         f"{len(pos.get('keep') or [])}（不删）"],
                ["成交", f"文件 {tr.get('in_file', 0)} 笔 → 新增 "
                         f"{tr.get('new', 0)}，重复 {tr.get('duplicate', 0)}"],
                ["自选", f"文件 {watch.get('in_file', 0)} 只 → 新增 "
                         f"{len(watch.get('add') or [])}"],
                ["资金", (f"{float(cash.get('before') or 0):,.2f}（不变）"
                          if (res.get("scope") == "trades"
                              or cash.get("after") == cash.get("before"))
                          else f"{float(cash.get('before') or 0):,.2f} → "
                               f"{float(cash.get('after') or 0):,.2f}")],
            ]),
            "",
        ]
        changes = []
        for item in (pos.get("add") or [])[:8]:
            changes.append(["新增持仓", item["code"], f"{item['shares']} 股"])
        for item in (pos.get("update") or [])[:8]:
            changes.append(["调整持仓", item["code"],
                            f"{item['shares_before']} → {item['shares_after']} 股"])
        for item in (pos.get("keep") or [])[:8]:
            changes.append(["保留持仓", item.get("code") or "",
                            f"{item.get('shares') or 0} 股（文件里没有，合并导入不删除）"])
        if changes:
            lines += [_table(["变化", "代码", "说明"], changes), ""]
        if res.get("exported_at"):
            lines.append(f"文件导出于：{res['exported_at']}")
        warnings = res.get("errors") or []
        if warnings:
            lines.append("⚠️ 格式提示：" + "；".join(warnings[:3]))
        lines += ["", "确认无误后用 `dry_run=false` 重新调用即可落账；"
                       "持仓一律按流水回放复算，文件里的持仓字段只在没有流水时使用。"]
        return "\n".join(lines)

    def _render_ledger_import_result(self, res: Dict[str, Any]) -> str:
        if not res.get("ok"):
            errs = res.get("errors") or []
            detail = ("\n".join(f"- {e}" for e in errs[:5]) if errs else "")
            return f"[Error] {res.get('message') or '导入失败'}" + (
                f"\n{detail}" if detail else "")
        applied = res.get("applied") or {}
        lines = [f"✅ {res.get('message') or '台账导入完成'}"]
        failed = applied.get("failed") or []
        if failed:
            lines += ["", "**未能导入的行**：",
                      _table(["代码", "原因"],
                             [[str(f.get("code") or ""),
                               str(f.get("error") or "")] for f in failed[:10]])]
        errors = res.get("errors") or []
        if errors:
            lines += ["", "⚠️ " + "；".join(str(e) for e in errors[:3])]
        lines += ["", "提示：资金不会随成交自动扣减（插件看不到你的银行出入金），"
                       "请核对券商余额后用 `astock_portfolio action=set_cash` 校准。"]
        return "\n".join(lines)

    def _render_trades_import(self, res: Dict[str, Any], dry: bool) -> str:
        """成交 CSV 导入：预览逐行标可导入/重复/失败；落账后给汇总。"""
        if not res.get("ok"):
            errs = res.get("errors") or []
            detail = ("\n".join(f"- {e}" for e in errs[:5]) if errs else "")
            return f"[Error] {res.get('message') or '导入失败'}" + (
                f"\n{detail}" if detail else "")
        head = ("**成交导入预览**（未落账）" if dry else "**成交已导入**")
        summary = res.get("summary") or {}
        lines: List[str] = [head, ""]
        if dry:
            lines.append(f"{res.get('message') or ''}")
            rows = []
            for item in (res.get("rows") or [])[:30]:
                note = "；".join(item.get("problems") or [])
                if item.get("duplicate"):
                    note = "台账里已有同一笔"
                rows.append([
                    str((item.get("index") or 0) + 2),
                    str(item.get("trade_date") or ""),
                    f"{item.get('code') or ''} {item.get('name') or ''}".strip(),
                    "买入" if item.get("side") == "buy" else "卖出",
                    _f(item.get("price")),
                    str(item.get("shares") or ""),
                    {"ok": "✅ 可导入", "duplicate": "跳过（重复）",
                     "error": "❌ 校验不通过"}.get(item.get("status"), ""),
                ] + ([note] if note else [""]))
            if rows:
                lines += [_table(["行", "日期", "代码/名称", "方向", "价格",
                                   "数量", "结论", "说明"], rows)]
            lines += ["", "确认无误后用 `dry_run=false` 重新调用即可落账；"
                           "重复行会自动跳过，同一份文件导两次不会记两遍。"]
        else:
            lines.append(f"导入 {summary.get('applied', 0)} 笔，"
                         f"跳过重复 {summary.get('skipped', 0)} 笔，"
                         f"失败 {summary.get('failed', 0)} 笔")
            applied = res.get("applied") or []
            if applied:
                lines += ["", _table(
                    ["日期", "代码", "方向", "价格", "数量", "费用", "导入后持仓"],
                    [[str(a.get("trade_date") or ""), str(a.get("code") or ""),
                      "买入" if a.get("side") == "buy" else "卖出",
                      _f(a.get("price")), str(a.get("shares") or ""),
                      _f(a.get("fee")), str(a.get("shares_after") or "")]
                     for a in applied[:20]])]
            failed = res.get("failed") or []
            if failed:
                lines += ["", "**失败明细**：", _table(
                    ["日期", "代码", "原因"],
                    [[str(f.get("trade_date") or ""), str(f.get("code") or ""),
                      str(f.get("error") or "")] for f in failed[:10]])]
            lines += ["", "提示：资金不会随成交自动扣减，"
                           "请核对券商余额后用 `astock_portfolio action=set_cash` 校准。"]
        return "\n".join(lines)

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
        if data_.get("today_pnl_incomplete"):
            miss = "、".join(str(c) for c in
                            (data_.get("today_pnl_missing") or [])[:5])
            lines += [f"⚠️ **今日盈亏不完整**：{miss or '当日成交过多'} "
                      f"缺现价/昨收，已按可算部分展示（不是 0）。", ""]
        # 看板是"扫一眼"的视图，明细数字仍需可核对 → 附一行口径说明
        lines.append(
            f"共 {len(data_['positions'])} 只持仓、"
            f"{len(watch)} 只自选；卡片首行＝现价｜持仓收益率（相对成本）"
            f"｜今日涨跌幅（相对昨收）｜股数·成本·浮盈·今日可卖，"
            f"箭头后为该做什么。"
            f"完整数值用 `astock_portfolio action=trades` 或右栏「持仓」面板核对。")
        return "\n".join(lines)
