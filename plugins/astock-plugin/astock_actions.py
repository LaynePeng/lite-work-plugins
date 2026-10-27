"""操作指示：把「该买什么」变成「你现在该具体按哪些按钮」。

**为什么这是最后一块拼图**：前面几个模块解决了"选什么、买多少、靠不靠谱"，
但用户拿到一张买入清单后仍要自己对照持仓算「哪只先卖、资金够不够、
卖完够不够买」。这一步最容易出错（卖少了钱不够、卖多了空仓），
也最耗神——正是"傻瓜化"要解决的部分。

**能力边界（本插件永远不下单）**：
本模块只产出**给人看的操作指示**（卖什么/买什么/什么价/什么数量），
不连接券商、不自动交易、不生成委托文件。用户必须自己在券商 App 里执行。

────────────────────────────────────────────────────────────────────────
执行顺序为什么必须是「先卖后买」
────────────────────────────────────────────────────────────────────────

A 股是 T+1 且卖出资金当日可用于买入，所以：
  ① 先卖出 → 释放现金；
  ② 再用「原有现金 + 卖出所得」买入。
反过来先买会因资金不足而失败。这不只是"建议顺序"，是 A 股的资金规则。

**T+1 限制**：今日买入的股份今日不可卖。所以卖出清单只包含
`available_today` 内的数量——给出卖不掉的数字等于给出废单。

────────────────────────────────────────────────────────────────────────
何时建议卖出
────────────────────────────────────────────────────────────────────────

只基于**明确规则**，不做主观判断：
  · 触发止损/止盈阈值（沿用 hints 的配置）；
  · 不在本次选股名单里，且回撤/亏损超过阈值 → 考虑换仓；
  · 单票占比超上限 → 减到上限内。

每一条都带 `reason` 与 `basis`，用户能核对"为什么让我卖这只"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from astock_market import FeeConfig, calc_fee, board_of, round_lot, round_price


@dataclass
class SellAction:
    """一条卖出指示。"""

    code: str
    name: str
    shares: int                  # 建议卖出股数（已受 T+1 可卖约束）
    price: float                 # 参考价
    reason: str                  # 为什么卖（人能读懂）
    basis: str                   # 依据（可核对）
    urgency: str = "normal"      # urgent / normal
    est_amount: float = 0.0
    est_fee: float = 0.0
    pnl_pct: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {"code": self.code, "name": self.name, "shares": self.shares,
               "price": self.price, "reason": self.reason,
               "basis": self.basis, "urgency": self.urgency,
               "est_amount": round(self.est_amount, 2),
               "est_fee": round(self.est_fee, 2)}
        if self.pnl_pct is not None:
            out["pnl_pct"] = self.pnl_pct
        return out


@dataclass
class BuyAction:
    """一条买入指示。"""

    code: str
    name: str
    shares: int
    price: float
    score: float
    amount: float = 0.0
    fee: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = {"code": self.code, "name": self.name, "shares": self.shares,
               "price": self.price, "score": self.score,
               "amount": round(self.amount, 2), "fee": round(self.fee, 2),
               "reason": self.reason}
        if self.stop_loss is not None:
            out["stop_loss"] = self.stop_loss
        if self.take_profit is not None:
            out["take_profit"] = self.take_profit
        return out


@dataclass
class ActionPlan:
    """完整操作指示：先卖后买，带资金闭环核算。"""

    sells: List[SellAction] = field(default_factory=list)
    buys: List[BuyAction] = field(default_factory=list)
    cash_before: float = 0.0
    sell_proceeds: float = 0.0
    buy_cost: float = 0.0
    cash_after: float = 0.0
    notes: List[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sells": [s.as_dict() for s in self.sells],
            "buys": [b.as_dict() for b in self.buys],
            "cash_before": round(self.cash_before, 2),
            "sell_proceeds": round(self.sell_proceeds, 2),
            "buy_cost": round(self.buy_cost, 2),
            "cash_after": round(self.cash_after, 2),
            "notes": list(self.notes),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }


# ====================================================================== 卖出判定


def decide_sells(positions: Sequence[Dict[str, Any]],
                 quotes: Dict[str, Dict[str, Any]],
                 pick_codes: Sequence[str],
                 cfg: Any,
                 total_market_value: float = 0.0,
                 fee_cfg: Optional[FeeConfig] = None,
                 ) -> List[SellAction]:
    """判定该卖什么。只依据明确规则，不做主观判断。

    `cfg` 为 `astock_hints.HintsConfig`（复用同一套阈值，避免两处配置不一致）。
    `pick_codes` = 本轮选中的标的；**不在此列**且亏损超阈值的持仓会被建议换仓。

    注意：这里只给"建议卖出量"，实际可卖数受 T+1 约束（见 `available_today`）。
    """
    fee_cfg = fee_cfg or FeeConfig()
    out: List[SellAction] = []
    picked = {str(c) for c in pick_codes}
    stop_pct = abs(float(getattr(cfg, "stop_loss_pct", 8.0) or 8.0))
    take_pct = abs(float(getattr(cfg, "take_profit_pct", 15.0) or 15.0))
    cap_pct = float(getattr(cfg, "position_cap_pct", 25.0) or 25.0)

    for p in positions or []:
        code = str(p.get("code") or "")
        if not code:
            continue
        q = quotes.get(code) or {}
        price = q.get("price") or p.get("cost_price")
        if not price or price <= 0:
            continue
        held = int(p.get("shares") or 0)
        available = int(p.get("available_today") or 0)
        cost = float(p.get("cost_price") or 0)
        if held <= 0:
            continue

        pnl_pct = ((price - cost) / cost * 100.0) if cost > 0 else None
        # 名称优先取行情里的（行情自带证券简称，比台账录入的更可靠——
        # 早期只读台账，手工录入时漏传 name 就会把整个清单显示成纯代码）。
        name = str(q.get("name") or p.get("name") or code)

        def _mk(shares: int, reason: str, basis: str, urgency: str = "normal"):
            shares = min(int(shares), available)
            if shares <= 0:
                return None
            lot = 200 if board_of(code) == "star" else 100
            # 零股只允许随清仓卖出：非清仓时向下取整到整手
            if shares < held:
                shares = max(0, int(shares // lot) * lot)
            if shares <= 0:
                return None
            amount = round_price(price * shares)
            fee = calc_fee(amount, "sell", fee_cfg, board_of(code)).total
            return SellAction(
                code=code, name=name, shares=shares, price=round_price(price),
                reason=reason, basis=basis, urgency=urgency,
                est_amount=amount, est_fee=fee,
                pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None)

        # ① 止损（最优先，urgent）
        if pnl_pct is not None and pnl_pct <= -stop_pct:
            a = _mk(held, f"浮亏 {pnl_pct:+.2f}% 触发止损线 -{stop_pct:g}%",
                    f"浮亏比例 {pnl_pct:+.2f}% ≤ -{stop_pct:g}%", "urgent")
            if a:
                out.append(a)
            continue

        # ② 止盈：减半锁定利润
        if pnl_pct is not None and pnl_pct >= take_pct:
            half = round_lot(held / 2, code)
            a = _mk(half or held,
                    f"浮盈 {pnl_pct:+.2f}% 触及止盈线 +{take_pct:g}%，减半锁利",
                    f"浮盈比例 {pnl_pct:+.2f}% ≥ +{take_pct:g}%")
            if a:
                out.append(a)
            continue

        # ③ 换仓：不在本轮选股名单里且浮亏（不占着资金等回本）
        if picked and code not in picked and pnl_pct is not None \
                and pnl_pct < 0:
            a = _mk(held,
                    f"不在本轮选股名单且浮亏 {pnl_pct:+.2f}%，建议换仓",
                    "该标的本轮未入选 + 浮亏；腾出资金给得分更高的标的")
            if a:
                out.append(a)
            continue

        # ④ 仓位超限：减到上限内
        if total_market_value > 0 and cap_pct > 0:
            mv = held * price
            weight = mv / total_market_value * 100.0
            if weight > cap_pct:
                target_mv = cap_pct / 100.0 * total_market_value
                excess_shares = int((mv - target_mv) / price)
                a = _mk(excess_shares,
                        f"单票占比 {weight:.1f}% 超上限 {cap_pct:g}%，减到上限内",
                        f"占比 {weight:.1f}% > {cap_pct:g}%")
                if a:
                    out.append(a)

    # urgent 排前面（止损优先执行）
    out.sort(key=lambda x: (x.urgency != "urgent", -x.est_amount))
    return out


# ====================================================================== 买入判定


def decide_buys(allocations: Sequence[Any], proceeds: float,
                strategy: Any) -> List[BuyAction]:
    """把选股结果转成买入指示（数量已在 size_positions 里算好）。

    `allocations` 为 `astock_quant.Allocation` 列表。这里不改数量——
    数量是"按预算算出来的"，改了就与报告里的数字对不上。
    """
    out: List[BuyAction] = []
    for a in allocations or []:
        if getattr(a, "skipped", False) or not getattr(a, "shares", 0):
            continue
        out.append(BuyAction(
            code=a.code, name=a.name, shares=int(a.shares),
            price=float(a.price), score=float(getattr(a, "score", 0.0)),
            amount=float(getattr(a, "amount", 0.0) or 0.0),
            fee=float(getattr(a, "fee", 0.0) or 0.0),
            stop_loss=getattr(a, "stop_loss", None),
            take_profit=getattr(a, "take_profit", None),
            reason=f"得分 {getattr(a, 'score', 0):.1f}"
                   + (f"；{strategy.label}" if strategy is not None else "")))
    return out


# ====================================================================== 编排


def build_action_plan(positions: Sequence[Dict[str, Any]],
                      quotes: Dict[str, Dict[str, Any]],
                      allocations: Sequence[Any],
                      cash: float,
                      cfg: Any,
                      strategy: Any = None,
                      total_market_value: float = 0.0,
                      fee_cfg: Optional[FeeConfig] = None,
                      ) -> ActionPlan:
    """生成完整操作指示：先卖后买 + 资金闭环核算。

    资金核算必须能对上账：`cash_after = cash + 卖出净额 - 买入总成本`。
    对不上就说明有笔账算错了，所以这里会把差额写进 notes 让人看见。
    """
    plan = ActionPlan(cash_before=float(cash or 0))
    pick_codes = [getattr(a, "code", "") for a in (allocations or [])]

    sells = decide_sells(positions, quotes, pick_codes, cfg,
                         total_market_value=total_market_value,
                         fee_cfg=fee_cfg)
    plan.sells = sells
    # 卖出净额 = 成交额 - 卖出费用
    plan.sell_proceeds = round_price(
        sum(max(0.0, s.est_amount - s.est_fee) for s in sells))

    buys = decide_buys(allocations, plan.sell_proceeds, strategy)
    plan.buys = buys
    plan.buy_cost = round(sum(b.amount + b.fee for b in buys), 2)

    available = plan.cash_before + plan.sell_proceeds
    plan.cash_after = round(available - plan.buy_cost, 2)

    if not sells and not buys:
        # 无操作时**仍要给出说明性提示**：用户往往正是这时最想知道
        # "为什么没有指示、我的 T+1 锁了多少"。早期实现直接 return，
        # notes 全空 → 界面只剩一句「无需操作」，等于什么都没说。
        plan.blocked = True
        plan.block_reason = "本轮既无需卖出，也没有可买入的标的"
        _append_common_notes(plan, positions)
        return plan

    # 资金不足：买入总量超过「现金 + 卖出净额」
    if plan.buy_cost > available + 1e-6:
        plan.notes.append(
            f"⚠️ 买入总额 {plan.buy_cost:,.2f} 元超过可用资金 {available:,.2f} 元"
            f"（差 {plan.buy_cost - available:,.2f} 元）。"
            f"请先按卖出指示回笼资金，或减少买入数量。")
        plan.blocked = True
        plan.block_reason = "买入资金不足，需先执行卖出"

    if sells:
        plan.notes.append(
            "**必须先卖出、再买入**：A 股卖出资金当日可用于买入，"
            "反过来先买会因资金不足而失败。")
    _append_common_notes(plan, positions)
    return plan


def _append_common_notes(plan: ActionPlan,
                         positions: Sequence[Dict[str, Any]]) -> None:
    """补充通用提示（止损优先、T+1 锁定、免责）。

    **抽出来单独一个函数**是因为"无操作"分支也要用它：那条分支此前直接
    return，导致 notes 全空——用户看到的只有一句「无需操作」，
    而真正想知道的信息（T+1 锁了多少、为什么不动）全都没了。
    """
    if any(s.urgency == "urgent" for s in plan.sells):
        plan.notes.append(
            "止损卖出最优先：这些标的已触发止损线，继续持有可能扩大亏损。")
    locked = [p for p in (positions or [])
              if int(p.get("shares") or 0) > int(p.get("available_today") or 0)]
    if locked:
        total_locked = sum(int(p.get("shares") or 0)
                           - int(p.get("available_today") or 0)
                           for p in locked)
        plan.notes.append(
            f"有 {len(locked)} 只持仓共 {total_locked} 股处于 T+1 锁定"
            f"（今日买入，明日才可卖）。卖出指示已按「今日可卖」裁剪，"
            f"不会让你下废单。")
    plan.notes.append(
        "本插件不连接券商、不下单。以上为操作指示，请自行在券商 App 执行。")


# ====================================================================== 渲染


def render_action_plan(plan: ActionPlan, strategy_label: str = "") -> str:
    """渲染成"照着做"的清单（聊天里看）。"""
    lines: List[str] = []
    title = "操作指示"
    if strategy_label:
        title += f"（{strategy_label}）"
    lines.append(f"## 📋 {title}")
    lines.append("")

    if plan.blocked and not plan.sells and not plan.buys:
        lines.append(f"{plan.block_reason}。")
        lines.append("")
        lines.append("_可先跑一次选股，或检查台账里是否已有持仓。_")
        return "\n".join(lines)

    # ── 资金概览 ──
    lines.append("| 资金 | 金额 |")
    lines.append("|---|---|")
    lines.append(f"| 当前可用现金 | {plan.cash_before:,.2f} 元 |")
    if plan.sells:
        lines.append(f"| 卖出回笼（净额） | +{plan.sell_proceeds:,.2f} 元 |")
    if plan.buys:
        lines.append(f"| 买入支出（含费） | -{plan.buy_cost:,.2f} 元 |")
    lines.append(f"| **操作后现金** | **{plan.cash_after:,.2f} 元** |")
    lines.append("")

    # ── 卖出 ──
    if plan.sells:
        lines.append("### 第一步：卖出（先做这个，才有钱买）")
        lines.append("")
        lines.append("| 标的 | 卖出股数 | 参考价 | 预计净额 | 原因 |")
        lines.append("|---|---|---|---|---|")
        for s in plan.sells:
            mark = "🔴 " if s.urgency == "urgent" else ""
            lines.append(f"| {mark}{s.name}({s.code}) | **{s.shares}** | "
                         f"{s.price:.2f} | {s.est_amount - s.est_fee:,.0f} | "
                         f"{s.reason} |")
        lines.append("")
        lines.append("**在券商 App 逐条操作**：卖出 → 输入代码 → 数量填上表股数 → "
                     "限价填参考价（或市价）→ 确认。")
        lines.append("")
    else:
        lines.append("### 第一步：卖出")
        lines.append("")
        lines.append("本轮无需卖出，直接看买入清单。")
        lines.append("")

    # ── 买入 ──
    if plan.buys:
        lines.append("### 第二步：买入（卖出成交后再做）")
        lines.append("")
        lines.append("| 标的 | 买入股数 | 参考价 | 金额 | 止损价 | 止盈价 |")
        lines.append("|---|---|---|---|---|---|")
        for b in plan.buys:
            lines.append(
                f"| {b.name}({b.code}) | **{b.shares}** | {b.price:.2f} | "
                f"{b.amount:,.0f} | "
                f"{f'{b.stop_loss:.2f}' if b.stop_loss else '-'} | "
                f"{f'{b.take_profit:.2f}' if b.take_profit else '-'} |")
        lines.append("")
        if any(b.stop_loss for b in plan.buys):
            lines.append("**买入后挂条件单**（不用盯盘）：在券商 App 的"
                         "「条件单/止盈止损」里按上表止损、止盈价填。")
            lines.append("")

    if plan.notes:
        lines.append("### 注意事项")
        lines.append("")
        for n in plan.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("**这不是自动交易**：插件不连接任何券商接口、不下单、"
                 "不生成委托文件。以上只是按既定规则算出的操作指示，"
                 "请自行复核后在券商 App 执行。")
    return "\n".join(lines)
