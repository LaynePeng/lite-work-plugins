# lite-work 社区插件：盯梢 / 定时计划的**工具面**
#
# 为什么需要这个插件
# ---------------------------------------------------------------------------
# lite-work 的盯梢系统（`litework/watch/`）是一个完整子系统：调度循环、指纹比对、
# 跨进程触发租约、通知总线、无人值守权限档。但它对外只有一个 REST 面
# （`/api/schedules`），**没有任何 Agent 工具**。于是出现一个尴尬的错位：
#
#   · 界面上能建计划、能改 `probe_config`，人点得动；
#   · 模型却既看不见「现在盯的是什么」，也没法「改成盯别的」——
#     只能回一句"请到右栏盯梢页手动新建"。
#
# 第三方插件（如 astock）为了绕开这个缺口，直接在进程内读写
# `app.schedule_store`（见 astock_plan.py）。那条路能用，但每个插件都要自己
# 重写一遍，且只有写自己那条计划的权限。本插件把它变成**通用的工具面**：
# 任何盯梢源的计划都能被列出、创建、修改、立即跑一次。
#
# 与宿主 REST 的关系
# ---------------------------------------------------------------------------
# 两者走**同一条数据通路**：`app.schedule_store`（`ScheduleStore.upsert` 内部即是
# 原子写盘）+ `app.watch_scheduler.reschedule()`。不是去改 `schedules.json`，
# 所以不会与界面上的操作互相覆盖。
#
# 安全边界（这是本插件最需要小心的地方）
# ---------------------------------------------------------------------------
# 1. **workspace 钉死为当前工作区**：计划里的 workspace 决定探针盯哪个目录
#    （`ProbeContext.workspace`），可写会让模型把探针指到任意路径。因此
#    不接受调用方传 workspace（传了也忽略并说明）。
# 2. **权限档白名单**：只允许 `read` / `read+web`（与宿主路由层的
#    `ALLOWED_PERMISSIONS` 一致）。无人值守时没人点审批，写/执行类必须给不出去。
# 3. **source 必须已注册**：`source` 写错时宿主 `fire()` 只会记一条 error，
#    计划永远跑不出结果——所以在写入前就对照 `watch_registry` 校验。
# 4. **跨工作区保护**：列出全部计划（只读无害），但**修改/删除**默认只允许
#    当前工作区的计划；要动别的项目得显式 `force=true`。
# 5. **删除默认是预览**（`dry_run=true`）：删计划会丢运行历史与指纹基线
#    （恢复后会把当前所有信号当"新变化"重报一遍），必须显式确认。
#
# 安装：放进 `~/.lite-work/plugins/watch-plugin/`（本仓库 `plugins/watch-plugin/`）。
# 零第三方依赖（只用标准库 + 宿主稳定接口）。
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

logger = logging.getLogger("litework.plugin.watch")

#: 无人值守权限档白名单。与宿主 `server/routers/schedules.py` 的
#: `ALLOWED_PERMISSIONS` 同步——不同步会让 UI 能存的档位与工具能写的档位不一致，
#: 用户改完在界面上看不出差别、探针却拿不到工具。
ALLOWED_PERMISSIONS = ("read", "read+web")
ALLOWED_MODES = ("interval", "daily", "cron")
ALLOWED_CATCH_UP = ("once", "all", "skip")
ALLOWED_NOTIFY = ("in_app", "email", "push")

#: 探测间隔下限（秒）。宿主 `_advance()` 也是 `max(60, ...)`；写小了不会更快，
#: 只会在回读时显示成 60 而让人以为没改成功。
MIN_INTERVAL = 60

#: 单条计划保留的运行历史条数上限由宿主裁剪（`ScheduleStore.MAX_RUNS`），
#: 这里只决定**展示**最近几条。
SHOW_RUNS = 3


# ---------------------------------------------------------------- 宿主访问


def _app_of(plugin: Any) -> Any:
    return getattr(plugin, "_app", None)


def _store(app: Any) -> Any:
    """取宿主的 schedule_store；拿不到返回 None（调用方降级说明原因）。"""
    if app is None:
        return None
    try:
        store = getattr(app, "schedule_store", None)
        if store is None:
            return None
        store.all()          # 轻量探活：确认真是可用的 store 而非同名属性
        return store
    except Exception as exc:  # noqa: BLE001 - 宿主改结构时降级，不炸插件
        logger.debug("[watch] 宿主未提供可用的 schedule_store: %s", exc)
        return None


def _scheduler(app: Any) -> Any:
    try:
        return getattr(app, "watch_scheduler", None)
    except Exception:  # noqa: BLE001
        return None


def _registry(app: Any) -> Any:
    try:
        return getattr(app, "watch_registry", None)
    except Exception:  # noqa: BLE001
        return None


def _workspace(app: Any) -> str:
    try:
        return str(getattr(app, "workspace", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _now_text(ts: Any) -> str:
    if not ts:
        return "还没跑过"
    try:
        import datetime as dt
        return dt.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


_RUN_STATUS = {
    "ok": "✅ 正常",
    "changed": "🔔 发现变化（已唤醒 Agent）",
    "unchanged": "✔️ 无异动",
    "error": "❌ 出错",
    "skipped": "⏭ 跳过",
}


# ---------------------------------------------------------------- 取值助手


def _as_bool(value: Any, default: bool = False) -> bool:
    """语义化布尔（`bool("false") == True` 的坑与 astock/jev 同款）。

    模型经工具传参时常见 `"false"` / `"0"` 这类字符串（尤其从 JSON 文本
    二次解析出来的），原样当布尔用会把"关掉"变成"打开"。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "否", "关"):
        return False
    return default


def _coerce_json(value: Any, field: str) -> Tuple[Any, str]:
    """把可能是 JSON 字符串的值还原成结构（计划编辑器也这么做过）。

    模型有时会把对象序列化成字符串传进来（`"{\\"a\\":1}"`）。不还原就会把
    一段文本存进 `probe_config`，插件读 `cfg["a"]` 永远拿不到——静默失效。
    """
    if not isinstance(value, str):
        return value, ""
    text = value.strip()
    if not text:
        return {}, ""
    try:
        return json.loads(text), ""
    except (ValueError, TypeError):
        return None, f"{field} 不是合法 JSON：{text[:60]}"


# ---------------------------------------------------------------- 计划渲染


def _probe_fields(sched: Any) -> List[str]:
    """`probe_config` 的键值摘要（一行一键，便于模型与人对齐）。

    直接 `json.dumps` 整个对象在窄栏里会折成一行看不清；逐键列出可读性更好，
    且能一眼看出"哪几个键我传对了、哪几个没落上"。
    """
    cfg = getattr(sched, "probe_config", None)
    if not isinstance(cfg, dict) or not cfg:
        return ["  （无）"]
    out: List[str] = []
    for key in sorted(cfg):
        val = cfg[key]
        if isinstance(val, (list, dict)):
            text = json.dumps(val, ensure_ascii=False, sort_keys=True)
            if len(text) > 240:
                text = text[:240] + f"…（共 {len(val)} 项）"
        else:
            text = str(val)
        out.append(f"  - `{key}` = {text}")
    return out


def _run_lines(sched: Any) -> List[str]:
    runs = list(getattr(sched, "runs", None) or [])
    if not runs:
        return ["  - 还没探测过（计划刚建或尚未到点）"]
    out: List[str] = []
    for rec in runs[-SHOW_RUNS:]:
        if isinstance(rec, dict):
            at, status = rec.get("at"), rec.get("status", "")
            detail = rec.get("detail", "")
        else:
            at = getattr(rec, "at", None)
            status = getattr(rec, "status", "")
            detail = getattr(rec, "detail", "")
        label = _RUN_STATUS.get(str(status), str(status) or "未知")
        line = f"  - {_now_text(at)}　{label}"
        if detail:
            line += f"（{str(detail)[:80]}）"
        out.append(line)
    return out


def _fmt_plan(sched: Any, index: Optional[int] = None) -> str:
    """单条计划的完整视图（Markdown，含 probe_config 与最近运行）。"""
    head = f"**{getattr(sched, 'name', '') or '(未命名)'}**"
    if index is not None:
        head = f"{index}. {head}"
    src = str(getattr(sched, "source", "") or "")
    lines = [
        head,
        f"  - id: `{getattr(sched, 'id', '')}`",
        f"  - 数据源: `{src or '(纯定时任务)'}`　"
        f"状态: {'✅ 启用' if getattr(sched, 'enabled', True) else '⏸ 已暂停'}",
        f"  - 调度: `{getattr(sched, 'schedule_mode', '')}` "
        f"每 {int(getattr(sched, 'interval_seconds', 0) or 0)} 秒"
        + (f"，时间 {getattr(sched, 'time_of_day', '')}"
           if str(getattr(sched, 'schedule_mode', '')) == "daily" else "")
        + (f"，cron `{getattr(sched, 'cron', '')}`"
           if str(getattr(sched, 'schedule_mode', '')) == "cron" else ""),
        f"  - 工作区: `{getattr(sched, 'workspace', '') or '(跟随当前)'}`",
        f"  - 权限档: `{getattr(sched, 'permissions', 'read')}`　"
        f"通知: {', '.join(getattr(sched, 'notify', None) or []) or '-'}",
    ]
    prompt = str(getattr(sched, "prompt", "") or "")
    if prompt:
        lines.append(f"  - 唤醒说明: {prompt[:160]}")
    lines.append("  - **探针参数（probe_config）**：")
    lines.extend("  " + item for item in _probe_fields(sched))
    lines.append("  - 最近运行：")
    lines.extend(_run_lines(sched))
    return "\n".join(lines)


def _resolve(app: Any, ident: str) -> Tuple[Any, str]:
    """按 id / 名称 / 名称片段定位一条计划。返回 (计划, 错误说明)。"""
    store = _store(app)
    if store is None:
        return None, ("拿不到宿主的计划存储（旧版 lite-work 或非正常启动）；"
                      "无法读写盯梢计划")
    key = (ident or "").strip()
    if not key:
        return None, "需要 id 或计划名称（先用 schedule_list 看有哪些）"
    try:
        items = store.all()
    except Exception as exc:  # noqa: BLE001
        return None, f"读取计划失败：{type(exc).__name__}: {exc}"
    for s in items:
        if str(getattr(s, "id", "")) == key:
            return s, ""
    exact = [s for s in items if str(getattr(s, "name", "") or "") == key]
    if len(exact) == 1:
        return exact[0], ""
    fuzzy = [s for s in items
             if key in str(getattr(s, "name", "") or "")
             or key == str(getattr(s, "source", "") or "")]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        names = "、".join(f"{getattr(s, 'name', '')}(`{getattr(s, 'id', '')}`)"
                          for s in fuzzy[:6])
        return None, f"“{key}”匹配到多条计划，请用 id 指定：{names}"
    return None, f"找不到计划 “{key}”（先用 schedule_list 看有哪些）"


def _scope_guard(app: Any, sched: Any, force: bool) -> str:
    """跨工作区保护：改/删别的项目的计划必须先显式 force。

    `workspace` 决定探针盯哪个目录（读哪个台账、哪个仓库）。一个窗口里的模型
    顺手改掉另一个项目的计划，症状是"那个项目突然不提醒了"且无从排查。
    """
    if force:
        return ""
    cur = _workspace(app)
    own = str(getattr(sched, "workspace", "") or "")
    if own and cur and own != cur:
        return (f"计划 `{getattr(sched, 'id', '')}` 属于工作区 `{own}`，"
                f"当前工作区是 `{cur}`。确认要动它请加 force=true")
    return ""


# ---------------------------------------------------------------- 插件本体


class WatchPlanPlugin(ToolPlugin):
    """盯梢 / 定时计划的读写工具面（schedule_*）。"""

    name = "watch-plugin"
    version = "1.0.0"
    description = ("盯梢 / 定时计划管理：列出、查看、创建、修改、立即触发、删除"
                   "宿主的盯梢计划（含 probe_config 探针参数）")

    def __init__(self) -> None:
        self._app = None

    def install(self, kernel) -> None:
        """捕获 app（workspace / schedule_store / watch_registry 都在它上面）。"""
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:  # noqa: BLE001
            self._app = None
        super().install(kernel)

    # -------------------------------------------------- 声明

    def get_tools(self) -> List[ToolDefinition]:
        ident_prop = {
            "type": "string",
            "description": "计划的 id（sch_xxx）或名称；也可用名称片段",
        }
        return [
            ToolDefinition(
                name="schedule_sources",
                description=(
                    "列出可用的盯梢数据源（source_kind + 展示名 + 该源的 probe_config "
                    "字段说明）。**配置盯梢前先调它**——probe_config 的键名由各数据源"
                    "自己定义（如 astock 的 rules/codes/kinds/min_level），写错键名不会"
                    "报错，只是探针读不到、静默不生效。"),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="schedule_list",
                description=(
                    "列出盯梢 / 定时计划（含探针参数 probe_config 与最近几次运行结果）。"
                    "想看「现在到底在盯什么」用这个——盯盘/定时是宿主内置子系统，"
                    "曾在插件的 status 里只看到开关状态、看不到规则内容。"
                    "默认只列当前工作区的计划（include_other_workspaces=true 看全部）。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "只看某个数据源（如 astock / email / url）",
                        },
                        "include_other_workspaces": {
                            "type": "boolean",
                            "description": "true＝连其他工作区的计划一起列（只读）",
                        },
                        "include_disabled": {
                            "type": "boolean",
                            "description": "false＝只看启用的计划（默认 true 全列）",
                        },
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="schedule_get",
                description="查看单条计划的完整内容（探针参数 + 最近运行 + 运行历史）。",
                parameters={
                    "type": "object",
                    "properties": {"id": ident_prop},
                    "required": ["id"],
                },
            ),
            ToolDefinition(
                name="schedule_upsert",
                description=(
                    "创建或修改一条盯梢 / 定时计划（这是**设置盯梢内容的唯一工具通道**）。"
                    "不传 id＝新建；传 id（或名称）＝修改该计划。\n"
                    "探针参数写 probe_config：**整体替换**用 probe_config，"
                    "只改其中几个键用 merge_probe_config（推荐，避免误删其他键），"
                    "删键用 drop_probe_keys。\n"
                    "工作区固定为当前工作区（不可指定）；权限档只允许 read / read+web。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "要修改的计划 id 或名称；留空＝新建",
                        },
                        "name": {"type": "string", "description": "计划名称"},
                        "enabled": {
                            "type": "boolean",
                            "description": "是否启用（停用＝暂停探测，不删历史与指纹基线）",
                        },
                        "source": {
                            "type": "string",
                            "description": ("数据源 source_kind（如 astock）；留空＝纯定时任务"
                                            "（到点直接跑 prompt）。先看 schedule_sources"),
                        },
                        "schedule_mode": {
                            "type": "string",
                            "enum": list(ALLOWED_MODES),
                            "description": "interval 间隔 / daily 每天定点 / cron 表达式",
                        },
                        "interval_seconds": {
                            "type": "number",
                            "description": f"间隔秒数（最小 {MIN_INTERVAL}）",
                        },
                        "time_of_day": {
                            "type": "string",
                            "description": "daily 模式的时间 HH:MM",
                        },
                        "cron": {
                            "type": "string",
                            "description": "cron 模式表达式（分 时 日 月 周，宿主内置格式，不是系统 crontab）",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "探针发现变化后唤醒 Agent 时的说明（也是纯定时任务要跑的内容）",
                        },
                        "probe_config": {
                            "type": "object",
                            "description": "探针参数（整体替换）。键名见 schedule_sources",
                        },
                        "merge_probe_config": {
                            "type": "object",
                            "description": "探针参数（只覆盖给出的键，其余保留）",
                        },
                        "drop_probe_keys": {
                            "type": "array", "items": {"type": "string"},
                            "description": "要删除的探针参数键名",
                        },
                        "permissions": {
                            "type": "string",
                            "enum": list(ALLOWED_PERMISSIONS),
                            "description": "无人值守权限档（默认 read；需要抓网页才用 read+web）",
                        },
                        "notify": {
                            "type": "array", "items": {"type": "string"},
                            "description": "通知渠道（默认 in_app）",
                        },
                        "catch_up": {
                            "type": "string",
                            "enum": list(ALLOWED_CATCH_UP),
                            "description": "应用关闭期间错过的触发怎么补（默认 once）",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "true＝允许修改其他工作区的计划（默认拒绝）",
                        },
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="schedule_run",
                description=(
                    "立即触发一次计划（无视到期时间与 enabled）。走的是与定时触发"
                    "完全相同的路径：探针 → 指纹比对 → 有变化才唤 Agent。"
                    "返回本次的探测结果（无异动 / 发现变化 / 出错）。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": ident_prop,
                        "force": {
                            "type": "boolean",
                            "description": "true＝允许触发其他工作区的计划",
                        },
                    },
                    "required": ["id"],
                },
            ),
            ToolDefinition(
                name="schedule_delete",
                description=(
                    "删除一条计划。**默认只预览不删**（dry_run 缺省 true），"
                    "确认后再传 dry_run=false。删除会丢运行历史与指纹基线——"
                    "想暂停请用 schedule_upsert enabled=false（暂停不丢历史）。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": ident_prop,
                        "dry_run": {
                            "type": "boolean",
                            "description": "默认 true＝只预览；false 才真删",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "true＝允许删除其他工作区的计划",
                        },
                    },
                    "required": ["id"],
                },
            ),
        ]

    # -------------------------------------------------- 执行

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        args = args or {}
        try:
            if name == "schedule_sources":
                return self._tool_sources()
            if name == "schedule_list":
                return self._tool_list(args)
            if name == "schedule_get":
                return self._tool_get(args)
            if name == "schedule_upsert":
                return self._tool_upsert(args)
            if name == "schedule_run":
                return await self._tool_run(args)
            if name == "schedule_delete":
                return self._tool_delete(args)
        except Exception as exc:  # noqa: BLE001 - 工具异常必须变成可读文本
            logger.exception("[watch] 工具 %s 执行异常", name)
            return f"[Error] {name} 执行异常：{type(exc).__name__}: {exc}"
        return f"[Error] 未知工具 {name}"

    # ---- schedule_sources ----

    def _tool_sources(self) -> str:
        reg = _registry(self._app)
        if reg is None:
            return ("[Error] 拿不到宿主的盯梢数据源注册表（旧版 lite-work）；"
                    "仍可用纯定时任务（source 留空）")
        try:
            items = reg.describe_all()
        except Exception as exc:  # noqa: BLE001
            return f"[Error] 读取数据源失败：{type(exc).__name__}: {exc}"
        if not items:
            return "没有可用的盯梢数据源。"
        lines = ["## 可用盯梢数据源", ""]
        for src in items:
            lines.append(f"### `{src.get('source_kind')}` — {src.get('display_name')}")
            if src.get("description"):
                lines.append(src["description"])
            hint = src.get("probe_interval_hint")
            if hint:
                lines.append(f"- 建议探测间隔：{int(hint)} 秒")
            schema = src.get("config_schema") or {}
            if schema:
                lines.append("- **probe_config 字段**：")
                for key, spec in schema.items():
                    if isinstance(spec, dict):
                        label = spec.get("label") or spec.get("hint") or ""
                        typ = spec.get("type") or ""
                        lines.append(f"  - `{key}`"
                                     + (f"（{typ}）" if typ else "")
                                     + (f"：{label}" if label else ""))
                    else:
                        lines.append(f"  - `{key}`：{spec}")
            else:
                lines.append("- 该数据源无额外 probe_config 字段")
            lines.append("")
        lines.append("用 `schedule_upsert source=<source_kind> probe_config={...}` 落配置。")
        return "\n".join(lines)

    # ---- schedule_list ----

    def _tool_list(self, args: Dict[str, Any]) -> str:
        store = _store(self._app)
        if store is None:
            return ("[Error] 拿不到宿主的计划存储（旧版 lite-work 或非正常启动）；"
                    "请到右栏「盯梢」页手动管理")
        try:
            items = store.all()
        except Exception as exc:  # noqa: BLE001
            return f"[Error] 读取计划失败：{type(exc).__name__}: {exc}"
        want_source = str(args.get("source") or "").strip()
        include_other = _as_bool(args.get("include_other_workspaces"), False)
        include_disabled = _as_bool(args.get("include_disabled"), True)
        cur = _workspace(self._app)

        shown: List[Any] = []
        hidden = 0
        for s in items:
            if want_source and str(getattr(s, "source", "") or "") != want_source:
                continue
            if not include_disabled and not getattr(s, "enabled", True):
                continue
            own = str(getattr(s, "workspace", "") or "")
            if own and cur and own != cur and not include_other:
                hidden += 1
                continue
            shown.append(s)

        lines = [f"## 盯梢 / 定时计划（{len(shown)} 条）", ""]
        if not shown:
            lines.append("当前工作区还没有计划。用 `schedule_upsert` 新建，"
                         "或在右栏「盯梢」页手动建。")
        for i, s in enumerate(shown, 1):
            lines.append(_fmt_plan(s, index=i))
            lines.append("")
        if hidden:
            lines.append(f"> 另有 {hidden} 条属于其他工作区的计划未列出"
                         f"（加 include_other_workspaces=true 查看）。")
        return "\n".join(lines).rstrip()

    # ---- schedule_get ----

    def _tool_get(self, args: Dict[str, Any]) -> str:
        sched, err = _resolve(self._app, str(args.get("id") or ""))
        if sched is None:
            return f"[Error] {err}"
        return "## 计划详情\n\n" + _fmt_plan(sched)

    # ---- schedule_upsert ----

    def _tool_upsert(self, args: Dict[str, Any]) -> str:
        app = self._app
        store = _store(app)
        if store is None:
            return ("[Error] 拿不到宿主的计划存储（旧版 lite-work 或非正常启动）；"
                    "无法自动建计划，请到右栏「盯梢」页手动新建")
        ident = str(args.get("id") or "").strip()
        cur_ws = _workspace(app)

        existing = None
        if ident:
            existing, err = _resolve(app, ident)
            if existing is None:
                return f"[Error] {err}"
            guard = _scope_guard(app, existing, _as_bool(args.get("force"), False))
            if guard:
                return f"[Error] {guard}"

        # ---- 字段校验（先全部校验再写盘：半写会让计划处于自相矛盾的状态）
        source = args.get("source")
        if source is not None:
            source = str(source).strip()
            if source:
                reg = _registry(app)
                if reg is None or reg.get(source) is None:
                    known = []
                    if reg is not None:
                        try:
                            known = reg.kinds()
                        except Exception:  # noqa: BLE001
                            known = []
                    return (f"[Error] 未知数据源 `{source}`。"
                            f"可用：{', '.join(known) or '(无)'}；"
                            f"留空＝纯定时任务")

        permissions = args.get("permissions")
        if permissions is not None:
            permissions = str(permissions).strip()
            if permissions not in ALLOWED_PERMISSIONS:
                return (f"[Error] 权限档只允许 {list(ALLOWED_PERMISSIONS)}"
                        f"（无人值守没人点审批，写/执行类给不出去）")

        mode = args.get("schedule_mode")
        if mode is not None:
            mode = str(mode).strip()
            if mode not in ALLOWED_MODES:
                return f"[Error] schedule_mode 只允许 {list(ALLOWED_MODES)}"

        catch_up = args.get("catch_up")
        if catch_up is not None:
            catch_up = str(catch_up).strip()
            if catch_up not in ALLOWED_CATCH_UP:
                return f"[Error] catch_up 只允许 {list(ALLOWED_CATCH_UP)}"

        interval = args.get("interval_seconds")
        if interval not in (None, ""):
            try:
                interval = max(MIN_INTERVAL, int(float(interval)))
            except (TypeError, ValueError):
                return f"[Error] interval_seconds 不是数字：{interval!r}"

        notify = args.get("notify")
        if notify is not None:
            if isinstance(notify, str):
                notify = [notify]
            if not isinstance(notify, list):
                return "[Error] notify 应为字符串数组"
            notify = [str(n) for n in notify if str(n).strip()]

        # probe_config 三种写法：整体替换 / 合并 / 删键
        new_cfg: Optional[Dict[str, Any]] = None
        raw_cfg = args.get("probe_config")
        if raw_cfg not in (None, ""):
            cfg, err = _coerce_json(raw_cfg, "probe_config")
            if err:
                return f"[Error] {err}"
            if not isinstance(cfg, dict):
                return "[Error] probe_config 应为对象（键值表）"
            new_cfg = dict(cfg)
        merge_cfg, err = _coerce_json(args.get("merge_probe_config") or {}, "merge_probe_config")
        if err:
            return f"[Error] {err}"
        if merge_cfg and not isinstance(merge_cfg, dict):
            return "[Error] merge_probe_config 应为对象"
        drops = args.get("drop_probe_keys") or []
        if isinstance(drops, str):
            drops = [drops]
        if not isinstance(drops, list):
            return "[Error] drop_probe_keys 应为字符串数组"

        # ---- 组装（新建 / 修改）
        if existing is None:
            sched = self._new_schedule(args, source=source or "",
                                       mode=mode or "interval",
                                       interval=interval if interval else 3600,
                                       catch_up=catch_up or "once",
                                       permissions=permissions or "read",
                                       notify=notify or ["in_app"],
                                       workspace=cur_ws)
            action = "created"
        else:
            sched = existing
            action = "updated"
            if source is not None:
                sched.source = source
            if mode is not None:
                sched.schedule_mode = mode
            if interval is not None:
                sched.interval_seconds = interval
            if catch_up is not None:
                sched.catch_up = catch_up
            if permissions is not None:
                sched.permissions = permissions
            if notify is not None and notify:
                sched.notify = notify
            # 工作区**不接受调用方指定**：探针盯哪个目录必须等于当前工作区，
            # 否则模型可以把探针指到任意路径去读文件。
            if cur_ws:
                sched.workspace = cur_ws

        for key, attr in (("name", "name"), ("prompt", "prompt"),
                          ("time_of_day", "time_of_day"), ("cron", "cron")):
            if args.get(key) is not None:
                setattr(sched, attr, str(args.get(key)))
        if args.get("enabled") is not None:
            sched.enabled = _as_bool(args.get("enabled"), True)

        # 探针参数：合并语义（最常用的"只改一条规则"不该清掉别的键）
        cfg_now = dict(getattr(sched, "probe_config", {}) or {})
        if new_cfg is not None:
            cfg_now = new_cfg
        if merge_cfg:
            cfg_now.update(merge_cfg)
        for key in drops:
            cfg_now.pop(str(key), None)
        sched.probe_config = cfg_now

        try:
            store.upsert(sched)
        except Exception as exc:  # noqa: BLE001
            return f"[Error] 写入计划失败：{type(exc).__name__}: {exc}"
        scheduler = _scheduler(app)
        rescheduled = False
        if scheduler is not None:
            try:
                scheduler.reschedule(sched.id)
                rescheduled = True
            except Exception:  # noqa: BLE001
                logger.debug("[watch] 调度器重算失败（下次 tick 自动生效）", exc_info=True)

        head = ("✅ 已创建计划" if action == "created" else "✅ 已更新计划")
        tail = [] if rescheduled else ["（调度器未就绪，下次 tick 会自动带出来）"]
        return "\n".join([f"{head}（source={sched.source or '定时任务'}，"
                          f"每 {int(sched.interval_seconds or 0)} 秒）", ""]
                         + tail + [_fmt_plan(sched)])

    def _new_schedule(self, args: Dict[str, Any], *, source: str, mode: str,
                      interval: Any, catch_up: str, permissions: str,
                      notify: List[str], workspace: str) -> Any:
        """构造 Schedule（延迟 import：旧宿主没有 watch 模块时给出可读错误）。"""
        from litework.watch.schedule_store import Schedule

        return Schedule(
            name=str(args.get("name") or ""),
            enabled=True,
            schedule_mode=mode,
            interval_seconds=int(interval or 3600),
            time_of_day=str(args.get("time_of_day") or "09:00"),
            cron=str(args.get("cron") or ""),
            workspace=workspace or "",
            prompt=str(args.get("prompt") or ""),
            source=source,
            probe_config={},
            permissions=permissions,
            notify=list(notify) or ["in_app"],
            catch_up=catch_up,
        )

    # ---- schedule_run ----

    async def _tool_run(self, args: Dict[str, Any]) -> str:
        app = self._app
        sched, err = _resolve(app, str(args.get("id") or ""))
        if sched is None:
            return f"[Error] {err}"
        guard = _scope_guard(app, sched, _as_bool(args.get("force"), False))
        if guard:
            return f"[Error] {guard}"
        scheduler = _scheduler(app)
        if scheduler is None:
            return ("[Error] 调度器未启动（服务未就绪或非 Web 模式启动）；"
                    "无法立即触发。稍后重试，或用右栏「盯梢」页的「立即运行」")
        try:
            rec = await scheduler.fire(sched)
        except Exception as exc:  # noqa: BLE001
            return f"[Error] 触发失败：{type(exc).__name__}: {exc}"
        status = getattr(rec, "status", "") if not isinstance(rec, dict) else rec.get("status", "")
        detail = getattr(rec, "detail", "") if not isinstance(rec, dict) else rec.get("detail", "")
        label = _RUN_STATUS.get(str(status), str(status) or "未知")
        lines = [f"## 已触发一次：{getattr(sched, 'name', '') or getattr(sched, 'id', '')}",
                 "",
                 f"- 结果：{label}",
                 f"- 详情：{detail or '-'}"]
        if status == "unchanged":
            lines.append("- 说明：探针指纹没变 → **没有唤醒 Agent**（这是设计如此，省钱）")
        elif status == "changed":
            lines.append("- 说明：发现变化，已启动一个 Agent 任务处理（结果写回计划绑定的会话）")
        elif status == "error":
            lines.append("- 排查：看数据源是否已安装、probe_config 是否合法；"
                         "`schedule_get` 可看到探针参数")
        return "\n".join(lines)

    # ---- schedule_delete ----

    def _tool_delete(self, args: Dict[str, Any]) -> str:
        app = self._app
        store = _store(app)
        if store is None:
            return "[Error] 拿不到宿主的计划存储，无法删除"
        sched, err = _resolve(app, str(args.get("id") or ""))
        if sched is None:
            return f"[Error] {err}"
        guard = _scope_guard(app, sched, _as_bool(args.get("force"), False))
        if guard:
            return f"[Error] {guard}"
        dry = _as_bool(args.get("dry_run"), True)
        name = getattr(sched, "name", "") or getattr(sched, "id", "")
        if dry:
            return "\n".join([
                f"## 预览：将删除计划「{name}」",
                "",
                _fmt_plan(sched),
                "",
                "**这是预览，没有删除。** 确认后传 `dry_run=false` 再调一次。",
                "只想暂停请用 `schedule_upsert enabled=false`——暂停保留运行历史与指纹基线，"
                "删除会让恢复时把当前所有信号当「新变化」重报一遍。",
            ])
        try:
            ok = store.delete(str(getattr(sched, "id", "")))
        except Exception as exc:  # noqa: BLE001
            return f"[Error] 删除失败：{type(exc).__name__}: {exc}"
        if not ok:
            return "[Error] 删除失败：计划不存在（可能已被其他窗口删除）"
        scheduler = _scheduler(app)
        if scheduler is not None:
            try:
                scheduler.reschedule()
            except Exception:  # noqa: BLE001
                logger.debug("[watch] 删除后重算失败（下次 tick 自动生效）", exc_info=True)
        return f"✅ 已删除计划「{name}」"
