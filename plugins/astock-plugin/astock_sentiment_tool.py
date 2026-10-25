"""舆情工具层：`astock_sentiment` 工具的组装与 Markdown 渲染。

设计要点（与数据层/分析层分工对应）：

    取数  astock_sentiment_data.collect()    七类证据，单类失败不影响其它
    归集  astock_sentiment.summarize()        分类信号 + 背离提示 + 数据质量
    渲染  本文件                               人读得懂的 Markdown（也给 Agent 消费）

**渲染上的三条坚持**：
1. 每类证据先给"可靠度"再给内容——让用户知道该信多少；
2. 冲突提示放在**最前面**（比正面证据更重要），因为背离才是风险信号；
3. 缺数据/失败项**必须显式列出**，不能因为没抓到就当"中性"。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from astock_sentiment import (
    CATEGORY_LABELS, CATEGORY_ORDER, RELIABILITY_LABELS, SentimentReport,
)
from astock_sentiment_data import RELIABILITY_ORDER, Evidence


def _f(v: Any, digits: int = 2, dash: str = "-") -> str:
    if v is None or v == "":
        return dash
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _money(v: Any) -> str:
    if v is None or v == "":
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e8:
        return f"{sign}{x / 1e8:.2f}亿"
    if x >= 1e4:
        return f"{sign}{x / 1e4:.2f}万"
    return f"{sign}{x:.2f}"


_REL_TAG = {
    "hard_fact": "【事实】",
    "official_view": "【观点】",
    "media": "【媒体】",
    "crowd": "【情绪】",
}

_TONE_STYLE = {
    "aligned_positive": "🟢 证据同向偏正面",
    "aligned_negative": "🔴 证据同向偏负面",
    "conflicted": "🟡 证据背离（关键）",
    "thin": "⚪ 证据不足",
}


def render(report: SentimentReport, items: Sequence[Evidence],
           extras: Optional[Dict[str, Any]] = None,
           max_per_category: int = 3) -> str:
    """舆情报告 Markdown。"""
    extras = extras or {}
    out: List[str] = []

    # ---------- 头部：结论先说是"结构"不是"预测"
    out.append(f"## 舆情分析：{report.name or report.code}（{report.code}）")
    out.append("")
    out.append(f"**{_TONE_STYLE.get(report.tone, report.tone)}** — {report.tone_text}")
    out.append("")
    out.append(f"> 证据 {report.evidence_count} 条 · 覆盖 {len(report.categories)} 类 · "
               f"数据基准日 {report.as_of}。本模块只归集证据与背离，**不预测涨跌、不给评级**。")
    out.append("")

    # ---------- 冲突提示放最前（比正面证据重要）
    if report.conflicts:
        out.append("### ⚠️ 信号背离（优先看这里）")
        out.append("")
        for c in report.conflicts:
            mark = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(c.severity, "•")
            out.append(f"{mark} **{c.title}**")
            out.append(f"   {c.detail}")
            if c.advice:
                out.append(f"   → {c.advice}")
            out.append("")

    # ---------- 分类证据
    out.append("### 证据清单（按类别）")
    out.append("")
    if not report.categories:
        out.append("未取到任何分类证据。")
        out.append("")
    for cat in CATEGORY_ORDER:
        sig = next((c for c in report.categories if c.category == cat), None)
        if sig is None:
            continue
        label = CATEGORY_LABELS.get(cat, cat)
        rel = RELIABILITY_LABELS.get(sig.reliability, sig.reliability)
        dir_cn = {"positive": "偏正面", "negative": "偏负面",
                  "neutral": "中性", "unknown": "方向未判定"}.get(sig.direction, "未判定")
        unjudged = (f"，{sig.unknown} 条未判出方向" if sig.unknown else "")
        head = (f"**{label}**（{sig.count} 条，可靠度：{rel}，整体{dir_cn}{unjudged}")
        if sig.latest_as_of:
            head += f"，最新 {sig.latest_as_of}"
        out.append(head + "）")

        # 类别专属结构化摘要
        trend = (sig.meta or {}).get("trend")
        if cat == "margin" and trend:
            out.append(f"   · 融资余额变化 {trend['delta_pct']:+.2f}%"
                       f"（{trend['window']}）")
        if cat == "holder" and trend:
            out.append(f"   · {trend.get('note') or ''}，共 {_f(trend.get('holders'), 0)} 户")
        if cat == "hot":
            m = sig.meta or {}
            if m.get("rank"):
                extra = []
                if m.get("heat"):
                    extra.append(f"热度 {_f(m['heat'], 0)}")
                if m.get("rank_change") not in (None, 0):
                    extra.append(f"较上次 {m['rank_change']:+g} 名")
                if m.get("popularity"):
                    extra.append(str(m["popularity"]))
                if m.get("concepts"):
                    extra.append("概念：" + "/".join(m["concepts"][:3]))
                out.append(f"   · 排名第 {m['rank']}" + (f"（{'，'.join(extra)}）" if extra else ""))
            if m.get("platform_reason"):
                out.append(f"   · 平台归因：{str(m['platform_reason'])[:100]}…")

        # 代表条目（按可靠度+时间已排好序，取前面几条）
        picked = [e for e in items if e.category == cat][:max_per_category]
        for e in picked:
            tag = _REL_TAG.get(e.reliability, "")
            line = f"   - {tag}{e.as_of} {e.title[:70]}"
            out.append(line)
            # 研报/龙虎榜/两融这类带结构化 meta 的，补一行关键数字
            meta = e.meta or {}
            bits: List[str] = []
            if meta.get("rating"):
                bits.append(f"评级 {meta['rating']}")
            if meta.get("org"):
                bits.append(str(meta["org"]))
            if meta.get("eps_forecast") is not None:
                bits.append(f"预测EPS {_f(meta['eps_forecast'])}")
            if meta.get("pe_forecast") is not None:
                bits.append(f"预测PE {_f(meta['pe_forecast'])}")
            if meta.get("net_amount") is not None:
                bits.append(f"净额 {_money(meta['net_amount'])}")
            if meta.get("type"):
                bits.append(str(meta["type"]))
            if bits:
                out.append(f"     _（{' · '.join(bits)}）_")
        out.append("")

    # ---------- 热度未上榜 / 行业背景（不算证据，但缺了要说明）
    if extras.get("hot_miss"):
        out.append(f"**市场热度**：{extras['hot_miss']}")
        out.append("")
    background = extras.get("news_background") or []
    if background:
        out.append(f"**行业/弱相关背景**（{len(background)} 条，未计入舆情方向）")
        for b in background[:4]:
            out.append(f"   - {b.get('as_of', '')} {str(b.get('title') or '')[:60]}")
        out.append("")

    # ---------- 数据质量：缺什么必须说清楚
    if report.errors or report.stale_items:
        out.append("### 数据质量说明")
        out.append("")
        for cat, reason in report.errors.items():
            out.append(f"- `{CATEGORY_LABELS.get(cat, cat)}`：{reason}")
        if report.stale_items:
            out.append(f"- 超过 30 天的旧闻 {len(report.stale_items)} 条"
                       f"（已保留但不作为近期证据）：{report.stale_items[0][:50]}…")
        out.append("")

    out.append("---")
    out.append("**怎么用这份报告**：公告/龙虎榜/两融/股东户数是**客观数据**，"
               "研报是**卖方观点**，人气温榜是**散户注意力**——权重依次递减。"
               "出现背离时以客观数据为准。以上仅供参考，不构成投资建议。")
    return "\n".join(out)


def render_brief(report: SentimentReport) -> str:
    """一句话摘要（给面板/盯梢用，省上下文）。"""
    if report.tone == "thin":
        return f"{report.name or report.code}：证据不足（{report.evidence_count} 条），暂无法判断"
    parts = []
    for c in report.conflicts[:2]:
        parts.append(c.title)
    head = _TONE_STYLE.get(report.tone, report.tone)
    if parts:
        return f"{report.name or report.code}：{head} — " + "；".join(parts)
    return f"{report.name or report.code}：{head}"


def render_conflict_lines(report: SentimentReport) -> List[str]:
    """背离提示的短列表（供持仓面板/盯梢源引用）。"""
    return [f"{c.title}（{c.severity}）" for c in report.conflicts]
