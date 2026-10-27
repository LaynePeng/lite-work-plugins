"""精确价位规则引擎：按**写死的价位**判定信号（v2.1.4 新增）。

────────────────────────────────────────────────────────────────────────
为什么需要它（并入 ruthless-watch 的原因）
────────────────────────────────────────────────────────────────────────
此前 astock 的盯盘只认**百分比阈值**：浮亏 -8%、当日 ±5%、量比 >2。
但真实盯盘常需要"**跌破前低 36.01 清仓**""**放量站回 37.11**"这类
**具体技术位**——百分比表达不出来，因为不知道该以哪个价作基准。

这些规则原先由独立插件 `ruthless-watch`（source=ruthless）承担。合并进来
的理由：两者做的事高度重叠，独立后所有基础能力都得重做一遍，且实测付出
了代价——它只继承 `WatchSource` 不继承 `Plugin`，导致**盯梢调度能用、
插件管理页却完全不显示**（用户以为没装上）。

合并后额外获得的（独立插件拿不到的）：
  · 邮件通知（探针发现信号时顺带发信，见 astock_notify）
  · 台账上下文（持仓成本 / 浮亏多少 / 今日可卖数）
  · 行情层的限速、缓存、主备源切换（不必再自己写 urllib）
  · 统一的去重/限流

────────────────────────────────────────────────────────────────────────
配置形态（计划编辑器 probe_config）
────────────────────────────────────────────────────────────────────────
    rules: [
      {"code": "000651", "name": "格力电器", "kind": "stop_loss",
       "price": 36.00, "op": "lt", "label": "跌破前低 36.00 清仓",
       "severity": "urgent"},
      {"code": "000651", "kind": "rebound", "price": 37.11, "op": "gt",
       "vol_ratio_gt": 1.2, "label": "放量站回 37.11"},
    ]
    op: lt 跌破 / gt 站上 / le / ge
    vol_ratio_lt / vol_ratio_gt: 可选量能附加条件（缩量 / 放量）
    once_per_day: 同一信号当天只报一次（默认 true）

**为什么 once_per_day 默认 true**：价位条件是**持续满足**的——跌破 36.00
之后只要不涨回去，每 5 分钟探测都会命中。不按天去重就是每小时 12 封邮件
（正好撞上 Notifier 的限流上限，然后一整天都不再报新的信号）。
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("litework.astock.price_rule")

#: 支持的比较方向
_OPS = {
    "lt": lambda price, target: price < target,
    "le": lambda price, target: price <= target,
    "gt": lambda price, target: price > target,
    "ge": lambda price, target: price >= target,
}

#: 合法信号类型（用于校验与展示；未知类型不拒，按 label 处理）
KNOWN_KINDS = ("stop_loss", "rebound", "watch", "invalid", "take_profit")


def parse_rules(raw: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """宽容解析 rules 配置。返回 (有效规则, 错误说明)。

    **为什么宽容**：probe_config 是用户在计划编辑器里手写的 JSON，写错一个
    字段不该让整个盯盘探针报错停摆（那会连其他持仓的信号一起丢掉）。
    非法条目被跳过并在返回值里说明，探针把它们放进 summary 让人看见。
    """
    errors: List[str] = []
    out: List[Dict[str, Any]] = []
    if raw in (None, "", []):
        return out, errors
    if isinstance(raw, dict):        # 容忍单条写成对象
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        errors.append(f"rules 应为数组，实际是 {type(raw).__name__}")
        return out, errors
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"第 {i+1} 条不是对象，已跳过")
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            errors.append(f"第 {i+1} 条缺 code，已跳过")
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            errors.append(f"第 {i+1} 条 price 非法，已跳过")
            continue
        op = str(item.get("op") or "lt").strip().lower()
        if op not in _OPS:
            errors.append(f"第 {i+1} 条 op={op!r} 不支持（lt/le/gt/ge），按 lt 处理")
            op = "lt"
        rule = dict(item)
        rule["code"] = code
        rule["price"] = price
        rule["op"] = op
        out.append(rule)
    return out, errors


def match_rule(rule: Dict[str, Any], quote: Dict[str, Any]
               ) -> Optional[Dict[str, Any]]:
    """判定单条规则是否触发。触发返回信号 dict，否则 None。

    纯函数（不查行情、不读时钟），便于离线单测覆盖各分支。

    返回字段与 `astock_hints.Hint` 对齐（kind/code/name/severity/title/
    detail/suggestion），这样探针可以直接把它塞进既有的邮件渲染与去重链路，
    不必为精确价位另开一条通路。
    """
    price = quote.get("price")
    if price is None:
        return None
    try:
        price = float(price)
        target = float(rule.get("price"))
    except (TypeError, ValueError):
        return None

    if not _OPS[rule.get("op") or "lt"](price, target):
        return None

    # 量能附加条件：任一不满足即视为未触发（"放量站上"必须真的放量）
    vr = quote.get("volume_ratio")
    cond = rule.get("vol_ratio_lt")
    if cond is not None and vr is not None:
        try:
            if not (float(vr) < float(cond)):
                return None
        except (TypeError, ValueError):
            pass
    cond = rule.get("vol_ratio_gt")
    if cond is not None and vr is not None:
        try:
            if not (float(vr) > float(cond)):
                return None
        except (TypeError, ValueError):
            pass

    code = rule.get("code") or quote.get("code") or ""
    name = str(rule.get("name") or quote.get("name") or code)
    kind = str(rule.get("kind") or "watch")
    label = str(rule.get("label") or "")
    severity = str(rule.get("severity") or "action").lower()
    if severity not in ("info", "action", "urgent"):
        severity = "action"

    chg = quote.get("change_pct")
    chg_txt = f"｜{chg:+.2f}%" if chg is not None else ""
    vr_txt = f"｜量比 {float(vr):.2f}" if vr is not None else ""
    arrow = {"lt": "跌破", "le": "触及", "gt": "站上", "ge": "站上"}[
        rule.get("op") or "lt"]
    title = label or f"{name} {arrow} {target:g}"
    return {
        "kind": kind,
        "code": code,
        "name": name,
        "severity": severity,
        # 标题带上现价与量能：邮件里只给"跌破 36.00"没有信息量，
        # 用户要的是"跌到多少了、量能如何"
        "title": f"{title}（现价 {price:.2f}{chg_txt}{vr_txt}）",
        "detail": (f"现价 {price:.2f}，触发条件：{arrow} {target:g}"
                   + (f"，量比 {float(vr):.2f}" if vr is not None else "")),
        "suggestion": _suggest(kind, severity),
        "target": target,
        "op": rule.get("op") or "lt",
    }


def _suggest(kind: str, severity: str) -> str:
    """按信号类型给一句"该做什么"。不预测，只提醒既定纪律。"""
    return {
        "stop_loss": "按计划执行止损；若不认可该止损位，需重新设定并记录理由。",
        "rebound": "站上关键位可视为回稳信号；确认量能是否配合，别一次性重仓。",
        "take_profit": "按计划分批止盈，或上移止盈线继续持有。",
        "invalid": "观察前提已失效，考虑换标的或重新设定条件。",
    }.get(kind, "信号已触发，按你设定的计划处理；本条只是提醒，不构成建议。")


class PriceRuleState:
    """跨探测去重状态（`once_per_day`）。

    挂在盯梢源实例上（长期存活）：探针对象在宿主侧是单例，状态才能跨 5 分钟
    一次的调用累积。真机上若宿主重建实例，最坏结果是当天多报一次。
    """

    def __init__(self) -> None:
        self._fired: Dict[str, str] = {}     # "code:kind:price" → "YYYY-MM-DD"
        self._day: str = ""
        self._hour: str = ""

    def should_fire(self, sig: Dict[str, Any], now: Optional[dt.datetime] = None,
                    once_per_day: bool = True) -> bool:
        now = now or dt.datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self._day != today:               # 跨日重置
            self._fired.clear()
            self._day = today
        if not once_per_day:
            return True
        key = f"{sig.get('code')}:{sig.get('kind')}:{sig.get('target')}"
        if self._fired.get(key) == today:
            return False
        return True

    def mark(self, sig: Dict[str, Any], now: Optional[dt.datetime] = None) -> None:
        now = now or dt.datetime.now()
        key = f"{sig.get('code')}:{sig.get('kind')}:{sig.get('target')}"
        self._fired[key] = now.strftime("%Y-%m-%d")

    def reset(self) -> None:
        self._fired.clear()


def evaluate(rules: List[Dict[str, Any]],
             quotes: Dict[str, Dict[str, Any]],
             state: Optional[PriceRuleState] = None,
             once_per_day: bool = True,
             now: Optional[dt.datetime] = None,
             ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """对一批规则跑一轮判定。返回 (触发的信号, snapshot 标签)。

    `snapshot` 只放**信号标签**（如 `000651:stop_loss:36.0`），不放价格——
    宿主按 snapshot 的 sha256 判断"有无变化"，把价格放进去等于每次探测都
    判为变化，每 5 分钟白烧一次 LLM。
    """
    fired: List[Dict[str, Any]] = []
    tags: List[str] = []
    state = state or PriceRuleState()
    for rule in rules:
        code = str(rule.get("code") or "")
        quote = quotes.get(code) or {}
        if not quote:
            continue
        sig = match_rule(rule, quote)
        if sig is None:
            continue
        if not state.should_fire(sig, now=now, once_per_day=once_per_day):
            continue
        state.mark(sig, now=now)
        fired.append(sig)
        tags.append(f"{sig['code']}:{sig['kind']}:{sig['target']:g}")
    return fired, tags
