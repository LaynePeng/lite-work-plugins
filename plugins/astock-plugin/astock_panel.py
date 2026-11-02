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


def _head(d: Dict[str, Any]) -> str:
    return (f"**{len(d['positions'])} 只持仓**｜市值 {_money(d['total_market_value'])}｜"
            f"浮盈 {_f(d['float_pnl'])}（今日 {_f(d['today_pnl'])}）｜"
            f"可用 {_money(d['cash'])}")


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


def render_market(ctx: PanelContext, d: Optional[Dict[str, Any]] = None) -> str:
    """大盘与整体形势看板（kpi + 表格 + 背离提示）。

    回答三个面板此前无法回答的问题：
      · 今天大盘什么状态？（指数 + 涨跌家数 + 成交额）
      · 是什么风格在涨？（各指数强弱排序）
      · 我的持仓相对大盘如何？（超额收益）

    `d` 是持仓快照（可为 None／空仓）。**只在有持仓时**才渲染「持仓 vs 大盘」，
    空仓时那一段没有意义（没有持仓可比较）。
    """
    if ctx.mood is None:
        return ""
    try:
        snap = ctx.mood()
    except Exception as exc:
        logger.debug("[astock] 大盘数据获取失败: %s", exc)
        return "**大盘**\n\n　　大盘数据获取失败（接口限流或断网），稍后自动重试"
    indices = snap.get("indices") or []
    breadth = snap.get("breadth")
    if not indices and not breadth:
        return ("**大盘**\n\n　　未取到指数与涨跌家数"
                "（接口限流或断网），稍后自动重试")

    total_amount = snap.get("amount")
    summary = mood.market_summary(indices, breadth, total_amount)
    ratio = summary.get("ratio")
    tone = summary.get("tone") or "-"
    sh_pct = summary.get("sh_pct")

    by_code = {i["code"]: i for i in indices}
    sh = by_code.get("000001") or {}
    ss = by_code.get("399001") or {}

    # ── 大盘压到**两张卡**（v2.5.2）。
    #    用户明确要求："大盘就在一到两个卡片里"，而持仓才是重点。
    #    因此把原来 4 张 KPI + 指数表 + 三段说明压成：
    #      卡1「大盘」= 上证/深证涨跌 + 上涨占比定性
    #      卡2「我的持仓 vs 大盘」= 组合涨幅与超额（**这张才是用户真正关心的**）
    #    指数强弱排序、涨跌家数明细、背离提示都收进一张卡的文字里（不占块）。
    cards: List[Dict[str, Any]] = []

    # 卡1：大盘一句话（点位 + 涨跌幅 + 上涨占比）
    mkt_bits = []
    if sh.get("price") is not None:
        mkt_bits.append(f"上证 {_f(sh.get('price'))} {_pct(sh.get('change_pct'))}")
    if ss.get("price") is not None:
        mkt_bits.append(f"深证 {_pct(ss.get('change_pct'))}")
    cards.append({
        "label": "大盘",
        "value": f"{ratio:.0f}% {tone}" if ratio is not None else (tone or "-"),
        "delta": "　".join(mkt_bits) if mkt_bits else "指数未取到",
        "trend": "up" if (sh_pct or 0) >= 0 else "down",
    })

    # 卡2：持仓 vs 大盘（超额收益）——用户最关心的对照
    pos_excess = _portfolio_excess(d, sh_pct) if d else None
    if pos_excess is not None:
        cards.append({
            "label": "我的持仓",
            "value": _pct(d.get("today_pnl_pct")),
            "delta": f"vs 上证 {_pct(sh_pct)}　超额 {pos_excess:+.2f}pp",
            "trend": "up" if pos_excess >= 0 else "down",
        })

    out: List[str] = ["```kpi",
                      _json.dumps({"items": cards}, ensure_ascii=False),
                      "```", ""]

    # ── 卡片下只留**一行**补充：指数强弱排序（看出风格）+ 涨跌家数明细。
    #    不放表格、不拆成多段——右栏太窄，多一段就要滚动。
    ranking = mood.style_ranking(indices)
    if ranking:
        parts = []
        for i in ranking[:4]:       # 只取前四强，够看出风格
            chg = i.get("change_pct")
            short = (i["name"].replace("指数", "").replace("成指", "").strip()
                     or i["name"])
            if chg is not None:
                parts.append(f"{short}{chg:+.2f}%")
        if parts:
            out.append(f"　风格：{' · '.join(parts)}")

    if breadth and summary.get("amount_yi"):
        out.append(f"　{breadth['up']}↑ / {breadth['down']}↓　"
                   f"成交 {summary['amount_yi'] / 10000:.2f} 万亿")

    div = summary.get("divergence")
    if div:
        out.append(div)

    rs = mood.rs_verdict(pos_excess) if pos_excess is not None else ""
    if rs and rs != "基本同步":
        out.append(f"　→ 今日{rs}"
                   + ("，持仓拖了后腿" if pos_excess < 0 else "，持仓强于大盘"))
    return "\n".join(x for x in out if x is not None).rstrip()


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
    """markdown 形态：富围栏版（宿主面板已支持 ```kpi / ```progress 渲染）。

    结构：
    - 头部一句纯文本合计（面板标题条之外的眼睛锚点）；
    - ```kpi：4 张指标卡（总资产/持仓市值/浮动盈亏/可用资金）——数字大、
      一眼看到钱；
    - 每只持仓一段：名称行（markdown）+ ```progress（仓位占比、距止损余量
      两根进度条，超限自动红）；
    - 老环境兜底：宿主若不支持富围栏（旧版 lite-work），围栏内容会显示为
      代码块——仍可读（JSON 里的数字都在），不至于空白。
    """
    head = _head(d)
    wline = watch_line(ctx.app)
    if not d["positions"]:
        # 空仓时大盘**更要显示**：此时用户最关心的是「市场转暖了吗、要不要进场」，
        # 而面板此前在空仓时只留一句"当前空仓"，等于什么信息都不给。
        empty = head + ("\n\n当前空仓。录入成交："
                        "`astock_record side=buy code=... price=... shares=...`")
        market_block = render_market(ctx, d)
        if market_block:
            empty += "\n\n---\n\n" + market_block
        if wline.strip():
            empty += "\n\n---\n\n**盯盘**\n\n" + wline.strip()
        return empty

    # ── KPI 卡：先回答"我的钱怎么样了" ──
    equity = (d.get("total_market_value") or 0) + (d.get("cash") or 0)
    pnl_v = d.get("float_pnl") or 0
    kpi = {
        "items": [
            {"label": "总资产", "value": _f(equity),
             "delta": f"今日 {_f(d.get('today_pnl') or 0)}",
             "trend": "up" if (d.get("today_pnl") or 0) >= 0 else "down"},
            {"label": "持仓市值", "value": _money(d.get("total_market_value") or 0),
             "trend": "flat"},
            {"label": "浮动盈亏", "value": _f(pnl_v),
             "delta": _pnl_pct_of(d), "trend": "up" if pnl_v >= 0 else "down"},
            {"label": "可用资金", "value": _money(d.get("cash") or 0),
             "trend": "flat"},
        ]}
    out = [head, "", "```kpi", _json.dumps(kpi, ensure_ascii=False), "```", ""]

    # ── 大盘看板（v2.5.0）：先给市场背景，再看自己的持仓。
    #    顺序有讲究：不知道大盘怎么样时，「浮亏 -8%」这个数字没有意义。
    market_block = render_market(ctx, d)
    if market_block:
        out += [market_block, "", "---", ""]

    # ── 每股：名称行 + progress 双条（仓位/距止损）。
    #    v2.5.2 回滚了 v2.5.1 的"最多一条"压缩：用户明确说**持仓才是重点**，
    #    把两条合成一条是为了给大盘腾地方——方向错了。正确做法是**压缩大盘**
    #    （已压到 2 张卡 + 2 行字），把空间还给持仓。
    cfg = ctx.hints_cfg()
    total_mv = d.get("total_market_value") or 0
    # 单票占比的分母＝账户总资产（持仓市值 + 可用现金），与 hint 引擎同口径。
    # 用持仓市值当分母时，只持一只股就恒为 100%，仓位条永远画成「已超限」。
    equity_base = equity if equity > 0 else total_mv
    cap = float(getattr(cfg, "position_cap_pct", 25.0) or 25.0)
    stop_pct = abs(float(cfg.stop_loss_pct or 0))
    for p in d["positions"][:MAX_ROWS]:
        q = d["quotes"].get(p["code"]) or {}
        price = q.get("price") or p["cost_price"]
        cost = float(p["cost_price"] or 0)
        shares = int(p["shares"] or 0)
        mv = price * shares
        pnl = (price - cost) * shares
        pnl_pct = mkt.pct_change(price, cost)
        chg_pct = q.get("change_pct")
        arrow = "▲" if (chg_pct or 0) > 0 else ("▼" if (chg_pct or 0) < 0 else "—")
        out.append(f"**{p['name'] or p['code']}**　{_f(price)}　{arrow}{_pct(chg_pct)}"
                   f"　{shares}股 · 成本{_f(cost, 3)} · 浮盈{_f(pnl)}（{_pct(pnl_pct)}）")

        # progress 语义对齐宿主色阶（<40 红 / ≥80 绿，为「健康度」设计）：
        # 一律喂**安全余量**——越高越安全越绿，触线/超限 = 0% = 红。
        # 此前喂「已消耗量」（浮亏触到止损线 → 100% 满条**绿色**，看着
        # 最健康实际最危险——语义反转，真实报障）。
        items: List[Dict[str, Any]] = []
        if equity_base > 0 and cap > 0:
            weight = mv / equity_base * 100
            headroom = max(0.0, (cap - weight) / cap * 100)
            items.append({"label": f"仓位空间（上限 {cap:g}%）",
                          "value": round(headroom, 1),
                          "note": (f"现占 {weight:.1f}%" +
                                   (" · 超限" if weight > cap else ""))})
        if pnl_pct is not None and stop_pct > 0:
            used = max(0.0, min(100.0, -pnl_pct / stop_pct * 100)) if pnl_pct < 0 else 0
            items.append({"label": f"距止损余量（跌破 -{stop_pct:g}% 触发）",
                          "value": round(100.0 - used, 1),
                          "note": _pct(pnl_pct) if pnl_pct < 0 else "浮盈中"})
        if items:
            out += ["```progress",
                    _json.dumps({"items": items}, ensure_ascii=False),
                    "```", ""]
    # ── 自选区（未持仓）：紧凑表 + 备注提示。与聊天看板「无需关注」列对齐——
    #    面板此前只画持仓，自选完全不出现，用户换 tab 看不到候选池。
    wq = d.get("watch_quotes") or {}
    wcount = int(d.get("watch_count") or 0)
    if wq:
        out.append("**自选观察**（未持仓）")
        rows = []
        for code, q in list(wq.items())[:MAX_ROWS]:
            chg = q.get("change_pct")
            arrow = "▲" if (chg or 0) > 0 else ("▼" if (chg or 0) < 0 else "—")
            rows.append([f"{q.get('name') or code}（{code}）", _f(q.get("price")),
                         f"{arrow}{_pct(chg)}", _f(q.get("low")), _f(q.get("high"))])
        out.append(_table(["名称", "现价", "涨跌", "日低", "日高"], rows))
        out.append("")
    elif wcount > 0:
        # 行情拉取失败不得静默消失（此前 if wq: 直接跳过整节，用户
        # 无从分辨「没自选」还是「拉挂了」——真实报障：自选表不见了）。
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
        blocks.append("**盯盘**\n\n" + wline.strip())
    if flag and flag.strip():
        blocks.append("**舆情**\n\n" + flag.strip())
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
                + (f"\n\n---\n\n**盯盘**\n\n{wl}" if wl else ""))
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
