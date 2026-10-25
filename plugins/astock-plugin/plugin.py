"""A 股助手插件（astock-plugin）：行情研究 + 手工交易台账 + 下单提示。

**一句话定位**：用户自己在券商 App 下单，本插件负责「取数 → 记账 → 算账 →
提示该不该动手、动多少」，**完全不接触任何委托通道**（没有下单、撤单、
券商登录、也不生成委托文件）。

工具面（10 个）
    astock_quote      实时行情快照（涨跌停价、量比、换手、市值；自动补交易时段说明）
    astock_kline      K 线 + 技术摘要（均线、区间高低、量能变化）
    astock_search     按代码/名称/拼音检索标的
    astock_screen     条件选股（涨跌幅/成交额/换手/市值/主力净流入）
    astock_flow       个股资金流（主力/超大单/大单/中单/小单净额）
    astock_record     录入**已成交**的买卖（dry_run 预览校验，commit 落账）
    astock_portfolio  持仓总览（实时浮盈、占比、当日盈亏）＋ 资金/已实现盈亏/流水/纠错
    astock_hint       下单提示（T+1 可卖、止损止盈、加减仓档位、买卖股数估算、舆情背离）
    astock_sentiment  投资舆情分析（公告/研报/新闻/热度/龙虎榜/两融/股东户数）
    astock_watch      自选股增删查

扩展点
    AStockWatch       盯梢源：自选股/持仓出现异动时才唤醒 Agent（不烧 token 盯盘）
    contributes.panels 「持仓」右栏面板：Markdown 表格实时浮盈

设计约束（遵循本仓库 AGENTS.md）
- **零第三方依赖**：httpx/pandas 都不用，纯标准库（urllib/sqlite3/json/dataclasses）。
  打包版 lite-work 是 PyInstaller frozen 进程，少一个依赖就少一份 wheels 与
  跨平台负担；`scripts/check_plugin_deps.py` 因此天然通过。
- **多文件插件形态**：宿主把插件目录插入 sys.path，故同目录兄弟模块用裸
  import + `astock_` 前缀（与 jev 插件同一约定），不用包相对导入。
- **无参构造**：workspace / config 在 `install(kernel)` 里从 `kernel.get_service("app")` 取。
- **合规底线**：所有结论标注"仅供参考，不构成投资建议"；不预测、不给目标价。
- **数据落盘**：`<workspace>/.astock/ledger.sqlite`，可在设置页改用绝对路径。

版本：见 `AstStockPlugin.version`，与 manifest.json 同步 bump。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

# 盯梢源基类（lite-work ≥1.10.9）。旧宿主没有 litework.watch 时下面的
# AStockWatch 不会注册——基类退化为空 object，本插件其它工具照常工作。
try:
    from litework.watch.registry import ProbeContext, ProbeResult, WatchSource
except ImportError:  # 旧宿主降级
    class WatchSource:  # type: ignore[no-redef]
        """空基类占位（旧宿主无盯梢扩展点）。"""

        name = "watch-source"
        source_kind = ""
        display_name = ""
        description = ""
        version = "1.0.0"
        probe_interval_hint = 300
        config_schema: Dict[str, str] = {}

        async def probe(self, cfg: Dict[str, Any], ctx: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

    class ProbeResult:  # type: ignore[no-redef]
        """旧宿主的本地占位（与宿主同名字段，保证 probe() 仍可被单测/复用）。

        宿主没有 litework.watch 时，AStockWatch 本来就不会被注册成盯梢源；
        但**方法本身仍应可调用**——否则单测与任何直接复用的代码都会炸在
        `ProbeResult(...)` 上（真实缺陷：降级路径不是"什么都不做"，而是
        "不崩"）。`fingerprint()` 与宿主实现保持同一算法，便于比对。
        """

        def __init__(self, snapshot: str = "", summary: str = "",
                     changed_hint: str = "", ok: bool = True,
                     error: str = "") -> None:
            self.snapshot = snapshot
            self.summary = summary
            self.changed_hint = changed_hint
            self.ok = ok
            self.error = error

        def fingerprint(self) -> str:
            import hashlib
            return hashlib.sha256(
                self.snapshot.encode("utf-8", errors="replace")).hexdigest()[:32]

    ProbeContext = None  # type: ignore[assignment,misc]

import astock_data as data
import astock_hints as hints
import astock_market as mkt
import astock_sentiment as senti
import astock_sentiment_data as senti_data
import astock_sentiment_tool as senti_tool
from astock_ledger import Ledger, LedgerError, default_ledger_path

logger = logging.getLogger("litework.plugins.astock")

PLUGIN_NAME = "astock-plugin"

#: 一次行情请求最多带多少个标的（腾讯接口实测 60+ 仍正常，留余量）
MAX_QUOTE_BATCH = 50
#: 面板/提示默认最多展示多少只持仓
MAX_ROWS = 30


# ---------------------------------------------------------------- 配置读取


def _cfg(app: Any, key: str, default: Any = None) -> Any:
    """读 `app.config["astock-plugin"][key]`（设置页经通用插件 UI 协议写入该命名空间）。"""
    if app is None:
        return default
    cfg = getattr(app, "config", None) or {}
    own = cfg.get(PLUGIN_NAME) if isinstance(cfg, dict) else None
    if not isinstance(own, dict):
        return default
    value = own.get(key, default)
    return default if value is None or value == "" else value


def _as_bool(value: Any, default: bool = False) -> bool:
    """宽容布尔解析（配置可能来自 JSON 布尔、字符串或数字）。

    必要性：`bool("false")` 在 Python 里是 `True`。用户手改 config.json 写
    `"enable_add": "false"` 时会意外打开摊平提示——这类"配置读反"的 bug
    排查成本极高，因此统一走这个函数。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def _num(app: Any, key: str, default: float) -> float:
    try:
        return float(_cfg(app, key, default))
    except (TypeError, ValueError):
        return default


def _bool(app: Any, key: str, default: bool) -> bool:
    """读插件配置里的布尔项（复用 _as_bool 的宽容解析）。"""
    return _as_bool(_cfg(app, key, default), default)


def fee_config(app: Any) -> mkt.FeeConfig:
    base = mkt.FeeConfig()
    return mkt.FeeConfig(
        commission_rate=_num(app, "commission_rate", base.commission_rate),
        min_commission=_num(app, "min_commission", base.min_commission),
        stamp_tax_rate=_num(app, "stamp_tax_rate", base.stamp_tax_rate),
        transfer_fee_rate=_num(app, "transfer_fee_rate", base.transfer_fee_rate),
        other_rate=_num(app, "other_rate", base.other_rate),
    )


def hints_config(app: Any) -> hints.HintsConfig:
    base = hints.HintsConfig()
    out = hints.HintsConfig()
    for key in list(out.__dataclass_fields__):
        default = getattr(base, key)
        if isinstance(default, bool):
            setattr(out, key, _bool(app, key, default))
        else:
            setattr(out, key, _num(app, key, default))
    return out


# ---------------------------------------------------------------- 格式化小工具


def _f(value: Any, digits: int = 2, dash: str = "-") -> str:
    """安全格式化数字（None → 破折号，避免把"没有"显示成 0）。"""
    if value is None or value == "":
        return dash
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any) -> str:
    """带符号百分比（涨跌幅用；换手率/占比这类无方向的指标请用 _ratio）。"""
    if value is None:
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _ratio(value: Any) -> str:
    """无方向百分比（换手率、占比、振幅）——加 + 号会让人误以为是涨跌幅。"""
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _money(value: Any) -> str:
    """金额 → 人类可读（万 / 亿）。"""
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    """Markdown 表（Agent 与前端都能正常呈现）。"""
    def cell(v: Any) -> str:
        text = str(v if v is not None else "-")
        return text.replace("|", "\\|")
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------- 盯梢源


class AStockWatch(WatchSource):
    """盯梢源：持仓/自选股出现**异动**时才唤醒 Agent。

    关键设计：`snapshot` 里**只放触发信号**（"600000:stop_loss" 这样的标签），
    不放价格。价格每秒都在变，放进去会导致每次探测都被判为"有变化"，
    等于每 5 分钟白烧一次 LLM。信号集合变化才是真正值得叫醒用户的时刻。

    配置（计划编辑器里填）：
        codes      额外盯的代码，逗号分隔（默认盯台账里的持仓 + 自选股）
        kinds      要盯的信号类型，逗号分隔；默认
                   stop_loss,take_profit,trailing,limit_up,limit_down,event
    """

    name = "astock-watch"
    source_kind = "astock"
    display_name = "A股持仓/自选异动"
    description = ("盯持仓与自选股：触及止损/止盈线、涨跌停、当日异动、T+1 可卖"
                   "等信号集合变化时触发。价格本身变动不会触发（避免高频误唤醒）。")
    probe_interval_hint = 300
    config_schema = {
        "codes": "额外盯的代码，逗号分隔（留空＝台账里的持仓+自选股）",
        "kinds": "要盯的信号类型，逗号分隔（默认止损/止盈/回撤/涨跌停/异动）",
    }

    DEFAULT_KINDS = ("stop_loss", "take_profit", "trailing", "limit_up",
                     "limit_down", "event")

    async def probe(self, cfg: Dict[str, Any], ctx: Any) -> Any:
        own = (ctx.config or {}).get(PLUGIN_NAME) if isinstance(ctx.config, dict) else {}
        own = own if isinstance(own, dict) else {}
        workspace = str(getattr(ctx, "workspace", "") or "")
        path = str(own.get("ledger_path") or "").strip() or default_ledger_path(workspace)
        try:
            ledger = Ledger(path, mkt.FeeConfig(
                commission_rate=float(own.get("commission_rate") or 0.00025),
                min_commission=float(own.get("min_commission") or 5.0),
            ))
        except Exception as exc:  # 台账打不开不该让盯梢变"有变化"
            return ProbeResult(ok=False, error=f"台账读取失败：{exc}")

        cfg_codes = [c.strip() for c in str(cfg.get("codes") or "").replace("，", ",").split(",") if c.strip()]
        want_kinds = {k.strip() for k in str(cfg.get("kinds") or "").replace("，", ",").split(",") if k.strip()} \
            or set(self.DEFAULT_KINDS)

        positions = {p["code"]: p for p in ledger.list_positions()}
        codes = list(cfg_codes) or list(positions)
        for w in ledger.list_watch():
            if w["code"] not in codes:
                codes.append(w["code"])
        if not codes:
            return ProbeResult(ok=False, error="台账里没有持仓或自选股，先在插件里录入持仓/加自选")

        items: List[Tuple[str, str, str]] = []
        for c in codes[:MAX_QUOTE_BATCH]:
            try:
                code, venue = mkt.normalize_code(c)
            except ValueError:
                continue
            items.append((code, venue, "stock"))
        if not items:
            return ProbeResult(ok=False, error="没有可识别的代码")
        try:
            quotes = await asyncio.to_thread(data.fetch_quotes, items)
        except data.DataError as exc:
            return ProbeResult(ok=False, error=f"行情获取失败：{exc}")

        hcfg = hints.HintsConfig()
        for key in hcfg.__dataclass_fields__:
            raw = own.get(key, getattr(hcfg, key))
            if isinstance(getattr(hcfg, key), bool):
                # bool("false") == True —— 配置里存字符串时必须按语义解析，
                # 否则用户在 config.json 里写 "enable_add": "false" 会变成开启
                setattr(hcfg, key, _as_bool(raw))
            else:
                try:
                    setattr(hcfg, key, float(raw))
                except (TypeError, ValueError):
                    pass

        signals: List[str] = []
        detail_lines: List[str] = []
        for q in quotes:
            pos = positions.get(q.code)
            qd = q.as_dict()
            if not pos:
                # 自选股：只看当日异动与涨跌停
                if q.limit_up and q.price and abs(q.price - q.limit_up) < 0.005:
                    sig = "limit_up"
                elif q.limit_down and q.price and abs(q.price - q.limit_down) < 0.005:
                    sig = "limit_down"
                else:
                    chg = q.change_pct or 0
                    sig = "event" if abs(chg) >= hcfg.drop_alert_pct else ""
                if sig and sig in want_kinds:
                    signals.append(f"{q.code}:{sig}")
                    detail_lines.append(f"- {q.name}({q.code}) {sig}：现价 {q.price}（{_pct(q.change_pct)}）")
                continue
            hs = hints.build_position_hints(pos, qd, hcfg)
            for h in hs:
                if h.kind in want_kinds:
                    signals.append(f"{q.code}:{h.kind}")
                    detail_lines.append(f"- {h.title}｜{h.suggestion}")

        snapshot = "\n".join(sorted(signals))
        if not snapshot:
            return ProbeResult(snapshot="", summary=f"盯 {len(quotes)} 只，无异动")
        return ProbeResult(
            snapshot=snapshot,
            summary=f"{len(signals)} 条异动信号",
            changed_hint="信号变化：\n" + "\n".join(detail_lines[:10]))


# ---------------------------------------------------------------- 插件主体


class AstockPlugin(ToolPlugin):
    """A 股助手：行情研究 + 手工交易台账 + 下单提示（不含任何委托通道）。"""

    name = PLUGIN_NAME
    version = "1.1.0"
    description = ("A 股助手：行情/K线/选股/资金流 + 投资舆情分析（七类证据+背离提示） + 手工交易台账（持仓成本·T+1 可卖·"
                   "已实现盈亏）+ 下单提示（止损止盈/加减仓档位/买卖股数估算）。"
                   "用户自己在券商下单，插件只记账与提示，不接任何委托通道。"
                   "零第三方依赖；数据落盘 <workspace>/.astock/ledger.sqlite。")

    contributes = {
        "settings": [
            {"key": "ledger_path", "type": "str", "label": "台账文件路径",
             "default": "",
             "hint": "留空＝<workspace>/.astock/ledger.sqlite。想跨项目共用一份台账时"
                     "填绝对路径（如 ~/Documents/astock/ledger.sqlite）"},
            {"key": "commission_rate", "type": "number", "label": "佣金率",
             "default": 0.00025, "hint": "万 2.5 填 0.00025；按你券商实际费率改"},
            {"key": "min_commission", "type": "number", "label": "最低佣金（元）",
             "default": 5.0, "hint": "多数券商单笔最低 5 元；小额定投时这项影响很大"},
            {"key": "stamp_tax_rate", "type": "number", "label": "印花税（卖出）",
             "default": 0.0005, "hint": "现行 0.05%，仅卖出收取。政策变动时改这里"},
            {"key": "transfer_fee_rate", "type": "number", "label": "过户费",
             "default": 0.00001, "hint": "沪深双向 0.001%；ETF 不计"},
            {"key": "stop_loss_pct", "type": "number", "label": "止损线（%）",
             "default": 8.0, "hint": "浮亏达到该比例提示止损。调太松会失去纪律意义"},
            {"key": "take_profit_pct", "type": "number", "label": "止盈线（%）",
             "default": 15.0},
            {"key": "trailing_pct", "type": "number", "label": "移动止盈回撤（%）",
             "default": 10.0, "hint": "自持仓期最高价回撤该比例、且仍浮盈时提示"},
            {"key": "add_step_pct", "type": "number", "label": "加仓档位间隔（%）",
             "default": 5.0, "hint": "每跌该比例提示一次加仓；需同时开启「加仓提示」"},
            {"key": "reduce_step_pct", "type": "number", "label": "减仓档位间隔（%）",
             "default": 10.0, "hint": "每涨该比例提示一次减仓"},
            {"key": "enable_add", "type": "boolean", "label": "开启加仓提示",
             "default": False, "hint": "默认关：摊平提示容易被误当成「喊单」，"
                                       "建议想清楚分批计划后再开"},
            {"key": "enable_reduce", "type": "boolean", "label": "开启减仓提示",
             "default": False},
            {"key": "position_cap_pct", "type": "number", "label": "单票占比上限（%）",
             "default": 25.0, "hint": "超过则该股买入数量估算会被此上限约束并提示减仓"},
            {"key": "volume_ratio_alert", "type": "number", "label": "放量阈值（量比）",
             "default": 2.0},
            {"key": "drop_alert_pct", "type": "number", "label": "下跌异动阈值（%）",
             "default": 5.0},
            {"key": "rise_alert_pct", "type": "number", "label": "上涨异动阈值（%）",
             "default": 7.0},
            {"key": "rate_limit_per_sec", "type": "number", "label": "行情请求速率上限（次/秒）",
             "default": 5.0, "hint": "公共免费接口，限速是礼貌也是稳定性前提；"
                                     "调太快会拿到连接被掐断的错误"},
            {"key": "http_timeout", "type": "number", "label": "请求超时（秒）",
             "default": 10.0},
            {"key": "quote_cache_ttl", "type": "number", "label": "行情缓存（秒）",
             "default": 3.0, "hint": "同一轮对话里反复问同一只股票时不必重复打接口"},
            {"key": "kline_cache_ttl", "type": "number", "label": "K 线缓存（秒）",
             "default": 60.0},
            {"key": "panel_style", "type": "str", "label": "右栏持仓面板形态",
             "default": "markdown",
             "hint": "markdown＝紧凑表格（横向数值对齐，适合盘后核对）；"
                     "tree＝一只股一张卡，卡内写触发规则与建议动作并按严重度着色"
                     "（适合盘中扫一眼）。两者数据完全相同，只是排版不同"},
        ],
        "panels": [
            {"id": "portfolio", "title": "持仓"},
        ],
    }

    def __init__(self) -> None:
        self._app: Any = None
        self._ledger: Optional[Ledger] = None
        self._ledger_key: str = ""

    # -------------------------------------------------- 安装

    def install(self, kernel) -> None:
        """捕获 app（workspace / config 都从这里取，支持项目热切换）。"""
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:
            self._app = None
        try:
            data.configure(
                rate_per_sec=_num(self._app, "rate_limit_per_sec", 5.0),
                timeout=_num(self._app, "http_timeout", 10.0),
                cache_ttl=_num(self._app, "quote_cache_ttl", 3.0),
                kline_ttl=_num(self._app, "kline_cache_ttl", 60.0),
            )
        except Exception:      # 配置异常不应让插件加载失败
            logger.warning("[astock] 行情参数配置失败，使用默认值", exc_info=True)
        self._ledger = None    # workspace/配置可能已变，下次用时重建
        super().install(kernel)

    # -------------------------------------------------- 基础设施

    def _workspace(self) -> str:
        return str(getattr(self._app, "workspace", "") or "") if self._app else ""

    def _ledger_path(self) -> str:
        raw = str(_cfg(self._app, "ledger_path", "") or "").strip()
        if raw:
            return os.path.abspath(os.path.expanduser(raw))
        return default_ledger_path(self._workspace())

    def _get_ledger(self) -> Ledger:
        """台账句柄（路径变化时自动重建）。"""
        path = self._ledger_path()
        if self._ledger is None or self._ledger_key != path:
            self._ledger = Ledger(path, fee_config(self._app))
            self._ledger_key = path
        else:
            self._ledger.refresh_fee(fee_config(self._app))
        return self._ledger

    def _hints_cfg(self) -> hints.HintsConfig:
        return hints_config(self._app)

    # -------------------------------------------------- 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        code_prop = {
            "type": "string",
            "description": "股票代码：600000 / sh600000 / 000001.sz 均可",
        }
        return [
            ToolDefinition(
                name="astock_quote",
                description=(
                    "A 股实时行情快照：现价、涨跌幅、涨跌停价、量比、换手、市值、"
                    "五档外的关键字段与交易时段说明。不给 codes 时返回台账里的持仓+自选股。"
                    "批量最多 50 只。数据来源腾讯行情（失败自动换新浪）。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "codes": {
                            "type": "array", "items": {"type": "string"},
                            "description": "代码列表，如 [\"600000\",\"000001\"]；留空＝持仓+自选股",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["stock", "index", "etf"],
                            "description": "标的类型；查指数必须传 index（000001 既是个股也是上证指数）",
                        },
                        "no_cache": {"type": "boolean", "description": "跳过本地缓存强制刷新"},
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="astock_kline",
                description=(
                    "K 线 + 技术摘要：日/周/月/分钟线，默认前复权。返回均线(MA5/10/20/60)、"
                    "区间高低、累计涨跌幅、最近 N 根量能对比。用于判断位置与趋势，但**不做预测**。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "code": code_prop,
                        "period": {
                            "type": "string",
                            "enum": ["day", "week", "month", "5m", "15m", "30m", "60m"],
                            "description": "周期，默认 day",
                        },
                        "limit": {"type": "number", "description": "取多少根，默认 120，上限 640"},
                        "adjust": {
                            "type": "string", "enum": ["qfq", "hfq", "none"],
                            "description": "复权方式，默认 qfq（前复权）；分钟线不支持复权",
                        },
                        "with_bars": {
                            "type": "boolean",
                            "description": "是否附带最近 K 线的原始数据（默认 false，只给摘要）",
                        },
                    },
                    "required": ["code"],
                },
            ),
            ToolDefinition(
                name="astock_search",
                description="按代码/名称/拼音检索 A 股标的（返回代码、类型、板块）。买入前先确认代码。",
                parameters={
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "代码或名称或拼音首字母，如 浦发 / PFYH / 600000"},
                        "limit": {"type": "number", "description": "最多几条，默认 10"},
                    },
                    "required": ["keyword"],
                },
            ),
            ToolDefinition(
                name="astock_screen",
                description=(
                    "条件选股（全市场快照排序+过滤）：按涨跌幅/成交额/换手/市值/主力净流入排序，"
                    "可过滤涨跌幅区间、成交额、换手率、市值、是否排除 ST。金额单位：元。"
                    "只做筛选，不构成推荐。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "universe": {"type": "string", "enum": ["all", "main", "gem", "star", "bj", "etf"],
                                     "description": "市场范围，默认 all"},
                        "sort_by": {"type": "string",
                                    "enum": ["change_pct", "price", "amount", "volume", "turnover", "mcap", "pe", "main_net"],
                                    "description": "排序字段，默认 change_pct"},
                        "ascending": {"type": "boolean", "description": "升序（默认降序）"},
                        "limit": {"type": "number", "description": "返回条数，默认 20"},
                        "min_change": {"type": "number", "description": "涨跌幅下限 %"},
                        "max_change": {"type": "number", "description": "涨跌幅上限 %"},
                        "min_amount": {"type": "number", "description": "成交额下限（元）"},
                        "max_amount": {"type": "number", "description": "成交额上限（元）"},
                        "min_turnover": {"type": "number", "description": "换手率下限 %"},
                        "max_turnover": {"type": "number", "description": "换手率上限 %"},
                        "min_mcap": {"type": "number", "description": "总市值下限（元）"},
                        "max_mcap": {"type": "number", "description": "总市值上限（元）"},
                        "exclude_st": {"type": "boolean", "description": "排除 ST，默认 true"},
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="astock_flow",
                description="个股资金流：近 N 日主力/超大单/大单/中单/小单净流入（元）。用来看资金是否持续流入。",
                parameters={
                    "type": "object",
                    "properties": {
                        "code": code_prop,
                        "days": {"type": "number", "description": "取最近几天，默认 10"},
                    },
                    "required": ["code"],
                },
            ),
            ToolDefinition(
                name="astock_record",
                description=(
                    "录入一笔**已在自己券商成交**的买卖（本插件不下单、不连接券商）。"
                    "buy/sell + 成交价 + 数量 + 成交日期；自动算费用、更新移动加权成本、"
                    "校验 T+1 可卖与整手规则。dry_run=true 先预览校验结果不落账。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "side": {"type": "string", "enum": ["buy", "sell"], "description": "买入或卖出"},
                        "code": code_prop,
                        "price": {"type": "number", "description": "实际成交价（元）"},
                        "shares": {"type": "number", "description": "成交数量（股）"},
                        "trade_date": {"type": "string", "description": "成交日期 YYYY-MM-DD，默认今天"},
                        "name": {"type": "string", "description": "名称（可选，首次录入时便于展示）"},
                        "note": {"type": "string", "description": "备注（可选）"},
                        "fee": {"type": "number", "description": "实际费用合计（可选；不给则按设置费率自动算）"},
                        "dry_run": {"type": "boolean", "description": "true＝只预览校验，不写台账"},
                    },
                    "required": ["side", "code", "price", "shares"],
                },
            ),
            ToolDefinition(
                name="astock_portfolio",
                description=(
                    "台账总览：持仓（实时浮盈/占比/今日盈亏）+ 可用资金 + 已实现盈亏 + 累计费用。"
                    "action=trades 查流水；action=set_cash 设资金；action=delete_trade 删错录的流水"
                    "（会自动按剩余流水重算成本）；action=summary 只要汇总。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["list", "summary", "trades", "set_cash", "delete_trade"],
                                   "description": "默认 list"},
                        "cash": {"type": "number", "description": "action=set_cash 时的可用资金余额"},
                        "trade_id": {"type": "number", "description": "action=delete_trade 时的流水 id"},
                        "code": {"type": "string", "description": "action=trades 时按代码过滤"},
                        "limit": {"type": "number", "description": "action=trades 返回条数，默认 30"},
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="astock_hint",
                description=(
                    "**下单提示**（本插件的核心）：结合实时行情与台账，给出"
                    "① 该不该动手（止损/止盈/回撤/涨跌停/异动是否触发）；"
                    "② 动多少（买入股数按资金与单票上限估算、卖出股数/清仓或减半）；"
                    "③ 参考价格区间与费用；④ T+1 今日可卖多少；⑤ 附舆情背离提醒（研报vs两融、热度vs资金等）。"
                    "指定 code 只算该标的，留空则扫全部持仓。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "只算这一只（可选）"},
                        "buy": {"type": "boolean", "description": "同时给出买入数量估算（需配合 code）"},
                        "sell_ratio": {"type": "number", "description": "卖出比例，如 0.5 表示减半、1 清仓"},
                        "no_sentiment": {"type": "boolean",
                                         "description": "true＝跳过舆情背离速查（默认会查，慢约 1-3 秒）"},
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="astock_sentiment",
                description=(
                    "**投资舆情分析**（重要参考）：按七类证据归集——公司公告、机构研报、"
                    "相关新闻、市场热度、龙虎榜、融资融券、股东户数。每条标可靠度"
                    "（事实/观点/媒体/情绪）与影响方向，并**优先给出信号背离提示**"
                    "（如「研报偏多但融资余额下降」）。不合成情绪分、不预测涨跌。"
                    "可选返回行业背景与全市场快讯。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "code": code_prop,
                        "scope": {
                            "type": "array",
                            "items": {"type": "string",
                                      "enum": ["announce", "report", "news", "hot",
                                               "lhb", "margin", "holder", "feed"]},
                            "description": "只取指定类别（默认除 feed 外全部）；查行业政策时加 feed",
                        },
                        "days": {"type": "number",
                                 "description": "证据回溯天数，默认 7（研报固定至少看 6 个月）"},
                        "brief": {"type": "boolean",
                                  "description": "true＝只要一句话结论 + 背离，不要完整证据清单"},
                    },
                    "required": ["code"],
                },
            ),
            ToolDefinition(
                name="astock_watch",
                description="自选股管理：查看 / 添加 / 删除。自选股会被行情快照与盯梢源自动纳入。",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "add", "remove"], "description": "默认 list"},
                        "code": code_prop,
                        "name": {"type": "string", "description": "名称（可选）"},
                        "note": {"type": "string", "description": "备注（可选）"},
                    },
                    "required": [],
                },
            ),
        ]

    # -------------------------------------------------- 执行分发

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        handlers = {
            "astock_quote": self._tool_quote,
            "astock_kline": self._tool_kline,
            "astock_search": self._tool_search,
            "astock_screen": self._tool_screen,
            "astock_flow": self._tool_flow,
            "astock_record": self._tool_record,
            "astock_portfolio": self._tool_portfolio,
            "astock_hint": self._tool_hint,
            "astock_sentiment": self._tool_sentiment,
            "astock_watch": self._tool_watch,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"[Error] 未知工具 {name}"
        try:
            return await handler(args or {})
        except (LedgerError, data.DataError, ValueError) as exc:
            # 这三类都是「用户可修正」的错误：消息本身就是可行动提示
            return f"[Error] {exc}"
        except Exception as exc:     # 兜底：任何意外都返回可读信息而不是抛栈
            logger.exception("[astock] 工具 %s 执行失败", name)
            return (f"[Error] {name} 执行失败：{type(exc).__name__}: {exc}。"
                    f"若反复出现请检查台账文件路径与网络。")

    # -------------------------------------------------- 行情

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

    # -------------------------------------------------- 台账：录成交

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
        lines += ["", "提示：台账里的可用资金不会自动扣减（出入金不可见），"
                      "需要时用 astock_portfolio action=set_cash 手动校准。"]
        return "\n".join(lines)

    # -------------------------------------------------- 台账：总览

    async def _tool_portfolio(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "list")
        ledger_snapshot = await asyncio.to_thread(self._snapshot)
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
        return ledger_snapshot["text"]

    def _snapshot(self) -> Dict[str, Any]:
        """同步取台账 + 实时行情，返回 {'text':..., 'data':...}。"""
        ledger = self._get_ledger()
        positions = ledger.list_positions()
        cash = ledger.get_cash()
        realized = ledger.realized_pnl()
        fees_total = ledger.total_fee()

        quotes: Dict[str, Dict[str, Any]] = {}
        if positions:
            items = []
            for p in positions:
                try:
                    code, venue = mkt.normalize_code(p["code"])
                except ValueError:
                    continue
                items.append((code, venue, "stock"))
            if items:
                try:
                    for q in data.fetch_quotes(items):
                        quotes[q.code] = q.as_dict()
                except data.DataError as exc:
                    logger.warning("[astock] 持仓行情获取失败: %s", exc)

        rows = []
        total_mv = 0.0
        total_float_pnl = 0.0
        total_today_pnl = 0.0
        for p in positions:
            q = quotes.get(p["code"]) or {}
            price = q.get("price") or p["cost_price"]
            mv = price * p["shares"]
            total_mv += mv
            float_pnl = (price - p["cost_price"]) * p["shares"]
            total_float_pnl += float_pnl
            today_pnl = (q.get("change") or 0) * p["shares"]
            total_today_pnl += today_pnl
            pnl_pct = mkt.pct_change(price, p["cost_price"])
            rows.append([
                f"{p['name'] or '-'}({p['code']})", p["shares"],
                _f(p["cost_price"], 3), _f(price),
                f"{_f(float_pnl)} ({_pct(pnl_pct)})",
                f"{_f(today_pnl)}",
                _f(p["available_today"], 0), _money(mv),
            ])

        equity = total_mv + float(cash["balance"] or 0)
        lines = [
            "**持仓总览**" + ("" if rows else "（当前空仓）"),
            "",
        ]
        if rows:
            lines.append(_table(
                ["名称(代码)", "股数", "成本价", "现价", "浮动盈亏", "今日盈亏",
                 "今日可卖", "市值"], rows))
            lines.append("")
        lines.append(_table(["合计", "数值"], [
            ["持仓市值", f"{total_mv:,.2f} 元"],
            ["可用资金", f"{float(cash['balance'] or 0):,.2f} 元"],
            ["账户总市值", f"{equity:,.2f} 元"],
            ["浮动盈亏", f"{total_float_pnl:+,.2f} 元"],
            ["今日盈亏", f"{total_today_pnl:+,.2f} 元"],
            ["已实现盈亏（累计）", f"{realized['realized_pnl']:+,.2f} 元"],
            ["累计交易费用", f"{fees_total:,.2f} 元"],
            ["持仓数量", f"{len(positions)} 只"],
        ]))
        if not quotes and positions:
            lines += ["", "⚠️ 行情获取失败，上表现价用成本价代替，盈亏仅供参考。"]
        return {
            "text": "\n".join(lines),
            "data": {
                "positions": positions, "quotes": quotes,
                "total_market_value": total_mv, "equity": equity,
                "cash": float(cash["balance"] or 0),
                "float_pnl": round(total_float_pnl, 2),
                "today_pnl": round(total_today_pnl, 2),
                "realized_pnl": realized["realized_pnl"],
            },
        }

    # -------------------------------------------------- 下单提示

    async def _tool_hint(self, args: Dict[str, Any]) -> str:
        only = str(args.get("code") or "").strip()
        want_buy = bool(args.get("buy"))
        sell_ratio = args.get("sell_ratio")
        snap = await asyncio.to_thread(self._snapshot)
        cfg = self._hints_cfg()
        ledger = self._get_ledger()
        ctx = hints.market_context()

        codes: List[str] = []
        pos_by_code = {p["code"]: p for p in snap["data"]["positions"]}
        if only:
            code, _ = mkt.normalize_code(only)
            codes = [code]
        else:
            codes = [p["code"] for p in snap["data"]["positions"]]
            for w in await asyncio.to_thread(ledger.list_watch):
                if w["code"] not in codes:
                    codes.append(w["code"])
        if not codes:
            return ("台账里没有持仓或自选股，没有可提示的对象。"
                    "可以先用 astock_record 录入持仓，或 astock_watch 添加自选股。")

        quotes: Dict[str, Dict[str, Any]] = dict(snap["data"]["quotes"])
        missing = [c for c in codes if c not in quotes]
        if missing:
            try:
                fetched = await self._fetch_quotes(missing)
                for q in fetched:
                    quotes[q.code] = q.as_dict()
            except data.DataError as exc:
                return f"[Error] 行情获取失败，无法生成提示：{exc}"

        highest = await asyncio.to_thread(self._highest_prices, list(quotes))
        lines = [f"**下单提示**（{ctx['text']}）", ""]
        all_hints: List[hints.Hint] = []
        total_mv = snap["data"]["total_market_value"] or 0

        for code in codes:
            q = quotes.get(code)
            if not q:
                continue
            pos = pos_by_code.get(code)
            if pos:
                hs = hints.build_position_hints(pos, q, cfg,
                                               highest_price=highest.get(code),
                                               total_market_value=total_mv)
                t1 = hints.build_t1_hint(pos, int(pos.get("available_today") or 0))
                if t1:
                    hs.append(t1)
                all_hints.extend(hs)
                if hs:
                    lines.append(f"**{pos.get('name') or code}({code})**")
                    for h in hs:
                        lines.append(f"- {h.title}｜{h.suggestion}（依据：{h.basis}）")
                    lines.append("")
                else:
                    pnl = mkt.pct_change(q.get("price") or 0, pos["cost_price"])
                    lines.append(f"**{pos.get('name') or code}({code})**："
                                 f"现价 {_f(q.get('price'))}，浮盈 {_pct(pnl)}，"
                                 f"未触发任何规则区间（止损 -{cfg.stop_loss_pct}% / "
                                 f"止盈 +{cfg.take_profit_pct}%）。")
                    lines.append("")
            else:
                # 只盯不持有：watch_only 只产出与仓位无关的信号（涨跌停/异动/放量）
                hs = hints.build_position_hints({"code": code}, q, cfg,
                                                watch_only=True)
                if hs:
                    lines.append(f"**{q.get('name') or code}({code})**（自选）")
                    for h in hs:
                        lines.append(f"- {h.title}｜{h.suggestion}")
                    lines.append("")
            # 卖出数量估算（有持仓时）
            if pos and sell_ratio is not None:
                try:
                    ratio = float(sell_ratio)
                except (TypeError, ValueError):
                    ratio = 1.0
                est = hints.suggest_sell(pos, q.get("price") or 0,
                                        int(pos.get("available_today") or 0),
                                        ratio=ratio, fee_cfg=ledger.fee)
                if est.get("suggest_shares"):
                    lines.append(_table(
                        ["卖出估算", "数值"], [
                            ["建议卖出", f"{est['suggest_shares']} 股（剩余 {est['remain_shares']}）"],
                            ["预计金额", f"{est['suggest_amount']:,.2f} 元"],
                            ["预计费用", f"{est['est_fee']:.2f} 元"],
                            ["预计净收入", f"{est['est_net_proceeds']:,.2f} 元"],
                            ["预计已实现盈亏", f"{est['est_realized_pnl']:+,.2f} 元"],
                        ]))
                    lines.append("")
            # 买入数量估算
            if want_buy and q.get("price"):
                pos_shares = int(pos["shares"]) if pos else 0
                est = hints.suggest_buy(
                    code, q.get("name") or code, q["price"],
                    cash=snap["data"]["cash"] + 0.0,
                    cfg=cfg, existing_shares=pos_shares,
                    existing_cost=float(pos["cost_price"]) if pos else 0.0,
                    total_market_value=total_mv, fee_cfg=ledger.fee)
                lines.append(_table(
                    ["买入估算", "数值"], [
                        ["参考价", f"{est['price']:.2f}"],
                        ["建议股数", f"{est['suggest_shares']} 股（{est['reason']}）"],
                        ["预计金额", f"{est['suggest_amount']:,.2f} 元"],
                        ["预计费用", f"{est['est_fee']:.2f} 元"],
                        ["预计总成本", f"{est['est_total_cost']:,.2f} 元"],
                        ["买入后成本价", _f(est.get("avg_cost_after"), 3)],
                        ["涨停 / 跌停价", f"{_f(est['limit_up'])} / {_f(est['limit_down'])}"],
                    ]))
                lines.append(f"注：{est['note']}")
                lines.append("")

        if not all_hints and not only:
            lines.insert(2, "本次扫描未触发任何规则信号（止损/止盈/回撤/涨跌停/异动）。\n")
        # 舆情背离：只在"有持仓或指定标的"且用户没关时才查（省接口与时间）
        if not args.get("no_sentiment"):
            flag_codes = [c for c in codes if c in pos_by_code] or codes[:1]
            flags = await self._sentiment_flags(flag_codes)
            if flags:
                lines.append("")
                lines.append("### 舆情参考（背离信号优先）")
                lines += flags
                lines.append("")
        lines.append(hints.DISCLAIMER)
        return "\n".join(lines)

    async def _sentiment_flags(self, codes: List[str],
                              limit: int = 3) -> List[str]:
        """给下单提示挂一段舆情背离摘要。

        只取**背离**（高/中优先级），不铺完整证据清单——下单那一刻用户需要的是
        "有没有反方向的证据",而不是读完七类公告。取不到就静默降级（行情本身
        仍可用），但会在结果里说"舆情未取到"，避免用户以为查过没问题。
        """
        if not codes:
            return []
        out: List[str] = []
        failed: List[str] = []
        for code in codes[:limit]:
            try:
                name = ""
                try:
                    q = data.fetch_quotes(
                        [(code, mkt.normalize_code(code)[1], "stock")])
                    if q:
                        name = q[0].name
                except data.DataError:
                    pass
                collected = senti_data.collect(code, name)
                report = senti.summarize(collected["items"], code, name,
                                        collected["errors"], collected["extras"])
            except Exception:
                logger.warning("[astock] 舆情速查失败 %s", code, exc_info=True)
                failed.append(code)
                continue
            if not report.conflicts:
                continue
            top = [c for c in report.conflicts if c.severity in ("high", "medium")][:2]
            if not top:
                continue
            out.append(f"**{name or code}({code}) 舆情背离**")
            for c in top:
                mark = "🔴" if c.severity == "high" else "🟠"
                out.append(f"- {mark} {c.title}：{c.detail}")
                if c.advice:
                    out.append(f"  → {c.advice}")
        if failed:
            out.append("_（" + "、".join(failed) +
                       " 舆情未取到，结论仅基于行情与规则）_")
        return out

    def _highest_prices(self, codes: List[str]) -> Dict[str, float]:
        """取持仓期最高价（用近 60 日最高价近似——台账记录开仓日期后
        可精确到开仓日，这里先用区间最高价，避免为每只股打一次接口）。"""
        out: Dict[str, float] = {}
        for code in codes[:20]:
            try:
                bars = data.fetch_klines(code, "day", limit=60, adjust="qfq")
                if bars:
                    out[code] = max(b.high for b in bars)
            except data.DataError:
                continue
        return out

    # -------------------------------------------------- 舆情

    async def _tool_sentiment(self, args: Dict[str, Any]) -> str:
        code_raw = str(args.get("code") or "").strip()
        if not code_raw:
            return ("[Error] 需要 code。舆情按标的维度分析——"
                    "要查行业/宏观事件请传 scope 含 feed 并给出可关联的标的代码。")
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

        report = senti.summarize(collected["items"], code, name,
                                collected["errors"], collected["extras"])
        if args.get("brief"):
            line = senti_tool.render_brief(report)
            conflicts = senti_tool.render_conflict_lines(report)
            out = [line]
            if conflicts:
                out.append("")
                out.append("**信号背离**：")
                out += [f"- {c}" for c in conflicts]
            if collected["errors"]:
                out += ["", "数据缺口：" + "；".join(
                    f"{k}={v}" for k, v in collected["errors"].items())]
            return "\n".join(out)
        return senti_tool.render(report, collected["items"], collected["extras"])

    # -------------------------------------------------- 自选股

    async def _tool_watch(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "list")
        ledger = self._get_ledger()
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

    # -------------------------------------------------- 右栏面板

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """右栏「持仓」面板。任何异常都降级为提示，不影响主流程。

        两种形态由设置项 `panel_style` 切换（宿主只支持 Markdown 或 lite-tree
        决策树二选一，插件无法自定义 HTML）：
        - `markdown`（默认）：紧凑表格，横向数值对齐好，适合盘后核对；
        - `tree`：一只股一张卡片，卡内直接写"触发规则 + 建议动作"，按严重度
          着色，适合盘中扫一眼。
        形态是偏好问题而非对错问题，所以做成开关而不是替你决定。
        """
        if panel_id != "portfolio":
            return ""
        try:
            snap = self._snapshot()
        except Exception as exc:
            logger.warning("[astock] 面板渲染失败: %s", exc)
            return f"台账读取失败：{exc}\n\n检查设置页「台账文件路径」，或先用 `astock_record` 录入持仓。"
        d = snap["data"]
        style = str(_cfg(self._app, "panel_style", "markdown") or "markdown").lower()
        flag = self._panel_sentiment_flag(d)
        if style == "tree":
            return self._panel_tree(d, flag)
        return self._panel_markdown(d, flag)

    # -------------------------------------------------- 面板渲染（两种形态）

    def _panel_head(self, d: Dict[str, Any]) -> str:
        return (f"**{len(d['positions'])} 只持仓**｜市值 {_money(d['total_market_value'])}｜"
                f"浮盈 {_f(d['float_pnl'])}（今日 {_f(d['today_pnl'])}）｜"
                f"可用 {_money(d['cash'])}")

    def _panel_sentiment_flag(self, d: Dict[str, Any]) -> str:
        """首要持仓的舆情背离速查。

        面板每 5 秒刷新 → 必须限流：只查 1 只、只取 4 个便宜类别（不查新闻/公告，
        那两类请求重且变化慢），失败静默（面板不能因为舆情接口挂了而空白）。
        """
        if not d["positions"]:
            return ""
        try:
            top = max(d["positions"], key=lambda p: p["shares"] *
                      ((d["quotes"].get(p["code"]) or {}).get("price")
                       or p["cost_price"]))
            collected = senti_data.collect(top["code"], top.get("name") or "",
                                          scope=("margin", "holder", "lhb", "report"))
            rep = senti.summarize(collected["items"], top["code"],
                                 top.get("name") or "",
                                 collected["errors"], collected["extras"])
            if rep.conflicts:
                c = rep.conflicts[0]
                mark = "🔴" if c.severity == "high" else "🟠"
                return f"{mark} **舆情背离**：{c.title}"
            if rep.tone != "thin":
                return f"⚪ 舆情：{senti_tool.render_brief(rep)}"
        except Exception:
            logger.debug("[astock] 面板舆情速查失败", exc_info=True)
        return ""

    def _panel_markdown(self, d: Dict[str, Any], flag: str) -> str:
        head = self._panel_head(d)
        if not d["positions"]:
            return head + ("\n\n当前空仓。录入成交："
                           "`astock_record side=buy code=... price=... shares=...`")
        rows = []
        for p in d["positions"][:MAX_ROWS]:
            q = d["quotes"].get(p["code"]) or {}
            price = q.get("price") or p["cost_price"]
            mv = price * p["shares"]
            pnl = (price - p["cost_price"]) * p["shares"]
            rows.append([f"{p['name'] or p['code']}", p["shares"],
                         _f(p["cost_price"], 3), _f(price),
                         f"{_f(pnl)}（{_pct(mkt.pct_change(price, p['cost_price']))}）",
                         _money(mv)])
        body = _table(["名称", "股数", "成本", "现价", "浮盈", "市值"], rows)
        return head + "\n\n" + body + (f"\n\n{flag}" if flag else "")

    def _panel_tree(self, d: Dict[str, Any], flag: str) -> str:
        """lite-tree 决策树形态：一只股一张卡，卡内写触发规则与建议动作。

        `trees[].options` 在原协议里表示"多个候选方案及其概率"。持仓提示不是
        概率选择，因此每卡只放**一条** option（=当前建议动作），prob 恒为 1.0、
        chosen=true；`kind` 承载信号类型，`question` 承载规则标题，`tone` 承载
        严重度——这样既复用现成渲染，又不伪造概率语义。
        """
        import json as _json
        positions = d["positions"][:MAX_ROWS]
        if not positions:
            return ("**0 只持仓**｜当前空仓\n\n"
                    "录入成交：`astock_record side=buy code=... price=... shares=...`")
        cfg = self._hints_cfg()
        total_mv = d["total_market_value"] or 0
        cards: List[Dict[str, Any]] = []
        for p in positions:
            q = d["quotes"].get(p["code"]) or {}
            price = q.get("price") or p["cost_price"]
            pnl = (price - p["cost_price"]) * p["shares"]
            pct = mkt.pct_change(price, p["cost_price"])
            hs = hints.build_position_hints(p, q, cfg, total_market_value=total_mv)
            t1 = hints.build_t1_hint(p, int(p.get("available_today") or 0))
            if t1:
                hs.append(t1)
            # 只展示最要紧的一条：卡片是"扫一眼"场景，堆 4 条反而看不出重点
            top_h = max(hs, key=lambda h: {"urgent": 2, "action": 1}.get(h.severity, 0)) if hs else None
            if top_h:
                tone = {"urgent": "danger", "action": "warn"}.get(top_h.severity, "neutral")
                kind, question, label = top_h.kind, top_h.title, top_h.suggestion
            else:
                tone = "neutral"
                kind = "holding"
                question = "未触发规则区间"
                label = f"继续持有（止损 -{cfg.stop_loss_pct}% / 止盈 +{cfg.take_profit_pct}%）"
            cards.append({
                "title": f"{p['name'] or p['code']}  {_f(price)}  {_pct(pct)}",
                "context": (f"{p['shares']}股 · 成本{_f(p['cost_price'], 3)}"
                            f" · 浮盈{_f(pnl)} · 可卖{int(p.get('available_today') or 0)}"
                            f" · 市值{_money(p['shares'] * price)}"),
                "kind": kind,
                "question": question,
                "options": [{"label": (label or "")[:80], "prob": 1.0,
                             "tone": tone, "chosen": True}],
                "confidence": 1.0,
                "accepted": True,
            })
        payload = {
            "type": "lite-tree",
            "status": {
                "text": f"{len(positions)} 只持仓",
                "tone": "ok" if (d["float_pnl"] or 0) >= 0 else "warn",
                "meta": [f"市值 {_money(d['total_market_value'])}",
                         f"浮盈 {_f(d['float_pnl'])}",
                         f"今日 {_f(d['today_pnl'])}",
                         f"可用 {_money(d['cash'])}"],
                "chips": ([flag.split("：")[0].replace("*", "").strip()]
                          if flag else []) + ["每 5 秒刷新"],
            },
            "trees": cards,
            "total": len(cards),
        }
        return _json.dumps(payload, ensure_ascii=False)
