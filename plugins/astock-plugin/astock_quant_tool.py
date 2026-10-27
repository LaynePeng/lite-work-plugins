"""量化选股的工具层：把 `astock_quant` 的纯计算接到行情/台账/面板上。

**为什么单独一层**（而不是塞进 plugin.py）：
`astock_quant` 是纯函数（给数据就出结果、无网络、无状态），本模块负责三件
"脏活"——取数、缓存、渲染。分开后单测可以只测前者的算法，不必打桩网络。

────────────────────────────────────────────────────────────────────────
缓存策略（面板每 5 秒刷新，选股要拉 30 只 K 线 ≈ 6 秒）
────────────────────────────────────────────────────────────────────────

**绝不在面板渲染路径上跑全市场扫描**。一次扫描 = 1 次全市场快照 + 最多 30 次
K 线请求，受令牌桶限速约 6 秒；面板 5 秒一刷，同步跑会把 UI 拖死、还会
把公共行情接口打到限流。

正确做法（本模块实现）：
  ① 面板只读**缓存**（`pick_cached`），有就渲染、没有就提示"点一下生成"；
  ② 缓存过期由**显式调用**（工具调用 / 设置页按钮 / 后台任务）刷新；
  ③ 每次扫描结果落台账 `pick_runs` 表 → 刷新页面后仍能看到上一轮结论，
     不用每次重算（也便于事后回看"当时按什么价位给的计划"）。

────────────────────────────────────────────────────────────────────────
不做什么
────────────────────────────────────────────────────────────────────────
不自动交易、不盯盘自动下单、不做回测。选股结果是"候选清单 + 股数换算"，
最终由用户在券商 App 下单——这与插件既有定位（不接任何委托通道）一致。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import astock_data as data
import astock_market as mkt
import astock_quant as quant

logger = logging.getLogger("litework.plugins.astock.quant")


#: 扫描结果的进程内缓存（key → (时间戳, 结果)）。
#: 按 strategy 分桶，切策略时各看各的，互不覆盖。
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
#: 缓存有效期（秒）。盘中 3 分钟够用：选股是日线级决策，不该每分钟重算。
CACHE_TTL = 180.0


def clear_cache() -> None:
    _CACHE.clear()


def cache_info() -> Dict[str, Any]:
    now = time.time()
    return {k: {"age_sec": round(now - ts, 1), "fresh": (now - ts) <= CACHE_TTL}
            for k, (ts, _v) in _CACHE.items()}


# ====================================================================== 扫描


def scan(strategy_key: str, *, cash: float, equity: float,
         universe: str = "main",
         sizing: Optional[quant.SizingConfig] = None,
         weights: Optional[Dict[str, float]] = None,
         candidate_limit: int = quant.MAX_KLINE_CANDIDATES,
         prefilter_limit: int = 300,
         fee_cfg: Optional[mkt.FeeConfig] = None,
         use_cache: bool = True) -> Dict[str, Any]:
    """跑一轮完整选股。同步实现（调用方用 asyncio.to_thread 包）。

    流程与耗时：
      1. 全市场快照粗筛（1 次请求，按成交额降序取前 prefilter_limit 只）
      2. 前 candidate_limit 只拉日 K 算技术因子（受限速，约每只 1/5 秒）
      3. 硬门槛过滤 → 多因子池内打分 → 仓位分配

    `universe` 默认 main（沪深主板+中小板+创业板），避开北交所的低流动性。
    """
    strat = quant.STRATEGIES.get(strategy_key)
    if strat is None:
        raise ValueError(f"未知策略 {strategy_key}；可选："
                         f"{'、'.join(quant.STRATEGIES)}")
    sizing = sizing or quant.SizingConfig()

    cache_key = (f"{strat.key}|{universe}|{candidate_limit}|"
                 f"{sizing.max_positions}|{sizing.position_cap_pct}|"
                 f"{sizing.cash_usage_pct}|{round(cash, 2)}|{round(equity, 2)}|"
                 f"{hash(tuple(sorted((weights or {}).items())))}")
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) <= CACHE_TTL:
            out = dict(hit[1])
            out["cached"] = True
            out["cache_age_sec"] = round(time.time() - hit[0], 1)
            return out

    # ── ① 全市场快照（按成交额降序 = 先看流动性好的） ──
    snap = data.screen(universe=universe, sort_by="amount", limit=prefilter_limit,
                       exclude_st=True)
    rows: List[Dict[str, Any]] = list(snap.get("rows") or [])
    scanned = int(snap.get("universe_total") or 0)

    if not rows:
        raise data.DataError("全市场快照返回空（接口可能限流），请稍后重试")

    # ── ② 逐只算技术因子 ──
    scored_all: List[quant.Scored] = []
    rejected: List[Dict[str, str]] = []
    fetch_failed = 0
    for row in rows[:max(1, candidate_limit)]:
        code = str(row.get("code") or "")
        if not code:
            continue
        try:
            bars = data.fetch_klines(code, period="day", limit=120)
            tech = quant.compute_technical([b.as_dict() for b in bars])
        except data.DataError as exc:
            fetch_failed += 1
            logger.debug("[astock] %s K线获取失败: %s", code, exc)
            continue
        ok, why = quant.passes_filters(row, tech, strat)
        if not ok:
            rejected.append({"code": code, "name": str(row.get("name") or ""),
                             "reason": why})
            continue
        scored_all.append(quant.Scored(
            code=code, name=str(row.get("name") or code),
            price=float(row.get("price") or tech.get("close") or 0),
            change_pct=row.get("change_pct"), tech=tech, row=row))

    ranked = quant.rank_candidates(scored_all, strat, weights=weights)

    # ── ③ 仓位分配 ──
    taken, skipped, summary = quant.size_positions(
        ranked, cash=cash, equity=equity, cfg=sizing, fee_cfg=fee_cfg,
        strategy=strat)

    result: Dict[str, Any] = {
        "strategy": strat.key,
        "strategy_label": strat.label,
        "strategy_description": strat.description,
        "scanned": scanned,
        "prefiltered": len(rows),
        "candidates": len(scored_all),
        "fetch_failed": fetch_failed,
        "ranked": ranked,
        "top": ranked[:10],
        "allocations": taken,
        "skipped": skipped,
        "summary": summary,
        "rejected_sample": rejected[:8],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cached": False,
        "cache_age_sec": 0.0,
        "universe": universe,
    }
    _CACHE[cache_key] = (time.time(), result)
    return result


def pick_cached(strategy_key: str) -> Optional[Dict[str, Any]]:
    """只取缓存（供面板用）。不触发扫描 —— 面板刷新绝不能打网络。"""
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for key, (ts, val) in _CACHE.items():
        if not key.startswith(f"{strategy_key}|"):
            continue
        if best is None or ts > best[0]:
            best = (ts, val)
    if best is None:
        return None
    out = dict(best[1])
    out["cache_age_sec"] = round(time.time() - best[0], 1)
    out["cached"] = True
    return out


# ====================================================================== 渲染


def render_scan(result: Dict[str, Any], max_rows: int = 10) -> str:
    """把一次扫描结果渲染成聊天里的报告（含股数与止损止盈）。"""
    strat = quant.STRATEGIES.get(result["strategy"]) or quant.STRATEGIES[
        quant.DEFAULT_STRATEGY]
    text = quant.format_pick_report(
        strat,
        result["allocations"], result["skipped"], result["summary"],
        scanned=result["scanned"], candidates=result["candidates"],
        max_rows=max_rows)
    notes: List[str] = []
    if result.get("cached") and result.get("cache_age_sec"):
        notes.append(f"（结果取自 {result['cache_age_sec']:.0f} 秒前的缓存；"
                     f"要最新的一轮加 fresh=true）")
    if result.get("fetch_failed"):
        notes.append(f"（{result['fetch_failed']} 只 K 线拉取失败已跳过）")
    if notes:
        text += "\n\n" + " ".join(notes)
    return text


def render_ranking(result: Dict[str, Any], n: int = 10) -> str:
    """只出得分排行（不给股数）——用于"我想自己决定买哪只"。"""
    ranked: Sequence[quant.Scored] = result.get("top") or []
    if not ranked:
        return "本轮没有候选通过策略筛选。"
    lines = [f"**得分排行**（{result.get('strategy_label') or result['strategy']}，"
             f"候选 {result.get('candidates')} 只，按分数降序）", "",
             "| # | 标的 | 现价 | 涨跌 | 得分 | 主要贡献因子 |",
             "|---|---|---|---|---|---|"]
    for i, c in enumerate(ranked[:n], 1):
        chg = c.change_pct
        lines.append(
            f"| {i} | {c.name}({c.code}) | {c.price:.2f} | "
            f"{(f'{chg:+.2f}%' if chg is not None else '-')} | {c.score:.1f} | "
            f"{c.explain(2)} |")
    lines.append("")
    lines.append("得分 = 候选池内各因子百分位 × 权重，**是相对排名不是上涨概率**。"
                 "想看具体买多少股，用 astock_pick（不传 rank_only）。")
    return "\n".join(lines)


def render_panel(result: Optional[Dict[str, Any]], strategy_key: str,
                 panel_width_note: bool = True,
                 rec: Any = None) -> str:
    """右栏面板内容：窄栏优化（每股 2 行 + 刻度条语义）。

    面板宽度只有约 331px，表格会挤成竖条——所以这里**不用表格**，
    改用「每只 2 行短句 + 一根刻度条」（与持仓面板同一套取舍）。

    `rec` 可为 `astock_backtest.Recommendation`：带上后会在顶部显示
    **策略推荐率**（来自回测，不是主观分），并明确标注"这是策略的历史
    表现、不是每只票上涨的概率"——这条界线画错就变成误导。
    """
    strat = quant.STRATEGIES.get(strategy_key) or quant.STRATEGIES[
        quant.DEFAULT_STRATEGY]
    out: List[str] = []
    grade_line = _grade_line(rec)
    if result is None:
        out.append(f"**量化选股 · {strat.label}**")
        out.append("")
        if grade_line:
            out.append(grade_line)
            out.append("")
        out.append(f"> {strat.description}")
        out.append("")
        out.append("尚未生成选股结果。点设置页「立即跑一轮选股」，"
                   "或在对话里说「帮我选股」。")
        out.append("")
        out.append("_面板只读缓存、不会自动扫全市场_"
                   "（一次扫描要拉 30 只 K 线，5 秒刷新跑不完会把接口打到限流）。")
        return "\n".join(out)

    summary = result.get("summary") or {}
    allocs: Sequence[quant.Allocation] = result.get("allocations") or []
    skipped: Sequence[quant.Allocation] = result.get("skipped") or []

    out.append(f"**量化选股 · {strat.label}**"
               f"｜{result.get('generated_at', '')[-8:] or '-'} 生成"
               f"（{result.get('cache_age_sec', 0):.0f}s 前）")
    out.append("")
    if grade_line:
        out.append(grade_line)
        out.append("")
    out.append(f"扫描 {result.get('scanned', 0)} 只 → 候选 "
               f"{result.get('candidates', 0)} 只 → 可买 {len(allocs)} 只"
               f"｜预算 {_money(summary.get('budget'))}"
               f"｜投 {_money(summary.get('allocated_amount'))}")
    out.append("")

    if not allocs:
        out.append("**本轮无可执行买入**")
        if skipped:
            out.append("")
            out.append(f"{len(skipped)} 只因资金不足一手被跳过，"
                       f"例如 {skipped[0].name}({skipped[0].code})："
                       f"{skipped[0].skip_reason}")
        else:
            out.append("")
            out.append("没有标的通过策略门槛。可换策略 tab，或放宽设置页的"
                       "「最低成交额」门槛后重跑。")
        out.append("")
        out.append(f"_得分是池内相对排名，不是上涨概率。{quant.DISCLAIMER}_")
        return "\n".join(out)

    import json as _json
    for i, a in enumerate(allocs, 1):
        chg = ""
        out.append(f"**{i}. {a.name}**（{a.code}）　{a.price:.2f}{chg}"
                   f"　得分 {a.score:.0f}")
        out.append(f"{a.shares} 股 ≈ {_money(a.amount)}"
                   + (f"　止损 {a.stop_loss:.2f}" if a.stop_loss else "")
                   + (f" / 止盈 {a.take_profit:.2f}" if a.take_profit else ""))
        # 刻度条语义：仓位**空间**（越大越安全），与持仓面板一致
        cap = float(summary.get("position_cap_pct") or 25.0)
        weight = float(a.weight_pct or 0)
        room = max(0.0, (cap - weight) / cap * 100) if cap > 0 else 0.0
        payload = {"items": [{
            "label": f"仓位空间（占 {weight:.1f}% / 上限 {cap:g}%）",
            "value": round(room, 1),
            "note": f"{a.shares} 股"}]}
        out += ["```progress", _json.dumps(payload, ensure_ascii=False), "```", ""]

    if skipped:
        out.append(f"_另有 {len(skipped)} 只通过筛选但资金不足一手_")
        out.append("")
    out.append("_得分是候选池内相对排名，不是上涨概率；不构成投资建议，请自行复核。_")
    return "\n".join(out)


def _grade_line(rec: Any) -> str:
    """推荐率一行文案（无回测数据时返回空串）。

    措辞刻意写全"策略"二字：推荐率衡量的是**这套选股规则**的历史表现，
    不是某只股票上涨的概率。这个区分是本功能可信度的全部——写错就成误导。
    """
    if rec is None:
        return ""
    win = getattr(rec, "win_rate", None)
    grade = getattr(rec, "grade", "")
    label = getattr(rec, "label", "")
    sample = getattr(rec, "sample", 0)
    if win is None:
        return f"⚪ **策略推荐率**：无法判定（{getattr(rec, 'reason', '')}）"
    mark = {"A": "🟢", "B": "🔵", "C": "🟠", "D": "🔴"}.get(grade, "⚪")
    return (f"{mark} **策略推荐率 {win:.0f}%**（{grade} 档·{label}）"
            f"　历史 {sample} 期中 {int(round(win / 100 * sample))} 期跑赢基准")


def _money(value: Any) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:,.0f}"
