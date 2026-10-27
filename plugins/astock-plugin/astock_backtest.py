"""回测引擎：把选股策略放回历史里跑，得出**有数学基础的推荐率**。

**它回答的问题**：`astock_quant` 给出的得分只是"今天谁排前面"，可它到底靠不
靠谱？本模块把同一套因子放到历史时点上重放，统计"按这套策略选出来的组合，
在之后 N 天里跑赢基准的比例是多少、平均赚多少、最差会亏多少"。

这份统计就是界面上的**推荐率**——它来自历史数据，不是主观打分。

═══════════════════════════════════════════════════════════════════════
必须先说清的三个限制（不写清楚，回测结果就会被当成承诺）
═══════════════════════════════════════════════════════════════════════

① **历史深度只有约 2.6 年（641 根日 K，接口硬上限）**。
   这**不足以覆盖一个完整牛熊周期**。所以回测结论只能说"在这段时间里有效"，
   不能说"长期有效"。任何号称长期胜率更高的说法，都必须先有更长的数据。

② **幸存者偏差（survivorship bias）——本模块无法消除**。
   能取到历史的股票，是**活到今天**的股票；这 2.6 年里退市的、被并购的、
   长期停牌的一律不在样本里。这会**系统性高估**收益（活得下来的本来就更好）。
   处理方式不是假装没有，而是：
     · 结论一律用**相对基准**（沪深300）表述，绝对收益少谈；
     · 报告里固定标注偏差方向，让用户知道真实值比测出来的更差；
     · 同时给出**最差一档**（最大回撤、最差单期），而不是只报平均。

③ **quality 策略的估值因子无法回测**。
   PE/PB 只有"当下快照"，没有历史序列，所以 `quality` 策略在本模块里会
   **缺 pe_inv/pb_inv 两项**，只用市值/波动/换手/流动性回放。报告里会明说
   "该策略回测不完整"，绝不拿残缺结果当完整结论。

═══════════════════════════════════════════════════════════════════════
防未来函数（这是回测可信与否的分水岭）
═══════════════════════════════════════════════════════════════════════

逐调仓日回放，每个时点**只喂入该日及之前的数据**（`bars[:i+1]` 的尾部窗口），
因子、打分、选股全在该截面上完成，买入价用**次日开盘**（不是当日收盘——
当日收盘时你还没下单）。若不小心用了当日收盘价买入，回测收益会明显虚高，
这是最常见的自欺方式。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import astock_quant as quant

#: 回测所需最少历史根数（算因子要 60 根 + 留出持有期）
MIN_BARS = 90
#: 基准代码（沪深300），用于算超额收益
BENCHMARK_CODE = "000300"


@dataclass
class BacktestConfig:
    """回测参数。默认值偏保守（调仓间隔长、持有期适中）。"""

    #: 每隔多少交易日调仓一次（10 ≈ 两周）
    rebalance_days: int = 10
    #: 每期买入几只（与实盘 max_positions 对齐更有可比性）
    top_k: int = 3
    #: 持有多少交易日（= 调仓间隔时为连续持仓；更短则中间空仓）
    hold_days: int = 10
    #: 单边费用率（佣金+印花税+滑点粗估）。来回一次约 2×。
    cost_rate: float = 0.0015
    #: 打分用的最小窗口（算 60 日均线等）
    lookback: int = 130

    def as_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class PeriodResult:
    """一期调仓的结果（可逐条核对）。"""

    date: str                 # 调仓日（信号日）
    entry_date: str           # 实际买入日（信号日的下一交易日）
    picks: List[str]          # 代码
    names: List[str]
    gross_return_pct: float   # 组合毛收益（%）
    net_return_pct: float     # 扣费后（%）
    benchmark_pct: float      # 同期基准（%）
    excess_pct: float         # 超额（net - benchmark）
    beat: bool                # 是否跑赢基准

    def as_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date, "entry_date": self.entry_date,
            "picks": list(self.picks), "names": list(self.names),
            "net_return_pct": round(self.net_return_pct, 2),
            "benchmark_pct": round(self.benchmark_pct, 2),
            "excess_pct": round(self.excess_pct, 2),
            "beat": self.beat,
        }


@dataclass
class BacktestResult:
    """一次回测的完整结论。所有字段都可展示给用户核对。"""

    strategy: str
    strategy_label: str
    start_date: str = ""
    end_date: str = ""
    #: 实际参与回测的股票数（受历史数据可得性限制）
    universe_size: int = 0
    periods: List[PeriodResult] = field(default_factory=list)
    #: 累计净值曲线（起点 1.0）
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    benchmark_curve: List[Tuple[str, float]] = field(default_factory=list)
    #: 因历史不足/取数失败被排除的股票数
    excluded: int = 0
    #: 策略里因缺历史数据而无法回测的因子
    unavailable_factors: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    # ---- 汇总指标（properties 里算，避免构造时重复遍历） ----

    @property
    def period_count(self) -> int:
        return len(self.periods)

    @property
    def win_rate(self) -> Optional[float]:
        """**推荐率**：跑赢基准的期数占比（%）。

        这是给用户看的核心数字，含义必须精确：
        「历史上按此策略选股，N 期里有 M 期跑赢沪深300」——
        不是"每只股票上涨的概率"。
        """
        if not self.periods:
            return None
        return round(sum(1 for p in self.periods if p.beat)
                     / len(self.periods) * 100.0, 1)

    @property
    def avg_return_pct(self) -> Optional[float]:
        if not self.periods:
            return None
        return round(sum(p.net_return_pct for p in self.periods)
                     / len(self.periods), 2)

    @property
    def avg_excess_pct(self) -> Optional[float]:
        if not self.periods:
            return None
        return round(sum(p.excess_pct for p in self.periods)
                     / len(self.periods), 2)

    @property
    def total_return_pct(self) -> Optional[float]:
        """累计净值收益（%）——按期复利。"""
        if not self.periods:
            return None
        nav = 1.0
        for p in self.periods:
            nav *= (1 + p.net_return_pct / 100.0)
        return round((nav - 1) * 100.0, 2)

    @property
    def benchmark_return_pct(self) -> Optional[float]:
        if not self.periods:
            return None
        nav = 1.0
        for p in self.periods:
            nav *= (1 + p.benchmark_pct / 100.0)
        return round((nav - 1) * 100.0, 2)

    @property
    def max_drawdown_pct(self) -> Optional[float]:
        """最大回撤（%，负数）。必报——只看平均会严重低估风险。"""
        if not self.equity_curve:
            return None
        peak = self.equity_curve[0][1]
        worst = 0.0
        for _d, v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                worst = min(worst, (v - peak) / peak * 100.0)
        return round(worst, 2)

    @property
    def best_period_pct(self) -> Optional[float]:
        if not self.periods:
            return None
        return round(max(p.net_return_pct for p in self.periods), 2)

    @property
    def worst_period_pct(self) -> Optional[float]:
        if not self.periods:
            return None
        return round(min(p.net_return_pct for p in self.periods), 2)

    @property
    def complete(self) -> bool:
        """回测是否完整（无因子缺失）。不完整时的结论要打折看。"""
        return not self.unavailable_factors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_label": self.strategy_label,
            "start_date": self.start_date, "end_date": self.end_date,
            "universe_size": self.universe_size,
            "period_count": self.period_count,
            "win_rate": self.win_rate,
            "avg_return_pct": self.avg_return_pct,
            "avg_excess_pct": self.avg_excess_pct,
            "total_return_pct": self.total_return_pct,
            "benchmark_return_pct": self.benchmark_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "best_period_pct": self.best_period_pct,
            "worst_period_pct": self.worst_period_pct,
            "complete": self.complete,
            "unavailable_factors": list(self.unavailable_factors),
            "excluded": self.excluded,
            "config": dict(self.config),
            "note": self.note,
        }


# ====================================================================== 工具


def _window_factors(bars: Sequence[Dict[str, Any]], idx: int,
                    lookback: int) -> Optional[Dict[str, Any]]:
    """取**截止 idx（含）**的尾部窗口算因子 —— 防未来函数的关键。

    绝不传 `bars[idx:]` 或全序列：那会让因子看到未来价格，
    回测收益随即虚高，且从结果上看不出来。
    """
    start = max(0, idx - lookback + 1)
    seg = list(bars[start: idx + 1])
    if len(seg) < 60:
        return None
    tech = quant.compute_technical(seg)
    return tech if tech.get("bars", 0) >= 60 else None


def _cross_section_score(cands: List[quant.Scored],
                         strategy: quant.Strategy,
                         weights: Optional[Dict[str, float]]) -> List[quant.Scored]:
    """在单个历史截面上打分（复用实盘的同一套函数，保证口径一致）。"""
    return quant.rank_candidates(cands, strategy, weights=weights)


def _equal_weight_return(picks: List[Dict[str, Any]], entry_idx: int,
                         exit_idx: int) -> Optional[float]:
    """等权组合从 entry 开盘到 exit 收盘的毛收益（%）。"""
    rets: List[float] = []
    for p in picks:
        bars = p["bars"]
        if entry_idx >= len(bars) or exit_idx >= len(bars):
            return None
        entry = bars[entry_idx].get("open") or bars[entry_idx].get("close")
        exit_ = bars[exit_idx].get("close")
        if not entry or not exit_ or entry <= 0:
            return None
        rets.append((exit_ - entry) / entry)
    if not rets:
        return None
    return sum(rets) / len(rets) * 100.0


def _benchmark_return(bench: Sequence[Dict[str, Any]],
                      entry_idx: int, exit_idx: int) -> float:
    if not bench or entry_idx >= len(bench) or exit_idx >= len(bench):
        return 0.0
    entry = bench[entry_idx].get("open") or bench[entry_idx].get("close")
    exit_ = bench[exit_idx].get("close")
    if not entry or not exit_ or entry <= 0:
        return 0.0
    return (exit_ - entry) / entry * 100.0


# ====================================================================== 回测主流程


def run_backtest(strategy_key: str, universe: Sequence[Dict[str, Any]],
                 benchmark: Sequence[Dict[str, Any]],
                 cfg: Optional[BacktestConfig] = None,
                 weights: Optional[Dict[str, float]] = None,
                 ) -> BacktestResult:
    """滚动回测。同步执行，调用方负责取数（用 to_thread 包）。

    `universe` 每项形如：
        {"code": "600000", "name": "浦发银行", "bars": [{"date","open","close",...}]}
    `benchmark` 为基准的 bars 数组（沪深300 日 K）。

    时间轴对齐方式：按**个股日期**推进（不同股票停牌日不同，硬对齐会产生
    虚假收益）。基准按日期字典查找，找不到该日就用最近的前一个交易日。
    """
    cfg = cfg or BacktestConfig()
    strat = quant.STRATEGIES.get(strategy_key)
    if strat is None:
        raise ValueError(f"未知策略 {strategy_key}")

    # 策略里哪些因子在回测中不可得（quality 的估值因子没有历史序列）
    unavailable: List[str] = []
    if strategy_key == "quality":
        unavailable = [f for f in ("pe_inv", "pb_inv") if f in strat.weights]

    # 只保留历史足够长的标的
    pool: List[Dict[str, Any]] = []
    excluded = 0
    for item in universe:
        bars = item.get("bars") or []
        if len(bars) < MIN_BARS:
            excluded += 1
            continue
        pool.append(item)
    if len(pool) < cfg.top_k:
        raise ValueError(
            f"可用历史数据的标的不足（{len(pool)} 只 < 需要 {cfg.top_k} 只），"
            f"无法回测。可减少每期买入只数，或放宽选股范围")

    # 基准日期索引（用于日期对齐）
    bench_by_date = {b["date"]: i for i, b in enumerate(benchmark or [])}
    bench_dates = [b["date"] for b in benchmark or []]

    # 以"最短的那只股票"决定共同可回测区间，避免某只新股拉长时间轴后
    # 前期只有 1 只股票参与打分（横截面太窄，打分无意义）
    common_len = min(len(p["bars"]) for p in pool)
    if common_len < MIN_BARS:
        raise ValueError("共同历史区间过短，无法回测")

    start_idx = 60                      # 留足算 60 日均线
    end_idx = common_len - cfg.hold_days - 1
    if end_idx <= start_idx:
        raise ValueError("历史区间不足以完成至少一期回测")

    result = BacktestResult(
        strategy=strat.key, strategy_label=strat.label,
        universe_size=len(pool), excluded=excluded,
        unavailable_factors=unavailable, config=cfg.as_dict())

    period_idx = list(range(start_idx, end_idx + 1, max(1, cfg.rebalance_days)))
    nav = 1.0
    bench_nav = 1.0
    result.equity_curve.append(("start", 1.0))
    result.benchmark_curve.append(("start", 1.0))

    for i in period_idx:
        signal_date = pool[0]["bars"][i]["date"]
        # ① 只用截止 i 的数据算因子（i 为信号日）
        cands: List[quant.Scored] = []
        for item in pool:
            bars = item["bars"]
            if i >= len(bars):
                continue
            tech = _window_factors(bars, i, cfg.lookback)
            if not tech:
                continue
            price = bars[i].get("close") or tech.get("close") or 0
            if not price:
                continue
            row = {"code": item["code"], "name": item.get("name") or item["code"],
                   "price": price, "change_pct": None,
                   "amount_yuan": (bars[i].get("volume_lot") or 0) * price * 100,
                   "total_mcap_yuan": None, "pe": None, "pb": None,
                   "turnover_pct": None, "main_net_inflow_yuan": None}
            cands.append(quant.Scored(
                code=item["code"], name=item.get("name") or item["code"],
                price=price, change_pct=None, tech=tech, row=row))
        if len(cands) < cfg.top_k:
            continue
        ranked = _cross_section_score(cands, strat, weights)
        picks = ranked[: cfg.top_k]

        # ② 次日开盘买入（信号日收盘时还没下单），持有 hold_days 后收盘卖出
        entry_idx = i + 1
        exit_idx = min(entry_idx + cfg.hold_days - 1, common_len - 1)
        if exit_idx <= entry_idx:
            continue
        pick_items = []
        for s in picks:
            item = next((x for x in pool if x["code"] == s.code), None)
            if item:
                pick_items.append(item)
        if len(pick_items) < cfg.top_k:
            continue
        gross = _equal_weight_return(pick_items, entry_idx, exit_idx)
        if gross is None:
            continue
        # 双边费用：买入 + 卖出各一次
        net = gross - cfg.cost_rate * 100.0 * 2

        exit_date = pool[0]["bars"][min(exit_idx, len(pool[0]["bars"]) - 1)]["date"]
        # 基准按同一对日期取（找不到就退到最近的前一交易日）
        b_entry = bench_by_date.get(pool[0]["bars"][entry_idx]["date"])
        b_exit = bench_by_date.get(exit_date)
        if b_entry is None and bench_dates:
            b_entry = next((bench_by_date[d] for d in reversed(bench_dates)
                            if d <= pool[0]["bars"][entry_idx]["date"]), None)
        if b_exit is None and bench_dates:
            b_exit = next((bench_by_date[d] for d in reversed(bench_dates)
                           if d <= exit_date), None)
        bench_ret = (_benchmark_return(benchmark, b_entry, b_exit)
                     if b_entry is not None and b_exit is not None else 0.0)

        excess = net - bench_ret
        result.periods.append(PeriodResult(
            date=signal_date,
            entry_date=pool[0]["bars"][entry_idx]["date"],
            picks=[s.code for s in picks],
            names=[s.name for s in picks],
            gross_return_pct=round(gross, 2),
            net_return_pct=round(net, 2),
            benchmark_pct=round(bench_ret, 2),
            excess_pct=round(excess, 2),
            beat=excess > 0))

        nav *= (1 + net / 100.0)
        bench_nav *= (1 + bench_ret / 100.0)
        result.equity_curve.append((exit_date, round(nav, 4)))
        result.benchmark_curve.append((exit_date, round(bench_nav, 4)))

    if not result.periods:
        raise ValueError("回测未产生任何有效区间（样本过少），请换策略或放宽范围")

    result.start_date = result.periods[0].entry_date
    result.end_date = result.periods[-1].date
    result.note = _build_note(result)
    return result


def _build_note(result: BacktestResult) -> str:
    bits = [f"样本 {result.period_count} 期、{result.universe_size} 只标的，"
            f"{result.start_date} 起。"]
    bits.append("含幸存者偏差（只含活到今天的股票），真实表现会略差于此处。")
    if result.unavailable_factors:
        names = "、".join(sorted(result.unavailable_factors))
        bits.append(f"**该策略回测不完整**：{names} 缺历史数据未参与，"
                    f"结论需打折看。")
    bits.append("历史不代表未来。")
    return "".join(bits)


# ====================================================================== 推荐率


@dataclass
class Recommendation:
    """推荐结论：把回测统计翻译成"傻瓜也能懂"的一档结论。

    **关键设计**：`grade` 是分档（A/B/C/D），`win_rate` 是**历史胜率数字**，
    两者都附带 `sample`（样本期数）。为什么必须带样本数：
    "胜率 100%" 若只有 2 期样本，那和没有结论一样——不标样本数就是误导。
    """

    grade: str                     # A / B / C / D
    label: str                     # 推荐 / 可考虑 / 谨慎 / 不建议
    win_rate: Optional[float]      # 历史跑赢基准的期数占比（%）
    sample: int                    # 样本期数
    avg_excess_pct: Optional[float]
    max_drawdown_pct: Optional[float]
    reason: str = ""
    caveats: List[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """样本可信度：期数太少时明确说"参考价值有限"。"""
        if self.sample >= 24:
            return "较充分"
        if self.sample >= 12:
            return "一般"
        if self.sample >= 5:
            return "偏少"
        return "过少"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "grade": self.grade, "label": self.label,
            "win_rate": self.win_rate, "sample": self.sample,
            "confidence": self.confidence,
            "avg_excess_pct": self.avg_excess_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "reason": self.reason, "caveats": list(self.caveats),
        }


#: 分档阈值（胜率 %，样本期数下限）
GRADE_RULES: Tuple[Tuple[str, str, float, int], ...] = (
    ("A", "推荐", 65.0, 12),
    ("B", "可考虑", 50.0, 8),
    ("C", "谨慎", 35.0, 8),
    ("D", "不建议", 0.0, 0),
)


def grade_recommendation(bt: BacktestResult) -> Recommendation:
    """回测统计 → 推荐分档。

    分档逻辑（全部可核对，不含主观成分）：
      A 推荐    ：胜率 ≥65% 且样本 ≥12 期
      B 可考虑  ：胜率 ≥50% 且样本 ≥8 期
      C 谨慎    ：胜率 ≥35% 且样本 ≥8 期
      D 不建议  ：其余（含样本太少无法判定）

    样本不足时**一律降到 D 并说明原因**，而不是"因为好数据不足所以按 B 算"。
    宁可说"测不准"，也不能给一个看起来不错的假结论。
    """
    wr = bt.win_rate
    n = bt.period_count
    caveats: List[str] = []
    if bt.unavailable_factors:
        caveats.append(f"策略不完整（{len(bt.unavailable_factors)} 个因子缺历史数据）")
    if bt.excluded:
        caveats.append(f"{bt.excluded} 只标的因历史不足被排除")
    caveats.append("含幸存者偏差，真实表现会略差")

    if wr is None or n == 0:
        return Recommendation(
            grade="D", label="不建议", win_rate=None, sample=0,
            avg_excess_pct=None, max_drawdown_pct=None,
            reason="没有可用的历史样本，无法给出推荐", caveats=caveats)

    if n < 8:
        return Recommendation(
            grade="D", label="样本不足", win_rate=wr, sample=n,
            avg_excess_pct=bt.avg_excess_pct,
            max_drawdown_pct=bt.max_drawdown_pct,
            reason=f"仅有 {n} 期历史样本，统计上不足以支撑结论"
                   f"（至少需要 8 期）", caveats=caveats)

    for grade, label, min_wr, min_n in GRADE_RULES:
        if wr >= min_wr and n >= min_n:
            reason = (f"历史 {n} 期中 {int(round(wr / 100.0 * n))} 期跑赢基准"
                      f"（{wr:.1f}%），平均超额 {bt.avg_excess_pct:+.2f}%")
            return Recommendation(
                grade=grade, label=label, win_rate=wr, sample=n,
                avg_excess_pct=bt.avg_excess_pct,
                max_drawdown_pct=bt.max_drawdown_pct,
                reason=reason, caveats=caveats)

    return Recommendation(
        grade="D", label="不建议", win_rate=wr, sample=n,
        avg_excess_pct=bt.avg_excess_pct,
        max_drawdown_pct=bt.max_drawdown_pct,
        reason=f"历史胜率仅 {wr:.1f}%，低于 35% 的最低门槛", caveats=caveats)


# ====================================================================== 渲染


def render_backtest(bt: BacktestResult, rec: Recommendation,
                    detail_periods: int = 0) -> str:
    """回测报告（聊天里看）。先给结论，再给证据，最后给限制。"""
    lines: List[str] = []
    mark = {"A": "🟢", "B": "🔵", "C": "🟠", "D": "🔴"}.get(rec.grade, "⚪")
    lines.append(f"## {mark} 回测结论：{bt.strategy_label} → "
                 f"**{rec.label}（{rec.grade} 档）**")
    lines.append("")
    lines.append(f"> {rec.reason}")
    lines.append("")

    if rec.win_rate is not None:
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        lines.append(f"| **推荐率（跑赢基准的期数占比）** | **{rec.win_rate:.1f}%**"
                     f"（{int(round(rec.win_rate / 100 * rec.sample))}/{rec.sample} 期）|")
        lines.append(f"| 样本可信度 | {rec.confidence}（{rec.sample} 期）|")
        if bt.avg_return_pct is not None:
            lines.append(f"| 平均每期收益（扣费） | {bt.avg_return_pct:+.2f}% |")
        if bt.avg_excess_pct is not None:
            lines.append(f"| 平均每期超额 | {bt.avg_excess_pct:+.2f}% |")
        if bt.total_return_pct is not None:
            lines.append(f"| 累计收益（策略 / 基准） | "
                         f"{bt.total_return_pct:+.2f}% / "
                         f"{bt.benchmark_return_pct:+.2f}% |")
        if bt.max_drawdown_pct is not None:
            lines.append(f"| 最大回撤 | {bt.max_drawdown_pct:.2f}% |")
        if bt.best_period_pct is not None:
            lines.append(f"| 最好 / 最差单期 | {bt.best_period_pct:+.2f}% / "
                         f"{bt.worst_period_pct:+.2f}% |")
        lines.append(f"| 回测区间 | {bt.start_date} ~ {bt.end_date} |")
        lines.append(f"| 参与标的 | {bt.universe_size} 只 |")
        lines.append("")

    if rec.caveats:
        lines.append("### 必须知道的前提")
        lines.append("")
        for c in rec.caveats:
            lines.append(f"- {c}")
        lines.append("")
        lines.append(f"_{bt.note}_")
        lines.append("")

    if detail_periods > 0 and bt.periods:
        lines.append("### 逐期明细（可核对每一期选了什么、赚了多少）")
        lines.append("")
        lines.append("| 调仓日 | 买入 | 收益 | 基准 | 超额 |")
        lines.append("|---|---|---|---|---|")
        for p in bt.periods[-detail_periods:]:
            names = "、".join(p.names[:3])
            mark2 = "✅" if p.beat else "❌"
            lines.append(f"| {p.date} | {names} | {p.net_return_pct:+.2f}% | "
                         f"{p.benchmark_pct:+.2f}% | {mark2} {p.excess_pct:+.2f}% |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("**怎么用这个结论**：推荐率是「历史上按此策略选股，有多少期跑赢"
                 "沪深300」，**不是**某只股票上涨的概率。它衡量的是**策略**，"
                 "不是单只标的。样本期数越少，这个数字越不可靠——"
                 "所以每档都标了样本数。")
    lines.append("")
    lines.append("历史不代表未来，回测不构成投资建议。")
    return "\n".join(lines)


def build_universe_plan(candidates: Sequence[Dict[str, Any]],
                        benchmark_bars: Sequence[Dict[str, Any]],
                        max_symbols: int = 25) -> Dict[str, Any]:
    """准备回测输入（供工具层调用，便于单测替换取数）。

    说明：回测股票池取**当前成交额靠前的 N 只**，这是本模块最大的偏差来源
    ——今天活跃的股票，在 2.6 年前未必活跃。这个偏差无法用现有接口修复，
    只能在报告里明确告知。
    """
    picked = list(candidates)[:max_symbols]
    return {"pool": picked, "benchmark": benchmark_bars}
