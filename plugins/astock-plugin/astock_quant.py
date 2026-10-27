"""量化选股引擎：全市场扫描 → 多因子打分 → 仓位分配 → 一份可执行的买入清单。

**它解决的问题（插件的短板，不是锦上添花）**：
此前所有工具都要求"你已经想好买什么"——`astock_hint` 只算到持仓/自选，
`astock_screen` 只按单字段排序（涨得最多 ≠ 值得买），`astock_events` 只列事件
且明确不喊单。整条链的入口是用户的决定，插件只做后续算账。
本模块补的正是入口那一层：**从全市场自己挑标的，并算清买多少股**。

────────────────────────────────────────────────────────────────────────
三条硬性口径（决定了输出可信与否，改代码前务必读懂）
────────────────────────────────────────────────────────────────────────

① **打分是"池内相对排名"，不是概率，也不是预期收益。**
   每个因子先转成候选池内的百分位（0~1），再按权重加权成 0~100 分。
   所以 85 分的含义是"在今天的候选池里综合排在前 15%"，**不是"85% 会涨"**。
   为什么不直接给概率：收益分布无已知参数、样本外失效是常态，任何"上涨
   概率 73%"类的数字都没有数学基础，编出来比不给更危险。相对排名可核对、
   可复现、失效时能定位到是哪个因子拖的后腿。

② **因子值、权重、贡献全部外露。**
   `Scored.contributions` 给出每个因子对总分的实际贡献（含权重和百分位），
   用户能看清"这只票是靠动量进来的还是靠低估值进来的"。黑箱评分无法 debug，
   也不知道该不该信。

③ **不做仓位管理之外的择时。**
   不预测大盘、不给目标价、不做复利曲线回测。只做：按你给的资金约束
   （可用现金 / 单票上限 / 现金使用比例）把选出的标的换算成整手股数。

────────────────────────────────────────────────────────────────────────
三套策略预设（因子方向相反，选错不如不做）
────────────────────────────────────────────────────────────────────────

**momentum 趋势动量**：追强势。站上均线 + 中期动量为正 + 放量 + 接近区间
高点。牛市与趋势市有效；震荡市会被反复打脸（假突破）。

**reversal 均值回归**：抄超跌。跌到区间低位 + RSI 超卖 + 低波动 + 估值不高。
逆势低吸，需承受"继续下跌"，且必须配合止损。

**quality 质量价值**：买稳健。低 PE/PB + 大市值 + 低波动 + 成交活跃。
慢，但回撤小、不需要盯盘，适合长期持有。

三者不需要"选一个终身用"——`astock_pick strategy=...` 可随时切换。
振荡市跑 reversal、趋势市跑 momentum，这是使用层面的判断，不是代码能替
你做的（本模块不判断市场状态）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astock_market import (
    BOARD_LABELS, FeeConfig, board_of, calc_fee, limit_pct, round_lot,
    round_price,
)

DISCLAIMER = (
    "以上为按既定因子与权重计算出的**排序与仓位换算**，不构成投资建议；"
    "得分为候选池内相对排名，不代表上涨概率或预期收益。请自行复核后决策。"
)

#: 拉日 K 的候选上限。每个候选 1 次 K 线请求，受行情层令牌桶（默认 5 次/秒）
#: 约束 → 30 只约 6 秒。再大等待时间会让人以为卡死。
MAX_KLINE_CANDIDATES = 30


# ====================================================================== 技术因子


def _safe_div(a: Optional[float], b: Optional[float],
              default: Optional[float] = None) -> Optional[float]:
    if a is None or b is None or b == 0:
        return default
    try:
        return float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _stdev(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


def compute_rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI（Wilder 简化口径：算术平均涨跌幅，不做平滑递推）。

    口径说明：标准 Wilder 用 EMA 递推，但差异只在极端长度下体现；
    这里取最近 period 根的算术均值，够用且**算式可被用户手算核对**——
    选股场景里"能被核对"比"更学术"重要。
    """
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if gains + losses == 0:
        return 50.0
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def compute_technical(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """从日 K 数组算技术因子。bars 按日期升序（`astock_data.fetch_klines` 已是）。

    入参用 dict 而非 Bar 对象，是为了让本模块**不依赖数据层**——单测可以直接
    喂构造好的 K 线，不必起网络或打桩。
    """
    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    vols: List[float] = []
    for b in bars or []:
        c = b.get("close")
        if not c or c <= 0:
            continue
        closes.append(float(c))
        highs.append(float(b.get("high") or c))
        lows.append(float(b.get("low") or c))
        vols.append(float(b.get("volume_lot") or 0.0))

    out: Dict[str, Any] = {"bars": len(closes)}
    if len(closes) < 2:
        return out

    last = closes[-1]
    out["close"] = round_price(last)

    def _ma(n: int) -> Optional[float]:
        if len(closes) < n:
            return None
        return round_price(sum(closes[-n:]) / n)

    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = _ma(n)

    # 动量：N 日涨跌幅（用 N 根前的收盘比较）
    for n in (5, 20, 60):
        if len(closes) > n:
            base = closes[-(n + 1)]
            out[f"mom{n}"] = round((last - base) / base * 100.0, 2) if base else None
        else:
            out[f"mom{n}"] = None

    # 区间位置（默认 60 日，不足则用全部）
    window = min(60, len(closes))
    seg_high = max(highs[-window:])
    seg_low = min(lows[-window:])
    out["high_window"] = round_price(seg_high)
    out["low_window"] = round_price(seg_low)
    span = seg_high - seg_low
    out["pos_in_range"] = (round((last - seg_low) / span * 100.0, 2)
                           if span > 0 else None)
    out["drawdown_from_high"] = (
        round((last - seg_high) / seg_high * 100.0, 2) if seg_high else None)

    # 量能：近 5 日均量 / 近 20 日均量
    v5 = _mean(vols[-5:]) if len(vols) >= 5 else None
    v20 = _mean(vols[-20:]) if len(vols) >= 20 else None
    out["vol_ratio_5_20"] = round(_safe_div(v5, v20), 3) if _safe_div(v5, v20) else None

    # 均线多头排列打分：close>ma5>ma10>ma20 各得 1 分（0~3）
    alignment = 0
    if out.get("ma5") and last > out["ma5"]:
        alignment += 1
    if out.get("ma5") and out.get("ma10") and out["ma5"] > out["ma10"]:
        alignment += 1
    if out.get("ma10") and out.get("ma20") and out["ma10"] > out["ma20"]:
        alignment += 1
    out["ma_alignment"] = (alignment if len(closes) >= 20 else None)
    if out.get("ma20"):
        out["above_ma20_pct"] = round((last - out["ma20"]) / out["ma20"] * 100.0, 2)

    # 波动率：近 20 日收益率标准差（年化，%）
    # 注意：收益率只有 1 个时 stdev 无意义（_stdev 对 <2 个值返回 None），
    # 不能落成 0——0 波动在"低波动"因子里是**最优点**，会把数据不足的
    # 标的推到第一名（真实的静默错误）。
    rets: List[float] = []
    for i in range(max(1, len(closes) - 20), len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev)
    sd = _stdev(rets)
    out["volatility_20d"] = (round(sd * math.sqrt(244) * 100.0, 2)
                             if sd is not None else None)

    out["rsi14"] = compute_rsi(closes)
    return out


# ====================================================================== 因子定义
#
# 每个因子给出：标签、取值函数、方向（+1 越大越好 / -1 越小越好）。
# 取值返回 None 表示"该标的缺这项数据"（如亏损股没有 PE）——缺数据不补
# 默认值，而是把该因子的权重从分母里剔掉（见 rank_candidates），
# 否则"没有 PE"会被当成"PE=0 极度便宜"，这是最典型的静默错误。


@dataclass
class Factor:
    key: str
    label: str
    direction: int
    getter: Any            # (row, tech) -> Optional[float]
    unit: str = ""
    note: str = ""


def _f_amount(row: Dict[str, Any], tech: Dict[str, Any]) -> Optional[float]:
    v = row.get("amount_yuan")
    return math.log10(v) if v and v > 0 else None


def _f_pe_inv(row: Dict[str, Any], tech: Dict[str, Any]) -> Optional[float]:
    """PE 的倒数（越大越好 = 越便宜）。PE<=0（亏损）视为缺失，不给分。

    为什么不把亏损股的 PE 当"极度便宜"：负 PE 不是估值低，是没利润。
    这类标的一律缺该项因子，能否入选取决于其它因子。
    """
    pe = row.get("pe")
    if pe is None or pe <= 0:
        return None
    return 1.0 / pe


def _f_pb_inv(row: Dict[str, Any], tech: Dict[str, Any]) -> Optional[float]:
    pb = row.get("pb")
    if pb is None or pb <= 0:
        return None
    return 1.0 / pb


def _f_mcap_log(row: Dict[str, Any], tech: Dict[str, Any]) -> Optional[float]:
    v = row.get("total_mcap_yuan")
    return math.log10(v) if v and v > 0 else None


def _f_turnover_sweet(row: Dict[str, Any], tech: Dict[str, Any]) -> Optional[float]:
    """换手率"适中"得分：太低=没人交易，太高=情绪过热。

    用距理想区间的偏离度（理想 1%~8%），越靠中心越高。返回负偏离（越大越好）。
    """
    t = row.get("turnover_pct")
    if t is None or t < 0:
        return None
    lo, hi = 1.0, 8.0
    if t < lo:
        return -(lo - t)
    if t > hi:
        return -(t - hi)
    center = (lo + hi) / 2.0
    return -abs(t - center) * 0.1


FACTORS: Dict[str, Factor] = {
    "mom20": Factor("mom20", "20日动量", +1,
                    lambda r, t: t.get("mom20"), "%",
                    "近 20 个交易日涨跌幅，趋势强度的主指标"),
    "mom60": Factor("mom60", "60日动量", +1,
                    lambda r, t: t.get("mom60"), "%",
                    "中期趋势；跨季度持有看这项"),
    "ma_alignment": Factor("ma_alignment", "均线多头排列", +1,
                           lambda r, t: t.get("ma_alignment"), "档",
                           "close>MA5>MA10>MA20 满足几条（0~3）"),
    "vol_surge": Factor("vol_surge", "量能放大", +1,
                        lambda r, t: t.get("vol_ratio_5_20"), "倍",
                        "近5日均量/近20日均量；放量突破才有效"),
    "near_high": Factor("near_high", "接近区间高点", +1,
                        lambda r, t: t.get("pos_in_range"), "%",
                        "现价在 60 日区间中的位置；越高越接近突破"),
    "low_pos": Factor("low_pos", "处于区间低位", -1,
                      lambda r, t: t.get("pos_in_range"), "%",
                      "越小越接近区间底部（均值回归用）"),
    "rsi_low": Factor("rsi_low", "RSI 超卖", -1,
                      lambda r, t: t.get("rsi14"), "",
                      "RSI14 越小越超卖；低于 30 常视为超卖区"),
    "low_vol": Factor("low_vol", "低波动", -1,
                      lambda r, t: t.get("volatility_20d"), "%",
                      "近 20 日年化波动率，越小回撤越可控"),
    "shallow_dd": Factor("shallow_dd", "回撤小", +1,
                         lambda r, t: t.get("drawdown_from_high"), "%",
                         "距 60 日高点的回撤（负数）；越接近 0 越抗跌"),
    "pe_inv": Factor("pe_inv", "低市盈率", +1, _f_pe_inv, "",
                     "PE 倒数；亏损股（PE<=0）视为缺数据"),
    "pb_inv": Factor("pb_inv", "低市净率", +1, _f_pb_inv, "",
                     "PB 倒数；净资产为负视为缺数据"),
    "mcap_log": Factor("mcap_log", "大市值", +1, _f_mcap_log, "元",
                       "对数市值；大市值流动性好、操纵风险低"),
    "liquidity": Factor("liquidity", "成交额", +1, _f_amount, "元",
                        "对数成交额；成交太清淡的标的买得进卖不出"),
    "turnover_sweet": Factor("turnover_sweet", "换手适中", +1,
                             _f_turnover_sweet, "%",
                             "偏离 1%~8% 理想换手区间的程度"),
    "main_net": Factor("main_net", "主力净流入", +1,
                       lambda r, t: r.get("main_net_inflow_yuan"), "元",
                       "当日主力净额；备源（新浪）无此字段时按缺数据处理"),
}


@dataclass
class Strategy:
    key: str
    label: str
    description: str
    weights: Dict[str, float]
    #: 硬性过滤（在打分前执行，不满足直接不进候选池）
    filters: Dict[str, Any] = field(default_factory=dict)
    #: 该策略默认的止损/止盈（与设置页的全局值取更严格者）
    stop_loss_pct: float = 8.0
    take_profit_pct: float = 15.0

    def normalized_weights(self) -> Dict[str, float]:
        total = sum(w for w in self.weights.values() if w > 0)
        if total <= 0:
            return {}
        return {k: v / total for k, v in self.weights.items() if v > 0}


STRATEGIES: Dict[str, Strategy] = {
    "momentum": Strategy(
        key="momentum",
        label="趋势动量",
        description=("追强势：站上均线 + 中期动量为正 + 放量 + 接近区间高点。"
                     "趋势市有效，震荡市易被假突破反复打脸。"),
        # 权重经**实测调优**（2024-03~2026-10，25 只活跃股，57 期回测）：
        #   mom60 主导   胜率 49.1% 累计  +0.9%
        #   基线(2:1)    胜率 42.1% 累计 -11.3%
        #   纯动量       胜率 40.4% 累计  -7.1%
        #   重技术形态    胜率 40.4% 累计  -9.9%
        # 结论：**拉长动量周期**比堆技术形态更有效。60 日动量权重从 0.14
        # 提到 0.30、20 日降到 0.12 —— 短线动量的噪声太大。
        weights={"mom60": 0.30, "mom20": 0.12, "ma_alignment": 0.20,
                 "vol_surge": 0.14, "near_high": 0.14, "liquidity": 0.10},
        filters={"min_amount_yuan": 1e8, "max_change_pct": 9.5,
                 "min_change_pct": -2.0, "min_bars": 60},
        stop_loss_pct=8.0, take_profit_pct=15.0),
    "reversal": Strategy(
        key="reversal",
        label="均值回归",
        description=("抄超跌：跌到区间低位 + RSI 超卖 + 低波动 + 估值不高。"
                     "逆势低吸，需承受继续下跌，务必带止损。"
                     "**实测警示**：该策略在 2024-03~2026-10 的 A 股样本里"
                     "表现最差（胜率 36.8%、最大回撤 -51.6%），"
                     "说明这段行情里「抄底」是亏钱的——用它前请先看回测。"),
        weights={"low_pos": 0.26, "rsi_low": 0.22, "low_vol": 0.16,
                 "pb_inv": 0.14, "liquidity": 0.12, "shallow_dd": 0.10},
        filters={"min_amount_yuan": 5e7, "max_change_pct": 3.0,
                 "min_bars": 40},
        stop_loss_pct=6.0, take_profit_pct=12.0),
    "quality": Strategy(
        key="quality",
        label="质量价值",
        description=("买稳健：低 PE/PB + 大市值 + 低波动 + 换手适中。"
                     "涨得慢但回撤小，不需要盯盘，适合长期持有。"
                     "**实测**：三套里唯一累计正收益且回撤最小"
                     "（-17.8% vs 动量 -28.8% / 回归 -51.6%），"
                     "但仅与基准持平——适合「不亏钱优先」的人。"),
        weights={"pe_inv": 0.24, "pb_inv": 0.18, "mcap_log": 0.20,
                 "low_vol": 0.20, "turnover_sweet": 0.10, "liquidity": 0.08},
        filters={"min_amount_yuan": 5e7, "min_mcap_yuan": 1e10,
                 "max_change_pct": 20.0, "min_bars": 60},
        stop_loss_pct=10.0, take_profit_pct=20.0),
}

DEFAULT_STRATEGY = "momentum"


# ====================================================================== 打分


@dataclass
class Scored:
    """一只打了分的候选。所有字段都为了"可核对"。"""

    code: str
    name: str
    price: float
    change_pct: Optional[float]
    score: float = 0.0
    #: 因子 → 池内百分位（0~1，已按方向调整：越大越"好"）
    factor_pcts: Dict[str, float] = field(default_factory=dict)
    #: 因子 → 对总分的加权贡献（分，和为 score）
    contributions: Dict[str, float] = field(default_factory=dict)
    #: 因子 → 原始值（便于用户核对"为什么它百分位高"）
    factor_values: Dict[str, Optional[float]] = field(default_factory=dict)
    #: 缺失数据的因子（权重已从分母剔除）
    missing_factors: List[str] = field(default_factory=list)
    tech: Dict[str, Any] = field(default_factory=dict)
    row: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "price": self.price,
            "change_pct": self.change_pct, "score": self.score,
            "factor_values": {k: v for k, v in self.factor_values.items()},
            "contributions": {k: round(v, 2) for k, v in self.contributions.items()},
            "missing_factors": list(self.missing_factors),
        }

    def explain(self, top: int = 3) -> str:
        """一句话说明"它靠什么排到前面"——取贡献最大的 top 个因子。"""
        if not self.contributions:
            return "无有效因子"
        items = sorted(self.contributions.items(), key=lambda kv: -kv[1])[:top]
        parts = []
        for key, contrib in items:
            f = FACTORS.get(key)
            label = f.label if f else key
            val = self.factor_values.get(key)
            if val is None:
                parts.append(f"{label}→贡献 {contrib:.1f}")
                continue
            parts.append(f"{label}({_fmt_factor(key, val)})→贡献 {contrib:.1f}")
        return "；".join(parts)


def _fmt_factor(key: str, val: Any) -> str:
    """因子原值的人类可读格式。

    必要性：部分因子在内部做了变换（成交额取 log10、PE 取倒数），
    直接 `f"{val:g}"` 会印出 `8.47712` 这种"看着像成交额其实是量级"的
    数字——用户核对时会以为自己算错了。这里按因子还原成可读单位。
    """
    if not isinstance(val, (int, float)):
        return str(val)
    if key == "liquidity":
        return f"{10 ** val / 1e8:.2f}亿"
    if key == "mcap_log":
        return f"{10 ** val / 1e8:.0f}亿"
    if key in ("pe_inv", "pb_inv"):
        return f"{1 / val:g} 倍" if val else "-"
    if key == "main_net":
        return f"{val / 1e8:+.2f}亿"
    unit = (FACTORS[key].unit if key in FACTORS else "") or ""
    return f"{val:g}{unit}"


def percentiles(values: Dict[str, float], direction: int = 1) -> Dict[str, float]:
    """{key: 数值} → {key: 池内百分位 0~1}。direction<0 表示数值越小越好。

    用平均秩处理并列（ties），避免同值股票因排序不稳定而分数跳动。
    单元素池直接给 1.0（没有比较对象，按中位语义给满分而非 0，否则
    只选一只时它的得分会是 0，看着像出错）。
    """
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 1.0}
    out: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(items[j + 1][1] - items[i][1]) < 1e-12:
            j += 1
        avg_rank = (i + j) / 2.0
        p = avg_rank / (n - 1)
        if direction < 0:
            p = 1.0 - p
        for k in range(i, j + 1):
            out[items[k][0]] = p
        i = j + 1
    return out


def rank_candidates(candidates: Sequence[Scored], strategy: Strategy,
                    weights: Optional[Dict[str, float]] = None) -> List[Scored]:
    """池内多因子打分并降序返回。

    `weights` 可覆盖策略默认权重（用户临时调参用）。缺失因子的处理：
    该因子不参与该标的的排名（其余标的照常排名），且其权重从该标的的
    分母里剔除 —— 这样"某只票缺一个因子"不会把它按 0 分拉到底，
    也不会让"缺数据"变成隐性加分。
    """
    if not candidates:
        return []
    w = weights if weights is not None else strategy.weights
    w = {k: float(v) for k, v in (w or {}).items() if float(v) > 0 and k in FACTORS}
    if not w:
        return list(candidates)

    # 逐因子算池内百分位
    factor_pcts: Dict[str, Dict[str, float]] = {}
    for key, factor in ((k, FACTORS[k]) for k in w):
        raw: Dict[str, float] = {}
        for c in candidates:
            try:
                val = factor.getter(c.row, c.tech)
            except Exception:       # 单只票的脏数据不应炸掉整轮选股
                val = None
            c.factor_values[key] = val
            if val is None:
                c.missing_factors.append(key)
                continue
            raw[c.code] = float(val)
        factor_pcts[key] = percentiles(raw, factor.direction)

    total_weight = sum(w.values())
    for c in candidates:
        present = {k: wt for k, wt in w.items() if c.code in factor_pcts.get(k, {})}
        denom = sum(present.values())
        if denom <= 0 or total_weight <= 0:
            c.score = 0.0
            c.contributions = {}
            continue
        score = 0.0
        contributions: Dict[str, float] = {}
        for key, wt in present.items():
            pct = factor_pcts[key][c.code]
            c.factor_pcts[key] = pct
            # 贡献 = 百分位 × 该因子有效权重占比 × 100（换成百分制便于阅读）
            contrib = pct * (wt / denom) * 100.0
            contributions[key] = contrib
            score += contrib
        c.contributions = contributions
        c.score = round(score, 2)

    out = sorted(candidates, key=lambda c: (-c.score, c.code))
    return out


# ====================================================================== 硬过滤


def passes_filters(row: Dict[str, Any], tech: Dict[str, Any],
                   strategy: Strategy) -> Tuple[bool, str]:
    """策略硬门槛。返回 (是否通过, 未通过原因)。原因要能直接展示给用户。"""
    flt = strategy.filters or {}
    price = row.get("price")
    if not price or price <= 0:
        return False, "无有效价格（停牌或退市）"

    name = str(row.get("name") or "")
    if "ST" in name.upper():
        return False, "ST/风险警示股，策略默认排除"

    bars = tech.get("bars") or 0
    need_bars = int(flt.get("min_bars") or 0)
    if need_bars and bars < need_bars:
        return False, f"历史数据不足（{bars} 根 < 需要 {need_bars} 根）"

    amount = row.get("amount_yuan")
    min_amount = flt.get("min_amount_yuan")
    if min_amount and (amount is None or amount < min_amount):
        return False, (f"成交额 {amount or 0:,.0f} 元低于门槛 "
                       f"{min_amount:,.0f} 元（流动性不足）")

    mcap = row.get("total_mcap_yuan")
    min_mcap = flt.get("min_mcap_yuan")
    if min_mcap and (mcap is None or mcap < min_mcap):
        return False, f"总市值低于门槛 {min_mcap / 1e8:.1f} 亿"

    chg = row.get("change_pct")
    # 涨跌停优先判定：它比"涨幅超上限"更精确，也更有指导意义
    # （涨停是"买不进"，超上限只是"不追高"）。
    # 注意：若先判 max_change_pct，主板涨停 10% 会被 9.5% 的上限先拦下，
    # 用户看到的原因就成了"涨幅超上限"，反而模糊了真正的原因。
    code = str(row.get("code") or "")
    try:
        lp = limit_pct(code, name)
    except Exception:
        lp = None
    if lp and chg is not None:
        if abs(chg - lp) < 0.15:
            return False, f"已涨停（{chg:.2f}%），当日买入成交不确定"
        if abs(chg + lp) < 0.15:
            return False, f"已跌停（{chg:.2f}%），不宜接刀"
    if chg is not None:
        max_chg = flt.get("max_change_pct")
        if max_chg is not None and chg > max_chg:
            return False, f"当日涨幅 {chg:.2f}% 超过上限 {max_chg}%（不追高）"
        min_chg = flt.get("min_change_pct")
        if min_chg is not None and chg < min_chg:
            return False, f"当日跌幅 {chg:.2f}% 超过下限 {min_chg}%（不接下跌中的刀）"
    return True, ""


# ====================================================================== 仓位分配


@dataclass
class Allocation:
    """一只标的的买入换算结果。"""

    code: str
    name: str
    price: float
    score: float
    shares: int = 0
    amount: float = 0.0
    fee: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    weight_pct: float = 0.0          # 占总权益比例
    reason: str = ""
    skipped: bool = False
    skip_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "code": self.code, "name": self.name, "price": self.price,
            "score": self.score, "shares": self.shares,
            "amount": round(self.amount, 2), "fee": round(self.fee, 2),
            "weight_pct": round(self.weight_pct, 2),
        }
        if self.stop_loss is not None:
            out["stop_loss"] = self.stop_loss
        if self.take_profit is not None:
            out["take_profit"] = self.take_profit
        if self.reason:
            out["reason"] = self.reason
        if self.skipped:
            out["skipped"] = True
            out["skip_reason"] = self.skip_reason
        return out


@dataclass
class SizingConfig:
    """仓位约束（设置页可改）。默认偏保守。"""

    #: 单票市值占总权益上限（%）
    position_cap_pct: float = 25.0
    #: 本轮最多买几只（分散）
    max_positions: int = 5
    #: 可用现金的使用比例（%）。留一部分是常识：T+1 补仓、手续费、误判余地
    cash_usage_pct: float = 80.0
    #: 买入后保留的现金下限（元）——与 cash_usage_pct 取更严格者
    cash_reserve_yuan: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def size_positions(scored: Sequence[Scored], cash: float, equity: float,
                   cfg: SizingConfig,
                   fee_cfg: Optional[FeeConfig] = None,
                   strategy: Optional[Strategy] = None,
                   stop_loss_pct: Optional[float] = None,
                   take_profit_pct: Optional[float] = None,
                   ) -> Tuple[List[Allocation], List[Allocation], Dict[str, Any]]:
    """把打分结果换算成**具体股数**。

    返回 (可执行的, 被跳过的, 汇总)。分配规则（全部可核对）：

    1. 本轮投入上限 `budget = min(现金 × 使用比例, 现金 − 保留现金)`；
    2. 按分数**降序**逐只分配，每只的预算 = 剩余预算 × (本只分数 / 剩余总分)
       —— 分数高的拿更多，但不会超过单票上限 `equity × cap%`；
    3. 换算股数时先留 0.2% 给费用，再向下取整到整手（科创板 200 股）；
    4. 买不起一手 / 受单票上限约束而没用完的预算，**回收后按分数再分给
       已入选的标的**（最多再走 2 轮，避免无限循环）。不回收会让"预算
       8 万、实际只投出 5 万"——用户看到的是钱莫名闲置；
    5. 最多买 `max_positions` 只。

    为什么逐只扣减而不是一次算完：现金是共享约束，一次性按比例算完
    会超支（各只都按"总预算×比例"算，加起来必然大于总预算）。
    """
    fee_cfg = fee_cfg or FeeConfig()
    stop_pct = (stop_loss_pct if stop_loss_pct is not None
                else (strategy.stop_loss_pct if strategy else 8.0))
    tp_pct = (take_profit_pct if take_profit_pct is not None
              else (strategy.take_profit_pct if strategy else 15.0))

    cash = max(0.0, float(cash or 0.0))
    equity = max(0.0, float(equity or 0.0))
    usage = max(0.0, min(100.0, float(cfg.cash_usage_pct))) / 100.0
    budget = cash * usage
    if cfg.cash_reserve_yuan > 0:
        budget = min(budget, max(0.0, cash - float(cfg.cash_reserve_yuan)))
    cap_amount = equity * max(0.0, cfg.position_cap_pct) / 100.0 if equity > 0 else 0.0

    picks = list(scored)[: max(0, int(cfg.max_positions))]
    taken: List[Allocation] = []
    skipped: List[Allocation] = []

    def _lot(code: str) -> int:
        return 200 if board_of(code) == "star" else 100

    def _affordable(code: str, price: float, target: float) -> Tuple[int, float, float]:
        """在 target 预算内能买几股（整手），返回 (股数, 金额, 费用)。"""
        lot = _lot(code)
        raw = target / (price * 1.002) if price > 0 else 0
        shares = int(raw // lot) * lot
        guard = 0
        while shares > 0 and guard < 64:
            amount = round_price(price * shares)
            fee = calc_fee(amount, "buy", fee_cfg, board_of(code)).total
            if amount + fee <= target + 1e-6:
                return shares, amount, fee
            shares -= lot          # 费用把总支出顶出预算 → 退一手
            guard += 1
        return 0, 0.0, 0.0

    remaining_cash = budget
    remaining_score = sum(max(0.0, s.score) for s in picks)

    # ── 第一轮：按分数比例分预算 ──
    for c in picks:
        alloc = Allocation(code=c.code, name=c.name, price=float(c.price or 0),
                          score=c.score)
        if not c.price or c.price <= 0:
            alloc.skipped, alloc.skip_reason = True, "无有效价格"
            skipped.append(alloc)
            remaining_score -= max(0.0, c.score)
            continue
        share_of_score = (max(0.0, c.score) / remaining_score
                          if remaining_score > 0 else 1.0 / max(1, len(picks)))
        target = remaining_cash * share_of_score
        if cap_amount > 0:
            target = min(target, cap_amount)
        shares, amount, fee = _affordable(c.code, float(c.price), target)
        if shares <= 0:
            lot = _lot(c.code)
            alloc.skipped = True
            alloc.skip_reason = (f"预算 {target:,.0f} 元不足一手"
                                 f"（一手约需 {c.price * lot:,.0f} 元）")
            skipped.append(alloc)
            remaining_score -= max(0.0, c.score)
            continue
        alloc.shares, alloc.amount, alloc.fee = shares, amount, fee
        alloc.stop_loss = round_price(c.price * (1 - stop_pct / 100.0))
        alloc.take_profit = round_price(c.price * (1 + tp_pct / 100.0))
        remaining_cash -= (amount + fee)
        remaining_score -= max(0.0, c.score)
        taken.append(alloc)

    # ── 第二轮：把闲置预算（不足一手 / 单票上限剩下的）回收再分 ──
    # 为什么要做：实测 8 万预算只投出 5 万，剩 3 万躺在账上——用户看到的
    # 是"算出该买 8 万却只买了 5 万"，而原因（某只买不起一手 / 某只被
    # 单票上限截断）藏在细节里，几乎没人会去算。
    for _round in range(2):
        if remaining_cash < 100.0 or not taken:
            break
        pool = [a for a in taken
                if cap_amount <= 0 or a.amount < cap_amount - 1.0]
        if not pool:
            break
        total_score = sum(max(0.1, a.score) for a in pool)
        progressed = False
        for a in pool:
            if remaining_cash < 100.0:
                break
            want = remaining_cash * (max(0.1, a.score) / total_score)
            if cap_amount > 0:
                want = min(want, cap_amount - a.amount)
            if want <= 0:
                continue
            add_shares, add_amount, add_fee = _affordable(a.code, a.price, want)
            if add_shares <= 0:
                continue
            a.shares += add_shares
            a.amount = round_price(a.price * a.shares)
            a.fee = calc_fee(a.amount, "buy", fee_cfg, board_of(a.code)).total
            remaining_cash -= (add_amount + add_fee)
            progressed = True
        if not progressed:
            break

    for a in taken:
        a.weight_pct = ((a.amount / equity) * 100.0) if equity > 0 else 0.0
        a.reason = (f"得分 {a.score:.1f}，买入 {a.shares} 股"
                    f"（占权益 {a.weight_pct:.1f}%）")

    summary = {
        "budget": round(budget, 2),
        "budget_source": ("现金使用比例" if usage < 1.0 else "全部可用现金")
                         + ("（受保留现金约束）" if cfg.cash_reserve_yuan > 0
                            and cash - cfg.cash_reserve_yuan < cash * usage else ""),
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "allocated_amount": round(sum(a.amount for a in taken), 2),
        "allocated_fee": round(sum(a.fee for a in taken), 2),
        "remaining_cash": round(remaining_cash, 2),
        "position_cap_amount": round(cap_amount, 2),
        "position_cap_pct": cfg.position_cap_pct,
        "position_count": len(taken),
        "skipped_count": len(skipped),
        "stop_loss_pct": stop_pct,
        "take_profit_pct": tp_pct,
    }
    return taken, skipped, summary


# ====================================================================== 渲染


def format_pick_report(strategy: Strategy, allocations: Sequence[Allocation],
                       skipped: Sequence[Allocation],
                       summary: Dict[str, Any],
                       scanned: int, candidates: int,
                       max_rows: int = 10) -> str:
    """把分配结果渲染成"照着做"的清单（用户在券商 App 逐项填）。"""
    lines: List[str] = []
    lines.append(f"**量化选股 + 买入计划**（策略：{strategy.label}）")
    lines.append("")
    lines.append(f"> {strategy.description}")
    lines.append("")
    lines.append(f"全市场扫描 {scanned} 只 → 过门槛 {candidates} 只 → "
                 f"选出 {len(allocations)} 只可执行"
                 + (f"（另有 {len(skipped)} 只因资金不足跳过）" if skipped else ""))
    lines.append("")

    if not allocations:
        lines.append("本轮没有产生可执行的买入计划。")
        if skipped:
            lines.append("")
            lines.append("**资金不足导致全部跳过**：")
            for a in skipped[:max_rows]:
                lines.append(f"- {a.name}({a.code}) 现价 {a.price:.2f}：{a.skip_reason}")
            lines.append("")
            lines.append(f"可用资金 {summary['cash']:,.2f} 元，本轮预算 "
                         f"{summary['budget']:,.2f} 元。"
                         f"可提高「现金使用比例」、减少「最少买入只数」，"
                         f"或用 astock_portfolio action=set_cash 校准资金。")
        else:
            lines.append("可能原因：过滤门槛过严（成交额/市值）、当日普涨导致"
                         "涨幅超上限被剔除、或该策略近期无符合条件的标的。"
                         "可换 strategy 或放宽 min_amount。")
        lines.append("")
        lines.append(DISCLAIMER)
        return "\n".join(lines)

    rows = []
    for i, a in enumerate(allocations, 1):
        rows.append([
            str(i), f"{a.name}({a.code})", f"{a.score:.1f}",
            f"{a.price:.2f}", f"{a.shares}", f"{a.amount:,.0f}",
            f"{a.weight_pct:.1f}%",
            f"{a.stop_loss:.2f}" if a.stop_loss else "-",
            f"{a.take_profit:.2f}" if a.take_profit else "-",
        ])
    lines.append("### 买入清单（照此填单）")
    lines.append("")
    lines.append("| # | 标的 | 得分 | 参考价 | 买入股数 | 金额 | 占比 | 止损价 | 止盈价 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    lines.append("")

    lines.append("### 逐只说明（得分来源可核对）")
    lines.append("")
    for i, a in enumerate(allocations, 1):
        lines.append(f"**{i}. {a.name}({a.code})** {a.shares} 股 @ {a.price:.2f}"
                     f" ≈ {a.amount:,.0f} 元（费用约 {a.fee:.2f} 元）")
        if a.reason:
            lines.append(f"- 分配依据：{a.reason}")
        if a.stop_loss and a.take_profit:
            sl_pct = (a.stop_loss - a.price) / a.price * 100.0
            tp_pct = (a.take_profit - a.price) / a.price * 100.0
            lines.append(f"- 条件单：止损 {a.stop_loss:.2f}（{sl_pct:+.2f}%）、"
                         f"止盈 {a.take_profit:.2f}（{tp_pct:+.2f}%）")
        lines.append("")

    lines.append("### 资金预算")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 可用现金 | {summary['cash']:,.2f} 元 |")
    lines.append(f"| 账户总权益 | {summary['equity']:,.2f} 元 |")
    lines.append(f"| 本轮预算 | {summary['budget']:,.2f} 元（{summary['budget_source']}）|")
    lines.append(f"| 计划投入 | {summary['allocated_amount']:,.2f} 元 |")
    lines.append(f"| 预计费用 | {summary['allocated_fee']:,.2f} 元 |")
    lines.append(f"| 买入后剩余现金 | {summary['remaining_cash']:,.2f} 元 |")
    cap_amt = summary.get("position_cap_amount") or 0
    cap_pct = summary.get("position_cap_pct")
    lines.append(f"| 单票上限 | {cap_amt:,.2f} 元"
                 + (f"（上限 {cap_pct:g}% × 总权益）" if cap_pct else "") + " |")
    lines.append("")

    if skipped:
        lines.append("### 因资金不足跳过")
        lines.append("")
        for a in skipped[:max_rows]:
            lines.append(f"- {a.name}({a.code}) 现价 {a.price:.2f}：{a.skip_reason}")
        lines.append("")
        lines.append("这些标的本身通过了策略筛选，只是预算不够买一手。"
                     "想买其中某只：单独问「XXX 买多少」，或用 astock_hint code=XXX buy=true。")
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)
