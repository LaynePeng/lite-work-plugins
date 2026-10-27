"""下单提示引擎：把「行情 + 持仓 + 规则」算成**具体的、可执行的提示**。

这是整个插件的价值所在——用户自己下单，所以插件必须回答四个问题：
① 现在该不该动（触发条件）；② 动多少（股数/金额）；③ 什么价格区间；
④ 什么情况下这个判断作废（止损/失效条件）。

**边界（重要）**：本模块只做**规则触发的算术**，不做选股预测、不给目标价、
不承诺收益。所有提示都带 `basis`（依据）字段说明它是用哪条规则算出来的，
`disclaimer` 字段固定标注"仅供参考、不构成投资建议"。这是合规底线，也是
避免模型把"算出来的数字"讲成"预测"的关键。

规则类型（全部可配置阈值，默认值保守）：

1. `stop_loss`     —— 现价跌破止损线（默认 -8%）
2. `take_profit`   —— 现价触及止盈线（默认 +15%）
3. `trailing`      —— 从最高价回撤超过阈值（默认 10%，需历史最高价）
4. `limit_up/down` —— 触及涨跌停（不可成交，提示"别挂单"）
5. `add_position`  —— 浮亏分批加仓机会（跌到 -5% / -10% 档，且需用户开启）
6. `reduce`        —— 盈利分批减仓（涨到 +10% / +20% 档，且需用户开启）
7. `position_cap`  —— 单票市值占**账户总资产**（持仓市值 + 可用现金）超上限（默认 25%）。
                       ⚠️ 分母必须是总资产，不是持仓市值——只持一只股时
                       「占持仓市值」恒为 100%，会把正常仓位误报成超限。
8. `t1_locked`     —— 今日买入不可卖（回答"现在能卖吗"）
9. `event`         —— 异常信号：放量（量比>2）、大跌(-5%)、大涨(+7%)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from astock_market import (
    BOARD_LABELS, board_of, calc_fee, FeeConfig, limit_prices, lot_size,
    round_price, round_lot, trading_status,
)

DISCLAIMER = "以上为按既定规则计算的结果，仅供参考，不构成投资建议；请自行复核后操作。"


@dataclass
class HintsConfig:
    """提示阈值（设置页可改）。默认值偏保守，宁可少喊。"""

    stop_loss_pct: float = 8.0          # 浮亏超过该比例 → 止损提示
    take_profit_pct: float = 15.0       # 浮盈超过该比例 → 止盈提示
    trailing_pct: float = 10.0          # 自最高价回撤 → 移动止盈提示
    add_step_pct: float = 5.0           # 每跌这么多给一次加仓提示（0=关闭）
    add_max_times: int = 2              # 最多提示几次加仓
    reduce_step_pct: float = 10.0       # 每涨这么多给一次减仓提示（0=关闭）
    reduce_max_times: int = 2
    position_cap_pct: float = 25.0      # 单票市值占流通/组合上限
    volume_ratio_alert: float = 2.0     # 量比超过该值 → 放量提示
    drop_alert_pct: float = 5.0         # 当日跌幅超过该值 → 异动提示
    rise_alert_pct: float = 7.0         # 当日涨幅超过该值 → 异动提示
    enable_add: bool = False            # 加仓提示默认关（容易被当成喊单）
    enable_reduce: bool = False         # 减仓提示默认关

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Hint:
    """一条下单提示。severity: info / action / urgent。"""

    kind: str
    code: str
    name: str
    severity: str = "info"
    title: str = ""
    detail: str = ""
    suggestion: str = ""
    shares: Optional[int] = None
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    basis: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "kind": self.kind, "code": self.code, "name": self.name,
            "severity": self.severity, "title": self.title,
            "detail": self.detail, "suggestion": self.suggestion,
            "basis": self.basis,
        }
        if self.shares is not None:
            out["shares"] = self.shares
        if self.price_low is not None or self.price_high is not None:
            out["price_range"] = [self.price_low, self.price_high]
        if self.meta:
            out["meta"] = self.meta
        return {k: v for k, v in out.items() if v not in (None, "")}


def _pnl_pct(price: float, cost: float) -> Optional[float]:
    if not cost:
        return None
    return round((price - cost) / cost * 100.0, 2)


def build_position_hints(position: Dict[str, Any], quote: Dict[str, Any],
                         cfg: HintsConfig,
                         highest_price: Optional[float] = None,
                         total_market_value: Optional[float] = None,
                         watch_only: bool = False,
                         total_equity: Optional[float] = None) -> List[Hint]:
    """单只持仓 → 提示列表。quote 为 `astock_data.Quote.as_dict()` 的结果。

    `total_equity`（账户总资产 = 持仓市值 + 可用现金）是**单票占比的真分母**。
    `total_market_value` 是持仓市值，只在拿不到总资产时作兜底——
    用它当分母是历史 bug：只持一只股时占比恒为 100%，正常仓位被误报超限
    （真实报障：总资产 15,081（现金 11,423 + 持仓 3,658），占比 24.3% 本在上限内，
    却被报成「单票占比 100% 超上限」并建议减仓）。

    `watch_only=True`（或 shares<=0）时只产出**与仓位无关**的信号（涨跌停、
    当日异动、放量）——自选股也关心这些。盈亏/止损/加仓类必须有持仓才有意义，
    无持仓时直接跳过（此前是整函数 early-return，导致自选股永远报不出涨跌停，
    `astock_hint` 里"自选只报涨跌停"的分支实际是死代码）。
    """
    hints: List[Hint] = []
    code = str(position.get("code") or quote.get("code") or "")
    name = str(position.get("name") or quote.get("name") or code)
    shares = int(position.get("shares") or 0)
    cost = float(position.get("cost_price") or 0)
    price = quote.get("price")
    prev_close = quote.get("prev_close")
    if not price:
        return hints
    positional = shares > 0 and not watch_only
    if not positional:
        shares = 0
        cost = 0.0

    pnl_pct = _pnl_pct(price, cost) if positional else None
    limit_up = quote.get("limit_up")
    limit_down = quote.get("limit_down")
    chg = quote.get("change_pct")

    # ① 涨跌停：不可成交，明确劝阻挂单
    if limit_up and abs(price - limit_up) < 0.005:
        hints.append(Hint(
            kind="limit_up", code=code, name=name, severity="info",
            title=f"{name} 已涨停（{limit_up:.2f}）",
            detail="涨停价买入需排队，卖出可立即成交；涨停打开前价格不会变化。",
            suggestion="想卖：可挂涨停价卖出；想买：不建议追涨停排队（成交不确定）。",
            price_low=limit_up, price_high=limit_up,
            basis=f"涨停价 = 昨收 {prev_close} × (1+{quote.get('limit_pct', 10)}%)"))
        return hints        # 涨停时其它盈亏规则不再重复喊
    if limit_down and abs(price - limit_down) < 0.005:
        hints.append(Hint(
            kind="limit_down", code=code, name=name, severity="urgent",
            title=f"{name} 已跌停（{limit_down:.2f}）",
            detail="跌停价卖出需排队，成交不确定；买入可立即成交。",
            suggestion="止损单在跌停时难以成交，请评估是否等打开跌停再处理。",
            price_low=limit_down, price_high=limit_down,
            basis=f"跌停价 = 昨收 {prev_close} × (1-{quote.get('limit_pct', 10)}%)"))
        # 跌停时价格已无变化空间，但"当日大跌"这个事实对自选股/持仓都有意义，
        # 所以只跳过盈亏类规则、保留异动类——此前直接 return 会让
        # 跌停股的最后一条信息变成"什么都没发生"（自选股场景尤其明显）
        if chg is not None and chg <= -abs(cfg.drop_alert_pct):
            hints.append(Hint(
                kind="event", code=code, name=name, severity="action",
                title=f"{name} 当日下跌 {chg:.2f}%",
                detail=f"现价 {price:.2f}，昨收 {prev_close}，已封跌停。",
                suggestion="跌停打开前难以成交；先确认是否有公告/行业利空再决定。",
                basis=f"当日涨跌幅 {chg:.2f}% ≤ -{cfg.drop_alert_pct}%"))
        return hints

    # ② 止损
    if pnl_pct is not None and pnl_pct <= -abs(cfg.stop_loss_pct):
        stop_price = round_price(cost * (1 - cfg.stop_loss_pct / 100.0))
        hints.append(Hint(
            kind="stop_loss", code=code, name=name, severity="urgent",
            title=f"{name} 触发止损线（浮亏 {pnl_pct:+.2f}%）",
            detail=(f"成本 {cost:.3f}，现价 {price:.2f}，浮亏 {pnl_pct:+.2f}%，"
                    f"已超过止损阈值 -{cfg.stop_loss_pct}%。"),
            suggestion=(f"按计划应减仓/清仓 {shares} 股；若不认可该止损位，"
                        f"需重新设定并记录理由（避免临场改规则）。"),
            shares=shares,
            price_low=round_price(price * 0.995), price_high=round_price(price * 1.005),
            basis=f"浮亏比例 {pnl_pct:+.2f}% ≤ -{cfg.stop_loss_pct}%",
            meta={"pnl_pct": pnl_pct, "stop_price": stop_price}))
    # ③ 止盈
    elif pnl_pct is not None and pnl_pct >= abs(cfg.take_profit_pct):
        half = round_lot(shares / 2, code)
        if half > 0:
            suggestion = (f"可考虑减半 {half} 股锁定利润、保留 {shares - half} 股；"
                          f"或上移止盈线继续持有。")
            sell_shares = half
        else:
            # 持仓不足两手的零股（如 150 股）：减半会拆出非法零股，
            # 只能整体清掉，或按整手卖 100 股
            if shares >= 100:
                suggestion = (f"持仓 {shares} 股不足两手，减半不合规：可按整手卖 100 股、"
                              f"或一次性清仓 {shares} 股（零股只能随清仓卖出）。")
                sell_shares = 100
            else:
                suggestion = f"持仓仅 {shares} 股（零股），只能一次性清仓。"
                sell_shares = shares
        hints.append(Hint(
            kind="take_profit", code=code, name=name, severity="action",
            title=f"{name} 触及止盈线（浮盈 {pnl_pct:+.2f}%）",
            detail=(f"成本 {cost:.3f}，现价 {price:.2f}，浮盈 {pnl_pct:+.2f}%。"),
            suggestion=suggestion,
            shares=sell_shares,
            price_low=round_price(price * 0.995), price_high=round_price(price * 1.005),
            basis=f"浮盈比例 {pnl_pct:+.2f}% ≥ +{cfg.take_profit_pct}%",
            meta={"pnl_pct": pnl_pct}))

    # ④ 移动止盈（需要历史最高价）
    if positional and highest_price and highest_price > 0 and price > 0:
        drawdown = (price - highest_price) / highest_price * 100.0
        if drawdown <= -abs(cfg.trailing_pct) and (pnl_pct or 0) > 0:
            hints.append(Hint(
                kind="trailing", code=code, name=name, severity="action",
                title=f"{name} 自高点回撤 {drawdown:.2f}%",
                detail=(f"持仓期最高价 {highest_price:.2f}，现价 {price:.2f}，"
                        f"回撤 {drawdown:.2f}%（阈值 -{cfg.trailing_pct}%），仍有浮盈 {pnl_pct:+.2f}%。"),
                suggestion="可减仓保住浮盈，或把止损上移到成本价上方。",
                shares=round_lot(shares / 2, code) or shares,
                basis=f"回撤 {drawdown:.2f}% ≤ -{cfg.trailing_pct}% 且仍浮盈",
                meta={"highest": highest_price, "drawdown_pct": round(drawdown, 2)}))

    # ⑤ 分批加仓（默认关）
    if positional and cfg.enable_add and pnl_pct is not None and pnl_pct <= 0:
        tiers = int(abs(pnl_pct) // max(cfg.add_step_pct, 0.01))
        tiers = min(tiers, cfg.add_max_times)
        if tiers >= 1:
            add_pct = cfg.add_step_pct
            add_price = round_price(cost * (1 - add_pct * tiers / 100.0))
            hints.append(Hint(
                kind="add_position", code=code, name=name, severity="info",
                title=f"{name} 处于第 {tiers} 档加仓区间（浮亏 {pnl_pct:+.2f}%）",
                detail=(f"成本 {cost:.3f}，现价 {price:.2f}。按每跌 {add_pct}% 加一档的"
                        f"计划，第 {tiers} 档参考价 {add_price:.2f}。"),
                suggestion=(f"如需补仓，单次不超过原持仓的一半（约 "
                            f"{round_lot(shares / 2, code)} 股）；注意加仓会拉低均价但同时放大风险。"),
                shares=round_lot(shares / 2, code),
                price_low=round_price(add_price * 0.99), price_high=add_price,
                basis=f"浮亏 {pnl_pct:+.2f}% 落在第 {tiers} 档（每 {add_pct}% 一档）"))

    # ⑥ 分批减仓（默认关）
    if positional and cfg.enable_reduce and pnl_pct is not None and pnl_pct >= cfg.reduce_step_pct:
        tiers = min(int(pnl_pct // max(cfg.reduce_step_pct, 0.01)), cfg.reduce_max_times)
        if tiers >= 1:
            cut = round_lot(shares * min(0.5, 0.25 * tiers), code) or round_lot(shares / 2, code)
            hints.append(Hint(
                kind="reduce", code=code, name=name, severity="info",
                title=f"{name} 处于第 {tiers} 档减仓区间（浮盈 {pnl_pct:+.2f}%）",
                detail=f"按每涨 {cfg.reduce_step_pct}% 减一档的计划，建议减 {cut} 股。",
                suggestion=f"分批减仓可降低择时压力；减仓后记得把止损上移。",
                shares=cut, basis=f"浮盈 {pnl_pct:+.2f}% ≥ 第 {tiers} 档"))

    # ⑦ 单票占比（分母＝账户总资产 = 持仓市值 + 可用现金）
    # 历史 bug：这里曾用 total_market_value（持仓市值）当分母，只持一只股时
    # 占比恒为 100%，即使只有 24% 也会被报成「超上限」并建议减仓。
    # equity 拿不到（≤0）时才退回持仓市值——那种情况等价于「全仓、无现金」。
    cap_base = float(total_equity or 0) or float(total_market_value or 0)
    if positional and cap_base > 0:
        mv = shares * price
        weight = mv / cap_base * 100.0
        if weight > cfg.position_cap_pct:
            # 卖出量按「超出上限的市值」折算，再用同一分母反推目标市值
            target_mv = cfg.position_cap_pct / 100.0 * cap_base
            hints.append(Hint(
                kind="position_cap", code=code, name=name, severity="action",
                title=f"{name} 单票占比 {weight:.1f}% 超上限",
                detail=(f"该股市值 {mv:,.0f} 元，占账户总资产（持仓 "
                        f"{float(total_market_value or 0):,.0f} 元 + 可用现金）"
                        f"{cap_base:,.0f} 元的 {weight:.1f}%，"
                        f"超过上限 {cfg.position_cap_pct}%。"),
                suggestion=f"建议至少减至 {cfg.position_cap_pct}% 以内以控制单票风险。",
                shares=round_lot((mv - target_mv) / price, code),
                basis=f"占总资产 {weight:.1f}% > 上限 {cfg.position_cap_pct}%",
                meta={"weight_pct": round(weight, 2)}))

    # ⑧ 当日异动
    if chg is not None:
        if chg <= -abs(cfg.drop_alert_pct):
            hints.append(Hint(
                kind="event", code=code, name=name, severity="action",
                title=f"{name} 当日下跌 {chg:.2f}%",
                detail=f"现价 {price:.2f}，昨收 {prev_close}。",
                suggestion="先确认是否有公告/行业消息；若属计划外下跌，按止损纪律处理而非摊平。",
                basis=f"当日涨跌幅 {chg:.2f}% ≤ -{cfg.drop_alert_pct}%"))
        elif chg >= abs(cfg.rise_alert_pct):
            hints.append(Hint(
                kind="event", code=code, name=name, severity="info",
                title=f"{name} 当日上涨 {chg:.2f}%",
                detail=f"现价 {price:.2f}，昨收 {prev_close}。",
                suggestion="涨幅较大时不建议追高；已有持仓可考虑部分止盈。",
                basis=f"当日涨跌幅 {chg:.2f}% ≥ +{cfg.rise_alert_pct}%"))

    # ⑨ 放量
    vr = quote.get("volume_ratio")
    if vr and vr >= cfg.volume_ratio_alert:
        hints.append(Hint(
            kind="event", code=code, name=name, severity="info",
            title=f"{name} 量比 {vr:.2f}（放量）",
            detail=f"当前成交量是近 5 日均量的 {vr:.2f} 倍。",
            suggestion="放量伴随方向突破才有效；单看放量容易追高，需结合价格位置。",
            basis=f"量比 {vr:.2f} ≥ {cfg.volume_ratio_alert}"))

    return hints


def build_t1_hint(position: Dict[str, Any], available: int,
                  today_bought: int = 0) -> Optional[Hint]:
    """回答"今天能卖吗"。available 由台账按 T+1 计算。"""
    shares = int(position.get("shares") or 0)
    code = str(position.get("code") or "")
    if shares <= 0:
        return None
    if available >= shares:
        return None
    locked = shares - available
    return Hint(
        kind="t1_locked", code=code, name=str(position.get("name") or code),
        severity="info",
        title=f"{position.get('name') or code} 今日可卖 {available} 股，{locked} 股 T+1 锁定",
        detail=(f"持仓 {shares} 股，其中 {locked} 股为今日买入，"
                f"按 T+1 规则明日才能卖出。"),
        suggestion=f"今日最多可卖 {available} 股。" if available > 0 else "今日不可卖出。",
        shares=available, basis="T+1：当日买入股份次一交易日方可卖出")


def suggest_buy(code: str, name: str, price: float, cash: float,
                cfg: HintsConfig, existing_shares: int = 0,
                existing_cost: float = 0.0,
                total_market_value: Optional[float] = None,
                fee_cfg: Optional[FeeConfig] = None) -> Dict[str, Any]:
    """给"想买多少"一个可执行的建议（按单票占比上限与可用资金双重约束）。

    返回含 `shares` / `amount` / `fee` / `limit_up` / `limit_down` 的建议，
    以及"为什么是这个数"的算式——用户能一眼核对。
    """
    fee_cfg = fee_cfg or FeeConfig()
    if not price or price <= 0:
        raise ValueError("需要有效的最新价才能估算可买数量")
    board = board_of(code)
    limit_up, limit_down, limit_pct = limit_prices(price, code, name)

    equity = (total_market_value or 0) + (cash or 0)
    cap_amount = equity * cfg.position_cap_pct / 100.0 if equity > 0 else 0.0
    # 留出费用：佣金最低 5 元 + 过户费，按 0.1% 粗留
    budget = min(cash or 0, cap_amount) if cap_amount > 0 else (cash or 0)
    raw_shares = budget / (price * 1.001) if price else 0
    shares = round_lot(raw_shares, code)
    amount = round_price(price * shares)
    fee_bd = calc_fee(amount, "buy", fee_cfg, board)

    reasons = []
    if not cash or cash <= 0:
        reasons.append("台账可用资金为 0 元，先用 astock_portfolio action=set_cash 录入资金"
                       "（或直接按你的实际可用资金自己定数量）")
    elif cap_amount > 0 and budget == cap_amount and cap_amount < cash:
        reasons.append(f"受单票占比 {cfg.position_cap_pct}% 上限限制"
                       f"（组合市值 {equity:,.0f} 元）")
    else:
        reasons.append(f"受可用资金 {cash:,.2f} 元限制")
    if shares == 0 and cash > 0:
        need = price * lot_size(code)
        reasons.append(f"资金不足一手（一手约需 {need:,.0f} 元）")

    return {
        "code": code, "name": name, "price": price,
        "board": BOARD_LABELS.get(board, board),
        "suggest_shares": shares,
        "suggest_amount": amount,
        "est_fee": fee_bd.total,
        "est_total_cost": round(amount + fee_bd.total, 2),
        "limit_up": limit_up, "limit_down": limit_down, "limit_pct": limit_pct,
        "existing_shares": existing_shares,
        "shares_after": existing_shares + shares,
        # 买入后的新成本价（含本次费用），让用户能预判成本变化
        "avg_cost_after": (round((existing_cost * existing_shares + amount +
                                  fee_bd.total) / (existing_shares + shares), 4)
                           if existing_shares + shares else None),
        "reason": "；".join(reasons),
        "note": ("买入需为 100 股整数倍（科创板 200 股）；" if board != "star"
                 else "科创板买入需 200 股起；") + "以上按最新价估算，实际以委托价为准。",
    }


def suggest_sell(position: Dict[str, Any], price: float, available: int,
                 ratio: Optional[float] = None, shares: Optional[int] = None,
                 fee_cfg: Optional[FeeConfig] = None) -> Dict[str, Any]:
    """给"卖多少"的可执行建议。ratio=0.5 表示卖一半；shares 优先。"""
    fee_cfg = fee_cfg or FeeConfig()
    code = str(position.get("code") or "")
    name = str(position.get("name") or code)
    held = int(position.get("shares") or 0)
    cost = float(position.get("cost_price") or 0)
    if held <= 0:
        raise ValueError(f"{code} 当前无持仓")
    if price <= 0:
        raise ValueError("需要有效的最新价才能估算卖出金额")
    if shares:
        qty = int(shares)
    elif ratio:
        ratio = max(0.0, min(float(ratio), 1.0))
        qty = held if ratio >= 1.0 else round_lot(held * ratio, code)
        if qty <= 0:
            qty = held
    else:
        qty = held
    qty = min(qty, max(available, 0))
    if qty <= 0:
        return {"code": code, "name": name, "sellable": 0,
                "reason": "今日可卖数量为 0（T+1 锁定），无法卖出"}
    amount = round_price(price * qty)
    fee_bd = calc_fee(amount, "sell", fee_cfg, board_of(code))
    gross_pnl = (price - cost) * qty
    return {
        "code": code, "name": name,
        "sellable": available, "suggest_shares": qty,
        "remain_shares": held - qty,
        "price": price, "suggest_amount": amount,
        "est_fee": fee_bd.total,
        "est_net_proceeds": round(amount - fee_bd.total, 2),
        "est_realized_pnl": round(gross_pnl - fee_bd.total, 2),
        "pnl_pct": _pnl_pct(price, cost),
        "note": "卖出零股需一次性清仓；以上按最新价估算，实际以委托价为准。",
    }


def market_context() -> Dict[str, Any]:
    """把交易时段状态包成给 Agent 的一段说明（避免它把收盘价当实时价）。"""
    st = trading_status()
    if st["open"]:
        text = f"当前处于{st['phase']}，价格为实时行情。"
    elif st["phase"] == "已收盘":
        text = ("当前已收盘，显示的为当日收盘价（不会再变动）；"
                "如需操作请规划到下一交易日。")
    elif st["phase"] == "午间休市":
        text = "当前午间休市，价格为上午收盘价。"
    elif st["phase"] == "开盘前":
        text = "当前开盘前，价格为上一交易日收盘价。"
    else:
        text = f"当前{st['phase']}，价格为上一交易日收盘价。"
    out = dict(st)
    out["text"] = text
    return out
