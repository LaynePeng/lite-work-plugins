"""自选股管理 + 盯盘开关（含盯梢计划的自动落地）。

- `astock_watch`：增删改查自选股。
- `start/stop/status`：不只切插件开关，还**自动创建/维护宿主盯梢计划**
  （`astock_plan`），并给出"卡在哪一环"的体检结论。
  旧版只切开关、把建计划推给用户，结果是"点了启动、什么都没发生"。

⚠️ **mixin 单独不可用**：假定宿主类提供 `_app` / `_fetch_quotes` /
`_save_plugin_config` / `_get_ledger`。

"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

import astock_data as data
import astock_market as mkt
import astock_plan as plan_ctl
from astock_common import _as_bool, _cfg, _f, _num, _pct, _table

logger = logging.getLogger("litework.astock.mixin")


class WatchToolsMixin:
    """自选股管理 + 盯盘开关（含盯梢计划的自动落地）。（由 AstockPlugin 继承，mixin 单独不可用）。"""

    async def _watch_control(self, action: str) -> str:
        """盯盘状态查询 / 启动 / 暂停（v2.1.1：自动创建并维护盯梢计划）。

        「开始盯盘」其实要两件事同时成立：
        ① **插件侧开关**（`watch_enabled`）：关掉时探针直接返回空快照、不产生信号；
        ② **宿主盯梢计划**：真正让探针被周期性调用的是 lite-work 的计划（source=astock）。

        旧版本只做 ①，把 ② 推给用户手动建——结果是「点了启动盯盘，什么都没发生」
        这个最典型的静默失败。现在 `astock_plan` 会在 install() 拿到的 app 上
        自动建/修计划（与宿主路由层同一条 `schedule_store` 路径），所以 start
        一次就把两件事都办完；拿不到 app 时才退回手动指引（并明确说明原因）。
        """
        st = mkt.trading_status()
        ok_now, why = mkt.should_watch_now()
        enabled = _as_bool(_cfg(self._app, "watch_enabled", True), True)
        only_hours = _as_bool(_cfg(self._app, "watch_only_trading_hours", True), True)
        workspace = str(getattr(self._app, "workspace", "") or "")
        interval = int(_num(self._app, "watch_interval_seconds", 300))

        plan_note = ""
        if action in ("start", "stop"):
            want = action == "start"
            err = self._save_plugin_config({"watch_enabled": want})
            if err:
                return (f"[Error] 无法写入盯盘开关：{err}\n"
                        f"请手动在设置页把「启用盯盘」设为 {str(want).lower()}。")
            enabled = want
            # 同步计划：启动=确保存在且启用；暂停=禁用（不删，保留指纹基线，
            # 否则恢复时会把当前所有信号当"新变化"重报一遍）
            if want:
                res = plan_ctl.ensure_plan(self._app, workspace, interval=interval,
                                           probe_cfg=self._probe_cfg())
                plan_note = res.get("message") or ""
                if not res.get("ok"):
                    plan_note = ("⚠️ 自动创建盯梢计划失败：" + plan_note
                                 + "　请按下面的步骤手动建一次。")
            else:
                res = plan_ctl.set_enabled(self._app, False)
                plan_note = res.get("message") or ""
                return ("⏸ 盯盘已暂停。探针将直接返回「无变化」，不会再唤醒你。\n"
                        f"（{plan_note}；用 `astock_watch action=start` 恢复）")

        # 体检：`enabled` 是插件开关（用户意图），`ok_now` 是此刻是否在交易时段。
        # 必须分开传——此前合成 `enabled and ok_now` 传进去，收盘后体检必然
        # 误报"开关是关闭的"并让你去点按钮，而开关本来就没关（v2.2.0 修）。
        diag = plan_ctl.diagnose(self._app, enabled, only_hours,
                                 in_window=ok_now, window_note=why)
        lines = ["## 盯盘状态" if action == "status" else "## 已启动盯盘", ""]
        # 计划那行要写清"计划在运行"与"此刻是否真的在探测"是两件事：
        # 收盘后计划仍是 enabled（保留指纹基线），但探针会直接返回「无变化」。
        if not diag["plan_ok"]:
            plan_cell = "❌ 未就绪或已禁用"
        elif ok_now:
            plan_cell = "✅ 运行中（每 %d 秒）" % diag["plan_interval"]
        else:
            plan_cell = "✅ 已就绪（每 %d 秒；当前非交易时段，暂时不探）" % diag["plan_interval"]
        lines.append(_table(["项目", "状态"], [
            ["插件盯盘开关", "✅ 开启" if enabled else "⏸ 已暂停"],
            ["盯梢计划", plan_cell],
            ["最近一次探测", diag.get("last_run_text") or "还没探测过"],
            ["当前时刻", f"{st['now']}（{st['phase']}）"],
            ["现在是否在盯", ("✅ 是" if ok_now else f"❌ 否 —— {why}")],
            ["下次开始", mkt.next_watch_window()],
            ["时段自律", ("✅ 仅工作日 9:10–15:00，午休跳过"
                          if only_hours else "⚠️ 已关闭（全天盯，会消耗 token）")],
        ]))
        lines.append("")
        lines.append(f"**体检结论**：{diag['verdict']}")
        if plan_note:
            lines.append("")
            lines.append(f"**计划**：{plan_note}")
        for act in diag["actions"]:
            lines.append(f"- {act}")

        if not diag["plan_ok"]:
            lines.append("")
            lines.append("### 手动建计划（自动创建不可用时）")
            lines.append("")
            lines.append("到 **右栏「盯梢」页 → 新建计划**（图形界面，不用命令行）：")
            lines.append("- 数据源选 **A股持仓/自选异动**（source_kind = `astock`）")
            lines.append("- 调度模式选 **间隔**，填 **300 秒**（5 分钟）")
            lines.append("- 权限档保持 **read**（无人值守，不需要写权限）")
            lines.append("")
            lines.append("> **关于 cron**：计划编辑器里的「cron」模式（`*/5 9-14 * * 1-5`）"
                         "是**宿主内置的调度格式**，与系统 crontab **完全无关**——"
                         "不需要 `crontab -e`。选「间隔 300 秒」即可。")

        lines.append("")
        lines.append("**盯盘按 A 股交易时段自动作息**：开盘前 5 分钟开始，"
                     "午休自动跳过，15:00 收盘自动停。非交易时段探针直接返回"
                     "「无变化」，**不消耗 token**。")
        lines.append("")
        lines.append("### 邮件通知依赖这条链路")
        lines.append("")
        lines.append("高风险邮件**不是独立进程**：探针发现信号时顺带调 email-plugin 发信。"
                     "所以「收不到邮件」的排查顺序是：① 看本页**盯梢计划是否运行中**；"
                     "② 设置页点「发送测试邮件」验证通道；③ 看阈值与收件人。")
        return "\n".join(lines)

    def _probe_cfg(self) -> Dict[str, Any]:
        """把设置页里与探针相关的阈值带进计划（计划侧优先，设置页兜底）。"""
        keys = ("stop_loss_pct", "take_profit_pct", "trailing_pct",
                "drop_alert_pct", "rise_alert_pct", "volume_ratio_alert",
                "min_level")
        out: Dict[str, Any] = {}
        for key in keys:
            val = _cfg(self._app, key, None)
            if val not in (None, ""):
                out[key] = val
        return out

    async def _tool_watch(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "list")
        ledger = self._get_ledger()
        if action in ("status", "start", "stop"):
            return await self._watch_control(action)
        if action == "add":
            code = str(args.get("code") or "").strip()
            if not code:
                return "[Error] 需要 code"
            name = str(args.get("name") or "").strip()
            if not name:
                try:
                    rows = await asyncio.to_thread(data.search, code, 1)
                    if rows:
                        name = rows[0]["name"]
                except data.DataError:
                    name = ""
            res = await asyncio.to_thread(ledger.add_watch, code, name,
                                          str(args.get("note") or ""))
            return f"✅ 已加入自选：{res['name'] or res['code']}({res['code']})"
        if action == "remove":
            code = str(args.get("code") or "").strip()
            if not code:
                return "[Error] 需要 code"
            ok = await asyncio.to_thread(ledger.remove_watch, code)
            return "✅ 已从自选移除" if ok else "该代码不在自选列表中"
        rows = await asyncio.to_thread(ledger.list_watch)
        if not rows:
            return "自选列表为空。用 astock_watch action=add code=600000 添加。"
        quotes: Dict[str, Dict[str, Any]] = {}
        codes = [r["code"] for r in rows]
        try:
            for q in await self._fetch_quotes(codes):
                quotes[q.code] = q.as_dict()
        except data.DataError:
            pass
        return _table(
            ["代码", "名称", "现价", "涨跌幅", "备注"],
            [[r["code"], r["name"] or (quotes.get(r["code"], {}) or {}).get("name", "-"),
              _f((quotes.get(r["code"]) or {}).get("price")),
              _pct((quotes.get(r["code"]) or {}).get("change_pct")),
              r["note"] or "-"] for r in rows])
