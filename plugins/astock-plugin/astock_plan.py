"""自动创建并维护宿主的「盯梢计划」——让盯盘真正会自己跑起来。

────────────────────────────────────────────────────────────────────────
为什么需要这个模块（此前必须手动建计划的原因与代价）
────────────────────────────────────────────────────────────────────────

「启动盯盘」只打开**插件侧开关**：探针被调用时才判断要不要干活。真正让探针
被周期性调用的是 lite-work 的**盯梢计划**（`source=astock`）。旧版本把创建
计划这件事推给用户："请在右栏「盯梢」页新建一次计划"。

这有四个实际问题：
  ① 用户点了「启动盯盘」以为已经生效，实际什么都没发生（最典型的静默失败）；
  ② 计划建错（间隔填成 3600 秒 / 权限档选成 write / 数据源选错）无人发现；
  ③ 计划被删或被禁用后，盯盘悄悄停摆，没有任何提示；
  ④ 每个新工作区都要重来一次。

**本模块的做法**：插件在 `install()` 时拿到宿主 `app`，直接读写
`app.schedule_store`（与宿主路由层 `POST /api/schedules` 完全同一条路径，
不是绕过宿主去改文件），于是可以：
  · 启动盯盘时**自动创建**计划（幂等：已有就复用/修正，绝不重复建）；
  · 每次 `astock_watch status` 顺手**体检**计划是否还在、间隔是否合理；
  · 暂停盯盘时把计划禁用（而不是删掉），恢复时重新启用。

────────────────────────────────────────────────────────────────────────
能力边界（必须诚实）
────────────────────────────────────────────────────────────────────────
· 只在**当前进程**能拿到 `app` 时可用（打包版/正常启动即是）。拿不到就
  如实降级为「请手动建一次」，绝不假装成功——这是本模块的第一原则。
· 宿主 `schedule_store` 是内部属性（非公开插件 API）。因此**全部调用都包在
  try/except 里**，宿主改结构时最坏结果是退回手动模式，而不是插件加载失败。
· 计划里的 `workspace` 必须写当前工作区：盯梢上下文靠它定位台账
  （`ctx.workspace`），写错会让探针**静默盯错目录**。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.astock.plan")

#: 本插件的盯梢源标识（`AStockWatch.source_kind`）。计划里的 `source` 字段。
SOURCE_KIND = "astock"
#: 自动建的计划名（用于识别"哪些计划是本插件管的"）。
PLAN_NAME = "A股盯盘（插件自动创建）"
#: 默认探测间隔：5 分钟。与 `AStockWatch.probe_interval_hint` 一致。
DEFAULT_INTERVAL = 300
#: 无人值守权限档。只需要读行情 + 读台账，**不需要写权限**。
PLAN_PERMISSIONS = "read"


def _store(app: Any) -> Any:
    """取宿主的 schedule_store；拿不到返回 None（调用方降级）。"""
    if app is None:
        return None
    try:
        store = getattr(app, "schedule_store", None)
        if store is None:
            return None
        # 触发一次轻量读取，确认真的是可用的 store（而不是同名属性）
        store.all()
        return store
    except Exception as exc:
        logger.debug("[astock] 宿主未提供可用的 schedule_store: %s", exc)
        return None


def is_managed(sched: Any) -> bool:
    """该计划是否由本插件创建/管理（按 source + 名字识别）。"""
    try:
        return (str(getattr(sched, "source", "") or "") == SOURCE_KIND
                and str(getattr(sched, "name", "") or "") == PLAN_NAME)
    except Exception:
        return False


def find_plans(app: Any) -> List[Any]:
    """列出本插件管的盯梢计划（不区分启用状态）。"""
    store = _store(app)
    if store is None:
        return []
    try:
        return [s for s in store.all() if is_managed(s)]
    except Exception:
        logger.debug("[astock] 读取盯梢计划失败", exc_info=True)
        return []


def _build_schedule(workspace: str, interval: int, probe_cfg: Dict[str, Any],
                    existing_id: Optional[str] = None) -> Any:
    """构造一条 Schedule（延迟 import：旧宿主没有 watch 模块时不影响插件）。"""
    from litework.watch.schedule_store import Schedule

    sched = Schedule(
        name=PLAN_NAME,
        enabled=True,
        schedule_mode="interval",
        interval_seconds=max(60, int(interval)),
        workspace=workspace or "",
        source=SOURCE_KIND,
        probe_config=dict(probe_cfg or {}),
        # 盯梢是"探针 → 指纹变化才唤醒 Agent"，prompt 仅用于唤醒后的一句话说明
        prompt="A股盯盘：探针发现信号变化时按快照内容给出简洁提醒。",
        permissions=PLAN_PERMISSIONS,
        notify=["in_app"],
        catch_up="once",
    )
    if existing_id:
        sched.id = existing_id          # 复用原 id：运行历史/指纹基线不丢
    return sched


def _persist(app: Any, sched: Any, store: Any) -> Tuple[bool, str]:
    """写盘并通知调度器重算到期时间（与宿主路由层同一套调用）。"""
    store.upsert(sched)                # upsert 内部已含原子写盘
    scheduler = getattr(app, "watch_scheduler", None)
    if scheduler is not None:
        try:
            scheduler.reschedule(sched.id)
        except Exception:
            # 调度器重算失败不影响"计划已存在"这个事实：下一 tick 会自动带出来
            logger.debug("[astock] 调度器重算失败（下次自动生效）", exc_info=True)
    return True, ""


def ensure_plan(app: Any, workspace: str, *, interval: int = DEFAULT_INTERVAL,
                probe_cfg: Optional[Dict[str, Any]] = None,
                respect_paused: bool = False,
                ) -> Dict[str, Any]:
    """**幂等**地确保计划存在、启用且间隔正确。

    `respect_paused`（v2.3.0）：**自愈路径必须传 True**。
    传 True 时，已存在的计划若处于禁用状态，视为「用户主动暂停」，
    **只修其它字段、绝不重新启用**，并返回 action="paused"。

    为什么需要这个开关（真实的用户报障）：
    早先 `ensure_plan` 只有一种语义——「计划禁用=故障」，于是无条件
    `sched.enabled = True`。而 `install()` 每次插件重载都会调它，实测
    一天重载 459 次（Agent 每轮结束都会重载全部插件）。结果：用户点
    「停用」后，几分钟内被自愈逻辑反复推翻，界面上看起来就是
    **"停用按钮完全没用"**。这不只是体验问题，是系统在推翻用户的
    明确意图——必须让"用户说停"优先于"系统想修"。


    返回 `{"ok", "action", "message", "schedule_id"}`：
      · `ok=True, action="created"`   新建了一条
      · `ok=True, action="repaired"`  已存在但被禁用/间隔不对 → 已修正
      · `ok=True, action="existing"`  已存在且正确（什么都没做）
      · `ok=False, action="unavailable"` 拿不到宿主 store → 调用方转手动指引

    为什么"修正"而不是"删了重建"：`Schedule.runs` / `last_hash` 是运行历史与
    指纹基线，删掉会丢失"上次提醒过什么"（导致重启后重复提醒同一件事）。
    所以始终保持同一个 id，只改必要字段。
    """
    if app is None:
        return {"ok": False, "action": "unavailable", "schedule_id": "",
                "message": "当前上下文拿不到宿主应用（插件未安装到运行中的实例）"}
    store = _store(app)
    if store is None:
        return {"ok": False, "action": "unavailable", "schedule_id": "",
                "message": "宿主未提供盯梢计划存储（旧版本 lite-work 或非正常启动）"}

    interval = max(60, int(interval or DEFAULT_INTERVAL))
    try:
        existing = store.all()
    except Exception:
        return {"ok": False, "action": "unavailable", "schedule_id": "",
                "message": "读取盯梢计划失败"}

    mine = [s for s in existing if is_managed(s)]
    if not mine:
        try:
            sched = _build_schedule(workspace, interval, probe_cfg or {})
            _persist(app, sched, store)
        except Exception as exc:
            logger.warning("[astock] 创建盯梢计划失败: %s", exc, exc_info=True)
            return {"ok": False, "action": "error", "schedule_id": "",
                    "message": f"创建失败：{type(exc).__name__}: {exc}"}
        return {"ok": True, "action": "created", "schedule_id": sched.id,
                "message": f"已自动创建盯梢计划（每 {interval} 秒探测一次）"}

    # 已存在：检查是否需要修正（取第一条，多余的只报告不擅删）
    sched = mine[0]
    need: List[str] = []
    paused = not bool(getattr(sched, "enabled", True))
    try:
        # 禁用状态**不再算作需要修复的缺陷**：
        #   · respect_paused=True（自愈路径）→ 完全跳过，保持停用；
        #   · respect_paused=False（用户点「启动」）→ 在下面显式启用。
        # 早先无条件把 plan_disabled 塞进 need 并 sched.enabled=True，
        # 导致用户点停用后每次插件重载都被改回启用（v2.3.0 修）。
        if paused and not respect_paused:
            need.append("plan_disabled")
        if str(getattr(sched, "schedule_mode", "")) != "interval":
            need.append("mode")
        if int(getattr(sched, "interval_seconds", 0) or 0) != interval:
            need.append("interval")
        if str(getattr(sched, "workspace", "") or "") != (workspace or ""):
            need.append("workspace")
        if str(getattr(sched, "permissions", "") or "") != PLAN_PERMISSIONS:
            need.append("permissions")
        # 阈值配置也要跟随设置页：改了止损/止盈阈值却没同步到计划，
        # 会出现"设置页改了、实际提醒没变"的静默不一致
        if probe_cfg:
            merged = dict(getattr(sched, "probe_config", {}) or {})
            for k, v in probe_cfg.items():
                if merged.get(k) != v:
                    need.append("probe_cfg")
                    break
    except Exception:
        need.append("unknown")
    extra = len(mine) - 1
    if not need:
        if paused:
            # 用户主动暂停中：如实报告，不要谎称"已启用"
            msg = f"盯梢计划已暂停（用户主动停用，自愈不会恢复；每 {interval} 秒的配置已就绪）"
            if extra:
                msg += f"；另有 {extra} 条同名计划未动"
            return {"ok": True, "action": "paused", "schedule_id": sched.id,
                    "message": msg}
        msg = f"盯梢计划已就绪（每 {interval} 秒探测，已启用）"
        if extra:
            msg += f"；另有 {extra} 条同名计划未动（可在盯梢页手动清理）"
        return {"ok": True, "action": "existing", "schedule_id": sched.id,
                "message": msg}

    try:
        # 只在"用户这次明确要启动"时才启用；自愈路径绝不碰 enabled。
        # 这一行是"停用无效"的直接肇事点（早先无条件 = True）。
        if not (paused and respect_paused):
            sched.enabled = True
        sched.schedule_mode = "interval"
        sched.interval_seconds = interval
        sched.source = SOURCE_KIND
        if workspace:
            sched.workspace = workspace
        sched.permissions = PLAN_PERMISSIONS
        if probe_cfg:
            merged = dict(getattr(sched, "probe_config", {}) or {})
            merged.update(probe_cfg)
            sched.probe_config = merged
        _persist(app, sched, store)
    except Exception as exc:
        logger.warning("[astock] 修正盯梢计划失败: %s", exc, exc_info=True)
        return {"ok": False, "action": "error", "schedule_id": "",
                "message": f"修正失败：{type(exc).__name__}: {exc}"}
    if paused and respect_paused:
        return {"ok": True, "action": "paused", "schedule_id": sched.id,
                "message": (f"盯梢计划保持暂停（已同步每 {interval} 秒等配置，"
                            f"未改动启用状态）")}
    return {"ok": True, "action": "repaired", "schedule_id": sched.id,
            "message": f"盯梢计划已修正并启用（每 {interval} 秒探测）"}


def set_enabled(app: Any, enabled: bool) -> Dict[str, Any]:
    """启用/禁用本插件的计划（暂停盯盘用；**不删除**，保留运行历史）。

    禁用而非删除：删掉会丢失 `last_hash` 指纹基线，恢复后会立刻把当前所有
    信号当"新变化"重报一遍（邮件轰炸）。禁用则保留基线，恢复时只报真变化。
    """
    store = _store(app)
    if store is None:
        return {"ok": False, "count": 0, "message": "宿主未提供盯梢计划存储"}
    mine = find_plans(app)
    if not mine:
        return {"ok": False, "count": 0, "message": "还没有自动创建的计划"}
    changed = 0
    for sched in mine:
        try:
            if bool(getattr(sched, "enabled", True)) != enabled:
                sched.enabled = enabled
                _persist(app, sched, store)
                changed += 1
        except Exception:
            logger.debug("[astock] 切换计划启用状态失败", exc_info=True)
    verb = "启用" if enabled else "暂停"
    return {"ok": True, "count": changed,
            "message": f"已{verb} {changed} 条盯梢计划" if changed else f"计划已处于{verb}状态"}


#: 运行状态 → 人类可读（宿主的 RunRecord.status 取值）
_RUN_STATUS = {
    "ok": "✅ 正常",
    "changed": "🔔 发现变化（已唤醒 Agent）",
    "unchanged": "✔️ 无异动",
    "error": "❌ 出错",
    "skipped": "⏭ 跳过",
}


def _last_run(sched: Any) -> Dict[str, Any]:
    """取最近一次触发的摘要（计划运行历史）。

    **为什么值得展示**：用户最想确认的不是"计划存在"，而是"它真的在跑"。
    `runs[-1]` 是宿主 `Schedule.record_run()` 追加的记录；没有任何记录时
    也要如实说"还没跑过"，而不是留空让人以为坏了。
    """
    out: Dict[str, Any] = {"last_run_at": "", "last_run_status": "",
                           "last_run_detail": "", "last_run_text":
                           "还没探测过（计划刚建或尚未到点）"}
    try:
        runs = list(getattr(sched, "runs", None) or [])
    except Exception:
        return out
    if not runs:
        return out
    last = runs[-1]
    at = getattr(last, "at", None)
    if at is None and isinstance(last, dict):
        at = last.get("at")
    status = (getattr(last, "status", "") if not isinstance(last, dict)
              else last.get("status", "")) or ""
    detail = (getattr(last, "detail", "") if not isinstance(last, dict)
              else last.get("detail", "")) or ""
    when = ""
    if at:
        try:
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(float(at)).strftime("%m-%d %H:%M")
        except Exception:
            when = ""
    label = _RUN_STATUS.get(status, status or "未知")
    out["last_run_at"] = when
    out["last_run_status"] = status
    out["last_run_detail"] = str(detail)
    out["last_run_text"] = f"{when}　{label}" if when else label
    if detail:
        out["last_run_text"] += f"（{str(detail)[:40]}）"
    return out


def diagnose(app: Any, now_watching: bool, only_hours: bool,
             in_window: bool = True, window_note: str = "") -> Dict[str, Any]:
    """盯盘体检：把「插件开关 / 计划 / 邮件」三处状态合成一句话结论。

    这三处任何一处没配好，用户看到的都是同一个症状——"说了要盯着，结果
    什么都没发生"。所以必须有一处集中告诉他**到底卡在哪一环**。

    `now_watching` 的语义是**插件开关是否打开**（`watch_enabled`），
    `in_window` / `window_note` 才是"此刻是否在交易时段"。

    **这两个必须分开**（v2.2.0 修的真实误报）：调用方此前传的是
    `enabled and ok_now`，于是**收盘后体检必然报"插件盯盘开关是关闭的"**——
    可用户明明开着。更糟的是它给出一条错误动作（"点按钮打开开关"），
    而开关本来就没关，用户会以为插件坏了。收盘不是故障，是正常作息。
    """
    out: Dict[str, Any] = {
        "plugin_on": bool(now_watching), "in_window": bool(in_window),
        "plan_ok": False, "plan_id": "", "plan_interval": 0,
        "plan_enabled": False, "plan_count": 0,
        "verdict": "", "actions": [],
    }
    mine = find_plans(app)
    out["plan_count"] = len(mine)
    if mine:
        sched = mine[0]
        out["plan_id"] = str(getattr(sched, "id", "") or "")
        out["plan_interval"] = int(getattr(sched, "interval_seconds", 0) or 0)
        out["plan_enabled"] = bool(getattr(sched, "enabled", True))
        out["plan_ok"] = out["plan_enabled"] and out["plan_interval"] > 0
        out.update(_last_run(sched))

    if not out["plugin_on"]:
        out["verdict"] = "插件盯盘开关是关闭的 —— 即使计划会跑，探针也会直接返回「无变化」"
        out["actions"].append("说「启动盯盘」或点设置页按钮打开开关")
    elif not mine:
        out["verdict"] = "还没有盯梢计划 —— 探针不会被周期性调用，等于没在盯"
        out["actions"].append("说「启动盯盘」，插件会自动创建计划")
    elif not out["plan_enabled"]:
        out["verdict"] = "计划存在但被禁用了 —— 探针不会被调用"
        out["actions"].append("说「启动盯盘」，插件会自动重新启用")
    elif not only_hours:
        out["verdict"] = "盯盘中；但「仅交易时段」已关闭，非交易时段也会跑并消耗 token"
        out["actions"].append("建议在设置页打开「仅交易时段盯盘」")
    elif not in_window:
        # 收盘/午休/周末：链路是好的，只是按自律本来就不该跑。
        # 这里必须说"就绪"，否则用户会去修一个没坏的东西。
        out["verdict"] = ("全链路就绪，当前不在盯盘时段"
                          + (f"（{window_note}）" if window_note else "")
                          + f" —— 到点自动开始，非交易时段不消耗 token")
    else:
        out["verdict"] = (f"全链路就绪 —— 每 {out['plan_interval']} 秒探测一次，"
                          f"仅工作日 9:10–15:00 生效")
    out["probe_config"] = _plan_probe_config(mine[0]) if mine else {}
    out["summary"] = summarize_probe_config(out["probe_config"]) if mine else ""
    return out


# ---------------------------------------------------------------- 探针参数摘要
#
# 为什么这块必须补（v2.3.1）：`diagnose()` 早先只读 interval/enabled/last_run，
# **从不读 probe_config**。于是 `astock_watch status` 的体检表能告诉你"在盯、
# 每 300 秒"，却答不出用户真正要问的那句——**到底在盯什么**。
# 实际后果：7 条精确价位规则躺在计划里，模型与用户都看不见；而设置页的
# `contributes.settings` 里根本没有 rules/codes/kinds 这些键（它们只存在于
# 计划级 probe_config），于是"改盯什么"也就成了纯 GUI 操作。
#
# 摘要而不是原始 JSON：窄栏与聊天里 JSON 会折成一行看不清，且人关心的是
# "哪只股、什么价、什么动作"，不是字段名。

#: probe_config 里**由插件读取**的键（顺序即展示顺序）。
#: 与 `AStockWatch.config_schema` / `probe.pick()` 保持一致——这里多一个键会
#: 让人以为配了就生效（实际探针不读），少一个键则是"配了却看不见"。
PROBE_KEYS_READ = ("codes", "kinds", "min_level", "rules", "once_per_day")


def _plan_probe_config(sched: Any) -> Dict[str, Any]:
    """取计划里的 probe_config（非 dict 时返回空表，绝不抛）。"""
    try:
        cfg = getattr(sched, "probe_config", None)
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001 - 宿主结构变动时如实降级为空
        return {}


def summarize_probe_config(cfg: Dict[str, Any]) -> str:
    """probe_config → 人类可读摘要（Markdown，可空）。

    **只陈述配置，不做判断**：这里既不查行情也不评估"这个止损位合不合理"，
    只把"你让它盯什么"如实翻译成一句话，避免体检页给出误导性结论。
    """
    if not isinstance(cfg, dict) or not cfg:
        return ""
    parts: List[str] = []

    raw_rules = cfg.get("rules")
    rules, errs = _parse_rules_lenient(raw_rules)
    if rules:
        codes = sorted({str(r.get("code") or "") for r in rules})
        stop = sum(1 for r in rules if str(r.get("kind") or "") == "stop_loss")
        take = sum(1 for r in rules if str(r.get("kind") or "") == "take_profit")
        urgent = sum(1 for r in rules if str(r.get("severity") or "") == "urgent")
        bits = [f"{len(rules)} 条价位规则", f"覆盖 {len(codes)} 只"]
        if stop:
            bits.append(f"止损 {stop}")
        if take:
            bits.append(f"止盈 {take}")
        if urgent:
            bits.append(f"紧急 {urgent}")
        parts.append("、".join(bits))
    if errs:
        parts.append(f"⚠️ {len(errs)} 条规则配置有误")

    codes_raw = str(cfg.get("codes") or "").strip()
    if codes_raw:
        parts.append(f"额外盯 {len([c for c in codes_raw.replace('，', ',').split(',') if c.strip()])} 只")
    kinds_raw = str(cfg.get("kinds") or "").strip()
    if kinds_raw:
        parts.append(f"信号类型限 {kinds_raw}")
    level = str(cfg.get("min_level") or "").strip()
    if level:
        parts.append(f"报告级别 {level} 起")
    once = cfg.get("once_per_day")
    if once not in (None, ""):
        parts.append("当天去重 " + ("开" if _truthy(once) else "关"))

    known = set(PROBE_KEYS_READ)
    unknown = [k for k in cfg if k not in known]
    text = "；".join(parts) if parts else "已配置但无可识别的键"
    if unknown:
        # 写了探针不读的键 = 静默不生效（最典型的"配了没反应"）
        text += f"（另有未识别键：{'、'.join(sorted(unknown))}，探针不会读）"
    return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def _parse_rules_lenient(raw: Any) -> "Tuple[List[Dict[str, Any]], List[str]]":
    """解析规则（复用 price_rule，但绝不因配置写错而让体检页报错）。

    `astock_price_rule` 是单独模块且被探针依赖；这里**容忍 import 失败**
    （单测里按文件加载时未插入插件目录的情况），退回"能数出几条就数几条"，
    宁可摘要粗糙，也不能让 `status` 直接抛。
    """
    try:
        import astock_price_rule as _pr
        return _pr.parse_rules(raw)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(raw, str):
        try:
            import json as _json
            raw = _json.loads(raw)
        except Exception:  # noqa: BLE001
            return [], ["rules 不是合法 JSON"]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], []
    out = [r for r in raw if isinstance(r, dict) and str(r.get("code") or "").strip()]
    return out, []


def describe_rules(cfg: Dict[str, Any], limit: int = 20) -> List[str]:
    """逐条列出价位规则（发给人的清单，不参与任何判定）。

    `limit` 兜底：规则多到几十条时全列会把 status 淹没，只列前 N 条并说明。
    """
    raw = (cfg or {}).get("rules")
    rules, _errs = _parse_rules_lenient(raw)
    if not rules:
        return []
    arrow = {"lt": "跌破", "le": "触及", "gt": "站上", "ge": "站上"}
    out: List[str] = []
    for r in rules[:max(1, limit)]:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        label = str(r.get("label") or "")
        op = str(r.get("op") or "lt")
        price = r.get("price")
        sev = str(r.get("severity") or "action")
        try:
            cond = f"{arrow.get(op, op)} {float(price):g}"
        except (TypeError, ValueError):
            cond = "(价位非法)"
        head = f"{name}({code})" if name else code
        line = f"- {head} {cond}"
        if label:
            line += f"｜{label}"
        line += f"　`{sev}`"
        vr_gt, vr_lt = r.get("vol_ratio_gt"), r.get("vol_ratio_lt")
        if vr_gt not in (None, ""):
            line += f"（放量 >{vr_gt}）"
        if vr_lt not in (None, ""):
            line += f"（缩量 <{vr_lt}）"
        out.append(line)
    if len(rules) > limit:
        out.append(f"- …另有 {len(rules) - limit} 条未列出")
    return out
