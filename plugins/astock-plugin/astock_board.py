"""聊天内持仓看板渲染：一票一卡特异的 `board` 富围栏。

**为什么用 board 而不是 cards**（这是与用户一起定下的版式，别改回去）：

- `cards` 的渲染层**不支持按严重度着色**（CardItem 无 tone 字段，卡片边框固定），
  而"扫一眼就知道该动哪只"恰恰依赖视觉分级；
- `board` 的 `BoardCard` 支持 `tone: good|bad|neutral`，渲染成彩色左边框，
  天然表达紧急度；同时它的列（column）正好承载"需处理 / 持有中 / 无需关注"
  的分组语义。

**分栏规则（按提示的严重度，而不是主观判断）**：
    需处理   ← urgent / action 级信号（止损、止盈、跌停、超仓位上限、T+1 锁死）
    持有中   ← 无信号但有浮盈/浮亏在阈值内（正常持有）
    无需关注 ← 自选股（无持仓）且未触发任何信号

**为什么 desc 要写得这么"啰嗦"**：board 的 desc 是 10.5px 灰色小字，读取成本高，
但它同时是卡片里唯一能放"建议动作"的位置。因此采用
`股数·成本·浮盈·可卖` + `换行` + `建议动作` 的两行结构：第一行是核对数据，
第二行是要不要动手——而不是把两件事糅在一句里让人找。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from astock_market import pct_change

#: 分栏定义（顺序即展示顺序）
COLUMN_URGENT = "需处理"
COLUMN_HOLDING = "持有中"
COLUMN_IDLE = "无需关注"

#: 触发"需处理"的信号类型
ACTION_KINDS = frozenset({
    "stop_loss", "take_profit", "limit_down", "limit_up", "trailing",
    "position_cap", "add_position", "reduce", "event", "t1_locked",
})


def _f(v: Any, digits: int = 2, dash: str = "-") -> str:
    if v is None or v == "":
        return dash
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _money(v: Any) -> str:
    if v is None or v == "":
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e8:
        return f"{sign}{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{sign}{x / 1e4:.2f}万"
    return f"{sign}{x:.2f}"


#: 风险类信号（该减风险）→ 红色
RISK_KINDS = frozenset({"stop_loss", "limit_down", "position_cap", "t1_locked"})
#: 机会类信号（可考虑动作，非危险）→ 绿色
OPPORTUNITY_KINDS = frozenset({"take_profit", "trailing", "limit_up",
                               "reduce", "add_position"})


def _tone_of(severity: str, kind: str = "") -> str:
    """信号 → 看板左边框色。

    **宿主只支持 good/bad/neutral 三色**（绿/红/灰，没有黄色）——所以不能用
    "红黄橙"三级。改为按**语义**着色，比按严重度着色更不容易误读：

        红 bad     ＝ 风险（该降风险：止损、跌停、超仓位上限、T+1 锁死）
        绿 good    ＝ 机会（可考虑动作但不危险：止盈、回撤止盈、涨停、减仓）
        灰 neutral ＝ 无信号 / 纯事件

    为什么不按"urgent 红、action 灰"：实测那样所有卡片都会是灰的，
    卡片之间毫无区别，"扫一眼就知道该动哪只"就失效了。
    """
    if kind in RISK_KINDS:
        return "bad"
    if kind in OPPORTUNITY_KINDS:
        return "good"
    if severity == "urgent":
        return "bad"
    return "neutral"


_KIND_SHORT = {
    "stop_loss": "止损",
    "take_profit": "止盈",
    "trailing": "回撤止盈",
    "limit_up": "涨停",
    "limit_down": "跌停",
    "position_cap": "超仓位上限",
    "t1_locked": "T+1 锁定",
    "add_position": "加仓区间",
    "reduce": "减仓区间",
    "event": "异动",
}


def _kind_label(kind: str, hint: Any = None) -> str:
    """信号类型 → 卡片角标（≤20 字）。

    角标只有一个位置且限 20 字，所以**不塞整句标题**（"寒武纪 单票占比 88.3%
    超上限" 会被硬截成半句）。优先用短标签；能拿到关键数字时才补一个。
    """
    base = _KIND_SHORT.get(kind) or (getattr(hint, "title", "") or kind)
    meta = getattr(hint, "meta", None) or {}
    if kind == "stop_loss" and meta.get("pnl_pct") is not None:
        return f"止损 {meta['pnl_pct']:+.0f}%"
    if kind == "take_profit" and meta.get("pnl_pct") is not None:
        return f"止盈 {meta['pnl_pct']:+.0f}%"
    if kind == "position_cap" and meta.get("weight_pct") is not None:
        return f"占仓 {meta['weight_pct']:.0f}% 超限"
    if kind == "trailing" and meta.get("drawdown_pct") is not None:
        return f"回撤 {meta['drawdown_pct']:.0f}%"
    if kind == "t1_locked":
        return "T+1 锁定"
    return base


def _clip(text: str, limit: int) -> str:
    """按宿主解析器字段上限截断（board: title 80 / desc 200 / tag 20）。

    不主动截的话前端会静默硬截，可能断在半句话上；这里加省略号，
    至少让人知道后面还有内容。
    """
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(1, limit - 1)] + "…"


def build_board(data: Dict[str, Any], hints_cfg: Any,
                hint_builder: Any, t1_builder: Any,
                watch_codes: Sequence[str] = (),
                max_per_column: int = 12) -> Optional[Dict[str, Any]]:
    """持仓数据 → board 规格（给前端渲染）。

    参数中 `hint_builder` / `t1_builder` 由调用方注入（plugin 的 hints 模块函数），
    本模块不 import plugin，避免循环依赖，也让单测可以传假实现。

    返回 None 表示没有任何可展示内容（调用方回退为文字说明）。
    """
    positions = list(data.get("positions") or [])
    quotes = data.get("quotes") or {}
    total_mv = float(data.get("total_market_value") or 0)

    buckets: Dict[str, List[Dict[str, Any]]] = {
        COLUMN_URGENT: [], COLUMN_HOLDING: [], COLUMN_IDLE: []}

    # ---------- 有持仓的
    for p in positions[:40]:
        code = p.get("code") or ""
        q = quotes.get(code) or {}
        price = q.get("price") or p.get("cost_price") or 0
        shares = int(p.get("shares") or 0)
        cost = float(p.get("cost_price") or 0)
        pnl = (price - cost) * shares
        pct = pct_change(price, cost)
        hs = hint_builder(p, q, hints_cfg, total_market_value=total_mv)
        t1 = t1_builder(p, int(p.get("available_today") or 0))
        if t1:
            hs.append(t1)
        hs = [h for h in hs if h.kind in ACTION_KINDS]

        head = (f"{_f(price)}　{_pct(pct)}　｜　{shares} 股 · 成本 "
                f"{_f(cost, 3)} · 浮盈 {_f(pnl)} · 可卖 "
                f"{int(p.get('available_today') or 0)}")
        if hs:
            top = max(hs, key=lambda h: {"urgent": 2, "action": 1}.get(h.severity, 0))
            card = {
                "title": _clip(f"{p.get('name') or code}（{code}）", 80),
                # desc 上限 200：首行核对数据 + 次行建议动作，超了会断句
                "desc": _clip(head + "\n→ " + (top.suggestion or top.title or ""), 200),
                "tag": _clip(_kind_label(top.kind, top), 20),
                "tone": _tone_of(top.severity, top.kind),
            }
            buckets[COLUMN_URGENT].append(card)
        else:
            # tag 只有一个位置：放"仓位占比"（组合层面最有用的风险指标）。
            # 收益率已在 desc 首行，重复写会浪费 tag
            # （踩过：tag 写成 "占仓 3.18" 其实是收益率 3.18%，语义完全错）。
            weight = (shares * price / total_mv * 100) if total_mv > 0 else None
            buckets[COLUMN_HOLDING].append({
                "title": _clip(f"{p.get('name') or code}（{code}）", 80),
                "desc": _clip(head + f"\n→ 未触发规则区间（止损 "
                                    f"-{hints_cfg.stop_loss_pct:g}% / 止盈 "
                                    f"+{hints_cfg.take_profit_pct:g}%）", 200),
                "tag": _clip(f"占仓 {weight:.1f}%" if weight is not None else "持有", 20),
                "tone": "neutral",
            })

    # ---------- 只有自选、无持仓的
    held = {p.get("code") for p in positions}
    for code in watch_codes:
        if code in held:
            continue
        q = quotes.get(code) or {}
        if not q:
            buckets[COLUMN_IDLE].append({
                "title": code, "desc": "（行情未取到）", "tag": "自选",
                "tone": "neutral"})
            continue
        hs = [h for h in hint_builder({"code": code}, q, hints_cfg, watch_only=True)
              if h.kind in ACTION_KINDS]
        head = f"{_f(q.get('price'))}　{_pct(q.get('change_pct'))}　｜　自选（未持仓）"
        if hs:
            top = hs[0]
            buckets[COLUMN_URGENT].append({
                "title": f"{q.get('name') or code}（{code}）",
                "desc": head + "\n→ " + (top.suggestion or top.title or ""),
                "tag": _clip(_kind_label(top.kind, top), 20),
                "tone": _tone_of(top.severity, top.kind)})
        else:
            buckets[COLUMN_IDLE].append({
                "title": _clip(f"{q.get('name') or code}（{code}）", 80),
                "desc": _clip(head, 200), "tag": "无异动", "tone": "neutral"})

    for key in buckets:
        buckets[key] = buckets[key][:max_per_column]

    if not any(buckets.values()):
        return None
    return {
        "title": (f"持仓看板 · {len(positions)} 只持仓"
                  f"｜市值 {_money(total_mv)}"
                  f"｜浮盈 {_f(data.get('float_pnl'))}"
                  f"（今日 {_f(data.get('today_pnl'))}）"
                  f"｜可用 {_money(data.get('cash'))}"),
        "columns": [{"name": name, "cards": buckets[name]} for name in
                    (COLUMN_URGENT, COLUMN_HOLDING, COLUMN_IDLE)],
    }


def render_board_json(board: Dict[str, Any]) -> str:
    """board 规格 → ```board 围栏文本（含空列占位）。"""
    import json
    return "```board\n" + json.dumps(board, ensure_ascii=False, separators=(",", ":")) + "\n```"


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)
