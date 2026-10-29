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
import json
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
        start_hhmm = str(_cfg(self._app, "watch_start_time", "") or "")
        ok_now, why = mkt.should_watch_now(start_hhmm=start_hhmm)
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
            ["下次开始", mkt.next_watch_window(start_hhmm=start_hhmm)],
            ["时段自律", (f"✅ 仅工作日 {mkt._resolve_start(start_hhmm)}–15:00，"
                          f"午休跳过" if only_hours
                          else "⚠️ 已关闭（全天盯，会消耗 token）")],
        ]))
        lines.append("")
        lines.append(f"**体检结论**：{diag['verdict']}")
        # ── 到底在盯什么（v2.3.1）───────────────────────────────────────
        # 体检表回答的是"链路通不通"，用户真正要问的是"它在盯哪只股票的
        # 什么价位"。这块内容此前**完全不可见**（diagnose 不读 probe_config，
        # 设置页 schema 里也没有这些键），于是精确价位规则成了只有 GUI 能看
        # 的东西——模型想核对或修改都无从下手。
        _append_watch_scope(lines, diag)
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
        write_ok = _as_bool(_cfg(self._app, "email_notify_enabled", False), False)
        mail_level = str(_cfg(self._app, "email_min_level", "urgent") or "urgent")
        probe_level = str(_cfg(self._app, "min_level", "action") or "action")
        _rank = {"info": 0, "action": 1, "urgent": 2}
        _probe_floor = _rank.get(probe_level, 1)
        _mail_floor = _rank.get(mail_level, 2)
        # 阈值阶梯：探针报得出、邮件发不出，是**两个独立开关**造成的
        # （探针默认 action、邮件默认 urgent）。这类失败完全静默——
        # 面板有提醒、邮箱没邮件、日志无异常，所以体检必须主动点出来。
        if write_ok and _mail_floor > _probe_floor:
            lines.append("")
            lines.append("### ⚠️ 邮件阈值会把部分信号拦掉")
            lines.append("")
            lines.append(f"探测级别 `{probe_level}`（会报出），"
                         f"邮件级别 `{mail_level}`（只发这一级及以上）——"
                         f"**级别低于 `{mail_level}` 的信号不会发邮件**。"
                         f"价格突破类价位规则通常是 `action` 级，"
                         f"若你的规则是 `action` 而阈值为 `urgent`，会一条邮件都收不到。")
            lines.append("")
            lines.append(f"要收到这类提醒：设置页把「通知阈值」调到 `{probe_level}` 或更低。")
        # 探针最近一次是否真的被阈值拦住了（进程内记录，重启后清空）
        blocked = getattr(self, "_mail_blocked", None)
        if isinstance(blocked, dict) and blocked.get("blocked"):
            lines.append("")
            lines.append("### 🔴 上次探测有信号被邮件阈值拦掉")
            lines.append("")
            lines.append(f"{blocked.get('blocked')} 条信号（级别 "
                         f"{'/'.join(blocked.get('severities') or ['?'])}，"
                         f"标的 {'、'.join(blocked.get('codes') or ['?'])}）"
                         f"低于邮件阈值 `{blocked.get('email_min_level')}`，未发邮件。")
        lines.append("")
        lines.append("### 邮件通知依赖这条链路")
        lines.append("")
        lines.append("高风险邮件**不是独立进程**：探针发现信号时顺带调 email-plugin 发信。"
                     "所以「收不到邮件」的排查顺序是：① 看本页**盯梢计划是否运行中**；"
                     "② 设置页点「发送测试邮件」验证通道；③ 看阈值与收件人。")
        return "\n".join(lines)

    # -------------------------------------------------- 盯什么：读 / 写

    def _watch_plan_view(self) -> str:
        """`action=plan`：把计划里的 `probe_config` 完整摊开给人/模型看。

        与 `action=status` 的分工：status 回答「链路通不通、现在在不在盯」，
        plan 回答「盯的到底是什么」。分两个动作而不是塞一起，是因为
        status 已经很长（体检表 + 邮件阈值 + 排查指引），再叠加规则清单会
        把最关键的"卡在哪一环"淹掉。
        """
        diag = plan_ctl.diagnose(
            self._app,
            _as_bool(_cfg(self._app, "watch_enabled", True), True),
            _as_bool(_cfg(self._app, "watch_only_trading_hours", True), True))
        cfg = diag.get("probe_config") or {}
        lines = ["## 盯盘规则（当前实际生效）", ""]
        if not diag.get("plan_ok") and not cfg:
            lines.append(f"⚠️ {diag.get('verdict')}")
            lines.append("")
            lines.append("没有可读的计划，所以现在没有「实际生效的规则」。"
                         "先说「启动盯盘」建计划（插件会自动创建）。")
            return "\n".join(lines)
        lines.append(f"- 计划 id：`{diag.get('plan_id') or '-'}`")
        lines.append(f"- 探测间隔：{diag.get('plan_interval') or 0} 秒")
        lines.append(f"- 覆盖代码：{cfg.get('codes') or '（默认：台账持仓 + 自选股）'}")
        lines.append(f"- 信号类型：{cfg.get('kinds') or '（默认：通用 9 类全开）'}")
        lines.append(f"- 报告级别：{cfg.get('min_level') or '（默认 action）'}")
        once = cfg.get("once_per_day")
        # 未配置＝探针默认 true（见 astock_price_rule.evaluate 的 once_per_day）
        once_on = True if once in (None, "") else plan_ctl._truthy(once)
        lines.append(f"- 当天去重：{'开' if once_on else '关'}")
        lines.append("")
        rules = plan_ctl.describe_rules(cfg, limit=50)
        if rules:
            lines.append(f"### 价位规则（{len(rules)} 条）")
            lines.extend(rules)
        else:
            lines.append("### 价位规则\n\n（没有配置精确价位规则，只按百分比阈值盯）")
        lines.append("")
        lines.append("### 原始 probe_config")
        lines.append("```json")
        lines.append(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
        lines.append("改动：`astock_watch action=set_rules`（rules 整体替换 / "
                     "add_rules 追加 / remove_rules 删除指定代码）。"
                     "通用通道：`schedule_upsert merge_probe_config={...}`。")
        return "\n".join(lines)

    def _watch_set_rules(self, args: Dict[str, Any]) -> str:
        """`action=set_rules`：写回计划里的 `rules`（以及可选的 codes/kinds/级别）。

        为什么要这个动作：`rules` 只存在于**计划级 `probe_config`**，设置页
        schema 里没有这些键。在补上工具通道之前，改一条止损价必须走 GUI；
        模型只能回"请到右栏盯梢页手动改"。

        安全语义（每条都对应一种真实误操作）：
        - **只动 rules 这一个键**：绝不整体覆盖 probe_config，否则用户配的
          codes/kinds/min_level 会被静默清空，症状是"改了个价位，其他提醒全没了"；
        - **先在内存里校验再写盘**：非法规则被 `parse_rules` 挑出来后直接报错，
          不让半截配置落盘；
        - **写盘走 `ensure_plan` 的 probe_cfg 合并语义**：保留指纹基线（last_hash），
          不会因为改规则就把当前所有信号当"新变化"重报一遍。
        """
        replace, add, remove = _rules_from_args(args)
        if replace in (None, "") and not add and not remove:
            return ("[Error] 需要 rules（整体替换）/ add_rules（追加）/ "
                    "remove_rules（按代码删除）三者之一")
        # `rules=[]` 是**合法的清空操作**（换成只按百分比阈值盯），
        # 不能与"什么都没传"混为一谈——否则用户想撤掉全部精确价位时无路可走。
        clearing = isinstance(replace, (list, tuple)) and len(replace) == 0

        diag = plan_ctl.diagnose(
            self._app,
            _as_bool(_cfg(self._app, "watch_enabled", True), True),
            _as_bool(_cfg(self._app, "watch_only_trading_hours", True), True),
        )
        if not diag.get("plan_id"):
            return ("[Error] 还没有盯梢计划——先 `astock_watch action=start` "
                    "建一条（插件会自动创建），再写规则")

        cfg = dict(diag.get("probe_config") or {})
        current, errs = plan_ctl._parse_rules_lenient(cfg.get("rules"))
        if errs:
            # 现有配置本身有问题时也照样往下走（用户可能正是来修它的），
            # 但要把旧问题带上，否则改完看不出"之前就是坏的"
            logger.info("[astock] 现有 rules 有 %d 处问题，继续按新配置覆盖", len(errs))

        if isinstance(replace, (list, tuple)):
            # 显式给了数组（含空数组＝清空）：整体替换
            new_rules, perrs = plan_ctl._parse_rules_lenient(replace)
        else:
            new_rules, perrs = list(current), []
            if add:
                add_ok, aerrs = plan_ctl._parse_rules_lenient(add)
                perrs = list(aerrs)
                new_rules.extend(add_ok)
            if remove:
                drop = {str(c).strip() for c in remove if str(c).strip()}
                new_rules = [r for r in new_rules
                             if str(r.get("code") or "") not in drop]

        if perrs:
            return ("[Error] 规则有误，未写入：\n"
                    + "\n".join(f"- {e}" for e in perrs[:8])
                    + "\n\n每条至少要有 code 与 price；op ∈ lt/le/gt/ge；"
                      "severity ∈ info/action/urgent")

        # 其它可调项（都只覆盖显式给出的键）
        extra: Dict[str, Any] = {}
        for key in ("codes", "kinds", "min_level", "once_per_day"):
            if args.get(key) not in (None, ""):
                extra[key] = args[key]
        if not new_rules and not extra and not clearing:
            return "[Error] 规则为空且没有其它配置项，未做任何修改"

        payload = dict(extra)
        payload["rules"] = new_rules
        head = "✅ 已写入盯盘规则"
        if clearing:
            head = "✅ 已清空价位规则（改为只按百分比阈值盯）"
        # respect_paused=True：**改规则不是"启动盯盘"**。传 False（默认）会让
        # ensure_plan 顺手把 enabled 改回 True——用户明明暂停了盯盘，只是让模型
        # 加一条规则，结果计划被静默重新启用。这与 v2.3.0 修的「点了停用几分钟后
        # 被自愈推翻」是同一类问题：系统推翻用户的明确意图。
        res = plan_ctl.ensure_plan(
            self._app, str(getattr(self._app, "workspace", "") or ""),
            interval=int(_num(self._app, "watch_interval_seconds", 300)),
            probe_cfg=payload,
            respect_paused=True,
        )
        if not res.get("ok"):
            return f"[Error] 写入盯梢计划失败：{res.get('message')}"

        # 触发面同步：规则改了但插件开关关着时，写了也不会跑
        enabled = _as_bool(_cfg(self._app, "watch_enabled", True), True)
        if not clearing:
            # 无改动时如实说"无需改动"，避免让人以为写坏了
            if res.get("action") == "existing":
                head = "✅ 规则与当前一致（无需改动）"
        lines = [f"{head}（共 {len(new_rules)} 条）", ""]
        refreshed = plan_ctl.summarize_probe_config(dict(payload))
        if refreshed:
            lines.append(f"**概览**：{refreshed}")
        rules_txt = plan_ctl.describe_rules(payload, limit=50)
        if rules_txt:
            lines.append("")
            lines.extend(rules_txt)
        if not enabled:
            lines.append("")
            lines.append("⚠️ 插件盯盘开关是**关闭**的：规则已保存，但探针不会跑。"
                         "说「启动盯盘」（`action=start`）才会生效。")
        lines.append("")
        lines.append(f"（{res.get('message')}）")
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
        if action == "plan":
            return self._watch_plan_view()
        if action == "set_rules":
            return self._watch_set_rules(args)
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


# ---------------------------------------------------------------- 盯什么（摘要）


def _append_watch_scope(lines: list, diag: Dict[str, Any]) -> None:
    """把「现在到底在盯什么」写进 status 输出（模块级，便于单测直调）。"""
    cfg = diag.get("probe_config") or {}
    summary = str(diag.get("summary") or "")

    lines.append("")
    lines.append("### 在盯什么")
    lines.append("")
    if not diag.get("plan_ok") and not cfg:
        lines.append("还没有可读的盯梢计划——先说「启动盯盘」建一条，再看这里。")
        return
    if not cfg:
        lines.append("计划存在，但 `probe_config` 是空的：按默认范围盯"
                     "（持仓 + 自选股的全部信号类型，报告级别 action 起）。")
        # 空配置不是错误，但要让人知道"以为配了特效、其实走的默认"
        lines.append("")
        lines.append("> 想加精确价位（如「跌破 36.00 清仓」）用 "
                     "`astock_watch action=set_rules`，"
                     "或到右栏「盯梢」页的计划编辑器里填 `probe_config.rules`。")
        return
    if summary:
        lines.append(f"**概览**：{summary}")

    rules = plan_ctl.describe_rules(cfg)
    if rules:
        lines.append("")
        lines.append(f"**价位规则**（{len(rules)} 行，完整内容见计划）：")
        lines.extend(rules)

    # 生效范围：这两项决定"没写规则的部分靠什么兜底"
    scope = []
    if not str(cfg.get("codes") or "").strip():
        scope.append("未限定代码 → 盯**台账持仓 + 自选股**全部")
    if not str(cfg.get("kinds") or "").strip():
        scope.append("未限定信号类型 → 通用 9 类全开"
                     "（止损/止盈/回撤/涨跌停/异动/竞价/跳空/尾盘）")
    if scope:
        lines.append("")
        lines.extend(f"- {s}" for s in scope)

    lines.append("")
    lines.append("> 这些内容**只存在于计划的 `probe_config`**（设置页 schema 里没有），"
                 "改动走 `astock_watch action=set_rules` 或右栏「盯梢」页的计划编辑器；"
                 "通用通道是 `schedule_upsert merge_probe_config={...}`。")


def _rules_from_args(args: Dict[str, Any]) -> tuple:
    """从工具参数里取规则（三种写法：整体、追加、删除）。"""
    add = args.get("add_rules")
    replace = args.get("rules")
    remove = args.get("remove_rules")
    if isinstance(add, dict):
        add = [add]
    if isinstance(replace, dict):
        replace = [replace]
    if isinstance(remove, str):
        remove = [remove]
    return replace, add or [], remove or []

