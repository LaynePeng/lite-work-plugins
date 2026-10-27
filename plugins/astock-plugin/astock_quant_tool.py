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
# 分两层：`collect`（取数，贵）+ `evaluate`（打分，纯 CPU）。
#
# **为什么要拆**：三套策略的差异只在 `filters` 与 `weights` 两个字段，
# 都是纯计算（毫秒级）；而贵的是数据采集——1 次全市场快照 + 最多 30 次
# K 线请求，受限速约 6 秒。此前每套策略各扫一次，等于为了看三套策略付
# 3 次采集代价，而**采集结果三套完全共用**。
# 拆开后 `scan_all()` 一次采集即可产出三套结果，成本从 3× 降到 1×。


def _cache_key(strategy_key: str, universe: str, candidate_limit: int,
               sizing: "quant.SizingConfig", cash: float, equity: float,
               weights: Optional[Dict[str, float]]) -> str:
    return (f"{strategy_key}|{universe}|{candidate_limit}|"
            f"{sizing.max_positions}|{sizing.position_cap_pct}|"
            f"{sizing.cash_usage_pct}|{round(cash, 2)}|{round(equity, 2)}|"
            f"{hash(tuple(sorted((weights or {}).items())))}")


def collect(universe: str = "main", *, candidate_limit: int = 30,
            prefilter_limit: int = 300) -> Dict[str, Any]:
    """**取数层**：全市场快照 → 前 N 只的日 K → 原始候选池。

    返回的 `rows` / `techs` 是**与策略无关**的中间产物，可被任意多套策略
    复用（`evaluate` 按各策略的 filters/weights 在其上打分）。

    这里只做「拿数据」，不做任何策略过滤与打分——过滤条件各策略不同，
    放到 `evaluate` 里按策略逐套跑。
    """
    snap = data.screen(universe=universe, sort_by="amount",
                       limit=prefilter_limit, exclude_st=True)
    rows: List[Dict[str, Any]] = list(snap.get("rows") or [])
    scanned = int(snap.get("universe_total") or 0)
    if not rows:
        raise data.DataError("全市场快照返回空（接口可能限流），请稍后重试")

    rows = rows[:max(1, candidate_limit)]
    techs: Dict[str, Dict[str, Any]] = {}
    fetch_failed = 0
    for row in rows:
        code = str(row.get("code") or "")
        if not code:
            continue
        try:
            bars = data.fetch_klines(code, period="day", limit=120)
            techs[code] = quant.compute_technical([b.as_dict() for b in bars])
        except data.DataError as exc:
            fetch_failed += 1
            logger.debug("[astock] %s K线获取失败: %s", code, exc)
    return {"rows": rows, "techs": techs, "scanned": scanned,
            "fetch_failed": fetch_failed, "universe": universe}


def evaluate(collected: Dict[str, Any], strategy_key: str, *,
             cash: float, equity: float,
             sizing: Optional["quant.SizingConfig"] = None,
             weights: Optional[Dict[str, float]] = None,
             fee_cfg: Optional[mkt.FeeConfig] = None) -> Dict[str, Any]:
    """**打分层**：在一份采集结果上跑单套策略（纯计算，无网络）。

    注意：`quant.rank_candidates` 会**原地写**候选对象（factor_values /
    missing_factors / score / contributions），所以每套策略都必须基于
    `collected` **新建** Scored 实例——共用同一批对象会让后一套策略
    覆盖前一套的打分痕迹。
    """
    strat = quant.STRATEGIES.get(strategy_key)
    if strat is None:
        raise ValueError(f"未知策略 {strategy_key}；可选："
                         f"、".join(quant.STRATEGIES))
    sizing = sizing or quant.SizingConfig()

    scored_all: List[quant.Scored] = []
    rejected: List[Dict[str, str]] = []
    for row in collected.get("rows") or []:
        code = str(row.get("code") or "")
        tech = (collected.get("techs") or {}).get(code)
        if not code or tech is None:
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
    taken, skipped, summary = quant.size_positions(
        ranked, cash=cash, equity=equity, cfg=sizing, fee_cfg=fee_cfg,
        strategy=strat)

    return {
        "strategy": strat.key,
        "strategy_label": strat.label,
        "strategy_description": strat.description,
        "scanned": collected.get("scanned", 0),
        "prefiltered": len(collected.get("rows") or []),
        "candidates": len(scored_all),
        "fetch_failed": collected.get("fetch_failed", 0),
        "ranked": ranked,
        "top": ranked[:10],
        "allocations": taken,
        "skipped": skipped,
        "summary": summary,
        "rejected_sample": rejected[:8],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cached": False,
        "cache_age_sec": 0.0,
        "universe": collected.get("universe") or "main",
    }


def scan(strategy_key: str, *, cash: float, equity: float,
         universe: str = "main",
         sizing: Optional[quant.SizingConfig] = None,
         weights: Optional[Dict[str, float]] = None,
         candidate_limit: int = quant.MAX_KLINE_CANDIDATES,
         prefilter_limit: int = 300,
         fee_cfg: Optional[mkt.FeeConfig] = None,
         use_cache: bool = True) -> Dict[str, Any]:
    """跑一轮完整选股（单策略）。同步实现（调用方用 asyncio.to_thread 包）。

    流程与耗时：
      1. 全市场快照粗筛（1 次请求，按成交额降序取前 prefilter_limit 只）
      2. 前 candidate_limit 只拉日 K 算技术因子（受限速，约每只 1/5 秒）
      3. 硬门槛过滤 → 多因子池内打分 → 仓位分配

    `universe` 默认 main（沪深主板+中小板+创业板），避开北交所的低流动性。

    **想看多套策略请用 `scan_all`**：它一次采集就能产出三套，成本约为
    分别调用本函数三次的三分之一。
    """
    strat = quant.STRATEGIES.get(strategy_key)
    if strat is None:
        raise ValueError(f"未知策略 {strategy_key}；可选："
                         f"、".join(quant.STRATEGIES))
    sizing = sizing or quant.SizingConfig()

    cache_key = _cache_key(strat.key, universe, candidate_limit, sizing,
                           cash, equity, weights)
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit and (time.time() - hit[0]) <= CACHE_TTL:
            out = dict(hit[1])
            out["cached"] = True
            out["cache_age_sec"] = round(time.time() - hit[0], 1)
            return out

    collected = collect(universe, candidate_limit=candidate_limit,
                        prefilter_limit=prefilter_limit)
    result = evaluate(collected, strat.key, cash=cash, equity=equity,
                      sizing=sizing, weights=weights, fee_cfg=fee_cfg)
    _CACHE[cache_key] = (time.time(), result)
    return result


def scan_all(*, cash: float, equity: float, universe: str = "main",
             sizing: Optional[quant.SizingConfig] = None,
             candidate_limit: int = quant.MAX_KLINE_CANDIDATES,
             prefilter_limit: int = 300,
             strategies: Optional[Sequence[str]] = None,
             fee_cfg: Optional[mkt.FeeConfig] = None,
             use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """**一次采集、多套策略**：返回 {strategy_key: result}。

    **为什么值得做**：三套策略共用同一份行情数据，差异只是过滤条件与
    打分权重（纯 CPU）。此前看三套要扫三次（约 18 秒 + 90 次 K 线请求），
    现在是 1 次（约 6 秒 + 30 次请求），且三套结果必然基于**同一时点**的
    价格——这一点比省时间更重要：分别扫出来的三套结果价格不同源，
    横向对比时"这只动量排第一、那只价值排第三"可能只是几十秒的价差。

    缓存逐个策略独立判定：命中且未过期的直接复用，只对缺失/过期的策略
    重新取数。全部命中时**零网络请求**。
    """
    sizing = sizing or quant.SizingConfig()
    keys = [k for k in (strategies or list(quant.STRATEGIES))
            if k in quant.STRATEGIES]
    if not keys:
        raise ValueError("没有可用的策略")

    out: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    now = time.time()
    for key in keys:
        if not use_cache:
            pending.append(key)
            continue
        hit = _CACHE.get(_cache_key(key, universe, candidate_limit, sizing,
                                    cash, equity, None))
        if hit and (now - hit[0]) <= CACHE_TTL:
            res = dict(hit[1])
            res["cached"] = True
            res["cache_age_sec"] = round(now - hit[0], 1)
            out[key] = res
        else:
            pending.append(key)
    if not pending:
        return out

    collected = collect(universe, candidate_limit=candidate_limit,
                        prefilter_limit=prefilter_limit)
    stamp = time.time()
    for key in pending:
        res = evaluate(collected, key, cash=cash, equity=equity,
                       sizing=sizing, fee_cfg=fee_cfg)
        _CACHE[_cache_key(key, universe, candidate_limit, sizing,
                          cash, equity, None)] = (stamp, res)
        out[key] = res
    return out




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


def render_compare(results: Dict[str, Dict[str, Any]],
                   recs: Optional[Dict[str, Any]] = None,
                   max_rows: int = 3,
                   with_advice: bool = True) -> str:
    """**三策略横向对比**（右栏选股 tab 的主视图）。

    设计取舍（为什么不是三个 tab 各看各的）：
      ① 331px 窄栏里，四次 tab 切换才能完成一次"对比"，而对比正是选股的
         真实诉求——**看得见分歧才有决策价值**；
      ② 三套结果出自同一次采集（v2.1.0），价格同源，横向比较才有意义；
      ③ 每套只列前 `max_rows` 只：窄栏塞不下更多，且第 4 名之后本来也很少
         被选中。想看完整排名用对话里的 `astock_pick`。

    刻意**不给"该选哪个策略"的结论**：三套策略在不同市况下各有胜率，
    替用户选就是替他承担择时风险。这里只把分歧摆出来（谁只被一套选中、
    谁被多套同时选中），判断留给用户。
    """
    recs = recs or {}
    keys = [k for k in quant.STRATEGIES if k in results]
    if not keys:
        return ("**量化选股 · 三策略对比**\n\n"
                "尚未生成选股结果。点设置页「立即跑一轮选股」，"
                "或在对话里说「帮我选股」。\n\n"
                "_面板只读缓存、不会自动扫全市场_"
                "（一次扫描要拉 30 只 K 线，5 秒刷新跑不完会把接口打到限流）。")

    out: List[str] = ["**量化选股 · 三策略对比**", ""]
    first = results[keys[0]]
    age = max(float(results[k].get("cache_age_sec") or 0) for k in keys)
    out.append(f"{first.get('generated_at', '')[-8:] or '-'} 生成｜"
               f"{age:.0f}s 前｜扫描 {first.get('scanned', 0)} 只")
    # 「该用哪套」放在最顶端：用户看到三个百分比后的第一反应就是这个。
    # 面板是窄栏，所以只出结论 + 最多一条警告。
    # `with_advice=False` 由对话侧使用——那里会单独输出**完整版**建议
    # （含理由、风险与用法），内嵌一份会造成同一段话出现两次。
    if with_advice:
        try:
            import astock_advise as advise_mod
            adv = advise_mod.advise(recs)
            if adv.get("headline"):
                out.append("")
                out.append(advise_mod.render_advice(adv, compact=True))
        except Exception:
            logger.debug("[astock] 策略建议渲染失败", exc_info=True)
    if age > CACHE_TTL:
        out.append("")
        out.append(f"> 数据已过期（>{CACHE_TTL:.0f}s），点「立即跑一轮选股」刷新。")
    out.append("")

    picked_by: Dict[str, List[str]] = {}
    for key in keys:
        res = results[key]
        strat = quant.STRATEGIES[key]
        allocs = list(res.get("allocations") or [])[:max_rows]
        grade = render_grade_line(recs.get(key))
        head = f"**{strat.label}**"
        if grade:
            import re as _re
            m = _re.search(r"([0-9.]+%)", grade)
            if m:
                head += f"　推荐率 {m.group(1)}"
        out.append(head)
        if not allocs:
            skipped = res.get("skipped") or []
            if skipped:
                out.append(f"　{len(skipped)} 只过筛但资金不足一手")
            else:
                out.append(f"　无标的过门槛（候选 {res.get('candidates', 0)} 只）")
        for a in allocs:
            picked_by.setdefault(a.code, []).append(strat.label[:2])
            out.append(f"　{a.name} {a.shares}股 @{a.price:.2f}"
                       f"（得分 {a.score:.0f}）")
        out.append("")

    multi = {c: labels for c, labels in picked_by.items() if len(labels) > 1}
    if multi:
        out.append("**多套同时选中**（分歧小、更值得看）")
        for code, labels in list(multi.items())[:5]:
            name = next(
                (a.name for k in keys
                 for a in (results[k].get("allocations") or [])
                 if a.code == code), code)
            out.append(f"　{name}：{'/'.join(labels)}")
        out.append("")
    elif picked_by:
        out.append("> 三套策略各自选了不同的标的——没有共识，"
                   "说明当前市况分歧较大，建议先看回测推荐率。")
        out.append("")

    out.append("_得分是候选池内相对排名，不是上涨概率。"
               "完整排行与股数用对话里的 astock_pick。_")
    return "\n".join(out)


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
    grade_line = render_grade_line(rec)
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


def render_grade_line(rec: Any) -> str:
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
