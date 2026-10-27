"""回测的工具层：取历史数据 → 跑回测 → 缓存结论 → 供面板/工具使用。

**为什么单独一层**：`astock_backtest` 是纯计算（喂数据出结论、零网络），
本模块负责取数（25 只 × 641 根 K 线，受限速约 5-10 秒）、缓存（回测结论
半天内不变，没必要每次重算）、以及把结论接进面板。

────────────────────────────────────────────────────────────────────────
缓存策略
────────────────────────────────────────────────────────────────────────

回测要拉 25 只股票的历史 K 线（每只 1 次请求，限速 5 次/秒 → 约 5-8 秒）。
这个开销绝不能放在面板刷新路径上（面板 5 秒一刷）。

因此：
  · 回测结果缓存 **30 分钟**（策略与因子不变，一天内结论几乎不变）；
  · 面板只读缓存，没有就显示"先去跑一次回测"；
  · 回测由显式动作触发（工具调用 / 设置页按钮）。

────────────────────────────────────────────────────────────────────────
推荐率的含义（必须与界面文案一致）
────────────────────────────────────────────────────────────────────────

**推荐率 = 历史上按此策略选股，跑赢沪深300 的期数占比。**

它衡量**策略**，不衡量单只股票。所以界面上必须写成
「策略推荐率 68%」而不是「这只股票 68% 会涨」——后者是编造，
前者是历史统计。这条线画错，整个功能就变成误导。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import astock_backtest as bt
import astock_archive as archive_mod
import astock_data as data
import astock_quant as quant

logger = logging.getLogger("litework.plugins.astock.backtest")

#: 回测结果缓存（strategy → (时间戳, (结果, 推荐))））
_CACHE: Dict[str, Tuple[float, Any]] = {}
#: 缓存有效期：30 分钟。回测是日线级结论，一天内几乎不变。
CACHE_TTL = 1800.0

#: 回测股票池大小。25 只 × 1 次 K 线请求，限速下约 5 秒（含缓存命中更快）。
#: 不宜过大：公共接口限流 + 回测结论对池子大小并不敏感（都是"活跃股"偏差）。
DEFAULT_POOL = 25


def clear_cache() -> None:
    _CACHE.clear()


def cache_info() -> Dict[str, Any]:
    now = time.time()
    return {k: round(now - ts, 1) for k, (ts, _v) in _CACHE.items()}


# ====================================================================== 取数


def _default_candidates(pool_size: int) -> List[Dict[str, Any]]:
    """构建回测池：优先全市场快照（活跃股），失败则退回台账持仓 + 自选股。

    为什么要有降级：全市场快照接口（东财 push2 / 新浪排行）在批量回测时
    **会被限流**——实测过整条链路因为这一步直接失败。回测只需要一小批
    有代表性的标的，用用户自己的持仓+自选完全够用，而且**偏差更小**
    （这些是他真正关心的股票），比直接报错让人无从下手要好得多。
    """
    try:
        snap = data.screen(universe="main", sort_by="amount", limit=pool_size * 2)
        rows = snap.get("rows") or []
        if rows:
            return rows
    except Exception as exc:
        logger.warning("[astock] 回测池快照失败，改用台账标的: %s", exc)

    fallback: List[Dict[str, Any]] = []
    try:
        ledger = _ledger_provider() if _ledger_provider else None
        if ledger is not None:
            for p in ledger.list_positions():
                fallback.append({"code": p.get("code"),
                                 "name": p.get("name") or p.get("code")})
            for w in ledger.list_watch():
                fallback.append({"code": w.get("code"),
                                 "name": w.get("name") or w.get("code")})
    except Exception as exc:
        logger.warning("[astock] 台账标的读取失败: %s", exc)
    return fallback[:pool_size]


def _ledger_provider() -> Any:
    """由插件层注入的台账获取函数（保持本模块不依赖 plugin.py）。"""
    return _LEDGER_GETTER() if _LEDGER_GETTER else None


#: 插件 install 时注入（返回 Ledger 或 None）。保持本模块可独立单测。
_LEDGER_GETTER: Any = None


def set_ledger_getter(getter: Any) -> None:
    """由 `AstockPlugin.install` 注入台账获取函数。"""
    global _LEDGER_GETTER
    _LEDGER_GETTER = getter


def _archive() -> Any:
    """取存档实例（由插件层注入；未注入时返回 None 表示不启用存档）。"""
    return _ARCHIVE_GETTER() if _ARCHIVE_GETTER else None


#: 插件 install 时注入（返回 Archive 或 None）
_ARCHIVE_GETTER: Any = None


def set_archive_getter(getter: Any) -> None:
    """由 `AstockPlugin.install` 注入存档获取函数。"""
    global _ARCHIVE_GETTER
    _ARCHIVE_GETTER = getter




def _load_bars(code: str, limit: int = 640,
               kind: str = "stock") -> Tuple[List[Dict[str, Any]], str]:
    """取一只标的的日 K，**存档优先**，在线数据兜底/补缺口。

    返回 (bars, origin)，origin ∈ {"archive", "merged", "online"}。

    `kind` 必须透传：基准指数 000300 与个股 000300 是同码不同标的，
    漏传会静默取到错误数据——表现为"基准恒为 0、胜率莫名变低"，
    极难从结果上发现（真实踩过）。

    为什么存档优先：在线接口只有 641 根（约 2.6 年），而存档是本地累积的、
    可以更长。用户存得越久，回测窗口越长——这正是存档存在的意义。
    但存档可能滞后，所以用在线数据**补齐缺口**；合并按日期去重、
    存档优先（存档记录的是当时的事实价，见 astock_archive）。
    """
    arc = _archive()
    archived: List[Dict[str, Any]] = []
    if arc is not None:
        try:
            archived = arc.load_bars(code, adjust="raw", limit=limit * 2)
        except Exception as exc:
            logger.debug("[astock] 读存档失败 %s: %s", code, exc)

    online: List[Dict[str, Any]] = []
    try:
        online = [b.as_dict() for b in
                  data.fetch_klines(code, period="day", limit=limit,
                                    adjust="none", kind=kind)]
    except data.DataError as exc:
        if not archived:
            raise
        logger.debug("[astock] 在线取数失败，仅用存档 %s: %s", code, exc)

    if not online:
        return archived, "archive"
    # 跑回测顺手把在线数据存下来（存档随时间累积，一年后窗口就多一年）。
    # 失败不影响回测——存档是"顺带收益"，不是前置条件。
    if arc is not None and _auto_archive_enabled():
        try:
            arc.save_bars(code, online, adjust="raw", source="online")
        except Exception as exc:
            logger.debug("[astock] 自动存档失败 %s: %s", code, exc)
    if archived:
        return archive_mod.merged_bars_dicts(archived, online), "merged"
    return online, "online"


#: 是否在回测取数时自动写存档（由插件层按设置页注入）
_AUTO_ARCHIVE: bool = True


def set_auto_archive(enabled: bool) -> None:
    global _AUTO_ARCHIVE
    _AUTO_ARCHIVE = bool(enabled)


def _auto_archive_enabled() -> bool:
    return _AUTO_ARCHIVE

def fetch_universe(candidates: Sequence[Dict[str, Any]],
                   pool_size: int = DEFAULT_POOL,
                   ) -> Tuple[List[Dict[str, Any]], int]:
    """按候选顺序取历史 K 线（存档优先）。返回 (可回测的标的, 失败数)。

    为什么按**候选顺序**（成交额降序）取：回测需要足够多的标的做横截面
    打分，而活跃股的历史数据最完整。代价是幸存者偏差 + 活跃度偏差，
    这一点在报告里已经明说，不在这里偷偷糊过去。
    """
    out: List[Dict[str, Any]] = []
    failed = 0
    for row in list(candidates)[:pool_size]:
        code = str(row.get("code") or "")
        if not code:
            continue
        try:
            bars, origin = _load_bars(code)
        except data.DataError as exc:
            failed += 1
            logger.debug("[astock] 回测取数失败 %s: %s", code, exc)
            continue
        if not bars:
            failed += 1
            continue
        out.append({"code": code, "name": str(row.get("name") or code),
                    "bars": bars, "origin": origin})
    return out, failed


def fetch_benchmark() -> List[Dict[str, Any]]:
    """基准（沪深300）日 K。取不到就返回空，回测里基准按 0 处理并提示。

    `kind="index"` 必须传：漏传会把 000300 当个股取，基准恒为 0，
    胜率随之失真（真实踩过——表现为"胜率从 63% 掉到 43%"）。
    """
    try:
        bars, _origin = _load_bars(bt.BENCHMARK_CODE, kind="index")
        return bars
    except Exception as exc:
        logger.warning("[astock] 基准指数取数失败: %s", exc)
        return []


# ====================================================================== 跑回测


def run(strategy_key: str, *, pool_size: int = DEFAULT_POOL,
        candidates: Optional[Sequence[Dict[str, Any]]] = None,
        cfg: Optional[bt.BacktestConfig] = None,
        use_cache: bool = True,
        pool: Optional[Sequence[Dict[str, Any]]] = None,
        bench: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """跑一轮回测（同步，调用方包 to_thread）。

    `candidates` 不传时自己拉一次全市场快照（按成交额降序）来决定回测池。

    `pool` / `bench` 用于**跳过取数**：多套策略回测同一批标的时，历史 K 线
    与基准完全相同，重复拉一遍纯属浪费（默认池 25 只 = 26 次请求）。
    `run_all()` 就是先取一次再分别喂给各策略。
    """
    key = f"{strategy_key}|{pool_size}|{hash(str((cfg or bt.BacktestConfig()).as_dict()))}"
    if use_cache:
        hit = _CACHE.get(key)
        if hit and (time.time() - hit[0]) <= CACHE_TTL:
            result, rec, failed = hit[1]
            return {"result": result, "recommendation": rec,
                    "fetch_failed": failed, "cached": True,
                    "cache_age_sec": round(time.time() - hit[0], 1)}

    failed = 0
    if pool is None:
        if candidates is None:
            candidates = _default_candidates(pool_size)
        if not candidates:
            raise data.DataError(
                "无法构建回测池：全市场快照接口被限流，且台账里也没有持仓/自选股。"
                "请等 30 秒后重试，或先用 astock_record / astock_watch 录入标的")
        pool, failed = fetch_universe(candidates, pool_size=pool_size)
    if len(pool) < 4:
        raise ValueError(f"仅取到 {len(pool)} 只标的历史数据，不足以回测")

    if bench is None:
        bench = fetch_benchmark()
    result = bt.run_backtest(strategy_key, list(pool), list(bench), cfg=cfg)
    rec = bt.grade_recommendation(result)

    _CACHE[key] = (time.time(), (result, rec, failed))
    return {"result": result, "recommendation": rec, "fetch_failed": failed,
            "cached": False, "cache_age_sec": 0.0, "pool_size": pool_size}


def run_all(*, pool_size: int = DEFAULT_POOL,
            candidates: Optional[Sequence[Dict[str, Any]]] = None,
            cfg: Optional[bt.BacktestConfig] = None,
            strategies: Optional[Sequence[str]] = None,
            use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """**一次取数、多套策略**回测：返回 {strategy_key: run(...) 同结构}。

    **为什么需要**：回测里最贵的是历史 K 线（每股一次请求，默认 25 只），
    而它**与策略无关**——三套策略用的是同一批标的的同一段历史。原先
    `compare` 面板分别调 `run()` 三次，等于把 25 只 K 线拉三遍。

    实现上复用了 `run(candidates=...)`：第一次把历史数据取出来交给后续策略，
    池子固定后各策略只做打分与统计（纯 CPU）。

    缓存逐个策略独立判定：全命中时零网络请求。
    """
    keys = [k for k in (strategies or list(bt.STRATEGIES)) if k in bt.STRATEGIES]
    if not keys:
        raise ValueError("没有可用的策略")
    out: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    if use_cache:
        for key in keys:
            hit = get_cached(key)
            # 池大小/配置不同则不复用（那会得出不可比的结论）
            if hit and hit.get("pool_size") in (None, pool_size):
                out[key] = hit
            else:
                pending.append(key)
    else:
        pending = list(keys)
    if not pending:
        return out

    # 取一次池子的历史数据 + 基准，各策略共用（这是本函数存在的唯一理由）
    cands = (list(candidates) if candidates is not None
             else _default_candidates(pool_size))
    if not cands:
        raise data.DataError(
            "无法构建回测池：全市场快照接口被限流，且台账里也没有持仓/自选股。"
            "请等 30 秒后重试，或先用 astock_record / astock_watch 录入标的")
    pool, failed = fetch_universe(cands, pool_size=pool_size)
    if len(pool) < 4:
        raise ValueError(f"仅取到 {len(pool)} 只标的历史数据，不足以回测")
    bench = fetch_benchmark()
    for key in pending:
        res = run(key, pool_size=pool_size, cfg=cfg, use_cache=False,
                  pool=pool, bench=bench)
        res["fetch_failed"] = failed
        res["pool_size"] = pool_size
        out[key] = res
    return out


def get_cached(strategy_key: str) -> Optional[Dict[str, Any]]:
    """只取缓存（面板用）。绝不触发网络。"""
    best: Optional[Tuple[float, Any]] = None
    for key, (ts, val) in _CACHE.items():
        if not key.startswith(f"{strategy_key}|"):
            continue
        if best is None or ts > best[0]:
            best = (ts, val)
    if best is None:
        return None
    result, rec, failed = best[1]
    # key 形如 "<strategy>|<pool_size>|<cfg-hash>"：把池大小带回去，让 run_all
    # 能判断"这份缓存是不是同一口径"（池子不同则两套策略不可比）
    try:
        pool_size = int(key.split("|")[1])
    except (IndexError, ValueError):
        pool_size = None
    return {"result": result, "recommendation": rec, "fetch_failed": failed,
            "cached": True, "cache_age_sec": round(time.time() - best[0], 1),
            "pool_size": pool_size}


# ====================================================================== 渲染


def render(result: bt.BacktestResult, rec: bt.Recommendation,
           detail_periods: int = 0, fetch_failed: int = 0) -> str:
    text = bt.render_backtest(result, rec, detail_periods=detail_periods)
    if fetch_failed:
        text += f"\n\n_（{fetch_failed} 只标的因取数失败未纳入回测）_"
    return text


# ====================================================================== 面板卡片


def build_recommendation_cards(picks: Sequence[quant.Allocation],
                               rec: Optional[bt.Recommendation],
                               strategy_label: str,
                               graded: bool = True) -> str:
    """把「买什么 + 买多少 + 推荐率」渲染成右栏**卡片看板**（lite-tree）。

    为什么要卡片而不是表格：右栏约 331px 宽，6 列表格会被挤成竖条读不出来。
    卡片式（一票一卡）能在窄栏里放下「名称/价格/股数/推荐率/止损」五项，
    且推荐率能用进度条直接看出高低——这是"傻瓜化"的关键：
    不用读数字，看条长短就知道该不该买。
    """
    import json as _json

    def _scale(label: str, value: float, tone: str = "alt") -> Dict[str, Any]:
        """借 option.prob 通道画进度条（宿主唯一会被渲染成图形的字段）。"""
        return {"label": label, "prob": max(0.0, min(1.0, value / 100.0)),
                "tone": tone, "chosen": False}

    win = rec.win_rate if rec else None
    cards: List[Dict[str, Any]] = []
    for a in picks:
        # 卡片正文：一行放下股数与金额（窄栏极限）
        context = f"{a.shares} 股 ≈ {a.amount:,.0f} 元"
        if a.stop_loss:
            context += f" · 止损 {a.stop_loss:.2f}"
        opts: List[Dict[str, Any]] = [
            {"label": f"买入 {a.shares} 股 @ {a.price:.2f}",
             "prob": 1.0, "tone": "good", "chosen": True}]
        if win is not None and graded:
            tone = ("good" if win >= 65 else "alt" if win >= 50
                    else "danger" if win >= 35 else "danger")
            opts.append(_scale(
                f"策略推荐率 {win:.0f}%（{rec.sample} 期样本）", win, tone))
        elif graded:
            opts.append(_scale("推荐率：尚无回测数据", 0.0, "neutral"))
        # 仓位刻度：与持仓面板同一语义（越大越安全）
        cap = 25.0
        weight = a.weight_pct or 0.0
        room = max(0.0, (cap - weight) / cap * 100) if cap > 0 else 0.0
        opts.append(_scale(f"仓位空间（占 {weight:.0f}%）", room,
                           "danger" if weight > cap else "alt"))
        question = (f"推荐率 {win:.0f}%" if win is not None
                    else "推荐率待回测")
        cards.append({
            "title": f"{a.name}  {a.price:.2f}",
            "context": context,
            "kind": "buy",
            "question": question,
            "options": opts[:3],
            "confidence": (win / 100.0) if win else 0.5,
            "accepted": bool(win is None or win >= 50),
        })

    grade_text = rec.label if rec else "未回测"
    payload = {
        "type": "lite-tree",
        "status": {
            "text": f"{strategy_label} · {grade_text}",
            "tone": ("ok" if rec and rec.grade in ("A", "B")
                     else "warn" if rec and rec.grade == "C" else "neutral"),
            "meta": [f"{len(picks)} 只可买",
                     (f"推荐率 {win:.0f}%" if win is not None else "推荐率 -"),
                     (f"样本 {rec.sample} 期" if rec else "样本 -")],
            "chips": ([f"{rec.grade} 档 {rec.label}"] if rec else []),
        },
        "trees": cards,
        "total": len(cards),
    }
    return _json.dumps(payload, ensure_ascii=False)
