"""聊天侧的富围栏渲染：选股结果 → ```kpi + ```board + ```table。

── 为什么单独一层 ──────────────────────────────────────────────────────
v2.2.0 之前，选股结果在聊天里是**全角空格缩进的纯文本列表**：

    趋势动量
    　天娱数科 400股 @8.24（得分 88）

三个问题：① 窄屏/中文标点下根本不对齐，"400股"和"100股"错位；② 没有
层级，三策略六只票是同一块灰字，扫不出"哪只是共识"；③ 数字与建议混在
一行，要核对金额得自己在脑子里乘。

改成富围栏后各自的职责：
    ```kpi    答"这轮扫描的规模与钱"——一眼看到可买几只、投多少；
    ```board  答"买哪几只、每只多少股、挂什么价"，tone 直接表达共识度；
    ```table  答"我要照抄的明细"——窄栏/宽栏都能横向对齐逐项核对。

── 与 astock_panel 的分工（别合并）────────────────────────────────────
右栏只有约 331px，用 `lite-tree` + 进度条（`astock_quant_tool.build_compare_tree`）；
聊天区宽，用 board 三列 + 表格。**同一份数据，两个前端**，合并会让两边都变形。
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional, Sequence

import astock_quant as quant

#: 宿主 board 围栏的字段上限（超了前端会静默硬截，可能断在半句上）
_TITLE_MAX = 80
_DESC_MAX = 200
_TAG_MAX = 20

#: 得分 → 颜色语义：只表达"在本轮候选池里排得靠前"，不是上涨概率
_SCORE_GOOD = 70.0
_SCORE_FAIR = 50.0


# ====================================================================== 小工具


def _clip(text: Any, limit: int) -> str:
    """按宿主字段上限截断，加省略号（静默硬截会断在半句话上）。"""
    t = str(text if text is not None else "").strip()
    if len(t) <= limit:
        return t
    return t[: max(1, limit - 1)] + "…"


def _f(value: Any, digits: int = 2, dash: str = "-") -> str:
    if value in (None, ""):
        return dash
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct_text(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


def _money(value: Any) -> str:
    """金额 → 元（选股单笔都是几百到几千，用"万"反而看不出量级）。"""
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):,.0f} 元"
    except (TypeError, ValueError):
        return str(value)


def _score_tone(score: Optional[float]) -> str:
    if score is None:
        return "neutral"
    return ("good" if score >= _SCORE_GOOD
            else "neutral" if score >= _SCORE_FAIR else "bad")


def _fence(name: str, payload: Dict[str, Any]) -> str:
    return f"```{name}\n" + _json.dumps(payload, ensure_ascii=False,
                                        separators=(",", ":")) + "\n```"


def _stop_take(a: Any) -> str:
    sl = f"{a.stop_loss:.2f}" if getattr(a, "stop_loss", None) else "-"
    tp = f"{a.take_profit:.2f}" if getattr(a, "take_profit", None) else "-"
    return f"止损 {sl} / 止盈 {tp}"


def _pick_line(a: Any) -> str:
    """一只票的"动作摘要"——board 卡片的 desc 首行就靠这句话。"""
    return (f"{a.shares} 股 ≈ {_money(a.amount)}"
            f"（占权益 {a.weight_pct:.1f}%）")


def _risk_line(summary: Dict[str, Any]) -> str:
    """止损/止盈口径一行（缺失时给破折号，不抛错）。"""
    sl = summary.get("stop_loss_pct")
    tp = summary.get("take_profit_pct")
    if sl is None and tp is None:
        return "见明细表"
    return (f"止损 {sl:g}% / 止盈 {tp:g}%"
            if sl is not None and tp is not None
            else (f"止损 {sl:g}%" if sl is not None else f"止盈 {tp:g}%"))


# ====================================================================== 单策略


def render_pick(result: Dict[str, Any], max_rows: int = 10) -> str:
    """单策略选股 → kpi + board + table（聊天里的"照着做"版）。

    分段顺序就是用户的阅读顺序：**规模与钱 → 买什么 → 逐项核对 → 口径**。

    为什么不直接包一层 `quant.format_pick_report` 的文字版：那个版本的
    表格是 markdown 管道表（8 列在窄屏会挤成竖条），且一次给全 8 列数字，
    没有"先看三件事、再核对细节"的层次。这里重排成
    `kpi`（4 个数字）→ `board`（一票一卡，能直接照抄）→ `table`（明细），
    文字版只保留**读一遍**的内容（预算口径、跳过原因、免责）。
    """
    strat = quant.STRATEGIES.get(result.get("strategy") or "") \
        or quant.STRATEGIES[quant.DEFAULT_STRATEGY]
    allocations: Sequence[Any] = list(result.get("allocations") or [])[:max_rows]
    skipped: Sequence[Any] = list(result.get("skipped") or [])
    summary: Dict[str, Any] = result.get("summary") or {}

    out: List[str] = [f"**量化选股 + 买入计划**（策略：{strat.label}）", "",
                      f"> {strat.description}", ""]

    # ── KPI：先答"这轮扫了多少、我能买几只、要花多少钱" ──
    out.append(_fence("kpi", {"items": [
        {"label": "全市场扫描", "value": f"{int(result.get('scanned') or 0):,} 只",
         "note": f"过门槛 {int(result.get('candidates') or 0)} 只",
         "trend": "flat"},
        {"label": "本轮可买", "value": f"{len(allocations)} 只",
         "delta": f"另 {len(skipped)} 只资金不足" if skipped else "无资金不足",
         "trend": "flat"},
        {"label": "计划投入", "value": _money(summary.get("allocated_amount")),
         "delta": f"预算 {_money(summary.get('budget'))}",
         "trend": "flat"},
        {"label": "买入后剩余", "value": _money(summary.get("remaining_cash")),
         "delta": f"费用约 {_f(summary.get('allocated_fee'))} 元",
         "trend": "flat"},
    ]}))
    out.append("")

    if not allocations:
        return "\n".join(out + _empty_reason(skipped, summary, max_rows))

    # ── board：一票一卡（tone 表达"分数在池内的位置"，不是涨跌预测）──
    cards: List[Dict[str, Any]] = []
    for a in allocations:
        cards.append({
            "title": _clip(f"{a.name} {a.price:.2f}", _TITLE_MAX),
            "desc": _clip(f"{_pick_line(a)}\n{_stop_take(a)} · 得分 {a.score:.1f}",
                          _DESC_MAX),
            "tag": _clip(f"{a.shares} 股", _TAG_MAX),
            "tone": _score_tone(a.score),
        })
    out.append(_fence("board", {"columns": [
        {"name": f"买入清单（照此填单）· {len(allocations)} 只", "cards": cards}]}))
    out.append("")

    # ── table：逐项核对的明细（宽窄栏都能对齐）──
    out.append("**明细核对**")
    out.append("")
    out.append(_fence("table", {
        "columns": ["标的", "参考价", "股数", "金额", "占比", "得分",
                    "止损", "止盈"],
        "rows": [[f"{a.name}({a.code})", f"{a.price:.2f}", str(a.shares),
                  f"{a.amount:,.0f}", f"{a.weight_pct:.1f}%",
                  f"{a.score:.1f}",
                  f"{a.stop_loss:.2f}" if a.stop_loss else "-",
                  f"{a.take_profit:.2f}" if a.take_profit else "-"]
                 for a in allocations]}))
    out.append("")

    # ── 预算与口径（文字版，属于"读一遍"的信息）──
    out.append("**资金口径**")
    out.append("")
    out.append(f"- 可用现金 {_money(summary.get('cash'))}｜"
               f"账户总权益 {_money(summary.get('equity'))}")
    out.append(f"- 本轮预算 {_money(summary.get('budget'))}"
               f"（{summary.get('budget_source') or '现金使用比例'}）")
    cap_amt = summary.get("position_cap_amount") or 0
    cap_pct = summary.get("position_cap_pct")
    out.append(f"- 单票上限 {_money(cap_amt)}"
               + (f"（{cap_pct:g}% × 总权益）" if cap_pct else ""))
    out.append(f"- 止损止盈口径：{_risk_line(summary)}")
    out.append("")

    if skipped:
        out.append(f"**因资金不足跳过 {len(skipped)} 只**"
                   "（本身过了策略筛选，只是预算不够买一手）")
        out.append("")
        for a in skipped[:max_rows]:
            out.append(f"- {a.name}({a.code}) 现价 {a.price:.2f}：{a.skip_reason}")
        out.append("")
        out.append("想买其中某只：单独问「XXX 买多少」，"
                   "或 `astock_hint code=XXX buy=true`。")
        out.append("")

    note = _cache_note(result)
    if note:
        out.append(note)
        out.append("")
    out.append(quant.DISCLAIMER)
    return "\n".join(out)


def _cache_note(result: Dict[str, Any]) -> str:
    """缓存与取数失败的脚注——"这结果是不是刚出炉的"必须说清。"""
    notes: List[str] = []
    age = result.get("cache_age_sec")
    if result.get("cached") and age:
        notes.append(f"（结果取自 {float(age):.0f} 秒前的缓存；"
                     f"要最新的一轮加 `fresh=true`）")
    failed = result.get("fetch_failed") or 0
    if failed:
        notes.append(f"（{int(failed)} 只 K 线拉取失败已跳过）")
    return " ".join(notes)


def _empty_reason(skipped: Sequence[Any], summary: Dict[str, Any],
                  max_rows: int) -> List[str]:
    """没选出标的时的解释——"为什么没有"比空列表重要得多。"""
    out: List[str] = ["**本轮没有产生可执行的买入计划。**", ""]
    if skipped:
        out.append("**资金不足导致全部跳过**：")
        out.append("")
        for a in skipped[:max_rows]:
            out.append(f"- {a.name}({a.code}) 现价 {a.price:.2f}：{a.skip_reason}")
        out.append("")
        out.append(f"可用资金 {_money(summary.get('cash'))}，"
                   f"本轮预算 {_money(summary.get('budget'))}。"
                   "可提高「现金使用比例」、减少「本轮最多买几只」，"
                   "或用 `astock_portfolio action=set_cash` 校准资金。")
    else:
        out.append("可能原因：过滤门槛过严（成交额/市值）、当日普涨导致涨幅超上限"
                   "被剔除、或该策略近期无符合条件的标的。"
                   "可换 strategy，或放宽设置页的「最低成交额门槛」。")
    out.append("")
    out.append(quant.DISCLAIMER)
    return out


# ====================================================================== 三策略对比


def render_compare(results: Dict[str, Dict[str, Any]],
                   recs: Optional[Dict[str, Any]] = None,
                   max_rows: int = 5) -> str:
    """三策略对比 → kpi + board（三列，共识=绿）+ 分歧说明。

    **为什么用 board 的三列而不是三张 cards**：列（column）本身就承载
    "一套策略一列"的分组语义，用户在横向上比较同一只票是否被多套选中；
    cards 是一条瀑布流，比出"谁和谁重叠"要靠眼睛在六张卡之间来回找。
    """
    recs = recs or {}
    keys = [k for k in quant.STRATEGIES if k in results]
    if not keys:
        return ("**量化选股 · 三策略对比**\n\n"
                "尚未生成选股结果。点设置页「立即跑一轮选股」，"
                "或在对话里说「帮我选股」。\n\n"
                "_面板只读缓存、不会自动扫全市场_")

    first = results[keys[0]]
    # 同一只票被哪些策略选中（code → 策略 key 列表）
    picked_by: Dict[str, List[str]] = {}
    meta_by_code: Dict[str, Any] = {}
    for key in keys:
        for a in (results[key].get("allocations") or []):
            picked_by.setdefault(a.code, []).append(key)
            meta_by_code.setdefault(a.code, a)
    multi = [c for c, ks in picked_by.items() if len(ks) > 1]
    total_picks = sum(len(results[k].get("allocations") or []) for k in keys)

    scanned = int(first.get("scanned") or 0)
    age = max(float(results[k].get("cache_age_sec") or 0) for k in keys)
    out: List[str] = ["**量化选股 · 三策略对比**", "",
                      f"扫描 {scanned:,} 只｜{first.get('generated_at') or '-'} 生成"
                      f"｜{age:.0f}s 前", ""]

    out.append(_fence("kpi", {"items": [
        {"label": "扫描规模", "value": f"{scanned:,} 只", "trend": "flat"},
        {"label": "三套共选出", "value": f"{total_picks} 只次",
         "note": f"去重后 {len(picked_by)} 只", "trend": "flat"},
        {"label": "多套共识", "value": f"{len(multi)} 只",
         "delta": "分歧小、更值得看" if multi else "三套各选各的，分歧大",
         "trend": "flat"},
        {"label": "数据新鲜度", "value": f"{age:.0f}s",
         "note": "过期请重跑", "trend": "flat"},
    ]}))
    out.append("")

    # ── board：一套策略一列 ──
    columns: List[Dict[str, Any]] = []
    for key in keys:
        strat = quant.STRATEGIES[key]
        allocs = list(results[key].get("allocations") or [])[:max_rows]
        win = _win_rate(recs.get(key))
        name = f"{strat.label}" + (f"｜推荐率 {win:.0f}%" if win is not None else "")
        cards: List[Dict[str, Any]] = []
        for a in allocs:
            others = [quant.STRATEGIES[k].label for k in picked_by[a.code]
                      if k != key]
            cards.append({
                "title": _clip(f"{a.name} {a.price:.2f}", _TITLE_MAX),
                "desc": _clip(f"{_pick_line(a)}\n{_stop_take(a)} · 得分 "
                              f"{a.score:.1f}"
                              + (f"｜另被 {'/'.join(others)} 选中"
                                 if others else ""), _DESC_MAX),
                "tag": _clip(f"{a.shares} 股", _TAG_MAX),
                "tone": "good" if others else _score_tone(a.score),
            })
        if not cards:
            skipped = results[key].get("skipped") or []
            cards.append({
                "title": f"{strat.label}：无入选",
                "desc": (f"{len(skipped)} 只过筛但资金不足一手"
                         if skipped else
                         f"无标的过门槛（候选 {results[key].get('candidates', 0)} 只）"),
                "tag": "空缺", "tone": "neutral"})
        columns.append({"name": _clip(name, _TITLE_MAX), "cards": cards})

    out.append(_fence("board", {"columns": columns}))
    out.append("")

    # ── 共识与分歧（纯文字：这是"判断"，不该被塞进卡片）──
    if multi:
        out.append(f"**多套同时选中 {len(multi)} 只**（分歧小、更值得看）")
        out.append("")
        for code in multi[:max_rows + 3]:
            a = meta_by_code[code]
            labels = "/".join(quant.STRATEGIES[k].label for k in picked_by[code])
            out.append(f"- {a.name}({code}) 现价 {a.price:.2f}｜{labels}"
                       f"｜{a.shares} 股 · 得分 {a.score:.1f}")
        out.append("")
    elif picked_by:
        out.append("> 三套策略各自选了不同标的——**没有共识**，"
                   "说明当前市况分歧较大，建议先看回测推荐率再决定用哪套。")
        out.append("")

    out.append("_得分是候选池内的相对排名，**不是上涨概率**；"
               "股数按当前现金与单票上限换算，价格以实际成交为准。_")
    return "\n".join(out)


def _win_rate(rec: Any) -> Optional[float]:
    """推荐率（回测的历史胜率）——取不到就返回 None，绝不编造。"""
    if rec is None:
        return None
    win = getattr(rec, "win_rate", None)
    try:
        return float(win) if win is not None else None
    except (TypeError, ValueError):
        return None


# ====================================================================== 得分排行


def render_ranking(result: Dict[str, Any], n: int = 10) -> str:
    """只出得分排行（不给股数）——"我想自己决定买哪只"时用。"""
    ranked: Sequence[Any] = list(result.get("top") or [])
    if not ranked:
        return "本轮没有候选通过策略筛选。"

    rows = []
    for i, c in enumerate(ranked[:n], 1):
        rows.append([str(i), f"{c.name}({c.code})", f"{c.price:.2f}",
                     _pct_text(c.change_pct), f"{c.score:.1f}", c.explain(2)])
    head = (f"**得分排行**（{result.get('strategy_label') or result.get('strategy')}"
            f"，候选 {result.get('candidates')} 只，按分数降序）")
    return "\n".join([
        head, "",
        _fence("table", {"columns": ["#", "标的", "现价", "涨跌", "得分",
                                    "主要贡献因子"], "rows": rows}),
        "",
        "得分 = 候选池内各因子百分位 × 权重，**是相对排名不是上涨概率**。"
        "想看具体买多少股，用 `astock_pick`（不传 rank_only）。",
    ])
