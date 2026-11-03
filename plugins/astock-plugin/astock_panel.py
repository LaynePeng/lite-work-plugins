"""右栏面板渲染：持仓（markdown / lite-tree 两形态）+ 选股（三策略对比）。

────────────────────────────────────────────────────────────────────────
为什么单独成文件
────────────────────────────────────────────────────────────────────────
面板渲染是**纯展示逻辑**（给定数据 → 字符串），与「取数 / 台账 / 工具调度」
是两件事。原先它和 30 多个工具处理函数挤在 `plugin.py` 里（近 4000 行），
改一处配色要在一个几千行的文件里翻找。

拆分后 `plugin.py` 只留装配层：类定义、设置页声明、工具声明、install。
本模块不 import `plugin`（避免循环依赖），依赖通过 `PanelContext` 注入。

────────────────────────────────────────────────────────────────────────
两条设计红线（改代码前先读）
────────────────────────────────────────────────────────────────────────
① **绝不在渲染路径上打网络**。面板每 5 秒刷新，而一次全市场扫描要拉
   30 只 K 线 ≈ 6 秒，还会把公共行情接口打到限流。扫描只由用户显式触发，
   这里一律只读进程内缓存（`quant_tool.pick_cached`）。
② **`options[].prob` 是可视化通道，不是概率**。宿主面板里它是唯一会被
   渲染成图形的字段（label + 彩色横条 + 百分比），于是复用它表达
   「仓位空间 / 距止损余量」这类 0-100 刻度。**别当 bug 改掉。**
   语义一律是「安全余量」：条越长越安全，触线 = 0 = 红。
"""
from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import astock_market as mkt
import astock_market_mood as mood
import astock_quant as quant
import astock_quant_tool as quant_tool
import astock_backtest_tool as backtest_tool
import astock_hints as hints
import astock_sentiment as senti
import astock_sentiment_data as senti_data
import astock_sentiment_tool as senti_tool

from astock_common import _as_bool, _cfg, _f, _money, _pct, _table

logger = logging.getLogger("litework.astock.panel")

#: 单次面板最多渲染多少只（再多在 331px 窄栏里也没人看得完）
MAX_ROWS = 12


@dataclass
class PanelContext:
    """面板渲染所需的外部能力（由 `plugin.py` 注入，避免反向 import）。

    `snapshot` / `ledger` / `hints_cfg` / `mood` 都是**取数**动作；其余是纯配置。

    `mood` 是 v2.5.0 新加大盘看板的数据来源，**必须是带缓存的回调**：
    面板每 5 秒刷新，若每次都真打接口会被限流（实测东财 clist 就是这样挂的）。
    由 plugin 注入 `lambda: self._mood_cache.get(time.monotonic())`。
    允许为 None（老宿主或单测），此时大盘区块直接不渲染。
    """

    app: Any
    snapshot: Callable[..., Dict[str, Any]]     # plugin._snapshot
    hints_cfg: Callable[[], Any]                # plugin._hints_cfg
    ledger: Callable[[], Any]                   # plugin._get_ledger
    mood: Optional[Callable[[], Dict[str, Any]]] = None   # 大盘（带缓存）


# ====================================================================== 入口


def render_panel(ctx: PanelContext, panel_id: str, config: Dict[str, Any]) -> str:
    """面板总入口。任何异常都降级为可读提示，绝不让宿主面板挂掉。"""
    if panel_id == "pick" or panel_id.startswith("pick-"):
        return render_pick_panel(ctx.app, panel_id)
    if panel_id != "portfolio":
        return ""
    try:
        # include_watch：自选股行情一并拉——面板要画自选区（此前只画持仓，
        # 与聊天看板不一致：聊天里有「无需关注」列，右栏却没有）
        snap = ctx.snapshot(include_watch=True)
    except Exception as exc:
        logger.warning("[astock] 面板渲染失败: %s", exc)
        return (f"台账读取失败：{exc}\n\n"
                f"检查设置页「台账文件路径」，或先用 `astock_record` 录入持仓。")
    d = snap["data"]
    style = str(_cfg(ctx.app, "panel_style", "markdown") or "markdown").lower()
    flag = render_sentiment_flag(d)
    if style == "tree":
        return render_tree(ctx, d, flag)
    return render_markdown(ctx, d, flag)


# ====================================================================== 选股面板


def render_pick_panel(app: Any, panel_id: str) -> str:
    """量化选股面板。

    v2.1.0 起是**一个「选股」tab + 三策略对比视图**，不再是三个 tab
    各看各的。理由：面板只读、不能触发扫描，而设置页按钮原先一次只跑
    一套策略 → 另外两个 tab 永远显示「尚未生成」，用户要改设置再点三次
    才能完成一次对比。现在一次跑三套、一屏内摆出分歧。

    **兼容旧 id**：`pick-momentum` / `pick-reversal` / `pick-quality`
    仍可访问，渲染该策略的详情视图（旧书签/习惯不失效）。

    **绝不在这里跑扫描**：见模块顶部红线①。
    """
    strategy = ""
    if panel_id.startswith("pick-"):
        strategy = panel_id.split("-", 1)[1]
        # 未知的 pick-xxx 不该静默降级成对比视图——那会让"面板 id 写错"
        # 变成一个看不出来的 bug。只有已知策略才走详情视图。
        if strategy not in quant.STRATEGIES:
            return ""
    # 全部缓存的推荐率（只读，绝不在这里跑回测——那是 25 只 K 线的开销）
    recs: Dict[str, Any] = {}
    for key in quant.STRATEGIES:
        try:
            bt = backtest_tool.get_cached(key)
            if bt:
                recs[key] = bt.get("recommendation")
        except Exception:
            logger.debug("[astock] 面板读回测缓存失败", exc_info=True)

    # 旧 id → 单策略详情视图
    if strategy in quant.STRATEGIES:
        try:
            cached = quant_tool.pick_cached(strategy)
        except Exception:
            logger.warning("[astock] 选股面板缓存读取失败", exc_info=True)
            cached = None
        return render_pick_detail(app, strategy, cached, recs.get(strategy))

    # 新「选股」tab → 三策略对比（v2.2.0：卡片看板，不再是缩进纯文本）
    results: Dict[str, Any] = {}
    try:
        for key in quant.STRATEGIES:
            cached = quant_tool.pick_cached(key)
            if cached:
                results[key] = cached
    except Exception:
        logger.warning("[astock] 选股面板缓存读取失败", exc_info=True)
    style = str(_cfg(app, "pick_panel_style", "card") or "card").lower()
    if results and style != "markdown":
        try:
            payload = quant_tool.build_compare_tree(results, recs=recs)
            if payload:
                return payload
        except Exception as exc:
            logger.warning("[astock] 对比卡片渲染失败，退回 markdown: %s", exc)
    return quant_tool.render_compare(results, recs=recs)


def render_pick_detail(app: Any, strategy: str,
                       cached: Optional[Dict[str, Any]], rec: Any) -> str:
    """单策略详情视图（旧 `pick-<strategy>` 入口，保留兼容）。"""
    # 卡片形态：有选股结果 + 用户没要求 Markdown 时，走 lite-tree 卡片看板
    # （「像炒股软件那样」的诉求）。卡片能以进度条表达推荐率/仓位，
    # 表格在 331px 窄栏里做不到这一点。
    style = str(_cfg(app, "pick_panel_style", "card") or "card").lower()
    if cached and (cached.get("allocations") or cached.get("skipped")) \
            and style != "markdown":
        try:
            return backtest_tool.build_recommendation_cards(
                cached.get("allocations") or [], rec,
                quant.STRATEGIES[strategy].label)
        except Exception as exc:
            logger.warning("[astock] 选股卡片渲染失败，退回 markdown: %s", exc)
    try:
        return quant_tool.render_panel(cached, strategy, rec=rec)
    except Exception as exc:
        logger.warning("[astock] 选股面板渲染失败: %s", exc)
        return (f"**量化选股 · {strategy}**\n\n"
                f"面板渲染出错：{type(exc).__name__}: {exc}\n\n"
                f"可在对话里说「帮我选股」直接拿结果。")


# ====================================================================== 持仓面板


def _section(title: str, note: str = "") -> str:
    """区块标题（`###`，比正文大一号）。

    **v2.5.6 改为真标题**（用户要求："大盘、我的资产、持仓、自选观察、盯盘
    是标题，可以比其他字体大一点"）。此前用 `**粗体**`，理由是"`###` 会被
    放大成两级字号、窄栏更挤"——实测那个顾虑夸大了：面板正文 13px，`###`
    在宿主里约 15px（1.17em），比 13px 的卡片正文略大，正是标题该有的层次；
    真正挤的是 margin，所以这里配 `.md` 的 `margin: 0.9em 0 0.4em` 由宿主
    控制，我们只负责用对语义。

    所以：**标题就是标题**（`###`），不再用粗体冒充。粗体留给正文强调。
    **末尾的空行是必须的**——markdown 只有空行才分段，单换行会并成一段
    （这正是用户报障"这堆太挤了"的直接原因）。
    """
    return f"### {title}" + (f"　{note}" if note else "")


def _caption(text: str) -> str:
    """附注小字（`#####`，比正文小一号）。

    **v2.5.6 新增**（用户要求："大盘里含义那一段的字小一点"）。宿主的
    ReactMarkdown **没装 rehype-raw**，所以 `<small>` / `<span style>` 这类
    内联 HTML 会被原样当文本输出——想改字号只能走 markdown 原生层级。
    `#####` 在宿主里约 11px（0.83em），比 13px 正文小、又比 `######` 可读。

    语义：这是**解释性附注**，不是标题——所以叫 caption 而不是 heading。
    """
    return f"##### {text}"


def _kpi(label: str, value: str, delta: str = "", note: str = "",
         trend: str = "flat", **extra: Any) -> Dict[str, Any]:
    """指标卡（空字段不写键——`note: ""` 会在宿主里渲染出一个空 span）。

    `trend` 决定颜色：up=绿 / down=红 / flat=中性，前端会自动补 ▲▼。
    冗余字段（`sort` / `reasons` 等）以 `_` 前缀或由调用方清理，
    避免内部键漏进宿主渲染的 JSON。
    """
    item: Dict[str, Any] = {"label": label, "value": value, "trend": trend}
    if delta:
        item["delta"] = delta
    if note:
        item["note"] = note
    for k, v in extra.items():
        if v is not None:
            item[f"_{k}"] = v
    return item


def render_sentiment_flag(d: Dict[str, Any]) -> str:
    """首要持仓的舆情背离速查。

    面板每 5 秒刷新 → 必须限流：只查 1 只、只取 4 个便宜类别（不查新闻/公告，
    那两类请求重且变化慢），失败静默（面板不能因为舆情接口挂了而空白）。
    """
    if not d["positions"]:
        return ""
    try:
        top = max(d["positions"], key=lambda p: p["shares"] *
                  ((d["quotes"].get(p["code"]) or {}).get("price")
                   or p["cost_price"]))
        collected = senti_data.collect(top["code"], top.get("name") or "",
                                       scope=("margin", "holder", "lhb", "report"))
        rep = senti.summarize(collected["items"], top["code"],
                              top.get("name") or "",
                              collected["errors"], collected["extras"])
        label = top.get("name") or top["code"]
        if rep.conflicts:
            c = rep.conflicts[0]
            mark = "🔴" if c.severity == "high" else "🟠"
            return f"{mark} **{label} 舆情背离**：{c.title}"
        if rep.tone != "thin":
            return f"⚪ **{label} 舆情**：{senti_tool.render_brief(rep)}"
    except Exception:
        logger.debug("[astock] 面板舆情速查失败", exc_info=True)
    return ""


def _pnl_pct_of(d: Dict[str, Any]) -> str:
    """组合层浮动盈亏率（成本基数为 0 时显示 '-'）。"""
    cost_base = sum(float(p.get("cost_price") or 0) * int(p.get("shares") or 0)
                    for p in d.get("positions") or [])
    if cost_base <= 0:
        return "-"
    return _pct((d.get("float_pnl") or 0) / cost_base * 100)


def _arrow_pct(chg: Optional[float]) -> str:
    """涨跌幅 + 方向箭头（表格/卡片里一眼分辨涨跌；None 显示 '-'）。

    不用「—」（em dash）表示零涨跌——用户明确要求不要这个符号，
    且它和减号/破折号在窄栏里视觉上分不清。零涨跌用「0.00%」原样。
    """
    if chg is None:
        return "-"
    arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "")
    return f"{arrow}{_pct(chg)}"


def _hint_reason(h: Any) -> str:
    """一条 hint → 卡片里的一句「为什么」（标题 + 建议，去掉重复的前缀）。

    hint 的 title 通常已包含股票名（如「天山铝业 单票占比 33.8% 超上限」），
    卡片 title 已写明股票名，这里不再拼一次（拼了就是「天山铝业：天山铝业…」）。
    """
    title = (getattr(h, "title", "") or "").strip()
    body = (getattr(h, "suggestion", "") or getattr(h, "detail", "") or "").strip()
    name = (getattr(h, "name", "") or "").strip()
    if name and title.startswith(name):
        title = title[len(name):].strip()
    if not body:
        return title
    # 不用破折号（用户明确要求「不要破折号」）：改为句号断句。
    # 早先写成 `title —— body`，在面板里渲染成 `- **浦发银行**：触发止损线
    # （浮亏 -20.00%） —— 按计划应减仓/清仓 …`——正是用户反复要求去掉的那个
    # 符号；且 em dash 在窄栏里与减号/破折号分不清。
    sep = "" if title.endswith(("。", "！", "？", "；", "，", "、", "：")) else "。"
    return f"{title}{sep}{body}" if title else body


# ====================================================================== 大盘看板
#
# 为什么要在持仓面板里加大盘（v2.5.0）：
# 只看自己的持仓，会**把市场普跌误读成"我选错了"**，也会把"普涨里的滞涨"
# 当成正常。持仓跌 2% 是大事还是小事，取决于全市场在跌多少。
#
# 三条实现约束：
# ① 数据来自 `ctx.mood()`（带 TTL 缓存的取数回调），**渲染函数本身不打网络**；
# ② 任何失败都降级成一行说明，绝不让整个面板空白；
# ③ 只描述当下分布，不给"多空信号"、不预测。


def market_context(ctx: PanelContext) -> Optional[Dict[str, Any]]:
    """取一次大盘快照并做完全部派生计算（供大盘区块与「今日超额」共用）。

    ── 为什么必须只取一次（这关系到"刷新会不会闪/重播动画"）──
    面板每 5 秒刷新，宿主前端是**按内容是否变化**决定要不要重渲染的
    （`loadPluginPanel`：内容相同不 setState）。之前 `render_markdown` 里
    既调 `render_market()`、又为算超额单独调了一次 `ctx.mood()`：
    `ctx.mood()` 虽有 TTL 缓存，但两次调用**可能落在 TTL 边界两侧**
    （第一次命中旧值、第二次拿到新值），于是同一个面板里「上证 +1.08%」
    和「vs 上证 +1.09%」自相矛盾，且内容每次微变 → 宿主判定"变了"
    → 重渲染 → 入场动画重播。用户两次强调"刷新不要有动画"，
    根因就在这里：不是 CSS 问题，是**输出不稳定**。

    修法：整个面板只取一次快照，派生值全部从这里出。
    返回 None 表示大盘不可用（回调没注入 / 取数失败），调用方各自降级。
    """
    if ctx.mood is None:
        return None
    try:
        snap = ctx.mood()
    except Exception as exc:
        logger.debug("[astock] 大盘数据获取失败: %s", exc)
        return None
    if not isinstance(snap, dict):
        return None
    indices = snap.get("indices") or []
    breadth = snap.get("breadth")
    summary = mood.market_summary(indices, breadth, snap.get("amount"))
    by_code = {i.get("code"): i for i in indices if isinstance(i, dict)}
    return {
        "snap": snap,
        "indices": indices,
        "breadth": breadth,
        "summary": summary,
        "sh": by_code.get("000001") or {},
        "ss": by_code.get("399001") or {},
        "ranking": mood.style_ranking(indices),
        "ok": bool(indices or breadth),
    }


def render_market(ctx: PanelContext, d: Optional[Dict[str, Any]] = None,
                  mctx: Optional[Dict[str, Any]] = None) -> str:
    """大盘区块（置顶）：2 张 KPI 卡 + 一段「含义」文字。

    `mctx` 由 `market_context()` 预先算好传入（避免二次取数，见其注释）；
    不传时自己取一次（单独调用本函数的场景，如单测）。
    """
    if mctx is None:
        mctx = market_context(ctx)
    if mctx is None:
        return (_section("大盘") + "\n\n大盘数据获取失败（接口限流或断网），"
                "稍后自动重试")
    indices = mctx["indices"]
    breadth = mctx["breadth"]
    if not mctx["ok"]:
        return (_section("大盘") + "\n\n未取到指数与涨跌家数（接口限流或断网），"
                "稍后自动重试")

    summary = mctx["summary"]
    ratio = summary.get("ratio")
    tone = summary.get("tone") or "-"
    sh = mctx["sh"]
    ss = mctx["ss"]
    ranking = mctx["ranking"]

    out: List[str] = [_section("大盘", _market_phase())]

    # ── 大盘压成**两张卡**（v2.5.5，用户报障："上面五个，没必要"）。
    #    331px 宽下 5 张卡会排成 3 行，比原来的文字行还费地方。
    #    「一个卡里放多条数据」用 note 实现：
    #      卡1 上证   = 点位 + 涨跌 + 深证同比 + 成交额 + 涨跌家数
    #      卡2 领涨指数 = 最强的那个 + 四强明细
    #    超额（跑输/跑赢）**不在这里**——它属于「我的资产」，见 render_markdown。
    cards: List[Dict[str, Any]] = []

    # 卡1：上证（点位为主值，delta 涨跌，note 塞深证/成交/家数）
    sh_price = sh.get("price")
    if sh_price is not None or ss.get("price") is not None:
        main_price = sh_price if sh_price is not None else ss.get("price")
        main_pct = (sh.get("change_pct") if sh_price is not None
                    else ss.get("change_pct"))
        label = "上证" if sh_price is not None else "深证"
        bits: List[str] = []
        if sh_price is not None and ss.get("change_pct") is not None:
            bits.append(f"深证{_pct(ss.get('change_pct'))}")
        if summary.get("amount_yi"):
            bits.append(f"成交{summary['amount_yi'] / 10000:.2f}万亿")
        if breadth:
            bits.append(f"{breadth['up']}↑/{breadth['down']}↓")
        cards.append(_kpi(
            label, _f(main_price), _pct(main_pct),
            note=" · ".join(bits),
            trend="up" if (main_pct or 0) >= 0 else "down"))

    # 卡2：领涨指数（主值=最强指数名，delta=涨幅，note=四强全量 + 上涨占比）
    if ranking:
        parts = []
        for i in ranking[:4]:       # 只取前四强，够看出强弱
            chg = i.get("change_pct")
            short = _short_index_name(i.get("name") or "")
            if chg is not None:
                parts.append(f"{short}{chg:+.2f}%")
        best = ranking[0]
        if parts:
            note = " · ".join(parts)
            if ratio is not None:
                note += f"（上涨 {ratio:.0f}% {tone}）"
            cards.append(_kpi(
                "领涨指数", _short_index_name(best.get("name") or ""),
                _pct(best.get("change_pct")),
                note=note,
                trend="up" if (best.get("change_pct") or 0) >= 0 else "down"))

    if cards:
        out += ["```kpi", _json.dumps({"items": cards}, ensure_ascii=False),
                "```", ""]

    # ── 含义：一行说明，**小一号**（v2.5.6：用户要求"含义那段的字小一点"）
    style_note = _style_note(ranking) if ranking else ""
    if style_note:
        out += [_caption(f"含义　{style_note}"), ""]

    div = summary.get("divergence")
    if div:
        out += [div, ""]

    return "\n".join(out).rstrip()


def _market_phase() -> str:
    """交易时段短标签（盘中/收盘/未开盘）；失败不显示。"""
    try:
        st = mkt.trading_status()
        return str(st.get("phase") or "")
    except Exception:
        return ""


def _short_index_name(name: str) -> str:
    """指数简称：科创50指数 → 科创50；深证成指 → 深证。"""
    return (name.replace("指数", "").replace("成指", "").strip() or name)


def _style_note(ranking: List[Dict[str, Any]]) -> str:
    """把「指数强弱排序」翻译成人话（用户问过"风格是什么意思"）。

    只做相对比较与描述，不推断"该买什么"、不预测。

    与 `mood.style_verdict` 的分工：那个函数产出的是**带 markdown 加粗的
    完整句子**（供聊天侧直接输出）；面板要的是短语（跟在「含义」标签后），
    且必须去掉「风格」这类内部术语，所以这里另写一份措辞。
    两者判据一致（0.8pp 阈值），改阈值要同时改——单测钉住了这个数值。
    """
    if len(ranking) < 2:
        return ""
    best, worst = ranking[0], ranking[-1]
    best_pct = best.get("change_pct")
    worst_pct = worst.get("change_pct")
    if best_pct is None or worst_pct is None:
        return ""
    spread = best_pct - worst_pct
    best_n = _short_index_name(best["name"])
    worst_n = _short_index_name(worst["name"])
    if spread < 0.8:
        return (f"各指数涨跌接近（最强 {best_n} 与最弱 {worst_n} 仅差 "
                f"{spread:.2f}pp），没有明显结构分化")
    # 不用破折号（用户明确要求）：改为句号断句
    return (f"{best_n} 明显强于 {worst_n}（差 {spread:.2f}pp）。"
            f"资金更偏向 {best_n} 这类标的，不是全面普涨")


def _portfolio_excess(d: Dict[str, Any],
                      index_pct: Optional[float]) -> Optional[float]:
    """组合今日涨跌幅相对指数的超额（百分点）。

    组合涨跌幅用**市值加权**：小仓位大涨不该代表整个组合。
    分母是昨日市值（今日市值 / (1+涨幅) 的近似不可靠，直接用成本+
    昨收算），这里简化为「按持仓市值加权个股涨跌幅」——精确的做法需要
    昨收价，而 `change_pct` 已经是个股相对昨收的涨幅，加权即得组合涨幅。
    """
    positions = d.get("positions") or []
    if not positions:
        return None
    quotes = d.get("quotes") or {}
    total_mv = 0.0
    weighted = 0.0
    for p in positions:
        q = quotes.get(p.get("code")) or {}
        chg = q.get("change_pct")
        price = q.get("price") or p.get("cost_price") or 0
        mv = float(price) * int(p.get("shares") or 0)
        if chg is None or mv <= 0:
            continue
        total_mv += mv
        weighted += float(chg) * mv
    if total_mv <= 0:
        return None
    port_pct = weighted / total_mv
    d["today_pnl_pct"] = round(port_pct, 2)      # 供上层显示
    return mood.relative_strength(port_pct, index_pct)


def watch_line(app: Any) -> str:
    """面板里的盯盘状态行（含"去哪儿点按钮"的指引）。

    宿主面板是**只读**的（Markdown / lite-tree 都点不了），按钮只能放
    设置页——所以这里必须明确告诉用户点哪里，否则他会以为看板上能点。
    """
    try:
        enabled = _as_bool(_cfg(app, "watch_enabled", True), True)
        start_hhmm = str(_cfg(app, "watch_start_time", "") or "")
        ok_now, why = mkt.should_watch_now(start_hhmm=start_hhmm)
        st = mkt.trading_status()
    except Exception:
        return ""
    if not enabled:
        return ("⏸ **盯盘已暂停**　到「设置 → 插件 → A股助手」点"
                "「启动 / 暂停盯盘」恢复")
    if ok_now:
        return f"🟢 **正在盯盘**（{st['phase']}）"
    # 块内空行分段：单换行会被 markdown 渲染器合并成一行（v1.7.1）
    return (f"⚪ **当前不盯盘**：{why}　"
            f"下次 {mkt.next_watch_window(start_hhmm=start_hhmm)}"
            f"\n\n　时段自律：工作日 {mkt._resolve_start(start_hhmm)}–15:00，午休跳过，收盘自动停")


def render_markdown(ctx: PanelContext, d: Dict[str, Any], flag: str) -> str:
    """markdown 形态：富围栏版（宿主面板已支持 ```kpi / ```cards / ```progress）。

    ── 四块版式（v2.5.5，用户连续三轮报障后的定稿）──
      ① **大盘**——**2 张**指标卡（上证含深证/成交/家数 + 领涨指数含四强）；
      ② **我的资产**——4 张指标卡 + **今日超额卡**（跑输/跑赢在这里，不在大盘）；
      ③ **持仓**——**每只一张卡片**（用户明确要求），有风险的才额外画进度条；
      ④ **自选观察**——3 列窄表。

    变化史（别再走回头路）：
      v2.5.3 把持仓做成**表格** → 用户否决："我让你每一个持仓一个卡片"。
      v2.5.4 大盘散成 5 张卡 → 用户否决："上面五个，没必要"。
      本版把大盘压回 2 张、超额归入资产、持仓恢复一票一卡。

    老环境兜底：宿主若不支持富围栏（旧版 lite-work），围栏内容显示为代码块，
    数字仍在 JSON 里可读，不至于空白。
    """
    wline = watch_line(ctx.app)
    # **只取一次大盘快照**，两处（大盘区块 / 今日超额）共用同一份派生值。
    # 二次调用 ctx.mood() 会落在 TTL 边界两侧拿到不同数据 → 输出抖动 →
    # 宿主判定"内容变了" → 重渲染 → 入场动画重播（用户两次强调的痛点）。
    mctx = market_context(ctx)
    market_block = render_market(ctx, d, mctx) if mctx is not None else ""
    equity = (d.get("total_market_value") or 0) + (d.get("cash") or 0)

    if not d["positions"]:
        # 空仓时大盘**更要显示**：此时用户最关心的是「市场转暖了吗、要不要进场」，
        # 而面板此前在空仓时只留一句"当前空仓"，等于什么信息都不给。
        out: List[str] = []
        if market_block:
            out += [market_block, "", "---", ""]
        out += [_section("我的资产", "空仓"),
                f"总资产 {_money(equity)}　可用资金 {_money(d.get('cash') or 0)}",
                "",
                "当前空仓。录入成交："
                "`astock_record side=buy code=... price=... shares=...`"]
        if wline.strip():
            out += ["", "---", "", _section("盯盘"), "", wline.strip()]
        return "\n".join(out).rstrip() + "\n"

    # ── ① 大盘（置顶）
    out = []
    if market_block:
        out += [market_block, "", "---", ""]

    # ── ② 我的资产：4 张 KPI 卡 + **今日超额卡**。
    #    删掉了原先的 `_head()` 长句——它把"2 只持仓｜市值｜浮盈｜可用"
    #    又写一遍，与这 4 张卡完全重复，是"顶部太挤"的主因。
    #    超额（跑输/跑赢）从大盘挪到这里（v2.5.5）：它是"我的"结果，
    #    不是大盘信息，放一起用户才看得出「市场涨 vs 我涨」的对照。
    total_mv = d.get("total_market_value") or 0
    pnl_v = d.get("float_pnl") or 0
    today_pnl = d.get("today_pnl") or 0
    cash = d.get("cash") or 0
    asset_cards: List[Dict[str, Any]] = [
        _kpi("总资产", _money(equity), f"今日 {_f(today_pnl)}",
             trend="up" if today_pnl >= 0 else "down"),
        _kpi("持仓市值", _money(total_mv),
             note=f"占总资产 {total_mv / equity * 100:.0f}%" if equity else ""),
        _kpi("浮动盈亏", _f(pnl_v), _pnl_pct_of(d),
             trend="up" if pnl_v >= 0 else "down"),
        _kpi("可用资金", _money(cash),
             note=f"占总资产 {cash / equity * 100:.0f}%" if equity else ""),
    ]
    # 今日超额：主值=超额 pp（红绿按跑赢/跑输），delta=定性，note=原因。
    # 上证涨跌幅取自**已算好的** mctx（不二次取数，见上方注释）。
    sh_pct_now = (mctx or {}).get("summary", {}).get("sh_pct")
    pos_excess = _portfolio_excess(d, sh_pct_now)
    if pos_excess is not None:
        rs = mood.rs_verdict(pos_excess)
        if rs and rs != "基本同步":
            asset_cards.append(_kpi(
                "今日超额", f"{pos_excess:+.2f}pp", rs,
                note=("持仓拖了后腿" if pos_excess < 0 else "持仓强于大盘"),
                trend="up" if pos_excess >= 0 else "down"))
        else:
            asset_cards.append(_kpi(
                "今日超额", f"{pos_excess:+.2f}pp", "基本同步",
                note=f"vs 上证 {_pct(sh_pct_now)}", trend="flat"))
    out += [_section("我的资产", f"{len(d['positions'])} 只持仓"),
            "```kpi",
            _json.dumps({"items": asset_cards}, ensure_ascii=False),
            "```", ""]

    # ── ③ 持仓：**一只一张卡片**（v2.5.4，用户明确要求："每一个持仓一个卡片"）
    cfg = ctx.hints_cfg()
    # 单票占比的分母＝账户总资产（持仓市值 + 可用现金），与 hint 引擎同口径。
    # 用持仓市值当分母时，只持一只股就恒为 100%，仓位条永远画成「已超限」。
    equity_base = equity if equity > 0 else total_mv
    cap = float(getattr(cfg, "position_cap_pct", 25.0) or 25.0)
    stop_pct = abs(float(cfg.stop_loss_pct or 0))

    cards_pos: List[Dict[str, Any]] = []
    progress_items: List[Dict[str, Any]] = []
    for p in d["positions"][:MAX_ROWS]:
        q = d["quotes"].get(p["code"]) or {}
        price = q.get("price") or p["cost_price"]
        cost = float(p["cost_price"] or 0)
        shares = int(p["shares"] or 0)
        mv = float(price) * shares
        pnl = (float(price) - cost) * shares
        pnl_pct = mkt.pct_change(price, cost)
        chg_pct = q.get("change_pct")
        name = p["name"] or p["code"]

        # 风险判定：与 hint 引擎同一套规则（不另立标准，避免"面板说没事、
        # astock_hint 说该卖"这种自相矛盾）。另加「逼近止损」这一条——
        # hint 只在**跌破**阈值时报警，而破位前 30% 的余量才是最该看的。
        weight = (mv / equity_base * 100) if equity_base > 0 else 0.0
        used = (max(0.0, min(100.0, -pnl_pct / stop_pct * 100.0))
                if (pnl_pct is not None and pnl_pct < 0 and stop_pct > 0) else 0.0)
        headroom = 100.0 - used
        try:
            hs = hints.build_position_hints(p, q, cfg, total_market_value=total_mv,
                                            total_equity=equity_base)
        except Exception:      # 单票 hint 算挂了不能拖垮整个面板
            hs = []
        severe = [h for h in hs if h.severity in ("action", "urgent")]
        covered = {h.kind for h in severe}   # hint 已报过的，别再重复一遍
        reasons: List[str] = []
        if severe:
            top = max(severe, key=lambda h: 2 if h.severity == "urgent" else 1)
            reasons.append(_hint_reason(top))
        # 超仓：hint 引擎已产出 position_cap 时不再重复（否则同一件事复读两遍）
        if "position_cap" not in covered and equity_base > 0 and weight > cap:
            reasons.append(f"单票占比 {weight:.1f}% 超过上限 {cap:g}%，"
                           f"建议减到 {cap:g}% 以内")
        # 逼近止损：hint 只在**跌破**阈值才报，破位前 30% 的余量才是最该看的；
        # 但 hint 已报 stop_loss 时同样不重复。
        if ("stop_loss" not in covered and stop_pct > 0 and pnl_pct is not None
                and pnl_pct < 0 and headroom < 30):
            reasons.append(f"距止损只剩 {headroom:.0f}%（浮亏 {abs(pnl_pct):.1f}% / "
                           f"阈值 −{stop_pct:g}%）")

        # 卡片：**一票一张 kpi 卡**（v2.5.5）。
        #    为什么用 kpi 而不是 cards：宿主 `cards` 组件的 CardItem 只有
        #    title/price/desc/tags，**没有颜色字段**，涨跌只能是黑字；
        #    而 kpi 卡的 delta 走 trend（up=绿 / down=红），是宿主里唯一
        #    能实现「红涨绿跌」的卡片形态（用户明确要求："卡片内能涨跌
        #    为什么不能用红色绿色的那种表示"）。
        #    ▲▼ 由前端按 trend 自动加，这里只传纯数字百分比。
        #    note 只放**数值事实**（盈亏/股数/仓位/可卖）；"为什么该处理"
        #    另起一行写在卡片下方——两处都写会复读（实测踩到）。
        pnl_word = "浮盈" if pnl >= 0 else "浮亏"
        bits = [f"{pnl_word} {_f(pnl)}（{_pct(pnl_pct)}）", f"{shares} 股"]
        if equity_base > 0:
            bits.append(f"占仓 {weight:.0f}%")
        available = int(p.get("available_today") or shares)
        if available < shares:
            bits.append(f"可卖 {available}")
        cards_pos.append(_kpi(
            name, _f(price), _pct(chg_pct),
            note=" · ".join(bits),
            trend="up" if (chg_pct or 0) >= 0 else "down",
            sort=(chg_pct if chg_pct is not None else -999),
            reasons=reasons))

        # 进度条：**只给真有风险的票**（仓位超限 / 浮亏已进黄区-红区）——
        # 全绿满条等于没信息，还会把卡片挤长。
        # 阈值 40 与宿主色阶一致（<40 红 / 40-79 黄 / ≥80 绿）：余量 ≥40%
        # 说明这条进度条本身就会画成黄绿，属于"没事"，不展示。
        pi: List[Dict[str, Any]] = []
        if equity_base > 0 and cap > 0 and weight > cap:
            pi.append({"label": f"{name} 仓位空间（上限 {cap:g}%）",
                       "value": round(max(0.0, (cap - weight) / cap * 100), 1),
                       "note": f"现占 {weight:.1f}% · 超限"})
        if stop_pct > 0 and pnl_pct is not None and pnl_pct < 0 and headroom < 40:
            pi.append({"label": f"{name} 距止损余量（跌破 -{stop_pct:g}% 触发）",
                       "value": round(headroom, 1),
                       "note": _pct(pnl_pct)})
        progress_items += pi

    # 按今日涨跌排序：涨的在上（用户第一眼该看到"今天谁在拉我"）。
    # 行情缺失（chg 为 None）的排末尾——不能让"没数据"混在中间冒充持平。
    cards_pos.sort(key=lambda c: c.get("_sort", -999), reverse=True)
    # 风险原因单独收集：kpi 卡的 note 会被前端省略号截断，把"为什么"
    # 塞进 note 会看不见，所以另起一行写在卡片下面。
    reasons_lines: List[str] = []
    for c in cards_pos:
        c.pop("_sort", None)          # 内部排序键不能漏进宿主渲染的 JSON
        rs = c.pop("_reasons", None)
        if rs:
            reasons_lines.append(f"- **{c['label']}**：{rs[0]}")

    out += [_section("持仓", f"{len(cards_pos)} 只 · 按今日涨跌"),
            "```kpi",
            _json.dumps({"items": cards_pos}, ensure_ascii=False),
            "```", ""]
    if reasons_lines:
        out += ["\n".join(reasons_lines), ""]
    if progress_items:
        out += ["```progress",
                _json.dumps({"items": progress_items[:8]}, ensure_ascii=False),
                "```", ""]

    # ── 自选区（未持仓）：紧凑表 + 备注提示。与聊天看板「无需关注」列对齐——
    #    面板此前只画持仓，自选完全不出现，用户换 tab 看不到候选池。
    wq = d.get("watch_quotes") or {}
    wcount = int(d.get("watch_count") or 0)
    if wq:
        # 5 列（含日低/日高）在 331px 里必然横向滚动，压到 3 列；
        # 日低/日高属于"要做日内"才看的字段，面板不抢这个空间。
        # 代码保留（自选是候选买入池，下单要代码），但用半角空格代替
        # 全角括号——少两个全角字符宽，窄栏里差别很明显。
        out.append(_section("自选观察", f"{len(wq)} 只 · 未持仓"))
        out.append("")
        wrows = []
        for code, q in list(wq.items())[:MAX_ROWS]:
            # 行情没带 name 时**不能**退回 code——否则拼出来是 `002532 002532`
            # 这种代码写两遍的怪行（自选股名称缺失很常见：行情源偶尔只回
            # 价格/涨跌幅）。缺名时只留代码。
            name = q.get("name")
            head = f"{name} {code}" if name else code
            wrows.append([head, _f(q.get("price")),
                          _arrow_pct(q.get("change_pct"))])
        out.append(_table(["名称", "现价", "今日"], wrows))
        out.append("")
    elif wcount > 0:
        # 行情拉取失败不得静默消失（此前 if wq: 直接跳过整节，用户无从分辨
        # 「没自选」还是「拉挂了」——真实报障：自选表不见了）。
        # 注意：只出提示行，不再画表（此时没有行情数据可画）。
        out.append(f"**自选观察**（未持仓）：{wcount} 只行情未取到"
                   "（接口限流或断网），稍后自动重试")
        out.append("")

    # ── 状态区：盯盘与舆情是两个语义不同的部分，各自成块、各带小标题。
    #    要点：① 分隔用标准 markdown 分割线 `---`（自造的 `──` 只是普通
    #    文本，渲染器不认）；② 块内行间用空行分段——单换行会被 markdown
    #    合并成一行，三行状态就会粘在一起（v1.7.1 修复的真正原因）。
    blocks: List[str] = []
    if wline.strip():
        blocks.append(_section("盯盘") + "\n\n" + wline.strip())
    if flag and flag.strip():
        blocks.append(_section("舆情") + "\n\n" + flag.strip())
    if blocks:
        out += ["", "---", ""]
        out.append("\n\n".join(blocks))
    return "\n".join(x for x in out if x is not None).rstrip() + "\n"


def render_tree(ctx: PanelContext, d: Dict[str, Any], flag: str) -> str:
    """lite-tree 决策树形态：一只股一张卡，卡内写触发规则与建议动作。

    `trees[].options` 在原协议里表示"多个候选方案及其概率"。持仓提示不是
    概率选择，因此每卡的第一条 option 承载"当前建议动作"，prob 恒为 1.0、
    chosen=true；`kind` 承载信号类型，`question` 承载规则标题，`tone` 承载
    严重度——既复用现成渲染，又不伪造概率语义。

    **`prob` 会被前端渲染成一条彩色进度条**（label + 横条 + 百分比，宿主实测）。
    这是面板里**唯一能被"看见"的图形元素**，因此把它当可视化通道复用：不表示
    概率，而表示**仓位占比 / 距止损的余量**这类 0-100 的刻度。每卡最多 3 条。
    """
    def _scale(label: str, value: float, tone: str = "alt") -> Dict[str, Any]:
        """把 0-100 的刻度值借 option.prob 通道渲染成进度条。"""
        return {"label": label,
                "prob": max(0.0, min(1.0, float(value) / 100.0)),
                "tone": tone, "chosen": False}

    positions = d["positions"][:MAX_ROWS]
    if not positions:
        wl = watch_line(ctx.app).strip()
        return ("**0 只持仓**｜当前空仓\n\n"
                "录入成交：`astock_record side=buy code=... price=... shares=...`"
                + (f"\n\n---\n\n{_section('盯盘')}\n\n{wl}" if wl else ""))
    cfg = ctx.hints_cfg()
    total_mv = d["total_market_value"] or 0
    # 单票占比的分母＝账户总资产（持仓市值 + 可用现金）。
    # 用持仓市值当分母时，只持一只股就恒为 100%，正常仓位会被标成超限。
    equity_base = ((d.get("equity") or 0)
                   or (total_mv + (d.get("cash") or 0)))
    cards: List[Dict[str, Any]] = []
    for p in positions:
        q = d["quotes"].get(p["code"]) or {}
        price = q.get("price") or p["cost_price"]
        pnl = (price - p["cost_price"]) * p["shares"]
        pct = mkt.pct_change(price, p["cost_price"])
        hs = hints.build_position_hints(p, q, cfg, total_market_value=total_mv,
                                        total_equity=equity_base)
        t1 = hints.build_t1_hint(p, int(p.get("available_today") or 0))
        if t1:
            hs.append(t1)
        # 只展示最要紧的一条：卡片是"扫一眼"场景，堆 4 条反而看不出重点
        top_h = (max(hs, key=lambda h: {"urgent": 2, "action": 1}.get(h.severity, 0))
                 if hs else None)
        if top_h:
            tone = {"urgent": "danger", "action": "warn"}.get(top_h.severity,
                                                              "neutral")
            kind, question, label = top_h.kind, top_h.title, top_h.suggestion
        else:
            tone = "neutral"
            kind = "holding"
            question = "未触发规则区间"
            label = (f"继续持有（止损 -{cfg.stop_loss_pct}% / "
                     f"止盈 +{cfg.take_profit_pct}%）")
        # 刻度语义与 markdown 形态对齐（v1.7.2）：一律「安全余量」——
        # 条越长越安全；触线/超限 → 0 → 短条。此前「已消耗量」画满条
        # 反而像"很健康"（语义反转）。
        # 刻度①：仓位空间 = 距上限的余量（超限 = 0）
        weight = (p["shares"] * price / equity_base * 100) if equity_base > 0 else 0.0
        cap = float(getattr(cfg, "position_cap_pct", 25.0) or 25.0)
        room = max(0.0, (cap - weight) / cap * 100) if cap > 0 else 0.0
        # 刻度②：距止损余量 = 1 - 浮亏消耗（触线 = 0）
        # 阈值配成 0 ＝ 关闭止损提示，此时不画（避免除零）
        stop_pct = abs(float(cfg.stop_loss_pct or 0))
        used = (max(0.0, min(100.0, -pct / stop_pct * 100.0))
                if (pct is not None and pct < 0 and stop_pct > 0) else 0.0)

        opts: List[Dict[str, Any]] = [
            {"label": (label or "")[:80], "prob": 1.0,
             "tone": tone, "chosen": True}]
        if equity_base > 0:
            opts.append(_scale(
                f"仓位空间 {room:.0f}%（现占 {weight:.1f}% / 上限 {cap:g}%）",
                room, "danger" if weight > cap else "alt"))
        if pct is not None and stop_pct > 0:
            opts.append(_scale(
                (f"距止损余量 {100 - used:.0f}%（浮亏 {abs(pct):.1f}% / "
                 f"阈值 −{stop_pct:g}%）"
                 if pct < 0 else f"浮盈 {pct:+.1f}%（未接近止损）"),
                100.0 - used, "danger" if used >= 100.0 else "alt"))
        cards.append({
            "title": f"{p['name'] or p['code']}  {_f(price)}  {_pct(pct)}",
            "context": (f"{p['shares']}股 · 成本{_f(p['cost_price'], 3)}"
                        f" · 浮盈{_f(pnl)} · 可卖{int(p.get('available_today') or 0)}"
                        f" · 市值{_money(p['shares'] * price)}"),
            "kind": kind,
            "question": question,
            "options": opts[:3],
            "confidence": 1.0,
            "accepted": True,
        })
    payload = {
        "type": "lite-tree",
        "status": {
            "text": f"{len(positions)} 只持仓",
            "tone": "ok" if (d["float_pnl"] or 0) >= 0 else "warn",
            "meta": [f"市值 {_money(d['total_market_value'])}",
                     f"浮盈 {_f(d['float_pnl'])}",
                     f"今日 {_f(d['today_pnl'])}",
                     f"可用 {_money(d['cash'])}"],
            "chips": ([flag.split("：")[0].replace("*", "").strip()]
                      if flag else []) + [
                "盯盘中" if mkt.should_watch_now(start_hhmm=str(_cfg(ctx.app, "watch_start_time", "") or ""))[0] else mkt.next_watch_window(start_hhmm=str(_cfg(ctx.app, "watch_start_time", "") or "")),
                "每 5 秒刷新"],
        },
        "trees": cards,
        "total": len(cards),
    }
    return _json.dumps(payload, ensure_ascii=False)
