"""策略推荐器：把三套回测结果压成**一句人话**——"现在该用哪套"。

────────────────────────────────────────────────────────────────────────
为什么需要它（"哪个效果好"这个问题不能直接甩给用户）
────────────────────────────────────────────────────────────────────────
三套策略各有一个推荐率，用户看到三个百分比后的第一反应是"所以呢？"。
把选择权原样丢回去（"你自己按市况挑"）在**技术上是诚实的**，但在**体验上
不成立**：他问的就是"该用哪套"。

本模块的立场（必须写在最前面，因为它决定了所有措辞）：
**推荐率是历史统计，不是未来概率。** 排名第一只说明"这段时间它更适应"，
不说明下个月还是。所以这里的输出永远长这样：

    「优先用 质量价值（D 档·历史 42% 跑赢）——但三套都在 D 档，
     说明这段样本里没有一套能稳定跑赢基准，宁可少动。」

而不是"建议用质量价值，胜率 42%"——那会被读成"42% 概率赚钱"。

────────────────────────────────────────────────────────────────────────
三档结论（全部来自可核对的规则，不含主观判断）
────────────────────────────────────────────────────────────────────────
1. **有 A/B 档**（历史确实跑赢过）：直接点名最优那套，并给第二选择。
2. **全是 C/D 档**：明说"没有一套值得追"，建议降仓/只买最稳的那套。
3. **样本不足**：明说"测不准"，不给结论——这比编一个更负责。

另外单独处理 **reversal 的历史警示**：它在实测里胜率最低、回撤最大，
即使偶然排第一也要附上警告（否则用户只会看排名）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("litework.astock.advisor")

#: 分档优先级（越小越优）
_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

#: 策略的"性格"，用于把推荐翻译成使用建议（不是评价好坏）
_PERSONA: Dict[str, str] = {
    "momentum": "追强势，趋势市有效；震荡市容易被假突破打脸",
    "reversal": "抄超跌，逆势低吸；需要承受继续下跌，务必带止损",
    "quality": "买稳健，涨得慢但回撤小；适合长期持有、不想盯盘",
}


def _usable(rec: Any) -> bool:
    """该结论是否可用于排序（样本不足的不参与竞争）。"""
    if rec is None:
        return False
    if getattr(rec, "win_rate", None) is None:
        return False
    return int(getattr(rec, "sample", 0) or 0) >= 8


def _sort_key(item: Tuple[str, Any]) -> Tuple[int, float, float]:
    """排序：分档优先 → 胜率高优先 → 回撤小优先（同档同胜率时更稳的靠前）。"""
    key, rec = item
    grade = getattr(rec, "grade", "D")
    wr = float(getattr(rec, "win_rate", 0) or 0)
    dd = float(getattr(rec, "max_drawdown_pct", 0) or 0)
    # 回撤是负数，越接近 0 越好 → 取绝对值排序
    return (_GRADE_RANK.get(grade, 3), -wr, abs(dd))


def rank_strategies(recs: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """按"该用哪套"排序（只含可用结论）。"""
    usable = [(k, r) for k, r in (recs or {}).items() if _usable(r)]
    return sorted(usable, key=_sort_key)


def advise(recs: Dict[str, Any]) -> Dict[str, Any]:
    """三套回测结论 → 傻瓜化建议。

    返回：
      · `headline` 一句话结论（可直接展示）
      · `picks`    建议顺序 [(key, rec), ...]（空 = 不建议动手）
      · `warnings` 必须附带的风险提示
      · `tone`     good / neutral / warn（用于着色）
    """
    out: Dict[str, Any] = {"headline": "", "picks": [], "warnings": [],
                           "tone": "neutral"}
    ranked = rank_strategies(recs)
    if not ranked:
        got = len(recs or {})
        out["headline"] = (
            "**暂时无法推荐**：三套策略都还没跑出有效回测（样本不足 8 期）。"
            "先点上「立即跑回测」出推荐率，再回来看该用哪套。"
            if not got else
            "**暂时无法推荐**：已有回测的样本期数不足 8 期，统计上撑不起结论。"
            "宁可说测不准，也不给一个看着不错的假结论。")
        out["tone"] = "warn"
        out["warnings"].append("样本不足时任何排名都不可信，别据此下重手。")
        return out

    best_key, best = ranked[0]
    best_label = _label(best_key, best)
    grade = getattr(best, "grade", "D")
    wr = float(getattr(best, "win_rate", 0) or 0)
    n = int(getattr(best, "sample", 0) or 0)

    if grade in ("A", "B"):
        out["headline"] = (
            f"**优先用 {best_label}**（{grade} 档·历史 {wr:.0f}% 跑赢基准，"
            f"{n} 期样本）——{_PERSONA.get(best_key, '')}。")
        out["tone"] = "good"
        out["picks"] = ranked[:2]
        if len(ranked) > 1:
            second_key, second = ranked[1]
            out["headline"] += (
                f"　次选 **{_label(second_key, second)}**"
                f"（{getattr(second, 'grade', 'D')} 档·{float(getattr(second, 'win_rate', 0) or 0):.0f}%）。")
    else:
        # 全 C/D：这才是真实常态（这段 A 股样本里没有稳定跑赢的策略）。
        # 措辞取"当前无优势策略，建议轻仓"：既如实说明没有好选择，
        # 又不至于像"三套都不算好"那样一次性否定掉工具本身。
        out["headline"] = (
            f"**当前无优势策略，建议轻仓**。相对最好的是 "
            f"**{best_label}**（{grade} 档·历史 {wr:.0f}% 跑赢基准，{n} 期样本）"
            f"——{_PERSONA.get(best_key, '')}。")
        out["tone"] = "warn"
        out["picks"] = ranked[:1]
        out["warnings"].append(
            "最好的那套也没到「可考虑」档：与其重仓赌一套，不如轻仓参与，"
            "或只买自己看得懂的标的。")

    # 回撤警告：按绝对值，超过 35% 的必须点名
    for key, rec in ranked:
        dd = getattr(rec, "max_drawdown_pct", None)
        if dd is not None and abs(float(dd)) >= 35.0:
            out["warnings"].append(
                f"{_label(key, rec)} 历史最大回撤 {abs(float(dd)):.0f}%——"
                f"这个幅度已经超出多数人的承受范围，用之前先想清楚能不能扛。")

    # reversal 专警告：实测最差，即便排第一也要说（用户往往只看排名）
    if best_key == "reversal":
        out["warnings"].append(
            "**reversal 是实测里表现最差的一套**（这段样本胜率最低、回撤最大），"
            "它排第一只说明「在这段行情里相对没那么差」，不代表它可靠。"
            "逆势低吸必须配止损，否则一次深跌就吃掉所有小胜。")

    grades = {getattr(r, "grade", "D") for _k, r in ranked}
    if grades <= {"C", "D"}:
        out["warnings"].append(
            "三套全在 C/D 档：这条结论本身比「选哪套」更重要——"
            "说明该策略在这段历史上没有稳定优势，别把仓位压上去。")
    return out


def _label(key: str, rec: Any) -> str:
    """**策略名**（"质量价值"），不是分档标签（"可考虑"）。

    踩过的坑：`Recommendation.label` 是分档标签（推荐/可考虑/谨慎），
    不是策略名。早先优先取它，于是结论行写成「优先用 可考虑」——
    读者完全不知道说的是哪套策略。
    """
    try:
        import astock_quant as quant
        return quant.STRATEGIES[key].label
    except Exception:
        pass
    # 兜底：策略注册表不可用时才退到 rec 上可能存在的非分档字段
    name = getattr(rec, "strategy_label", "") or ""
    return str(name) if name else str(key)


def render_advice(adv: Dict[str, Any], *, compact: bool = False) -> str:
    """建议 → Markdown（`compact=True` 用于窄栏面板，只留结论与必要警告）。"""
    lines = [adv.get("headline") or ""]
    warns = list(adv.get("warnings") or [])
    if compact:
        warns = warns[:1]
    if warns:
        lines.append("")
        for w in warns:
            lines.append(f"> ⚠️ {w}")
    if not compact:
        picks = adv.get("picks") or []
        if picks:
            lines.append("")
            lines.append("**怎么用**：")
            for key, rec in picks:
                lines.append(f"- {_label(key, rec)}：{_PERSONA.get(key, '')}")
        lines.append("")
        lines.append("_推荐率 = 历史上按这套规则选股、跑赢沪深300 的期数占比。"
                     "它衡量的是**策略**的历史表现，不是任何一只股票上涨的概率，"
                     "也不能外推成未来收益。_")
    return "\n".join([x for x in lines if x is not None]).strip()
