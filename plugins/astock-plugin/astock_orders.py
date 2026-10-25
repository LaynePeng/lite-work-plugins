"""条件单参数计算：把"该挂什么价"算成可直接照抄的数字。

**为什么这是本插件的核心价值**（用户原话：「购买建议完全靠你的，我只是操作，
而且我经常没时间操作」）：

盯盘提醒对没时间的人**没有用**——提醒了你也不一定来得及操作。真正能替代盯盘
的是**券商条件单**：晚上花几分钟挂好"到价触发"，白天完全不用看，触发与否由
券商系统执行。所以插件该做的是**把参数算准**，而不是频繁喊你。

本模块输出三件套（加上买入价共四个数）：

    委托价 price     —— 现在/下次开盘该挂的价
    止损价 stop_loss —— 跌破就自动卖出的触发价（券商"条件单/止盈止损"里填）
    止盈价 take_profit —— 涨到就自动卖出的触发价
    跟踪回撤 trailing  —— 支持"回落卖出"的券商填这个百分比

**三个必须写死在代码里的口径**（手算极易错，错一位就是亏钱）：

1. **止损价必须避开整数关口与跌停价**：整数位（9.00）是心理支撑，止损挂在那里
   容易被"扫一把"后反弹；跌破跌停价则当日根本不可能成交（白挂）。
2. **止盈价按涨跌停与"到手收益"校正**：止盈价不能超过涨停价（超了不成交）；
   且要扣掉卖出费用才是"真到手"的收益。
3. **仓位换算成整手**：算出来的股数必须向下取整到 100（科创板 200），
   否则是废单。

**不做的事**：不预测目标价、不给"建议买卖"的主观判断——所有数字都由
用户设定的阈值（止损 %、止盈 %、单票上限）推导，来源可核对。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from astock_market import (
    BOARD_MAIN, FeeConfig, board_of, calc_fee, limit_prices, lot_size,
    round_lot, round_price,
)


@dataclass
class ConditionalOrder:
    """一张可照抄的条件单（四件套）。"""

    code: str
    name: str
    side: str                     # buy / sell
    shares: int
    price: float                  # 委托参考价
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_pct: Optional[float] = None
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    est_amount: float = 0.0
    est_fee: float = 0.0
    notes: List[str] = None       # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "code": self.code, "name": self.name, "side": self.side,
            "shares": self.shares, "price": self.price,
            "est_amount": round(self.est_amount, 2),
            "est_fee": round(self.est_fee, 2),
        }
        for key in ("price_low", "price_high", "stop_loss", "take_profit",
                    "trailing_pct", "limit_up", "limit_down"):
            v = getattr(self, key)
            if v is not None:
                out[key] = v
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    def order_block(self) -> str:
        """生成"照着填"的委托参数块（用户在券商 App 逐项照抄）。"""
        side_cn = "买入" if self.side == "buy" else "卖出"
        lines = [f"【{self.name or self.code} {self.code}】{side_cn}",
                 f"  委托价  {self.price:.2f}"
                 + (f"　（挂单区间 {self.price_low:.2f} ~ {self.price_high:.2f}）"
                    if self.price_low and self.price_high else ""),
                 f"  数量    {self.shares} 股",
                 f"  预估金额 {self.est_amount:,.2f} 元　费用约 {self.est_fee:.2f} 元"]
        if self.stop_loss or self.take_profit:
            lines.append("  条件单：")
            if self.stop_loss:
                lines.append(f"    止损触发价 {self.stop_loss:.2f}"
                             f"（跌破自动卖出）")
            if self.take_profit:
                lines.append(f"    止盈触发价 {self.take_profit:.2f}"
                             f"（涨到自动卖出）")
            if self.trailing_pct:
                lines.append(f"    跟踪回撤 {self.trailing_pct:g}%"
                             f"（自最高价回落该比例卖出，支持该功能的券商才填）")
        if self.limit_up or self.limit_down:
            lines.append(f"  涨跌停  {self.limit_down or '-'} / {self.limit_up or '-'}")
        for n in self.notes:
            lines.append(f"  · {n}")
        return "\n".join(lines)


def _avoid_round_number(price: float, direction: str) -> float:
    """把止损/止盈价从整数关口挪开一格。

    整数位（如 9.00、10.00）是散户挂单密集区，价格常在此处"扫一下"再反向；
    止损挂整数位等于主动送人头。规则：向下取整后 -0.01（止损）/ +0.01（止盈）。
    """
    p = round_price(price)
    if abs(p - round(p)) < 0.005:
        return round_price(p - 0.01 if direction == "down" else p + 0.01)
    return p


def stop_loss_price(entry_price: float, pct: float, code: str,
                    name: str = "") -> Optional[float]:
    """止损触发价：entry × (1 - pct%)，且**不低于跌停价、避开整数关口**。"""
    if not entry_price or entry_price <= 0 or pct <= 0:
        return None
    raw = round_price(entry_price * (1 - pct / 100.0))
    _, down, _lim = limit_prices(entry_price, code, name)
    if down and raw < down:
        # 跌破跌停价无法成交（当日价格到不了），挂在跌停价上方一格
        raw = round_price(down + 0.01)
    return _avoid_round_number(raw, "down")


def take_profit_price(entry_price: float, pct: float, code: str,
                      name: str = "") -> Optional[float]:
    """止盈触发价：entry × (1 + pct%)，避开整数关口。

    **不按涨停价裁剪**（早期实现错了，写在这里备忘）：

    涨停只约束**当日**的成交价，不约束"目标价"这个数字本身。条件单是给
    未来若干天挂的——主板 10% 涨停意味着 15% 的止盈目标**当天到不了，但
    第二个交易日就可能到**。若把目标价裁到涨停附近：
      ① 算出来的数字不是用户设的 15%，而是"某个被压扁的值"，无法核对；
      ② 再叠加整数关口避让，实测出现过「止盈价 10.99 < 现价 11.20」的荒唐
         结果，条件单一挂上就成交（用户以为在设止盈，实际是立即卖出）。

    正确职责划分：
      - 本函数只算**目标价**（entry × (1+pct%)，避让整数关口）；
      - "今天能不能到"由调用方用涨停价比对后**写成提示**，不改数字。
    """
    if not entry_price or entry_price <= 0 or pct <= 0:
        return None
    return _avoid_round_number(round_price(entry_price * (1 + pct / 100.0)), "up")


def reachable_today(target: Optional[float], price: float, code: str,
                    name: str = "") -> Tuple[bool, str]:
    """目标价今天是否可达（用当日涨停价比对），返回 (可达, 说明)。"""
    if not target or not price or price <= 0:
        return True, ""
    up, _down, _pct = limit_prices(price, code, name)
    if up and target > up:
        return False, (f"止盈价 {target:.2f} 高于当日涨停 {up:.2f}，"
                       f"今日不可达（次日仍有效，条件单可保留）")
    return True, ""


def build_buy_order(code: str, name: str, price: float, budget: float,
                    stop_loss_pct: float, take_profit_pct: float,
                    trailing_pct: Optional[float] = None,
                    cap_amount: Optional[float] = None,
                    fee_cfg: Optional[FeeConfig] = None) -> ConditionalOrder:
    """买入条件单：股数按资金与单票上限双重约束，附止损/止盈价。"""
    fee_cfg = fee_cfg or FeeConfig()
    board = board_of(code)
    limit_up, limit_down, _ = limit_prices(price, code, name)
    amount_budget = min(budget, cap_amount) if cap_amount else budget
    # 留 0.2% 给费用，避免"刚好买不起"
    raw_shares = (amount_budget / (price * 1.002)) if price else 0
    shares = round_lot(raw_shares, code)
    amount = round_price(price * shares)
    fee = calc_fee(amount, "buy", fee_cfg, board).total
    order = ConditionalOrder(
        code=code, name=name, side="buy", shares=shares, price=round_price(price),
        limit_up=limit_up, limit_down=limit_down,
        est_amount=amount, est_fee=fee)
    if shares <= 0:
        need = price * lot_size(code)
        order.notes.append(f"资金不足一手（一手约需 {need:,.0f} 元），无法给出买入数量")
        return order
    order.stop_loss = stop_loss_price(price, stop_loss_pct, code, name)
    order.take_profit = take_profit_price(price, take_profit_pct, code, name)
    order.trailing_pct = trailing_pct
    _add_common_notes(order, price, code, name, is_buy=True)
    return order


def build_sell_order(code: str, name: str, price: float, shares: int,
                     cost_price: float, stop_loss_pct: float,
                     take_profit_pct: float,
                     trailing_pct: Optional[float] = None,
                     fee_cfg: Optional[FeeConfig] = None) -> ConditionalOrder:
    """卖出条件单：数量受 T+1 可卖约束（调用方传入可卖数）。"""
    fee_cfg = fee_cfg or FeeConfig()
    board = board_of(code)
    limit_up, limit_down, _ = limit_prices(price, code, name)
    shares = max(0, int(shares))
    amount = round_price(price * shares)
    fee = calc_fee(amount, "sell", fee_cfg, board).total
    order = ConditionalOrder(
        code=code, name=name, side="sell", shares=shares, price=round_price(price),
        limit_up=limit_up, limit_down=limit_down,
        est_amount=amount, est_fee=fee)
    if shares <= 0:
        order.notes.append("今日可卖数量为 0（T+1 锁定），无需挂卖出单")
        return order
    # 持仓的止损/止盈以**成本价**为基准（判断赚亏是常识口径），
    # 但触发价必须相对**当前价**仍然有效，否则挂上去立即成交：
    #   - 止盈价已低于现价（已涨过止盈位）→ 条件单立刻触发，
    #     用户以为是"设置止盈"，实际是"立即卖出"；
    #   - 止损价已高于现价（已跌破止损位）→ 同样立刻触发。
    # 处理：已失效一侧不给触发价，改为明确提示（该人工决定怎么处理）。
    if cost_price > 0:
        sl = stop_loss_price(cost_price, stop_loss_pct, code, name)
        tp = take_profit_price(cost_price, take_profit_pct, code, name)
        if sl is not None and sl < price:
            order.stop_loss = sl
        elif sl is not None:
            order.notes.append(
                f"止损线 {sl:.2f} 已高于现价 {price:.2f}（早已跌破），"
                f"该条件单已无意义——需人工决定是否止损")
        if tp is not None and tp > price:
            order.take_profit = tp
        elif tp is not None:
            order.notes.append(
                f"止盈线 {tp:.2f} 已低于现价 {price:.2f}（已涨过止盈位），"
                f"此时挂到价卖出会立即成交——若想继续持有，请改挂更高止盈或跟踪回撤")
        order.trailing_pct = trailing_pct
        pnl_pct = (price - cost_price) / cost_price * 100.0
        order.notes.append(f"相对成本 {cost_price:.3f} 当前浮动 {pnl_pct:+.2f}%")
    _add_common_notes(order, price, code, name, is_buy=False)
    return order


def _add_common_notes(order: ConditionalOrder, price: float, code: str,
                      name: str, is_buy: bool) -> None:
    """补充"为什么要这么挂"的可核对说明。"""
    if order.stop_loss and price > 0:
        d = (order.stop_loss - price) / price * 100
        order.notes.append(f"止损距现价 {d:+.2f}%")
    if order.take_profit and price > 0:
        d = (order.take_profit - price) / price * 100
        order.notes.append(f"止盈距现价 {d:+.2f}%")
    if is_buy and order.shares and order.shares % lot_size(code) != 0:
        order.notes.append("数量不是整手，券商可能拒单")
