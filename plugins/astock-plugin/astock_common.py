"""插件公共基础：配置读取、格式化、Markdown 表格。

**为什么单独一层**：这些是最底层、被所有模块依赖的纯函数（无网络、无状态）。
拆出来之后，`plugin.py`（装配层）与 `astock_panel.py`（面板渲染）都能引用，
避免互相 import 形成环。

命名约定与同目录其它模块一致：裸 import + `astock_` 前缀（宿主把插件目录
插入 sys.path，见 AGENTS.md「多文件插件形态」）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, List

import astock_hints as hints
import astock_market as mkt


# ---------------------------------------------------------------- 配置读取


#: 本插件在 app.config 里的命名空间（设置页经通用插件 UI 协议写入）
PLUGIN_NAME = "astock-plugin"


def _cfg(app: Any, key: str, default: Any = None) -> Any:
    """读 `app.config["astock-plugin"][key]`。

    **空串按"未配置"处理**（返回默认值）：设置页的文本框清空后存的是 `""`，
    若原样返回会让 `float("")` 抛错或让布尔判断走偏。
    """
    if app is None:
        return default
    try:
        cfg = getattr(app, "config", None) or {}
        own = cfg.get(PLUGIN_NAME) if isinstance(cfg, dict) else None
        if not isinstance(own, dict):
            return default
        value = own.get(key, default)
        return default if value is None or value == "" else value
    except Exception:
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """语义化布尔解析。

    `bool("false") == True` —— 配置文件里存的是字符串时必须按语义判断，
    否则用户在 config.json 里写 `"enable_add": "false"` 会变成开启。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    # 只认明确的正值；其余（含 "no"/"停用" 等）一律 False，与插件原语义一致。
    # 不认识的字符串交给 default 会让"写了个错词"静默变成默认值。
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def _num(app: Any, key: str, default: float) -> float:
    """读数字配置（非法值退回默认，绝不让配置错误炸掉工具）。"""
    try:
        return float(_cfg(app, key, default))
    except (TypeError, ValueError):
        return default


def _bool(app: Any, key: str, default: bool) -> bool:
    return _as_bool(_cfg(app, key, default), default)


def fee_config(app: Any) -> mkt.FeeConfig:
    """费用口径（按券商实际费率填）。

    **五个字段都要带上**：印花税（卖出单边 0.05%）与过户费（双向 0.001%）
    不是小数目，漏读会让"预计净收入"系统性偏高——用户对账时会发现差几块钱，
    然后不再相信整套账。
    """
    base = mkt.FeeConfig()
    return mkt.FeeConfig(
        commission_rate=_num(app, "commission_rate", base.commission_rate),
        min_commission=_num(app, "min_commission", base.min_commission),
        stamp_tax_rate=_num(app, "stamp_tax_rate", base.stamp_tax_rate),
        transfer_fee_rate=_num(app, "transfer_fee_rate", base.transfer_fee_rate),
        other_rate=_num(app, "other_rate", base.other_rate),
    )


def hints_config(app: Any) -> hints.HintsConfig:
    """提示阈值配置：默认值全在 HintsConfig 里，这里只覆盖设置页填过的。"""
    conf = hints.HintsConfig()
    for key in conf.__dataclass_fields__:
        raw = _cfg(app, key, None)
        if raw is None:
            continue
        if isinstance(getattr(conf, key), bool):
            setattr(conf, key, _as_bool(raw))
        else:
            try:
                setattr(conf, key, float(raw))
            except (TypeError, ValueError):
                pass
    return conf


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


def _span_years(first: str, last: str) -> float:
    """两个 YYYY-MM-DD 之间相差多少年（用于"存档覆盖多久"的展示）。"""
    try:
        a = dt.date.fromisoformat(str(first)[:10])
        b = dt.date.fromisoformat(str(last)[:10])
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (b - a).days / 365.25)


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
