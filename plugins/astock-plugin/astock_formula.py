"""技术公式库（纯函数 · 零网络 · 零 IO · 零第三方依赖）· v2.4.0 新增。

────────────────────────────────────────────────────────────────────────
为什么单独成文件
────────────────────────────────────────────────────────────────────────
这些公式此前散落三处，且**每次要在对话里手算或临时脚本重算**：

  · `astock_quant.compute_technical` 里内联了 `_ma` / RSI / 波动率
  · `astock_kline` 工具里又内联了一份 `ma(n)`
  · 临时排查（如"规则价该不该改"）在 /tmp 写脚本现算，算完删掉

后果实测过三次：
  1. **规则价漂移没人算**：11.55 挂了三天，实际 MA10 已漂到 11.593
  2. **复权口径搞错**：格力 08-27 除权（10 派 20），用不复权价算 MA60
     虚高约 0.9 元 → 验证线差点设成 38.60（应为 38.545）
  3. **方向写反无人发现**：格力 38.60 配 `lt`，但价格 38.52 本就在下方
     → 一条**永远不响的死规则**，界面看不出来、也不报错

共性都是：**算一遍就丢，下次重新算，每次都可能有新错**。
把它们固化成纯函数 + 单测，才有唯一口径。

────────────────────────────────────────────────────────────────────────
口径总则（每条函数都遵守，违反即为 bug）
────────────────────────────────────────────────────────────────────────
1. **缺数据返回 None，绝不用 0 代替**——0 在"低波动""低估值"这类因子里
   是**最优点**，会把数据不足的标的推到第一名（静默错误）。
2. **价格 round 2**（A 股最小变动 0.01），**均线/比率 round 4**。
   均线必须 4 位：11.593 截成 11.59 会让"漂移是否 >1%"的判定失真。
3. **序列一律按日期升序**，`[-1]` 是最新一根。
4. **均线看复权（qfq），成交价看原始（raw），两者不可混用**。
   见 `ADJUST_POLICY` 与 `dividend_adjust_factor`。
5. **不预测**：本模块只做统计，任何函数都不产出"会不会涨"。

────────────────────────────────────────────────────────────────────────
在插件里的位置
────────────────────────────────────────────────────────────────────────
    纯函数层（本文件）
        ↑ 被 astock_calc_tool（取数 + 渲染）与 astock_quant（因子）复用
        ↑ 不 import 数据层，单测可直接喂构造好的数组
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ====================================================================== 常量

#: A 股一年交易日数（年化换算用；不是日历日 365）
TRADING_DAYS_PER_YEAR = 244

#: 均线漂移告警阈值（%）。规则价与当日均线偏离超过它就该改价。
MA_DRIFT_THRESHOLD_PCT = 1.0

#: 复权口径策略（唯一权威说法，避免各处各写一套）
ADJUST_POLICY: Dict[str, str] = {
    "均线": "qfq（前复权）——除权会让长周期均线虚高，必须复权",
    "止损/止盈价": "raw（不复权）——你实际申报的就是这个价",
    "成交价/成本": "raw（不复权）——券商回单是原始价",
    "区间高低点": "qfq——跨除权比较才有意义",
    "回测": "qfq——否则除权缺口会被当成暴跌",
}


def adjust_for(purpose: str) -> str:
    """按用途给出该用的复权口径。未知用途默认 qfq（宁可复权）。"""
    return "qfq" if "qfq" in ADJUST_POLICY.get(purpose, "qfq") else "raw"


# ====================================================================== 基础统计


def mean(values: Sequence[Optional[float]]) -> Optional[float]:
    """算术平均。跳过 None；全空返回 None（不是 0）。"""
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(values: Sequence[Optional[float]], sample: bool = True
          ) -> Optional[float]:
    """标准差。样本（n-1）默认，<2 个值返回 None。

    **为什么 <2 返回 None**：单点没有离散度可言，返回 0 会让"低波动"
    因子把标的推到最优——这是 `astock_quant` 注释里明确记过的坑。
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n < (2 if sample else 1):
        return None
    m = sum(vals) / n
    denom = (n - 1) if sample else n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / denom)


def safe_div(a: Optional[float], b: Optional[float],
             default: Optional[float] = None) -> Optional[float]:
    """除法，b 为 None/0 时返回 default（默认 None）。"""
    if a is None or b is None:
        return default
    try:
        b = float(b)
        if b == 0:
            return default
        return float(a) / b
    except (TypeError, ValueError, ZeroDivisionError):
        return default


# ====================================================================== 均线


def round_price(p: float) -> float:
    """A 股报价最小变动单位 0.01 元（+1e-9 抵消浮点尾差）。"""
    return round(float(p) + 1e-9, 2)


def sma(closes: Sequence[float], n: int, *, digits: int = 4) -> Optional[float]:
    """简单移动平均（最近 n 根）。不足 n 根返回 None。

    **为什么是 SMA 不是 EMA**：国债/银行/红利这类慢标的用 SMA 足够，
    且**算式能被用户手算核对**——选股场景里"可核对"比"更学术"重要
    （同 `astock_quant.compute_rsi` 的口径说明）。
    """
    if n <= 0:
        return None
    vals = [float(c) for c in closes if c]
    if len(vals) < n:
        return None
    return round(sum(vals[-n:]) / n, digits)


def ma_set(closes: Sequence[float], periods: Sequence[int] = (5, 10, 20, 60)
           ) -> Dict[str, Optional[float]]:
    """一次算多周期均线。键名 `ma5`/`ma10`/…，缺数据为 None。"""
    return {f"ma{n}": sma(closes, n) for n in periods}


def deviation_pct(price: float, baseline: Optional[float],
                  *, digits: int = 2) -> Optional[float]:
    """现价相对基准线（均线/成本）的偏离百分比。正＝在上方。"""
    if baseline is None or not baseline:
        return None
    return round((float(price) - float(baseline)) / float(baseline) * 100.0,
                 digits)


def ma_position(price: float, mas: Dict[str, Optional[float]]) -> str:
    """一句话描述现价在均线群里的位置（上方/下方各几条）。

    用途：`astock_kline` 与盘后复盘的结论行，避免每次自己手算偏离。
    """
    above, below = [], []
    for key in ("ma5", "ma10", "ma20", "ma60"):
        v = mas.get(key)
        if v is None:
            continue
        label = key.upper()
        (above if price > v else below).append(label)
    if not above and not below:
        return "均线数据不足"
    if not below:
        return f"在 {'/'.join(above)} 全部上方（强势）"
    if not above:
        return f"在 {'/'.join(below)} 全部下方（弱势）"
    return f"上方 {'/'.join(above)}；下方 {'/'.join(below)}"


def alignment_score(price: float, mas: Dict[str, Optional[float]]) -> Optional[int]:
    """均线多头排列打分 0~3：close>ma5、ma5>ma10、ma10>ma20 各得 1 分。"""
    m5, m10, m20 = mas.get("ma5"), mas.get("ma10"), mas.get("ma20")
    if m5 is None or m10 is None or m20 is None:
        return None
    score = 0
    if price > m5:
        score += 1
    if m5 > m10:
        score += 1
    if m10 > m20:
        score += 1
    return score


# ====================================================================== 复权


def dividend_adjust_factor(prev_close: float, dividend_per_share: float,
                           *, digits: int = 6) -> float:
    """除息后的价格调整因子（每股派现口径）。

    前复权价 = 原始价 × factor（对除权日之前的每一根都乘）。

    **这个因子的实际用途**：判断"不复权算出的长周期均线会虚高多少"。
    格力 08-27「10 派 20 元」→ 每股 2 元，除权前收 41.58：
        factor = (41.58 - 2) / 41.58 = 0.9519
    即除权前的价格要**打折 4.8%** 才能与今天可比；不打折直接算 MA60，
    结果会虚高约 0.9 元——这就是验证线差点设错的原因。
    """
    if not prev_close or prev_close <= 0:
        return 1.0
    if dividend_per_share is None or dividend_per_share < 0:
        return 1.0
    return round((prev_close - dividend_per_share) / prev_close, digits)


def ma_distortion_estimate(prev_close: float, dividend_per_share: float,
                           n: int, *, digits: int = 3) -> Optional[float]:
    """估算"不复权"算 n 日均线会虚高多少百分比（近似）。

    假设除权日距今天数 < n（即均线窗口内包含除权前后的价格），
    虚高幅度约 = (1 - factor) × (窗口内除权前根数 / n)。

    返回负数表示**不复权均线比真实值高**（即现价看起来"离均线更远"）。
    这是近似值，用途是**提醒"该用复权了"**，不是精确换算。
    """
    factor = dividend_adjust_factor(prev_close, dividend_per_share)
    if factor >= 1.0:
        return None
    return round(-(1.0 - factor) * 100.0, digits)


# ====================================================================== 动量 / 波动


def momentum_pct(closes: Sequence[float], n: int, *, digits: int = 2
                 ) -> Optional[float]:
    """N 日动量（%）：用 n 根前的收盘比较。"""
    vals = [float(c) for c in closes if c]
    if len(vals) <= n:
        return None
    base = vals[-(n + 1)]
    if not base:
        return None
    return round((vals[-1] - base) / base * 100.0, digits)


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI（Wilder 简化口径：最近 period 根涨跌幅的算术均值，不做 EMA 递推）。

    与 `astock_quant.compute_rsi` 口径一致（那边被 quant 复用，这里独立
    实现是为了让本模块零依赖——两者数值应完全相同，单测里对拍）。
    """
    vals = [float(c) for c in closes if c]
    if len(vals) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = vals[i] - vals[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if gains + losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def volatility_annual_pct(closes: Sequence[float], window: int = 20,
                          *, digits: int = 2) -> Optional[float]:
    """年化波动率（%）：近 window 日收益率标准差 × √244。

    **为什么年化**：日波动 2% 听起来温和，年化是 31%——不同标的要可比
    必须换算到同一时间尺度。
    """
    vals = [float(c) for c in closes if c]
    if len(vals) < 2:
        return None
    rets: List[float] = []
    start = max(1, len(vals) - window)
    for i in range(start, len(vals)):
        prev = vals[i - 1]
        if prev:
            rets.append((vals[i] - prev) / prev)
    sd = stdev(rets)
    if sd is None:
        return None
    return round(sd * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0, digits)


def annualize_pct(total_return_pct: float, days: int, *, digits: int = 2
                  ) -> Optional[float]:
    """把区间总收益换算成年化（单利口径，供粗略比较）。

    **为什么单利而非复利**：持仓窗口往往只有几天到几周，复利年化在
    短期会产生荒谬的大数（3 天 +1% 复利年化 = +240%），误导性强。
    """
    if not days or days <= 0:
        return None
    return round(float(total_return_pct) * (TRADING_DAYS_PER_YEAR / days),
                 digits)


# ====================================================================== 回撤 / 位置


def max_drawdown(values: Sequence[float]) -> Optional[Dict[str, Any]]:
    """最大回撤。返回 dict：pct（负数）/ peak_idx / trough_idx / peak / trough。

    传净值序列或权益曲线均可；**不要传价格序列来代表策略回撤**
    （价格没考虑仓位与费用）。`astock_backtest` 用的是权益曲线。
    """
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    peak = vals[0]
    peak_i = 0
    worst = 0.0
    wi = wj = 0
    for i, v in enumerate(vals):
        if v > peak:
            peak, peak_i = v, i
        if peak > 0:
            dd = (v - peak) / peak * 100.0
            if dd < worst:
                worst, wi, wj = dd, peak_i, i
    return {
        "pct": round(worst, 2),
        "peak_idx": wi,
        "trough_idx": wj,
        "peak": round(vals[wi], 4) if vals else None,
        "trough": round(vals[wj], 4) if vals else None,
    }


def pos_in_range_pct(price: float, low: float, high: float,
                     *, digits: int = 2) -> Optional[float]:
    """现价在区间中的位置（0=最低，100=最高）。

    判"是否接近区间高点"用；趋势动量因子依赖它。
    """
    if not high or high <= low:
        return None
    return round((float(price) - float(low)) / (float(high) - float(low))
                 * 100.0, digits)


def drawdown_from_high_pct(price: float, high: float, *, digits: int = 2
                           ) -> Optional[float]:
    """距区间高点的回撤（%，负数）。越接近 0 越抗跌。"""
    if not high:
        return None
    return round((float(price) - float(high)) / float(high) * 100.0, digits)


def risk_adjusted_ratio(total_return_pct: Optional[float],
                        max_dd_pct: Optional[float], *, digits: int = 2
                        ) -> Optional[float]:
    """收益/回撤比（类 Calmar，非年化）。**选策略时先看这个再看收益。**

    实测：momentum 累计 +98.33% 但回撤 -50.05% → 1.96；
         reversal 累计 +85.28% 但回撤 -17.75% → 4.80。
    收益差 2 个点、风险差 3 倍——**回撤小的那套风险调整后好 2.4 倍**。

    回撤为 0 或缺失时返回 None（不能除以 0 说"无穷好"）。
    """
    if total_return_pct is None or max_dd_pct is None:
        return None
    dd = abs(float(max_dd_pct))
    if dd <= 0:
        return None
    return round(float(total_return_pct) / dd, digits)


# ====================================================================== 成本 / 盈亏


def weighted_avg_cost(old_shares: int, old_cost: float,
                      add_shares: int, add_price: float,
                      fee: float = 0.0, *, digits: int = 4) -> Optional[float]:
    """移动加权平均成本（买入时用；**卖出不改成本，只改股数**）。

    fee 计入总支出（买入手续费摊进成本，这是券商 App 的口径）。
    卖出方向不要调本函数——卖出只把 `(卖价-成本)*股数-费用` 计入已实现。
    """
    if add_shares <= 0:
        return None
    old_total = max(0, int(old_shares)) * float(old_cost)
    new_total = int(add_shares) * float(add_price) + float(fee or 0.0)
    total_shares = max(0, int(old_shares)) + int(add_shares)
    if total_shares <= 0:
        return None
    return round((old_total + new_total) / total_shares, digits)


def breakeven_price(buy_price: float, shares: int, buy_fee: float = 0.0,
                    sell_fee_rate: float = 0.0, *, digits: int = 4
                    ) -> Optional[float]:
    """盈亏平衡价（卖掉不赚不亏的价）。

    口径：`(买入总支出) / (股数 × (1 - 卖出费率))`
    卖出费率一般 = 佣金率 + 印花税 0.05% + 过户费 0.001%。

    **为什么要算它**：很多人以为"涨回成本价就回本"，实际还要覆盖来回
    手续费——10 元买 100 股，成本 10.00，实际平衡价约 10.02。
    """
    if shares <= 0 or not buy_price:
        return None
    cost_total = float(buy_price) * int(shares) + float(buy_fee or 0.0)
    rate = float(sell_fee_rate or 0.0)
    if rate >= 1:
        return None
    return round(cost_total / (int(shares) * (1 - rate)), digits)


def stop_take_prices(cost: float, stop_pct: float, take_pct: float,
                     *, digits: int = 2) -> Dict[str, Optional[float]]:
    """按成本价推算止损价与止盈价（价格 round 到 0.01）。"""
    if not cost:
        return {"stop": None, "take": None}
    return {
        "stop": round_price(float(cost) * (1 - abs(float(stop_pct)) / 100.0)),
        "take": round_price(float(cost) * (1 + abs(float(take_pct)) / 100.0)),
    }


def risk_reward_ratio(entry: float, stop: float, target: float,
                      *, digits: int = 2) -> Optional[float]:
    """盈亏比 = (目标-入场) / (入场-止损)。<1 意味着赚得比亏得少。"""
    risk = float(entry) - float(stop)
    if risk <= 0:
        return None
    return round((float(target) - float(entry)) / risk, digits)


def position_shares(cash: float, price: float, *,
                    equity: Optional[float] = None,
                    max_position_pct: Optional[float] = None,
                    lot: int = 100,
                    cash_usage_pct: float = 100.0,
                    digits: int = 0) -> Dict[str, Any]:
    """按现金 / 单票上限换算可买股数（向下取整到整手）。

    三道约束取最小：
      1. 可用现金 × cash_usage_pct
      2. 单票上限 = equity × max_position_pct（给了才约束）
      3. 整手（lot，科创板 200、其余 100）

    返回 dict：shares / amount / 受哪个约束限制 / 剩余现金。

    **为什么不直接 cash/price**：不取整手买不了；不设单票上限会在单一
    标的上压满仓——这两条都是"能算出来但执行不了"的数。
    """
    if price is None or float(price) <= 0:
        return {"shares": 0, "amount": 0.0, "binding": "价格非法",
                "cash_left": round(float(cash or 0), 2)}
    p = float(price)
    budget = max(0.0, float(cash or 0)) * min(100.0, max(0.0, cash_usage_pct)) / 100.0
    binding = "可用现金"
    if equity and max_position_pct:
        cap = float(equity) * float(max_position_pct) / 100.0
        if cap < budget:
            budget, binding = cap, "单票上限"
    if lot <= 0:
        lot = 100
    shares = int(budget // (p * lot)) * lot
    amount = round(shares * p, 2)
    return {
        "shares": shares,
        "amount": amount,
        "binding": binding if shares > 0 else f"{binding}不足一手",
        "cash_left": round(float(cash or 0) - amount, 2),
    }


# ====================================================================== 规则体检
#
# 这一组是 v2.4.0 的直接动机：三条规则在真实盯盘里**静默失效**过，
# 全是"算一遍就丢、没人回头验"导致的。把它们写成函数，就能每天跑一遍。


#: 操作符 → (允许的触发方向说明, 有效待触发时现价应处的区间)
_OP_SIDE = {
    "lt": ("跌破", "above"),     # 现价必须在规则价**上方**，否则已破/永不破
    "le": ("跌破或触及", "above"),
    "gt": ("站上", "below"),     # 现价必须在规则价**下方**
    "ge": ("站上或触及", "below"),
}


def check_rule_direction(price: float, rule_price: float, op: str
                         ) -> Dict[str, Any]:
    """校验价位规则的**方向是否可达**，并给出距离。

    返回：ok（是否有效待触发）/ side（above/below/flat）/ distance_pct /
          reason（人话说明）。

    ── 为什么必须有这个函数 ──
    真实事故：格力配了「跌破 38.60」(lt)，但当天收盘 38.52——**已在下方**。
    这条规则要么立即误触发一次、要么被当天去重吃掉，此后永不生效。
    界面看不出、日志不报错，**只有把现价代进去算才知道它是死的**。

    ok=True 表示"还没触发，等着就有意义"；ok=False 表示"方向已失效"。
    """
    if price is None or rule_price is None:
        return {"ok": False, "side": "unknown", "distance_pct": None,
                "reason": "价格或规则价缺失"}
    op = str(op or "lt").lower()
    if op not in _OP_SIDE:
        return {"ok": False, "side": "unknown", "distance_pct": None,
                "reason": f"未知操作符 {op!r}（只支持 lt/le/gt/ge）"}
    p, r = float(price), float(rule_price)
    side = "flat" if abs(p - r) < 1e-9 else ("above" if p > r else "below")
    want = _OP_SIDE[op][1]
    distance = round((p - r) / r * 100.0, 2) if r else None
    if side == "flat":
        return {"ok": False, "side": side, "distance_pct": 0.0,
                "reason": f"现价与规则价相同（{r}），方向已到位，规则随时触发一次"}
    if side == want:
        return {"ok": True, "side": side, "distance_pct": distance,
                "reason": (f"有效：现价 {p} 在规则价 {r} "
                           f"{'上方' if side == 'above' else '下方'}，"
                           f"距触发 {abs(distance):.2f}%")}
    return {"ok": False, "side": side, "distance_pct": distance,
            "reason": (f"⚠️ 方向已失效：{_OP_SIDE[op][0]} {r} 需要现价在"
                       f"{'上方' if want == 'above' else '下方'}，"
                       f"实际 {p} 已在{'下方' if side == 'below' else '上方'}——"
                       f"这条规则不会按预期生效，应改 op 或改价")}


def check_ma_drift(rule_price: float, ma_value: Optional[float],
                   threshold_pct: float = MA_DRIFT_THRESHOLD_PCT
                   ) -> Dict[str, Any]:
    """校验规则价相对当日均线是否漂移过多（默认 1%）。

    返回：drift_pct / need_update / suggest（建议改成多少）/ reason。

    均线每天在动，规则价是写死的——**不重算就会慢慢失效**：
    天山减仓线原挂 11.55（当时的 MA10），三天后 MA10 已到 11.593，
    规则价偏低 0.37%，等于要**多跌一点才触发**（保护变弱）。
    """
    if ma_value is None or not ma_value:
        return {"drift_pct": None, "need_update": False, "suggest": None,
                "reason": "均线数据不足，无法校验"}
    r, m = float(rule_price), float(ma_value)
    drift = round((r - m) / m * 100.0, 2)
    need = abs(drift) > abs(float(threshold_pct))
    suggest = round_price(m) if need else None
    if need:
        reason = (f"⚠️ 需更新：规则价 {r} vs 均线 {m}，偏差 {drift:+.2f}%"
                  f"（阈值 ±{threshold_pct}%）→ 建议改为 {suggest}")
    else:
        reason = (f"可保留：规则价 {r} vs 均线 {m}，偏差 {drift:+.2f}%"
                  f"（阈值 ±{threshold_pct}%）")
    return {"drift_pct": drift, "need_update": need, "suggest": suggest,
            "reason": reason}


def check_rule_set(rules: Sequence[Dict[str, Any]],
                   quotes: Dict[str, float],
                   ma_map: Optional[Dict[str, Optional[float]]] = None
                   ) -> List[Dict[str, Any]]:
    """批量体检一组价位规则，返回每条的问题清单（无问题也返回一行 ok）。

    rules: [{code, price, op, label?, ma_key?}]
    quotes: {code: 现价}
    ma_map: {code: 当日均线值}（可选，给了才做漂移检查）

    这是「每日收盘后规则体检」的可执行版本：方向 + 漂移两项一次跑完。
    **回读只能发现"丢了"，发现不了"写反了"——必须把价格代进去算。**
    """
    out: List[Dict[str, Any]] = []
    for r in rules or []:
        code = str(r.get("code") or "")
        price = quotes.get(code)
        direction = check_rule_direction(price, r.get("price"), r.get("op"))
        row: Dict[str, Any] = {
            "code": code,
            "label": r.get("label") or "",
            "rule_price": r.get("price"),
            "op": r.get("op"),
            "price": price,
            "direction_ok": direction["ok"],
            "distance_pct": direction["distance_pct"],
            "problems": [],
        }
        if not direction["ok"]:
            row["problems"].append(direction["reason"])
        if ma_map:
            ma_key = r.get("ma_key") or code
            drift = check_ma_drift(r.get("price"), ma_map.get(ma_key))
            row["drift_pct"] = drift["drift_pct"]
            row["suggest_price"] = drift["suggest"]
            if drift["need_update"]:
                row["problems"].append(drift["reason"])
        row["ok"] = not row["problems"]
        out.append(row)
    return out


# ====================================================================== 汇总


def summary(closes: Sequence[float], price: Optional[float] = None
            ) -> Dict[str, Any]:
    """一次算全套常用指标（供 astock_calc 直接输出）。"""
    vals = [float(c) for c in closes if c]
    if not vals:
        return {"bars": 0}
    last = float(price) if price is not None else vals[-1]
    mas = ma_set(vals)
    out: Dict[str, Any] = {
        "bars": len(vals),
        "close": round_price(last),
        **mas,
        "dev_ma5_pct": deviation_pct(last, mas.get("ma5")),
        "dev_ma10_pct": deviation_pct(last, mas.get("ma10")),
        "dev_ma20_pct": deviation_pct(last, mas.get("ma20")),
        "dev_ma60_pct": deviation_pct(last, mas.get("ma60")),
        "ma_position": ma_position(last, mas),
        "alignment": alignment_score(last, mas),
        "rsi14": rsi(vals),
        "volatility_20d": volatility_annual_pct(vals),
        "mom5": momentum_pct(vals, 5),
        "mom20": momentum_pct(vals, 20),
        "mom60": momentum_pct(vals, 60),
        "max_drawdown": max_drawdown(vals),
    }
    window = min(60, len(vals))
    seg_high, seg_low = max(vals[-window:]), min(vals[-window:])
    out["high_window"] = round_price(seg_high)
    out["low_window"] = round_price(seg_low)
    out["pos_in_range"] = pos_in_range_pct(last, seg_low, seg_high)
    out["drawdown_from_high"] = drawdown_from_high_pct(last, seg_high)
    return out
