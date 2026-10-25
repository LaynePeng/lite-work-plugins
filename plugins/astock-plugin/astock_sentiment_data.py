"""投资舆情数据层：公告 / 研报 / 新闻 / 热度榜 / 龙虎榜 / 两融 / 股东户数 / 快讯。

**设计立场（重要，别改）**：本模块只负责"取到原始证据"，**不做情绪打分**。
把新闻、研报、热榜混成一个 0-100 分是反效果的——这些信号的可靠性差着
数量级（公告是法定披露事实、研报是卖方观点、热榜是散户注意力），合成
一个数会掩盖"该信哪一条"。因此拆成七类证据，每条带：

    reliability  证据可靠度档位（hard_fact / official_view / media / crowd）
    direction    对持仓的影响方向（positive / negative / neutral / unknown）
    as_of        该证据的时间（判断是否过期的唯一依据）

打分与冲突提示在 `astock_sentiment.py` 里做**基于规则的**归集——仍然不
产生一个混合分数，只产生"同向/背离/证据不足"的提示。

数据源（2026-10 实测全部可用；失败都不抛错，返回空+原因，让上层降级）：

    公告      np-anotice-stock.eastmoney.com     合法披露，hard_fact
    研报      reportapi.eastmoney.com            卖方观点，official_view
    新闻      search-api-web.eastmoney.com       媒体报道，media
    热度榜    dq.10jqka.com.cn                    散户注意力，crowd
    龙虎榜    datacenter-web.eastmoney.com        资金席位，hard_fact
    两融      datacenter-web.eastmoney.com        杠杆资金，hard_fact
    股东户数  datacenter-web.eastmoney.com        筹码集中度，hard_fact
    全市场快讯 zhibo.sina.com.cn                 宏观/行业事件，media

已试过但**不可用**（别再加回来）：
- 东财股吧人气 emappdata（返回 `服务异常`）
- 雪球 xueqiu.com（WAF 返回加密 HTML）
- 财联社 cls.cn（需要签名，返回 `签名错误`）
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from astock_data import DataError, _http_get, _http_get_json, _LIMITER
from astock_market import normalize_code, to_float

logger = logging.getLogger("litework.astock")

#: 证据可靠度（从硬到软）。顺序有意义：冲突提示按它决定"该信谁"。
RELIABILITY_ORDER = ("hard_fact", "official_view", "media", "crowd")

RELIABILITY_LABELS: Dict[str, str] = {
    "hard_fact": "法定披露/客观数据",
    "official_view": "卖方观点",
    "media": "媒体报道",
    "crowd": "散户情绪",
}

#: 影响方向
DIRECTION_LABELS: Dict[str, str] = {
    "positive": "偏正面",
    "negative": "偏负面",
    "neutral": "中性",
    "unknown": "未判定",
}

_EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
    "Accept": "*/*",
}

ANNOUNCE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
REPORT_URL = "https://reportapi.eastmoney.com/report/list"
NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
HOT_URL = ("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock")
LHB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA_FEED_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"


@dataclass
class Evidence:
    """一条舆情证据。所有字段都可为空——宁可缺字段也不编造。"""

    category: str                     # announce / report / news / hot / lhb / margin / holder / feed
    title: str
    reliability: str = "media"
    direction: str = "unknown"
    as_of: str = ""
    source: str = ""
    url: str = ""
    summary: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "category": self.category,
            "title": self.title,
            "reliability": self.reliability,
            "reliability_label": RELIABILITY_LABELS.get(self.reliability, self.reliability),
            "direction": self.direction,
            "direction_label": DIRECTION_LABELS.get(self.direction, self.direction),
            "as_of": self.as_of,
            "source": self.source,
        }
        if self.summary:
            out["summary"] = self.summary
        if self.url:
            out["url"] = self.url
        if self.meta:
            out["meta"] = self.meta
        return out


# ---------------------------------------------------------------- 方向判定
#
# 关键词法，刻意保守：只判**明确方向**的词，拿不准就 unknown。
# 为什么不用 LLM 打分：本模块在 tool 调用里同步执行，调 LLM 会拖慢且
# 引入非确定性；命中率不高但**可解释**，且上层 Agent 会做二次阅读。

_POS_WORDS = (
    "增持", "回购", "中标", "签约", "预增", "扭亏", "超预期", "上调", "买入", "推荐",
    "涨价", "突破", "获批", "获批上市", "利好", "同比增长", "创新高", "订单", "扩产",
    "分红", "股权激励", "净利增长", "业绩增长", "龙头", "研报首次覆盖",
)
_NEG_WORDS = (
    "减持", "质押", "诉讼", "仲裁", "立案", "处罚", "警示", "问询", "预亏", "下调",
    "亏损", "违约", "退市", "冻结", "查封", "风险提示", "被调查", "解禁", "商誉减值",
    "终止", "失败", "召回", "停产", "同比下降", "净利下滑", "跌停", "卖出", "谨慎",
)


def judge_direction(text: str) -> str:
    """从标题/摘要判方向（保守：同时出现正负词 → neutral）。"""
    t = (text or "").lower()
    if not t:
        return "unknown"
    pos = sum(1 for w in _POS_WORDS if w.lower() in t)
    neg = sum(1 for w in _NEG_WORDS if w.lower() in t)
    if pos and neg:
        return "neutral"
    if pos:
        return "positive"
    if neg:
        return "negative"
    return "unknown"


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """去掉接口返回里的 <em> 高亮等标签。"""
    return _TAG_RE.sub("", str(text or "")).strip()


def _date_only(raw: str) -> str:
    """2026-10-23 00:00:00 → 2026-10-23。"""
    t = str(raw or "").strip()
    return t.split(" ")[0] if t else ""


# ---------------------------------------------------------------- 公告


def fetch_announcements(code: str, page_size: int = 10) -> List[Evidence]:
    """公司公告（法定披露，可靠度最高）。"""
    params = urllib.parse.urlencode({
        "page_size": max(1, min(int(page_size), 50)), "page_index": 1,
        "ann_type": "A", "client_source": "web", "stock_list": code,
    })
    try:
        data = _http_get_json(f"{ANNOUNCE_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 公告获取失败 %s: %s", code, exc)
        return []
    rows = ((data or {}).get("data") or {}).get("list") or []
    out: List[Evidence] = []
    for r in rows:
        title = _clean(r.get("title") or "")
        if not title:
            continue
        art = str(r.get("art_code") or "")
        columns = r.get("columns") or []
        col_name = ", ".join(_clean(c.get("column_name") or "") for c in columns
                             if isinstance(c, dict))
        out.append(Evidence(
            category="announce", title=title,
            reliability="hard_fact",
            direction=judge_direction(title),
            as_of=_date_only(r.get("notice_date") or r.get("display_time")),
            source="东方财富·公告",
            url=(f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                 if art else ""),
            meta={"type": col_name} if col_name else {},
        ))
    return out


# ---------------------------------------------------------------- 研报


def fetch_reports(code: str, months: int = 6, limit: int = 8) -> List[Evidence]:
    """机构研报（卖方观点）。带评级、目标价、当年 EPS/PE 预测。"""
    import datetime as _dt
    end = _dt.date.today()
    begin = end - _dt.timedelta(days=max(30, int(months) * 30))
    params = urllib.parse.urlencode({
        "industryCode": "*", "pageSize": max(1, min(int(limit), 50)),
        "industry": "*", "beginTime": begin.isoformat(), "endTime": end.isoformat(),
        "pageNo": 1, "qType": 0, "code": code, "pageNum": 1,
    })
    try:
        data = _http_get_json(f"{REPORT_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 研报获取失败 %s: %s", code, exc)
        return []
    rows = (data or {}).get("data") or []
    out: List[Evidence] = []
    for r in rows:
        title = _clean(r.get("title") or "")
        if not title:
            continue
        rating = _clean(r.get("emRatingName") or r.get("sRatingName") or "")
        org = _clean(r.get("orgSName") or r.get("orgName") or "")
        eps_this = to_float(r.get("predictThisYearEps"), None)
        pe_this = to_float(r.get("predictThisYearPe"), None)
        meta: Dict[str, Any] = {"org": org}
        if rating:
            meta["rating"] = rating
        if eps_this is not None:
            meta["eps_forecast"] = eps_this
        if pe_this is not None:
            meta["pe_forecast"] = pe_this
        # 评级本身比标题更能说明方向
        direction = judge_direction(title + rating)
        if rating:
            low = rating.lower()
            if any(k in rating for k in ("买入", "增持", "推荐", "强烈")):
                direction = "positive"
            elif any(k in rating for k in ("减持", "卖出", "回避")):
                direction = "negative"
            elif "中性" in rating or "持有" in rating or low in ("neutral", "hold"):
                direction = "neutral"
        out.append(Evidence(
            category="report", title=title, reliability="official_view",
            direction=direction,
            as_of=_date_only(r.get("publishDate")),
            source=f"卖方研报·{org}" if org else "卖方研报",
            url=(f"https://data.eastmoney.com/report/zw_stock.jshtml?"
                 f"infocode={r.get('infoCode')}"
                 if r.get("infoCode") else ""),
            meta=meta,
        ))
    return out


# ---------------------------------------------------------------- 新闻


def fetch_news(keyword: str, limit: int = 10, code: str = "") -> List[Evidence]:
    """个股相关新闻/媒体文章（按时间倒序）。"""
    payload = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": max(1, min(int(limit), 30)),
            "preTag": "", "postTag": "",
        }},
    }
    params = urllib.parse.urlencode({"cb": "", "param": json.dumps(payload,
                                                                  ensure_ascii=False)})
    try:
        text = _http_get(f"{NEWS_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 新闻获取失败 %s: %s", keyword, exc)
        return []
    # jsonp 形式可能包一层括号/回调
    body = text.strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    try:
        data = json.loads(body)
    except ValueError:
        logger.info("[astock] 新闻返回无法解析（接口可能变更）")
        return []
    rows = ((data or {}).get("result") or {}).get("cmsArticleWebOld") or []
    out: List[Evidence] = []
    for r in rows:
        title = _clean(r.get("title") or "")
        if not title:
            continue
        summary = _clean(r.get("content") or "")[:160]
        rel = _news_relevance(title, summary, keyword, code)
        out.append(Evidence(
            category="news", title=title,
            reliability="media",
            direction=judge_direction(title + summary),
            as_of=str(r.get("date") or "")[:16],
            source=_clean(r.get("mediaName") or "媒体"),
            url=str(r.get("url") or ""),
            summary=summary,
            # 相关性直接影响"这条能不能当该股的证据"，必须显式带出去
            meta={"relevance": rel},
        ))
    return out


#: 标题/摘要里出现这些词、且未提到公司名时，判为"行业新闻"而非个股新闻
_INDUSTRY_HINTS = ("行业", "板块", "全线", "集体", "多股", "等股", "概念股", "赛道",
                   "各大", "多家", "周报", "汇总", "盘点", "要闻")

#: 公司名之外、能确认"这条讲的就是这家公司"的标题特征
_SUBJECT_HINTS = ("公告", "被罚", "遭", "拟", "控股", "股东", "持股", "董事长",
                  "总经理", "任职", "回购", "增持", "减持", "中标", "签约", "业绩",
                  "净利", "营收", "评级", "目标价", "涨停", "封板")


def _news_relevance(title: str, summary: str, keyword: str,
                    code: str = "") -> str:
    """判断一条新闻与该标的的相关度：direct / industry / weak。

    为什么需要：东财搜索是**全文检索**，搜"浦发银行"会返回大量只在正文里
    顺带提一句的行业稿（"银行行业资金流出榜…"、"18只主动ETF开售…"）。
    若把它们都当该股的证据，情绪判断会被行业噪声带偏（实测踩过：20 条里
    只有 3 条真是讲这家公司的）。

    判定口径（**标题为准**，正文提到不算直接相关）：
    - direct  ：标题含公司名，且不是"各大/多家/周报"这类汇总句式；
                或标题含公司名 + 主体动作词（公告/被罚/持股/增持…）
    - industry：标题含行业/板块词，或正文提到公司名（背景信息）
    - weak    ：其余（检索噪声）
    """
    kw = (keyword or "").strip()
    t = title or ""
    body = f"{t} {summary}"
    # 有些稿子标题里只写代码（"营收翻倍！688256，遭北向资金减持"）——同样算直接相关
    in_title = bool(kw) and (kw in t or (code and code in t))
    summary_like = any(h in t for h in _INDUSTRY_HINTS)

    if in_title:
        # 标题里点了名，但句式是"多家/各大"的汇总稿 → 行业稿
        if summary_like and not any(h in t for h in _SUBJECT_HINTS):
            return "industry"
        return "direct"
    if summary_like:
        return "industry"
    if kw and kw in body:
        # 只在正文出现：可能只是列举中的一家，划为背景
        return "industry"
    return "weak"


# ---------------------------------------------------------------- 热度榜


def fetch_hot_rank(code: Optional[str] = None, kind: str = "hour",
                   limit: int = 10) -> Dict[str, Any]:
    """同花顺人气榜（散户注意力）。

    全榜 Top N（`code=None`）或查单只排名（`code` 给定时整榜扫描定位）。
    该榜的 `analyse` 字段是平台给的"上涨原因"归纳，属于**平台观点**，
    不是事实——可靠度标 crowd，别当证据链用。
    """
    kind = "day" if str(kind).lower() == "day" else "hour"
    params = urllib.parse.urlencode({
        "stock_type": "a", "type": kind, "list_type": "normal",
    })
    try:
        data = _http_get_json(f"{HOT_URL}?{params}",
                              headers={"User-Agent": _EM_HEADERS["User-Agent"],
                                       "Referer": "https://www.10jqka.com.cn/"})
    except DataError as exc:
        logger.info("[astock] 热度榜获取失败: %s", exc)
        return {"available": False, "error": str(exc), "rank": None, "top": []}
    rows = ((data or {}).get("data") or {}).get("stock_list") or []

    def norm(r: Dict[str, Any]) -> Dict[str, Any]:
        tags = r.get("tag") or {}
        concept = tags.get("concept_tag") or []
        return {
            "rank": int(to_float(r.get("order"), 0) or 0),
            "code": str(r.get("code") or ""),
            "name": _clean(r.get("name") or ""),
            "heat": to_float(r.get("rate"), None),
            "change_pct": to_float(r.get("rise_and_fall"), None),
            "rank_change": to_float(r.get("hot_rank_chg"), None),
            "concepts": [str(c) for c in concept][:5] if isinstance(concept, list) else [],
            "popularity": _clean(tags.get("popularity_tag") or ""),
            "reason": _clean(r.get("analyse") or "")[:200],
            "reason_title": _clean(r.get("analyse_title") or ""),
        }

    normalized = [norm(r) for r in rows]
    found = None
    if code:
        found = next((x for x in normalized if x["code"] == code), None)
    return {
        "available": True,
        "rank": found,
        "top": normalized[:max(1, min(int(limit), 30))],
        "period": "日榜" if kind == "day" else "小时榜",
    }


# ---------------------------------------------------------------- 龙虎榜


def fetch_lhb(code: str, limit: int = 5) -> List[Evidence]:
    """龙虎榜（当日上榜的营业部买卖明细，客观成交数据）。"""
    params = urllib.parse.urlencode({
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
        "pageSize": 1, "pageNumber": 1, "sortColumns": "TRADE_DATE",
        "sortTypes": -1,
        "filter": f'(SECURITY_CODE="{code}")',
    })
    try:
        data = _http_get_json(f"{LHB_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 龙虎榜获取失败 %s: %s", code, exc)
        return []
    rows = ((data or {}).get("result") or {}).get("data") or []
    out: List[Evidence] = []
    for r in rows[:max(1, min(int(limit), 20))]:
        date = _date_only(r.get("TRADE_DATE"))
        net = to_float(r.get("BILLBOARD_NET_AMT"), None)
        buy = to_float(r.get("BILLBOARD_BUY_AMT"), None)
        sell = to_float(r.get("BILLBOARD_SELL_AMT"), None)
        chg = to_float(r.get("CHANGE_RATE"), None)
        reason = _clean(r.get("EXPLANATION") or r.get("EXPLAIN") or "")
        direction = "unknown"
        if net is not None:
            direction = "positive" if net > 0 else "negative" if net < 0 else "neutral"
        out.append(Evidence(
            category="lhb",
            title=f"{date} 上榜" + (f"（{reason}）" if reason else ""),
            reliability="hard_fact", direction=direction, as_of=date,
            source="东方财富·龙虎榜",
            meta={"net_amount": net, "buy_amount": buy, "sell_amount": sell,
                  "change_pct": chg},
        ))
    return out


# ---------------------------------------------------------------- 两融


def fetch_margin(code: str, limit: int = 10) -> Dict[str, Any]:
    """融资融券余额趋势（杠杆资金态度，客观数据）。

    返回 {rows, trend}：trend 用最近一日与更早一日比，避免只看单日噪声。
    """
    params = urllib.parse.urlencode({
        "reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "ALL",
        "pageSize": max(2, min(int(limit), 60)), "pageNumber": 1,
        "sortColumns": "DATE", "sortTypes": -1,
        "filter": f'(scode="{code}")',
    })
    try:
        data = _http_get_json(f"{LHB_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 两融获取失败 %s: %s", code, exc)
        return {"available": False, "error": str(exc), "rows": [], "trend": None}
    rows = ((data or {}).get("result") or {}).get("data") or []
    series: List[Dict[str, Any]] = []
    for r in rows:
        series.append({
            "date": _date_only(r.get("DATE")),
            "fin_balance": to_float(r.get("RZYE"), None),      # 融资余额
            "fin_buy": to_float(r.get("RZMRE"), None),         # 融资买入额
            "fin_repay": to_float(r.get("RZCHE"), None),       # 融资偿还
            "sec_balance": to_float(r.get("RQYE"), None),      # 融券余额
            "total_balance": to_float(r.get("RZRQYE"), None),  # 两融余额
        })
    series.sort(key=lambda x: x["date"])
    trend = None
    if len(series) >= 2:
        last, prev = series[-1], series[-5] if len(series) >= 5 else series[0]
        if last["fin_balance"] and prev["fin_balance"]:
            delta = last["fin_balance"] - prev["fin_balance"]
            trend = {
                "window": f"{prev['date']} → {last['date']}",
                "delta": round(delta, 2),
                "delta_pct": round(delta / prev["fin_balance"] * 100, 2),
                "direction": "positive" if delta > 0 else "negative" if delta < 0 else "neutral",
            }
    return {"available": True, "rows": series, "trend": trend}


# ---------------------------------------------------------------- 股东户数


def fetch_holder_count(code: str, limit: int = 6) -> Dict[str, Any]:
    """股东户数变化（筹码集中度：户数下降=筹码集中，通常偏正面）。"""
    params = urllib.parse.urlencode({
        "reportName": "RPT_HOLDERNUMLATEST", "columns": "ALL",
        "pageSize": max(1, min(int(limit), 20)), "pageNumber": 1,
        "filter": f'(SECURITY_CODE="{code}")',
    })
    try:
        data = _http_get_json(f"{LHB_URL}?{params}", headers=_EM_HEADERS)
    except DataError as exc:
        logger.info("[astock] 股东户数获取失败 %s: %s", code, exc)
        return {"available": False, "error": str(exc), "rows": [], "trend": None}
    rows = ((data or {}).get("result") or {}).get("data") or []
    series = [{
        "end_date": _date_only(r.get("END_DATE")),
        "holders": to_float(r.get("HOLDER_NUM"), None),
        "change_pct": to_float(r.get("HOLDER_NUM_RATIO"), None),
        "avg_market_value": to_float(r.get("AVG_MARKET_CAP"), None),
    } for r in rows]
    series.sort(key=lambda x: x["end_date"])
    trend = None
    if series:
        latest = series[-1]
        chg = latest.get("change_pct")
        if chg is not None:
            # 户数减少（chg<0）= 筹码集中 = 通常偏正面
            trend = {
                "end_date": latest["end_date"],
                "holders": latest["holders"],
                "change_pct": chg,
                "direction": "positive" if chg < 0 else "negative" if chg > 0 else "neutral",
                "note": "户数下降＝筹码趋于集中" if chg < 0 else
                        "户数上升＝筹码分散（散户增多）" if chg > 0 else "持平",
            }
    return {"available": True, "rows": series, "trend": trend}


# ---------------------------------------------------------------- 全市场快讯


def fetch_feed(limit: int = 15, keyword: str = "") -> List[Evidence]:
    """新浪财经 7×24 快讯（宏观/行业事件流）。

    给了 keyword 时只保留命中的条目——个股舆情里用它捞"行业/政策"类
    外部事件（公司公告与新闻已覆盖个股本身）。
    """
    params = urllib.parse.urlencode({
        "page": 1, "page_size": max(1, min(int(limit), 50)),
        "zhibo_id": 152, "tag_id": 0, "dire": "f", "dpc": 1,
    })
    try:
        data = _http_get_json(f"{SINA_FEED_URL}?{params}",
                              headers={"User-Agent": _EM_HEADERS["User-Agent"],
                                       "Referer": "https://finance.sina.com.cn/7x24/"})
    except DataError as exc:
        logger.info("[astock] 快讯获取失败: %s", exc)
        return []
    rows = (((data or {}).get("result") or {}).get("data") or {}).get("feed", {}).get("list") or []
    out: List[Evidence] = []
    kw = (keyword or "").strip()
    for r in rows:
        text = _clean(r.get("rich_text") or r.get("text") or "")
        if not text:
            continue
        if kw and kw not in text:
            continue
        title = text.split("。")[0][:80]
        out.append(Evidence(
            category="feed", title=title, reliability="media",
            direction=judge_direction(text),
            as_of=str(r.get("create_time") or "")[:16],
            source="新浪财经·7×24", summary=text[:200],
            meta={"tags": [t.get("name") for t in (r.get("tag") or [])
                           if isinstance(t, dict) and t.get("name")]},
        ))
    return out


# ---------------------------------------------------------------- 汇总入口


def collect(code: str, name: str = "", scope: Sequence[str] = (),
            days: int = 7) -> Dict[str, Any]:
    """按范围抓取全部舆情证据。

    scope 可选值：announce / report / news / hot / lhb / margin / holder / feed
    留空 = 除 feed（全市场快讯，噪声大）以外的全部。
    单类失败不影响其它类（各自吞掉异常并记录），最终 `errors` 里如实列出。
    """
    code, _ = normalize_code(code)
    wanted = {s.strip() for s in scope if s.strip()} or {
        "announce", "report", "news", "hot", "lhb", "margin", "holder"}
    items: List[Evidence] = []
    errors: Dict[str, str] = {}
    extras: Dict[str, Any] = {}

    if "announce" in wanted:
        got = fetch_announcements(code, page_size=10)
        items.extend(got)
        if not got:
            errors["announce"] = "暂无公告或接口不可用"
    if "report" in wanted:
        # 研报是季度级更新的低频证据，最短看 6 个月——按 days//7 算会只剩
        # 1 个月窗口，导致"近期无研报"的误判（实测浦发银行 6 个月内是有研报的）
        got = fetch_reports(code, months=max(6, days // 7), limit=8)
        items.extend(got)
        if not got:
            errors["report"] = "近期无机构研报或接口不可用"
    if "news" in wanted:
        got = fetch_news(name or code, limit=20, code=code)
        # 只把「直接相关」的新闻计入情绪信号；行业稿/弱相关另存，仅作背景
        direct = [e for e in got if (e.meta or {}).get("relevance") == "direct"]
        loose = [e for e in got if (e.meta or {}).get("relevance") != "direct"]
        items.extend(direct[:10])
        if loose:
            extras["news_background"] = [
                {"title": e.title, "as_of": e.as_of, "source": e.source,
                 "relevance": (e.meta or {}).get("relevance")} for e in loose[:6]]
        if not direct:
            errors["news"] = ("无直接相关新闻（仅有行业/弱相关报道，已单独列为背景，"
                              "不计入舆情方向）")
    if "hot" in wanted:
        hot = fetch_hot_rank(code, limit=10)
        extras["hot"] = hot
        if not hot.get("available"):
            errors["hot"] = hot.get("error") or "热度榜不可用"
        elif not hot.get("rank"):
            # 不在榜上是常态（榜单只取 100 名），不是错误；但要说明清楚，
            # 免得用户以为"热度数据没取到"
            extras["hot_miss"] = ("未进入同花顺人气榜前 100（关注度低，"
                                  "本身就是一个中性偏淡的信号）")
        else:
            r = hot["rank"]
            direction = ("positive" if (r.get("change_pct") or 0) > 0
                         else "negative" if (r.get("change_pct") or 0) < 0
                         else "neutral")
            items.append(Evidence(
                category="hot",
                title=f"同花顺{hot.get('period') or '人气榜'}第 {r['rank']} 名",
                reliability="crowd", direction=direction,
                as_of=time.strftime("%Y-%m-%d %H:%M"),
                source="同花顺·人气榜",
                summary=(r.get("reason") or "")[:160],
                meta={"heat": r.get("heat"), "rank": r["rank"],
                      "rank_change": r.get("rank_change"),
                      "concepts": r.get("concepts") or [],
                      "popularity": r.get("popularity") or ""},
            ))
    if "lhb" in wanted:
        got = fetch_lhb(code, limit=5)
        items.extend(got)
        if not got:
            errors["lhb"] = "近期未上龙虎榜（不是异常）"
    if "margin" in wanted:
        mg = fetch_margin(code, limit=10)
        extras["margin"] = mg
        if not mg.get("available"):
            errors["margin"] = mg.get("error") or "两融数据不可用"
        elif mg.get("trend"):
            t = mg["trend"]
            items.append(Evidence(
                category="margin",
                title=f"融资余额 {t['window']} 变化 {t['delta_pct']:+.2f}%",
                reliability="hard_fact", direction=t["direction"],
                as_of=t["window"].split("→")[-1].strip(),
                source="东方财富·两融",
                meta={"delta": t["delta"], "delta_pct": t["delta_pct"]},
            ))
    if "holder" in wanted:
        hd = fetch_holder_count(code, limit=6)
        extras["holder"] = hd
        if not hd.get("available"):
            errors["holder"] = hd.get("error") or "股东户数不可用"
        elif hd.get("trend"):
            t = hd["trend"]
            items.append(Evidence(
                category="holder",
                title=f"股东户数 {t['end_date']}：{t['holders']:,.0f} 户（{t['change_pct']:+.2f}%）",
                reliability="hard_fact", direction=t["direction"],
                as_of=t["end_date"], source="东方财富·股东户数",
                summary=t.get("note") or "",
                meta={"holders": t["holders"], "change_pct": t["change_pct"]},
            ))
    if "feed" in wanted:
        got = fetch_feed(limit=30, keyword=name)
        items.extend(got)

    # 时间倒序（无日期的排后面），同类别内保持接口原序
    def sort_key(e: Evidence):
        d = e.as_of or ""
        return (d, RELIABILITY_ORDER.index(e.reliability)
                if e.reliability in RELIABILITY_ORDER else 9)
    items.sort(key=sort_key, reverse=True)
    return {"code": code, "name": name, "items": items,
            "extras": extras, "errors": errors}
