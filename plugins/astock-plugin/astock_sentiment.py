"""投资舆情分析：把七类证据归集成"信号 + 冲突提示"，**不产生混合评分**。

为什么不做 0-100 情绪分（设计立场，别改）：

1. **可靠性差异被抹平**：法定披露（公告、龙虎榜、两融、股东户数）与散户
   注意力（人气榜）放进同一个分数后，用户无法知道该信哪一条；
2. **分数会伪装成结论**：62 分是"偏多"还是"噪声"？没人说得清，但它看起来
   像可执行信号，容易诱发交易；
3. **无法追溯**：分数变了却说不清是哪条证据变了，复盘时无从校验。

因此本模块输出三样东西：
- `signals`：按类别归集的信号（含计数、最近时间、代表证据）；
- `conflicts`：**证据之间的背离**（如"研报看多但两融余额下降"），这是
  真正有价值的部分——单一信号本身几乎不含信息，背离才提示风险；
- `data_quality`：哪些类别缺数据/失败，明确告诉用户"别过度解读"。

`tone` 只有四档（aligned_positive / aligned_negative / conflicted / thin），
描的是**证据结构**而非"未来涨跌"——不预测是合规底线。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from astock_sentiment_data import (
    RELIABILITY_LABELS, RELIABILITY_ORDER, Evidence,
)

#: 类别展示名（顺序即报告里的固定顺序，方便两期对比）
CATEGORY_ORDER = ("announce", "report", "news", "hot", "lhb", "margin", "holder", "feed")

CATEGORY_LABELS: Dict[str, str] = {
    "announce": "公司公告",
    "report": "机构研报",
    "news": "相关新闻",
    "hot": "市场热度",
    "lhb": "龙虎榜",
    "margin": "融资融券",
    "holder": "股东户数",
    "feed": "行业快讯",
}

#: 各类别的默认权重（用于"证据强度"判断，不是打分）。
#: 数值含义：0-3，越大越该被重视。hard_fact 类给满，crowd 类压低。
_CATEGORY_WEIGHT = {
    "announce": 3, "lhb": 3, "margin": 3, "holder": 2,
    "report": 2, "news": 1, "hot": 1, "feed": 1,
}

#: 背离检查用的"客观锚点"类别（这些不依赖解读，最适合与其他信号对照）
_ANCHOR_CATEGORIES = ("lhb", "margin", "holder")

STALE_DAYS = 30          # 超过该天数的证据视为"旧闻"，只在报告里提一句
THIN_EVIDENCE_MIN = 2    # 有效证据少于该数量时判定为"证据不足"


@dataclass
class CategorySignal:
    """一个类别的归集结果。"""

    category: str
    count: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    unknown: int = 0
    latest_as_of: str = ""
    reliability: str = "media"
    headlines: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def net(self) -> int:
        return self.positive - self.negative

    @property
    def direction(self) -> str:
        """类别内的净方向。

        判不出方向（unknown）的条目占多数时返回 unknown，而不是让零星一条
        负面词决定整类方向——否则"10 条公告里 1 条含'终止'"会被总结成
        "公告整体偏负面"，这是典型的以偏概全（实测踩过）。
        """
        judged = self.positive + self.negative + self.neutral
        if judged == 0:
            return "unknown"
        if self.unknown > judged:
            return "unknown"
        if self.net > 0:
            return "positive"
        if self.net < 0:
            return "negative"
        return "neutral"

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "category": self.category,
            "label": CATEGORY_LABELS.get(self.category, self.category),
            "count": self.count,
            "positive": self.positive,
            "negative": self.negative,
            "neutral": self.neutral,
            "unknown_direction": self.unknown,
            "net_direction": self.direction,
            "reliability": self.reliability,
            "reliability_label": RELIABILITY_LABELS.get(self.reliability, self.reliability),
        }
        if self.latest_as_of:
            out["latest_as_of"] = self.latest_as_of
        if self.headlines:
            out["headlines"] = self.headlines[:3]
        if self.meta:
            out["meta"] = self.meta
        return out


@dataclass
class Conflict:
    """证据背离：两个（或多个）类别指向不同方向时的提示。"""

    kind: str
    severity: str            # high / medium / low
    title: str
    detail: str
    involved: List[str] = field(default_factory=list)
    advice: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "kind": self.kind, "severity": self.severity,
            "title": self.title, "detail": self.detail,
            "involved": self.involved,
        }
        if self.advice:
            out["advice"] = self.advice
        return out


@dataclass
class SentimentReport:
    """一次舆情分析的结果。"""

    code: str
    name: str
    as_of: str = ""
    tone: str = "thin"       # aligned_positive / aligned_negative / conflicted / thin
    tone_text: str = ""
    categories: List[CategorySignal] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    stale_items: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    evidence_count: int = 0
    weight_score: int = 0    # 证据强度（Σ 方向×类别权重），**仅供排序，不是情绪分**

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "as_of": self.as_of,
            "tone": self.tone, "tone_text": self.tone_text,
            "evidence_count": self.evidence_count,
            "categories": [c.as_dict() for c in self.categories],
            "conflicts": [c.as_dict() for c in self.conflicts],
            "stale_items": self.stale_items,
            "data_issues": self.errors,
            "weight_score": self.weight_score,
            "note": ("weight_score 是证据强度（方向×类别权重之和），"
                     "只用于同类标的横向排序，不是涨跌预测，也不是情绪评分"),
        }


def _parse_date(text: str) -> Optional[_dt.date]:
    t = (text or "").strip()[:10]
    if len(t) != 10:
        return None
    try:
        return _dt.date.fromisoformat(t)
    except ValueError:
        return None


def summarize(items: Sequence[Evidence], code: str, name: str = "",
              errors: Optional[Dict[str, str]] = None,
              extras: Optional[Dict[str, Any]] = None) -> SentimentReport:
    """证据 → 归类信号 + 背离提示 + 数据质量说明。"""
    today = _dt.date.today()
    errors = dict(errors or {})
    extras = extras or {}

    by_cat: Dict[str, List[Evidence]] = {}
    stale: List[str] = []
    for e in items:
        d = _parse_date(e.as_of)
        # 时效性 categorize：公告/研报是季度级低频证据，本身不怕旧；
        # 新闻/快讯/热度/龙虎榜是"事件型"证据，超过 30 天就不再代表当下。
        # 关键：**过期的事件型证据不计入方向判断**——否则半年龙虎榜会被
        # 当成今天的资金信号（实测踩过：4 月的龙虎榜让"当前"结论偏多）。
        expired = bool(d) and e.category in ("news", "feed", "hot", "lhb") \
            and (today - d).days > STALE_DAYS
        if expired:
            stale.append(f"{CATEGORY_LABELS.get(e.category, e.category)}："
                         f"{e.title[:30]}（{e.as_of}）")
            e.meta = dict(e.meta or {}, expired=True)
            # 保留在 items 里供展示，但不进 by_cat（不参与归集与方向）
            continue
        by_cat.setdefault(e.category, []).append(e)

    categories: List[CategorySignal] = []
    for cat in CATEGORY_ORDER:
        group = by_cat.get(cat) or []
        if not group:
            continue
        sig = CategorySignal(
            category=cat, count=len(group),
            reliability=group[0].reliability,
        )
        for e in group:
            if e.direction == "positive":
                sig.positive += 1
            elif e.direction == "negative":
                sig.negative += 1
            elif e.direction == "neutral":
                sig.neutral += 1
            else:
                sig.unknown += 1
        dates = sorted([e.as_of for e in group if e.as_of], reverse=True)
        sig.latest_as_of = dates[0] if dates else ""
        sig.headlines = [e.title for e in group if e.title]
        # 类别专属摘要（从 extras 里取结构化数据，比标题更有信息量）
        if cat == "margin" and (extras.get("margin") or {}).get("trend"):
            sig.meta["trend"] = extras["margin"]["trend"]
        if cat == "holder" and (extras.get("holder") or {}).get("trend"):
            sig.meta["trend"] = extras["holder"]["trend"]
        if cat == "hot":
            hot = extras.get("hot") or {}
            if hot.get("rank"):
                sig.meta["rank"] = hot["rank"].get("rank")
                sig.meta["heat"] = hot["rank"].get("heat")
                sig.meta["concepts"] = hot["rank"].get("concepts") or []
                sig.meta["popularity"] = hot["rank"].get("popularity") or ""
                sig.meta["rank_change"] = hot["rank"].get("rank_change")
                if hot["rank"].get("reason"):
                    sig.meta["platform_reason"] = hot["rank"]["reason"]
            else:
                # 兜底：从证据自身的 meta 取（否则不走 collect() 的调用方
                # ——如单测或只传证据的场景——热度背离规则会永远不触发）
                for e in group:
                    em = e.meta or {}
                    if em.get("rank"):
                        sig.meta.update({k: em.get(k) for k in
                                         ("rank", "heat", "rank_change",
                                          "concepts", "popularity")
                                         if em.get(k) is not None})
                        break
        categories.append(sig)

    # 证据强度（只用于横向排序）
    weight = 0
    for sig in categories:
        w = _CATEGORY_WEIGHT.get(sig.category, 1)
        if sig.direction == "positive":
            weight += w
        elif sig.direction == "negative":
            weight -= w

    # 有效证据数（unknown 方向不计入——判不出方向的条目不构成"证据"）
    effective = sum(sig.positive + sig.negative + sig.neutral for sig in categories)

    report = SentimentReport(
        code=code, name=name,
        as_of=today.isoformat(),
        categories=categories,
        stale_items=stale[:8],
        errors=errors,
        evidence_count=effective,
        weight_score=weight,
    )
    report.conflicts = _detect_conflicts(categories, extras)
    report.tone, report.tone_text = _judge_tone(categories, report.conflicts,
                                                effective, report.stale_items)
    return report


def _sig(categories: Sequence[CategorySignal], cat: str) -> Optional[CategorySignal]:
    for s in categories:
        if s.category == cat:
            return s
    return None


def _detect_conflicts(categories: Sequence[CategorySignal],
                      extras: Dict[str, Any]) -> List[Conflict]:
    """挑出证据之间的背离。

    只写**能明确说清因果方向**的规则。宁可漏报也不硬凑——凭空生成的
    "背离"会让用户对整套信号失去信任。
    """
    out: List[Conflict] = []
    report = _sig(categories, "report")
    margin = _sig(categories, "margin")
    holder = _sig(categories, "holder")
    hot = _sig(categories, "hot")
    lhb = _sig(categories, "lhb")
    announce = _sig(categories, "announce")
    news = _sig(categories, "news")

    # ① 卖方看多但杠杆资金在撤——最常见的"研报先行、资金不认"
    if report and margin and report.direction == "positive" and margin.direction == "negative":
        out.append(Conflict(
            kind="report_vs_margin", severity="high",
            title="研报偏多，但融资余额在下降",
            detail=(f"{report.count} 篇研报整体偏正面（{report.headlines[0][:40] if report.headlines else ''}…），"
                    f"但同期融资余额减少 {abs((margin.meta.get('trend') or {}).get('delta_pct', 0)):.2f}%。"
                    f"杠杆资金往往比研报更贴近真实交易意愿。"),
            involved=["report", "margin"],
            advice="把研报观点当参考、把两融当验证：在融资余额止跌回升前，不宜据研报加仓。",
        ))

    # ② 热度极高但大单在卖（情绪透支的典型形态）
    if hot and hot.meta.get("rank") and hot.meta["rank"] <= 10 and negative_anchor(categories):
        anchor = negative_anchor(categories)
        out.append(Conflict(
            kind="hot_vs_flow", severity="high",
            title="人气榜排名靠前，但资金数据偏空",
            detail=(f"同花顺人气榜第 {hot.meta['rank']} 名（散户关注度极高），"
                    f"而{CATEGORY_LABELS.get(anchor, anchor)}显示偏空。"
                    f"高关注度 + 资金流出通常意味着情绪先行、承接不足。"),
            involved=["hot", anchor],
            advice="热度不等于买盘。若已持有，可考虑在情绪高点减仓而非加仓。",
        ))

    # ③ 公告偏正面但股价/热度不认（利好兑现或信息未被理解）
    if announce and announce.direction == "positive" and news and news.direction == "negative":
        out.append(Conflict(
            kind="announce_vs_news", severity="medium",
            title="公告事件偏正面，但媒体报道偏负面",
            detail="公告本身含正面事件词（如增持/中标/预增），但同期媒体标题偏负面——"
                   "常见于利好兑现、或市场对事件有不同解读。",
            involved=["announce", "news"],
            advice="以公告原文为准，别只看标题：确认事件是否已在此前预期之内（利好兑现）。",
        ))

    # ④ 股东户数上升（筹码分散）+ 机构研报看多
    if holder and holder.direction == "negative" and report and report.direction == "positive":
        out.append(Conflict(
            kind="holder_vs_report", severity="medium",
            title="股东户数上升（筹码分散），但研报看多",
            detail="散户户数增加通常意味着筹码从集中转向分散，与机构看多观点形成反差。",
            involved=["holder", "report"],
            advice="分散过程中股价更容易震荡；研报观点需要更长周期验证，不适合短线依据。",
        ))

    # ⑤ 龙虎榜净买入但两融下降（短线资金 vs 杠杆资金）
    lhb_dir = lhb.direction if lhb else None
    margin_dir = margin.direction if margin else None
    if lhb_dir == "positive" and margin_dir == "negative":
        out.append(Conflict(
            kind="lhb_vs_margin", severity="medium",
            title="龙虎榜净买入，但融资余额下降",
            detail="短线席位在买、杠杆资金在撤，属于典型的资金结构分歧。",
            involved=["lhb", "margin"],
            advice="这类行情持续性差，参与需更严的止损。",
        ))

    return out


def negative_anchor(categories: Sequence[CategorySignal]) -> str:
    """找一个"偏空且客观"的锚点类别名（优先 lhb > margin > holder）。"""
    for cat in _ANCHOR_CATEGORIES:
        s = _sig(categories, cat)
        if s and s.direction == "negative":
            return cat
    return ""


def _judge_tone(categories: Sequence[CategorySignal],
                conflicts: Sequence[Conflict], effective: int,
                stale: Sequence[str]) -> tuple:
    """证据结构 → 四档 tone（描结构，不预测涨跌）。"""
    if effective < THIN_EVIDENCE_MIN:
        return ("thin",
                f"有效证据仅 {effective} 条，不足以形成舆情判断——"
                f"不要据此操作，先看公告原文与行情本身。")
    if conflicts:
        high = [c for c in conflicts if c.severity == "high"]
        if high:
            return ("conflicted",
                    f"证据背离（{len(conflicts)} 处，其中 {len(high)} 处为高优先级）："
                    f"不同来源指向不一致，按「以客观数据为准」处理。")
        return ("conflicted", f"存在 {len(conflicts)} 处证据背离，需分别核实。")
    pos = sum(s.positive for s in categories)
    neg = sum(s.negative for s in categories)
    if pos > neg * 2 and pos >= 3:
        return ("aligned_positive",
                f"各类证据同向偏正面（正面 {pos} 条 / 负面 {neg} 条），"
                f"但舆情不改变基本面，仍需结合估值与仓位纪律。")
    if neg > pos * 2 and neg >= 3:
        return ("aligned_negative",
                f"各类证据同向偏负面（负面 {neg} 条 / 正面 {pos} 条），"
                f"建议核对是否有公告/新闻解释了下跌原因。")
    return ("thin", f"证据方向不一致或不明显（正面 {pos} / 负面 {neg}），暂无一致结论。")
