"""A 股扩展数据层：打板情绪 / 资金筹码 / 事件驱动 / 研究资料 / 宏观利率。

**来源与改写说明（重要）**：本模块的端点选择与字段口径移植自
`a-stock-data`（https://github.com/simonlin1212/a-stock-data，Apache-2.0）
的 `SKILL.md`，但实现被**整体重写**以符合本仓库约束：

1. **零第三方依赖**：原实现用 `requests` + `pandas`（DataFrame 出表、
   `np.linspace` 算筹码），这里全部换成标准库 `urllib` + `list[dict]` +
   纯 Python 列表运算。打包版 lite-work 是 PyInstaller frozen 进程，
   多一个依赖就多一套 wheels/跨平台风险（AGENTS.md 硬约束）。
2. **限速复用主层**：所有请求走 `astock_data` 的令牌桶与浏览器指纹头，
   不另起限速器——东财对高频请求直接掐连接（实测 `RemoteDisconnected`），
   两套限速器会让总速率翻倍而失效。
3. **不做判断、不合成分数**：本层只把接口原始数据归一化成 dict。
   情绪、方向、好坏的结论由上层（`astock_extra` 渲染 / Agent 阅读）决定。
   证据可靠度差着数量级时合成一个分数会掩盖"该信哪一条"。

**分层理由**：`astock_data` 已承载行情/K线/选股/资金流四条主干，再加入
30 个端点会让单文件逼近 2000 行且难以定位。这里按"数据类别"独立成层，
被 `astock_mixin_extra`（工具层）与 `plugin.py`（装配层）引用。

字段与坑位（都在真实接口上核过，别按直觉改）：
  · 东财 datacenter 统一的 `reportName` + `filter` 语法，日期字段是
    `'YYYY-MM-DD 00:00:00'` 字符串，比较时要带引号；
  · 东财 clist 的 `diff` 有时是 list、有时是 dict（`ulist.np` 尤其如此），
    取值必须两种都认，否则会静默拿到空表；
  · 涨跌停池（push2ex）的 `data` 为 `null` 表示「非交易日/参数错」，
    不是「今天没有涨停」——两者必须分开，否则会把休市说成零涨停；
  · 同花顺涨停揭秘的 `first_limit_up_time` 是**秒级时间戳字符串**，
    不是 HHMMSS 整数（东财那边才是整数）；
  · 巨潮公告必须 POST 且带 `column`/`pageNum` 等固定字段，GET 直接 500。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import astock_data as data
from astock_data import DataError, _decompress, _LIMITER, _http_get, _http_get_json
from astock_market import now_cn, to_float

logger = logging.getLogger("litework.astock.extra")

# ---------------------------------------------------------------- 端点常量

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_SLIST = "https://push2.eastmoney.com/api/qt/slist/get"
EM_ZT = "https://push2ex.eastmoney.com"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

THS_LIMIT_POOL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
THS_STRONG = ("http://zx.10jqka.com.cn/event/api/getharden/date/{date}/"
              "orderby/date/orderway/desc/charset/GBK/")
THS_HOT_LIST = ("https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock")

CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
EM_REPORT = "https://reportapi.eastmoney.com/report/list"
SINA_FIN = ("https://quotes.sina.cn/cn/api/openapi.php/"
            "CompanyFinanceService.getFinanceReport2022")

TENCENT_TICK = "https://stock.gtimg.cn/data/index.php"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="

CHINABOND = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
CHINAMONEY = ("https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/"
              "currency/{name}-chrt.csv")
NBS_INDEX = "https://www.stats.gov.cn/sj/zxfb/"
SZSE_CAL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"

#: 东财各系接口的 Referer 要求不同（漏了会被 403，实测 datacenter 系尤其严）
_H_DC = dict(data._HEADERS, Referer="https://data.eastmoney.com/")
_H_QUOTE = dict(data._HEADERS, Referer="https://quote.eastmoney.com/")
_H_REPORT = dict(data._HEADERS, Referer="https://data.eastmoney.com/report/")
_H_CNINFO = dict(data._HEADERS, Referer="http://www.cninfo.com.cn/")
_H_QQ = dict(data._HEADERS, Referer="https://gu.qq.com/")


# ---------------------------------------------------------------- 通用请求


def _post(url: str, form: Dict[str, Any],
          headers: Optional[Dict[str, str]] = None,
          retries: int = 2) -> str:
    """表单 POST（巨潮公告/检索只认 POST）。

    与 `astock_data._http_get` 用同一个令牌桶与解压逻辑，只换请求方法。
    巨潮的 GET 会直接 500，所以不能图省事用 GET 兜底。
    """
    body = urllib.parse.urlencode(form).encode("utf-8")
    h = dict(headers or _H_CNINFO)
    h.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        if not _LIMITER.acquire():
            raise DataError("请求排队超时（本地限速器拥塞），请稍后重试")
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=data._TIMEOUT) as resp:
                raw = resp.read()
                raw = _decompress(raw, resp.headers.get("Content-Encoding"))
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise DataError(f"HTTP {exc.code}：{url.split('?')[0]} 返回错误") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                import time as _t
                _t.sleep(0.6 * (attempt + 1))
                continue
    raise DataError(f"接口连接被断开（{type(last_exc).__name__}）：{url.split('?')[0]}")


def _cached(key: str, ttl: float, fn) -> Any:
    """带 TTL 的取数缓存（复用主层缓存实例，避免两套缓存互不失效）。"""
    hit = data._CACHE.get(key, ttl)
    if hit is not None:
        return hit
    value = fn()
    data._CACHE.set(key, value)
    return value


def _day(raw: Any) -> Optional[str]:
    """东财日期串 → 'YYYY-MM-DD'；空值 None（不当成 1970）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    head = text[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
        return head
    if re.fullmatch(r"\d{8}", text[:8]):
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return None


def _num(raw: Any) -> Optional[float]:
    """数值归一：空/占位返回 None（**不用 0 代替**，否则"没有"会被读成"零"）。"""
    return to_float(raw, None)


def _today_cn() -> dt.date:
    return now_cn().date()


def _ymd(day: Optional[dt.date] = None) -> str:
    """YYYYMMDD（东财 push2ex 与同花顺涨停池要这个格式）。"""
    return (day or _today_cn()).strftime("%Y%m%d")


def _iso(day: Optional[dt.date] = None) -> str:
    return (day or _today_cn()).isoformat()


# ---------------------------------------------------------------- 东财 datacenter


def datacenter(report: str, filter_str: str = "", sort_columns: str = "",
               sort_types: str = "-1", page_size: int = 50, limit: int = 0,
               columns: str = "ALL", extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """东财数据中心统一查询（龙虎榜/解禁/两融/大宗/股东户数/分红/事件等共用）。

    **错误语义**（这是移植时最容易退化的地方）：
      · 第 1 页返回 9201「数据为空」→ 真没有数据，返回 `[]`；
      · 带了个股/日期条件时 0 行也是「确实没有」；
      · 但**全市场无条件下 0 行说明接口坏了**，抛 DataError 而不是返回空表
        ——否则「今天没有业绩预告」和「东财改版了」在调用方看来一模一样。
      · 翻页中途 9201 / 空页 / 条数对不上 → 抛错，不把部分结果当完整结果。
    """
    page_size = max(1, min(int(page_size), 500))
    want = int(limit or page_size)
    rows: List[Dict[str, Any]] = []
    page = 1
    pages_total: Optional[int] = None
    while True:
        params: Dict[str, Any] = {
            "reportName": report, "columns": columns, "filter": filter_str,
            "pageNumber": str(page), "pageSize": str(page_size),
            "sortColumns": sort_columns, "sortTypes": sort_types,
            "source": "WEB", "client": "WEB",
        }
        params.update(extra or {})
        payload = _http_get_json(f"{DC_URL}?{urllib.parse.urlencode(params)}",
                                 headers=_H_DC)
        if not isinstance(payload, dict):
            raise DataError(f"东财 {report} 返回的不是 JSON 对象")
        code = payload.get("code")
        if code == 9201:
            if page == 1:
                if filter_str:
                    return []
                raise DataError(
                    f"东财 {report} 全市场查询返回空（无过滤条件），接口口径可能已变")
            raise DataError(f"东财 {report} 第 {page} 页返回「数据为空」，"
                            f"已取 {len(rows)} 条，结果不完整")
        if code != 0:
            raise DataError(f"东财 {report} 返回错误 {code}：{payload.get('message')}")
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise DataError(f"东财 {report} 的 result 不是对象")
        block = result.get("data") or []
        if not isinstance(block, list):
            raise DataError(f"东财 {report} 的 data 不是列表，格式可能已变")
        pages_total = int(result.get("pages") or 1)
        rows.extend(r for r in block if isinstance(r, dict))
        if not block or page >= pages_total or len(rows) >= want:
            break
        page += 1
    return rows[:want]


def clist_all(fs: str, fields: str, page_size: int = 100,
              limit: int = 0, sort_by: str = "", ascending: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    """东财 clist 全量/限量翻页 → (行列表, 声明的总数)。

    板块数与 ST 名单都超过单页上限（行业 496 / 概念 531 / ST 562），
    写死 `pz=200` 会把 total 也误报成 200——必须按返回的 total 翻页。
    """
    page_size = max(1, min(int(page_size), 200))
    rows: List[Dict[str, Any]] = []
    total = 0
    page = 1
    while True:
        params = {
            "pn": str(page), "pz": str(page_size),
            # ⚠️ 东财的 po 语义与直觉相反：po=1 才是**降序**（实测 f3 从 9.94
            # 往下排），po=0 是升序。写反了「涨幅榜前 20」会变成「跌幅榜前 20」，
            # 而且行数、格式全都正常，只有结论是反的——最难发现的那类错。
            "po": "0" if ascending else "1",
            "np": "1", "fltt": "2", "invt": "2",
            "fid": sort_by or "f12", "fs": fs, "fields": fields,
        }
        payload = _http_get_json(f"{EM_CLIST}?{urllib.parse.urlencode(params)}",
                                 headers=_H_QUOTE)
        node = (payload or {}).get("data") or {}
        if not isinstance(node, dict) or not node:
            raise DataError(f"东财 clist 返回异常（fs={fs}）")
        total = int(node.get("total") or 0)
        diff = node.get("diff") or []
        if isinstance(diff, dict):        # push2 有时把 diff 给成 {序号: 行}
            diff = list(diff.values())
        if not isinstance(diff, list):
            raise DataError(f"东财 clist 的 diff 不是列表（fs={fs}）")
        rows.extend(r for r in diff if isinstance(r, dict))
        if not diff or len(rows) >= total or (limit and len(rows) >= limit):
            break
        page += 1
    return (rows[:limit] if limit else rows), total


# ---------------------------------------------------------------- 打板 / 涨停池

_ZT_ENDPOINT = {
    "zt": ("getTopicZTPool", "fbt:asc"),
    "zb": ("getTopicZBPool", "fbt:asc"),
    "dt": ("getTopicDTPool", "fund:asc"),
    "yzt": ("getYesterdayZTPool", "zs:desc"),
}
#: pzsh 的 ut 常量（东财行情中心前端硬编码，缺失会返回空 data）
_ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _fmt_zt_time(value: Any) -> str:
    """涨停板时间整数 → HH:MM:SS（92500 → 09:25:00）。非数值原样返回。"""
    text = str(value or "").strip()
    if not text.isdigit():
        return text
    s = text.zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def limit_pool(kind: str = "zt", date: Optional[str] = None) -> List[Dict[str, Any]]:
    """涨跌停池。kind: zt 涨停 / zb 炸板 / dt 跌停 / yzt 昨日涨停。

    date 传 YYYYMMDD。`data` 为 null 表示当天不是交易日（或参数错），
    此时返回空表并由上层提示「非交易日」，**不要**渲染成「零涨停」。
    """
    if kind not in _ZT_ENDPOINT:
        raise ValueError(f"kind 只能是 {list(_ZT_ENDPOINT)}")
    endpoint, sort = _ZT_ENDPOINT[kind]
    day = date or _ymd()
    params = {"ut": _ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0,
              "pagesize": 10000, "sort": sort, "date": day}
    payload = _http_get_json(f"{EM_ZT}/{endpoint}?{urllib.parse.urlencode(params)}",
                             headers=_H_QUOTE)
    node = (payload or {}).get("data")
    if not node:
        return []
    pool = node.get("pool") or []
    out: List[Dict[str, Any]] = []
    for p in pool:
        if not isinstance(p, dict):
            continue
        stat = p.get("zttj") or {}
        base = {
            "code": str(p.get("c") or ""),
            "name": str(p.get("n") or ""),
            "price": (to_float(p.get("p"), None) or 0) / 1000.0,
            "change_pct": round(to_float(p.get("zdp"), 0.0) or 0.0, 2),
            "turnover_pct": round(to_float(p.get("hs"), 0.0) or 0.0, 2),
            "industry": str(p.get("hybk") or ""),
            "streak_days": stat.get("days"),
            "streak_boards": stat.get("ct"),
        }
        if kind == "zt":
            base.update({
                "amount_yuan": to_float(p.get("amount"), None),
                "float_mcap_yuan": to_float(p.get("ltsz"), None),
                "limit_days": p.get("lbc"),
                "first_seal": _fmt_zt_time(p.get("fbt")),
                "last_seal": _fmt_zt_time(p.get("lbt")),
                "seal_fund_yuan": to_float(p.get("fund"), None),
                "break_times": p.get("zbc"),
            })
        elif kind == "zb":
            base.update({
                "limit_price": (to_float(p.get("ztp"), None) or 0) / 1000.0,
                "first_seal": _fmt_zt_time(p.get("fbt")),
                "break_times": p.get("zbc"),
                "amplitude_pct": to_float(p.get("zf"), None),
                "speed_pct": to_float(p.get("zs"), None),
            })
        elif kind == "dt":
            base.update({
                "pe": to_float(p.get("pe"), None),
                "seal_fund_yuan": to_float(p.get("fund"), None),
                "last_seal": _fmt_zt_time(p.get("lbt")),
                "dt_days": p.get("days"),
                "open_times": p.get("oc"),
            })
        else:      # yzt
            base.update({
                "amplitude_pct": to_float(p.get("zf"), None),
                "speed_pct": to_float(p.get("zs"), None),
                "y_first_seal": _fmt_zt_time(p.get("yfbt")),
                "y_limit_days": p.get("ylbc"),
            })
        out.append(base)
    return out


def limit_sentiment(date: Optional[str] = None) -> Dict[str, Any]:
    """打板情绪速算：涨停/炸板/跌停家数 + 炸板率 + 最高连板 + 连板梯队。

    炸板率 = 炸板 / (涨停 + 炸板)——分母含炸板本身，因为炸板也是
    「今天封过板」的标的，只用涨停数当分母会让炸板率超过 100%。
    """
    zt = limit_pool("zt", date)
    zb = limit_pool("zb", date)
    dtp = limit_pool("dt", date)
    ladder: Dict[int, int] = {}
    for s in zt:
        d = s.get("limit_days")
        if isinstance(d, int) and d > 0:
            ladder[d] = ladder.get(d, 0) + 1
    zt_n, zb_n = len(zt), len(zb)
    return {
        "date": date or _ymd(),
        "zt_count": zt_n,
        "zb_count": zb_n,
        "dt_count": len(dtp),
        "break_rate_pct": round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else None,
        "max_streak": max(ladder) if ladder else 0,
        "ladder": dict(sorted(ladder.items())),
        "zt_stocks": zt,
        "zb_stocks": zb,
        "dt_stocks": dtp,
    }


def ths_limit_reason(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """同花顺涨停揭秘：涨停原因题材 + 封板率 + 板型（东财没有这张表）。"""
    params = {
        "page": 1, "limit": 200,
        "field": ("199112,10,9001,330323,330324,330325,9002,330329,133971,"
                  "133970,1968584,3475914,9003,9004"),
        "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0",
        "date": date or _ymd(),
    }
    payload = _http_get_json(f"{THS_LIMIT_POOL}?{urllib.parse.urlencode(params)}",
                             headers={"User-Agent": data._HEADERS["User-Agent"]})
    info = ((payload or {}).get("data") or {}).get("info") or []
    out: List[Dict[str, Any]] = []
    for it in info:
        if not isinstance(it, dict):
            continue
        stamp = it.get("first_limit_up_time")
        first = ""
        if stamp:
            try:
                # ⚠️ 同花顺给的是**秒级时间戳字符串**，不是 HHMMSS 整数
                first = dt.datetime.fromtimestamp(int(stamp)).strftime("%H:%M:%S")
            except (TypeError, ValueError, OSError):
                first = ""
        out.append({
            "code": str(it.get("code") or ""),
            "name": str(it.get("name") or ""),
            "price": to_float(it.get("latest"), None),
            "change_pct": to_float(it.get("change_rate"), None),
            "reason": str(it.get("reason_type") or ""),
            "board_type": str(it.get("limit_up_type") or ""),
            "seal_rate": to_float(it.get("limit_up_suc_rate"), None),
            "break_times": it.get("open_num") or 0,
            "seal_amount_yuan": to_float(it.get("order_amount"), None),
            "high_days": str(it.get("high_days") or ""),
            "first_seal": first,
            "re_sealed": bool(it.get("is_again_limit")),
        })
    return out


def ths_strong(date: Optional[str] = None) -> List[Dict[str, Any]]:
    """同花顺当日强势股 + 题材归因（`reason` 是独家的题材标签）。

    date 接受 YYYYMMDD 或 YYYY-MM-DD（内部统一成 URL 要的 YYYY-MM-DD）。

    ⚠️ 这个接口**只有** id/name/code/reason/date/market 六个字段——原文档
    示例里的「收盘价/涨幅%/换手率」在这条端点上并不存在。价格类字段一律
    留 None（不用 0 代替），需要价格请配 `astock_quote` 或 `astock_screen`。
    """
    if not date:
        day = _iso()
    else:
        text = str(date).strip()
        day = (f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
               if re.fullmatch(r"\d{8}", text) else text[:10])
    payload = _http_get_json(THS_STRONG.format(date=day),
                             headers={"User-Agent": data._HEADERS["User-Agent"]})
    if (payload or {}).get("errocode"):
        raise DataError(f"同花顺热点错误：{(payload or {}).get('errormsg')}")
    rows = (payload or {}).get("data") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "code": str(r.get("code") or ""),
            "name": str(r.get("name") or ""),
            "reason": str(r.get("reason") or ""),
            "date": str(r.get("date") or ""),
            # 以下价格字段该端点并不返回：统一置 None，让上层显示 "-"
            "price": to_float(r.get("close"), None),
            "change_pct": to_float(r.get("zhangfu"), None),
            "turnover_pct": to_float(r.get("huanshou"), None),
            "amount_yuan": to_float(r.get("chengjiaoe"), None),
            "big_order_net": to_float(r.get("ddejingliang"), None),
        })
    return out


def ths_hot_list(period: str = "hour") -> List[Dict[str, Any]]:
    """同花顺热榜（人气值 + 概念标签 + 排名变化）。period: hour / day。"""
    params = {"stock_type": "a", "type": period, "list_type": "normal"}
    payload = _http_get_json(f"{THS_HOT_LIST}?{urllib.parse.urlencode(params)}",
                             headers={"User-Agent": data._HEADERS["User-Agent"]})
    lst = ((payload or {}).get("data") or {}).get("stock_list") or []
    out: List[Dict[str, Any]] = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        tag = it.get("tag") or {}
        out.append({
            "rank": it.get("order"),
            "code": str(it.get("code") or ""),
            "name": str(it.get("name") or ""),
            "heat": to_float(it.get("rate"), None),
            "change_pct": to_float(it.get("rise_and_fall"), None),
            "rank_change": it.get("hot_rank_chg"),
            "concepts": [str(c) for c in (tag.get("concept_tag") or [])],
            "tag": str(tag.get("popularity_tag") or ""),
        })
    return out


def industry_rank(top_n: int = 20) -> Dict[str, Any]:
    """全行业涨跌幅排名（东财行业板块，约 100+ 个）。

    `po=1` + `fid=f3` 才是按涨跌幅降序；缺 fid 时切片出来的「前后 N 名」
    并不是涨幅榜前后 N 名——这是原实现踩过的坑，注释保留。
    """
    rows, total = clist_all("m:90+t:2",
                            "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f204",
                            page_size=200, sort_by="f3")
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(rows):
        out.append({
            "rank": i + 1,
            "name": str(item.get("f14") or ""),
            "code": str(item.get("f12") or ""),
            "change_pct": to_float(item.get("f3"), None),
            "up_count": item.get("f104"),
            "down_count": item.get("f105"),
            # ⚠️ 领涨股：名称在 f128、代码在 f140、涨幅在 f136。
            # 把 f140 当名称会把「603381」显示成领涨股名字（实测踩到）。
            "leader": str(item.get("f128") or ""),
            "leader_code": str(item.get("f140") or ""),
            "leader_change_pct": to_float(item.get("f136"), None),
        })
    n = max(1, min(int(top_n or 20), len(out) or 1))
    return {"total": total or len(out), "top": out[:n], "bottom": out[-n:] if out else []}


#: 板块类型 → 东财 fs 参数
BOARD_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}
#: 周期 → (排序 fid, 主力净额, 主力净占比, 涨跌幅, 领涨股)
#: ⚠️ 5 日/10 日的涨跌幅字段不是 f3（那是当日），用错会把「今日涨跌」当成 5 日
_BOARD_PERIOD = {
    "today": ("f62", "f62", "f184", "f3", "f204"),
    "5d": ("f164", "f164", "f165", "f109", "f257"),
    "10d": ("f174", "f174", "f175", "f160", "f128"),
}


def board_fund_flow(board_type: str = "industry", period: str = "today",
                    top_n: int = 20) -> Dict[str, Any]:
    """板块资金流向排名（按主力净流入降序）。主力净额 = 超大单 + 大单。"""
    if board_type not in BOARD_FS:
        raise ValueError(f"board_type 只能是 {list(BOARD_FS)}")
    if period not in _BOARD_PERIOD:
        raise ValueError(f"period 只能是 {list(_BOARD_PERIOD)}")
    fid, f_main, f_pct, f_chg, f_leader = _BOARD_PERIOD[period]
    fields = ["f12", "f14", f_chg, f_main, f_pct, f_leader]
    rows, total = clist_all(BOARD_FS[board_type], ",".join(dict.fromkeys(fields)),
                            page_size=200, limit=max(20, min(int(top_n or 20), 200)),
                            sort_by=fid)
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(rows):
        out.append({
            "rank": i + 1,
            "name": str(it.get("f14") or ""),
            "code": str(it.get("f12") or ""),
            "change_pct": to_float(it.get(f_chg), None),
            "main_net_yuan": to_float(it.get(f_main), None),
            "main_pct": to_float(it.get(f_pct), None),
            "leader": str(it.get(f_leader) or ""),
        })
    return {"board_type": board_type, "period": period,
            "total": total or len(out), "rows": out[:max(1, int(top_n or 20))]}


def st_stocks() -> List[Dict[str, Any]]:
    """全市场 ST / *ST 名单（风险警示板快照）。

    沪深走东财「风险警示板」过滤（`f:4`），北交所不纳入该过滤，改为拉
    北交所全表按名称筛——否则会把「北交所没有 ST」说成事实。
    """
    def load() -> List[Dict[str, Any]]:
        shsz, _ = clist_all("m:0+f:4,m:1+f:4", "f12,f13,f14,f2,f3", page_size=200)
        bj, _ = clist_all("m:0+t:81+s:2048", "f12,f13,f14,f2,f3", page_size=200)
        out: List[Dict[str, Any]] = []
        for rec in shsz:
            name = str(rec.get("f14") or "").strip()
            if not name:
                continue
            out.append({"code": str(rec.get("f12") or ""), "name": name,
                        "market": "sh" if rec.get("f13") == 1 else "sz",
                        "st_type": "*ST" if name.startswith("*") else "ST",
                        "price": _num(rec.get("f2")),
                        "change_pct": _num(rec.get("f3"))})
        for rec in bj:
            name = str(rec.get("f14") or "").strip()
            if "ST" not in name.upper():       # 北交所要自己按名称筛
                continue
            out.append({"code": str(rec.get("f12") or ""), "name": name,
                        "market": "bj",
                        "st_type": "*ST" if name.startswith("*") else "ST",
                        "price": _num(rec.get("f2")),
                        "change_pct": _num(rec.get("f3"))})
        if not out:
            raise DataError("ST 名单为空，接口口径可能已变（不能当成「今天没有 ST」）")
        return out

    return _cached("extra:st", 600.0, load)


# ---------------------------------------------------------------- 资金 / 筹码


def margin_trading(code: str, limit: int = 30) -> List[Dict[str, Any]]:
    """融资融券明细（日级）。"""
    rows = datacenter("RPTA_WEB_RZRQ_GGMX", filter_str=f'(scode="{code}")',
                      sort_columns="DATE", sort_types="-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "date": _day(r.get("DATE")),
            "fin_balance_yuan": _num(r.get("RZYE")),
            "fin_buy_yuan": _num(r.get("RZMRE")),
            "fin_repay_yuan": _num(r.get("RZCHE")),
            "sec_balance_yuan": _num(r.get("RQYE")),
            "total_balance_yuan": _num(r.get("RZRQYE")),
            "fin_net_buy_yuan": _num(r.get("RZJME")),
        })
    return out


def block_trade(code: str, limit: int = 20) -> List[Dict[str, Any]]:
    """大宗交易记录（溢价率用当日收盘价算，接口给的 PREMIUM_RATIO 单位不稳）。"""
    rows = datacenter("RPT_DATA_BLOCKTRADE", filter_str=f'(SECURITY_CODE="{code}")',
                      sort_columns="TRADE_DATE", sort_types="-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        close = _num(r.get("CLOSE_PRICE"))
        deal = _num(r.get("DEAL_PRICE"))
        premium = round((deal / close - 1) * 100, 2) if (close and deal) else None
        out.append({
            "date": _day(r.get("TRADE_DATE")),
            "price": deal,
            "close": close,
            "premium_pct": premium,
            "volume": _num(r.get("DEAL_VOLUME")),
            "amount_yuan": _num(r.get("DEAL_AMT")),
            "buyer": str(r.get("BUYER_NAME") or ""),
            "seller": str(r.get("SELLER_NAME") or ""),
        })
    return out


def holder_count(code: str, limit: int = 10) -> List[Dict[str, Any]]:
    """股东户数变化（季度级）。户数下降＝筹码集中。"""
    rows = datacenter("RPT_HOLDERNUMLATEST", filter_str=f'(SECURITY_CODE="{code}")',
                      sort_columns="END_DATE", sort_types="-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "end_date": _day(r.get("END_DATE")),
            "holders": _num(r.get("HOLDER_NUM")),
            "change_num": _num(r.get("HOLDER_NUM_CHANGE")),
            "change_pct": _num(r.get("HOLDER_NUM_RATIO")),
            "avg_hold_value_yuan": _num(r.get("AVG_MARKET_CAP")),
        })
    return out


def dividend_history(code: str, limit: int = 20) -> List[Dict[str, Any]]:
    """分红送转历史（每股派息为税前；送转是「每 10 股」口径）。"""
    rows = datacenter("RPT_SHAREBONUS_DET", filter_str=f'(SECURITY_CODE="{code}")',
                      sort_columns="EX_DIVIDEND_DATE", sort_types="-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "ex_date": _day(r.get("EX_DIVIDEND_DATE")),
            "report_date": _day(r.get("REPORT_DATE")),
            "bonus_rmb_per_share": _num(r.get("PRETAX_BONUS_RMB")),
            "bonus_per_10": _num(r.get("BONUS_RATIO")),
            "transfer_per_10": _num(r.get("IT_RATIO")),
            "progress": str(r.get("ASSIGN_PROGRESS") or ""),
        })
    return out


def lockup_expiry(code: str, forward_days: int = 90,
                  today: Optional[dt.date] = None) -> Dict[str, Any]:
    """限售解禁日历：历史记录 + 未来 N 天待解禁。

    `able_free_shares`（实际可流通股数）比 `free_shares` 更贴近真实抛压：
    公告的解禁数量常含仍未流通的部分。
    """
    base = today or _today_cn()

    def normalize(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            out.append({
                "date": _day(r.get("FREE_DATE")),
                "type": str(r.get("FREE_SHARES_TYPE") or ""),
                "shares_10k": _num(r.get("FREE_SHARES")),
                "able_shares_10k": _num(r.get("ABLE_FREE_SHARES")),
                "ratio": _num(r.get("FREE_RATIO")),
            })
        return out

    history = datacenter("RPT_LIFT_STAGE", filter_str=f'(SECURITY_CODE="{code}")',
                         sort_columns="FREE_DATE", sort_types="-1", limit=15)
    end = base + dt.timedelta(days=max(1, int(forward_days)))
    upcoming = datacenter(
        "RPT_LIFT_STAGE",
        filter_str=(f'(SECURITY_CODE="{code}")'
                    f"(FREE_DATE>='{base.isoformat()}')"
                    f"(FREE_DATE<='{end.isoformat()}')"),
        sort_columns="FREE_DATE", sort_types="1", limit=20)
    return {"history": normalize(history), "upcoming": normalize(upcoming),
            "window": f"{base.isoformat()} ~ {end.isoformat()}"}


def lhb_stock(code: str, look_back_days: int = 180) -> Dict[str, Any]:
    """个股龙虎榜：回看窗口内的上榜记录 + 最近一次买卖席位 TOP5 + 机构动向。

    **席位变量必须在 `if records` 之前初始化**：回看窗口内没有上榜记录是
    大市值/低换手标的的常态，若在分支里才绑定，后续机构统计会
    `UnboundLocalError`——调用方拿到的是崩溃而不是"空结果"（原实现 issue #45）。
    """
    end = _today_cn()
    start = end - dt.timedelta(days=max(1, int(look_back_days)))
    records: List[Dict[str, Any]] = []
    raw = datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=(f"(TRADE_DATE>='{start.isoformat()}')"
                    f"(TRADE_DATE<='{end.isoformat()}')"
                    f'(SECURITY_CODE="{code}")'),
        sort_columns="TRADE_DATE", sort_types="-1", limit=50)
    for r in raw:
        net = _num(r.get("BILLBOARD_NET_AMT"))
        records.append({
            "date": _day(r.get("TRADE_DATE")),
            "reason": str(r.get("EXPLANATION") or r.get("EXPLAIN") or ""),
            "net_buy_wan": round(net / 1e4, 1) if net is not None else None,
            "turnover_pct": _num(r.get("TURNOVERRATE")),
            "change_pct": _num(r.get("CHANGE_RATE")),
        })

    buy_raw: List[Dict[str, Any]] = []
    sell_raw: List[Dict[str, Any]] = []
    seats: Dict[str, List[Dict[str, Any]]] = {"buy": [], "sell": []}
    latest = records[0]["date"] if records else None
    if latest:
        def seat_rows(report: str, sort_col: str) -> List[Dict[str, Any]]:
            return datacenter(
                report,
                filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
                sort_columns=sort_col, sort_types="-1", limit=10)

        buy_raw = seat_rows("RPT_BILLBOARD_DAILYDETAILSBUY", "BUY")
        sell_raw = seat_rows("RPT_BILLBOARD_DAILYDETAILSSELL", "SELL")
        for side, rows in (("buy", buy_raw), ("sell", sell_raw)):
            for r in rows[:5]:
                seats[side].append({
                    "name": str(r.get("OPERATEDEPT_NAME") or ""),
                    "buy_wan": round((_num(r.get("BUY")) or 0) / 1e4, 1),
                    "sell_wan": round((_num(r.get("SELL")) or 0) / 1e4, 1),
                    "net_wan": round((_num(r.get("NET")) or 0) / 1e4, 1),
                })

    #: 机构专用席位的营业部代码是 "0"（其余是真实营业部编号）
    inst = {"buy_wan": 0.0, "sell_wan": 0.0}
    for rows, side in ((buy_raw, "buy"), (sell_raw, "sell")):
        for r in rows:
            if str(r.get("OPERATEDEPT_CODE") or "") == "0":
                amt = _num(r.get("BUY")) if side == "buy" else _num(r.get("SELL"))
                inst[f"{side}_wan"] += round((amt or 0) / 1e4, 1)
    inst["net_wan"] = round(inst["buy_wan"] - inst["sell_wan"], 1)
    return {"records": records, "seats": seats, "institution": inst,
            "latest_date": latest, "window_days": look_back_days}


def lhb_market(date: Optional[str] = None, min_net_wan: Optional[float] = None) -> Dict[str, Any]:
    """全市场龙虎榜（某交易日）。净买入单位换算成万元。"""
    day = date or _iso()
    rows = datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{day}')(TRADE_DATE<='{day}')",
        sort_columns="BILLBOARD_NET_AMT", sort_types="-1", limit=500)
    stocks: List[Dict[str, Any]] = []
    actual = day
    for i, r in enumerate(rows):
        if i == 0:
            actual = _day(r.get("TRADE_DATE")) or day
        net_wan = round((_num(r.get("BILLBOARD_NET_AMT")) or 0) / 1e4, 1)
        if min_net_wan is not None and net_wan < float(min_net_wan):
            continue
        stocks.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "reason": str(r.get("EXPLANATION") or r.get("EXPLAIN") or ""),
            "close": _num(r.get("CLOSE_PRICE")),
            "change_pct": _num(r.get("CHANGE_RATE")),
            "net_buy_wan": net_wan,
            "buy_wan": round((_num(r.get("BILLBOARD_BUY_AMT")) or 0) / 1e4, 1),
            "sell_wan": round((_num(r.get("BILLBOARD_SELL_AMT")) or 0) / 1e4, 1),
            "turnover_pct": _num(r.get("TURNOVERRATE")),
        })
    return {"date": actual, "total": len(stocks), "stocks": stocks,
            "raw_records": len(rows)}


def concept_blocks(code: str, venue: str = "sh") -> Dict[str, Any]:
    """个股所属板块/概念归属（东财 slist，一次请求拿全）。

    slist 的 `diff` 是 **dict**（`{"0": {...}, "1": {...}}`），直接按 list
    遍历会得到一串字符串键，板块名全部丢失——必须 `values()`。
    """
    secid = f"{'1' if venue == 'sh' else '0'}.{code}"
    params = {"fltt": "2", "invt": "2", "secid": secid, "spt": "3",
              "pi": "0", "pz": "200", "po": "1",
              "fields": "f12,f14,f3,f128,f140"}
    payload = _http_get_json(f"{EM_SLIST}?{urllib.parse.urlencode(params)}",
                             headers=_H_QUOTE)
    diff = ((payload or {}).get("data") or {}).get("diff") or {}
    items = diff.values() if isinstance(diff, dict) else diff
    boards: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        boards.append({
            "name": str(it.get("f14") or ""),
            "code": str(it.get("f12") or ""),
            "change_pct": to_float(it.get("f3"), None),
            "leader": str(it.get("f128") or ""),
        })
    return {"total": len(boards), "boards": boards,
            "tags": [b["name"] for b in boards if b["name"]]}


# ---------------------------------------------------------------- 逐笔 / 筹码

#: 一页 70 笔；实测最活跃的票全天约 4800 笔（69 页）
_TICK_PAGE_SIZE = 70


def _tencent_snapshot(symbol: str) -> Dict[str, Any]:
    """腾讯行情快照里的交易日、时刻、当日成交额、盘后定价成交额。"""
    text = _http_get(TENCENT_QUOTE + symbol, headers=_H_QQ, encoding="gbk")
    if "v_pv_none_match" in text:
        raise ValueError(f"腾讯没有 {symbol} 这个代码")
    m = re.search(rf'v_{symbol}="([^"]*)"', text)
    if not m:
        raise DataError(f"腾讯快照 {symbol} 的返回里没有 v_{symbol} 变量，格式可能已变")
    fields = m.group(1).split("~")
    if len(fields) < 36 or not re.fullmatch(r"\d{14}", str(fields[30] or "")):
        raise DataError(f"腾讯快照 {symbol} 字段数 {len(fields)} 或时间字段异常")
    stamp = fields[30]
    parts = str(fields[35] or "").split("/")     # 最新价/成交量/成交额(元)
    if len(parts) != 3:
        raise DataError(f"腾讯快照 {symbol} 的价/量/额字段是 {fields[35]!r}")
    after_raw = str(fields[58]) if len(fields) > 58 else ""
    after = (round(float(after_raw) * 1e4, 2)
             if re.fullmatch(r"\d+(\.\d+)?", after_raw) else None)
    return {
        "day": f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}",
        "clock": stamp[8:],
        "amount_yuan": to_float(parts[2], None),
        "after_hours_yuan": after,
    }


def _tick_page(symbol: str, page: int) -> Optional[List[Dict[str, Any]]]:
    """第 page 页逐笔（0 起）。翻过末页时腾讯返回空内容 → None。"""
    params = {"appn": "detail", "action": "data", "c": symbol, "p": str(page)}
    text = _http_get(f"{TENCENT_TICK}?{urllib.parse.urlencode(params)}",
                     headers=_H_QQ, encoding="gbk").strip()
    if not text:
        return None
    m = re.fullmatch(rf'v_detail_data_{symbol}=\[(\d+),"([^"]*)"\];?', text)
    if not m or int(m.group(1)) != page:
        raise DataError(f"腾讯逐笔 {symbol} 第 {page} 页不是预期格式: {text[:80]!r}")
    if not m.group(2):
        return None
    out: List[Dict[str, Any]] = []
    for item in m.group(2).split("|"):
        bits = item.split("/")
        if len(bits) != 7:
            raise DataError(f"腾讯逐笔 {symbol} 第 {page} 页记录字段数异常: {item!r}")
        seq, clock, price, change, volume, amount, side = bits
        if not re.fullmatch(r"\d\d:\d\d:\d\d", clock) or side not in ("B", "S", "M"):
            raise DataError(f"腾讯逐笔 {symbol} 第 {page} 页记录格式改变: {item!r}")
        out.append({"seq": int(seq), "time": clock,
                    "price": to_float(price, None), "change": to_float(change, None),
                    "volume_lot": to_float(volume, None),
                    "amount_yuan": to_float(amount, None), "side": side})
    return out


def ticks(code: str, venue: str = "sh", max_pages: int = 10) -> Dict[str, Any]:
    """腾讯逐笔成交（分笔，不是 Level-2）。

    max_pages 默认 10 页 ≈ 700 笔（约半小时最活跃时段）。原实现翻满 300 页，
    但在插件里一次工具调用翻 69 页要 7 秒以上且容易触发限流——需要全量时
    显式调大。返回里带 `complete=False` 明确表示"这是前 N 笔，不是全天"。
    """
    if venue == "bj":
        raise ValueError("腾讯逐笔不支持北交所（接口返回空）")
    symbol = f"{venue}{code}"
    before = _tencent_snapshot(symbol)
    if not before["amount_yuan"]:
        raise ValueError(f"{symbol} 当日没有成交（停牌/未开盘/集合竞价未撮合）")
    rows: List[Dict[str, Any]] = []
    pages = 0
    for page in range(max(1, min(int(max_pages or 10), 300))):
        batch = _tick_page(symbol, page)
        if batch is None:
            pages = page + 1
            break
        rows.extend(batch)
        pages = page + 1
    complete = pages < max(1, min(int(max_pages or 10), 300))
    return {"code": symbol, "date": before["day"], "ticks": rows,
            "pages": pages, "complete": complete,
            "snapshot_amount_yuan": before["amount_yuan"],
            "fetched_at_clock": before["clock"]}


def kline_with_turnover(code: str, venue: str = "sh", days: int = 150,
                        adjust: str = "qfq") -> List[Dict[str, Any]]:
    """带换手率的日 K（东财 f61 字段）。

    筹码分布**必须**用换手率做衰减，主层 `astock_data.Bar` 不含换手率，
    所以这里单独取一次（字段顺序见下方注释，别和腾讯的顺序搞混）。
    """
    fqt = {"qfq": "1", "hfq": "2", "none": "0"}.get((adjust or "qfq").lower(), "1")
    params = {
        "secid": f"{'1' if venue == 'sh' else '0'}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        # f51 日期 f52 开 f53 收 f54 高 f55 低 f56 量(手) f57 额 f58 振幅
        # f59 涨跌幅 f60 涨跌额 f61 换手率 —— 东财是「开,收,高,低」顺序
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": fqt, "end": "20500101", "lmt": str(max(1, min(int(days), 640))),
    }
    payload = _http_get_json(f"{EM_KLINE}?{urllib.parse.urlencode(params)}",
                             headers=_H_QUOTE)
    lines = ((payload or {}).get("data") or {}).get("klines") or []
    out: List[Dict[str, Any]] = []
    for raw in lines:
        p = str(raw).split(",")
        if len(p) < 11:
            continue
        out.append({
            "date": p[0], "open": to_float(p[1], 0.0), "close": to_float(p[2], 0.0),
            "high": to_float(p[3], 0.0), "low": to_float(p[4], 0.0),
            "volume_lot": to_float(p[5], 0.0),
            "amount_yuan": to_float(p[6], None),
            "change_pct": to_float(p[8], None),
            "turnover_pct": to_float(p[10], None),
        })
    return out


def _triangular_weights(grid: Sequence[float], low: float, high: float,
                        avg: float) -> List[float]:
    """当日筹码在价格网格上的三角分布权重（峰值在均价，面积归一）。

    **为什么用三角分布**：日内成交在 VWAP（用 (H+L+C)/3 近似）附近密集、
    两端稀疏，三角形是"不做额外假设"的最小模型；均匀分布会把两端也当
    高密度，导致成本区间被系统性拉宽。
    """
    n = len(grid)
    w = [0.0] * n
    if high < low or not all(math.isfinite(x) for x in (low, high, avg)):
        return w
    if high - low < 1e-9:                     # 一字板：全部堆在一个价位
        best = min(range(n), key=lambda i: abs(grid[i] - low))
        w[best] = 1.0
        return w
    avg = min(max(avg, low), high)            # 均价必须落在当日区间内
    for i in range(n):
        g = grid[i]
        if low <= g <= avg:
            w[i] = (g - low) / (avg - low) if avg - low > 1e-9 else 1.0
        elif avg < g <= high:
            w[i] = (high - g) / (high - avg) if high - avg > 1e-9 else 1.0
    total = sum(w)
    if total > 0:
        return [x / total for x in w]
    # 振幅窄于网格步长时可能一个点都没落进 [low, high]，映射到最近网格点，
    # 否则该日的换手衰减会一并丢失，低波动标的 + 长窗口会累积成明显偏差。
    best = min(range(n), key=lambda i: abs(grid[i] - avg))
    w[best] = 1.0
    return w


def chip_distribution(bars: Sequence[Dict[str, Any]], grid_size: int = 300,
                      decay: float = 1.0) -> Dict[str, Any]:
    """筹码分布（CYQ）：获利比例 / 平均成本 / 成本区间 / 集中度 / 筹码峰。

    **两个反直觉但必须遵守的点**（原实现踩过，注释保留）：

    1. 初始筹码必须播种成「首日全部流通盘」，不能从全零开始。从零起步等于
       假设窗口之前没有任何持仓，再把窗口内的少量换手归一化成 100%：
       两个 1% 换手日（价 10 和 100）会被算成约 50/50，而真实情况是约 99%
       仍在 10 附近。
    2. 事件必须按时间**升序**推进。换手衰减是有方向的时序递推，传入常见的
       「最新在前」倒序会让衰减反向推、且"现价"取成最老的收盘价——结果
       完全错却不会报错。

    decay: 换手衰减系数，1.0 = 按真实换手率换手；同花顺口径常用 1.5~2.0。
    """
    need = ("date", "high", "low", "close", "turnover_pct")
    rows = [b for b in bars if all(b.get(k) is not None for k in need)]
    rows = [b for b in rows if (b.get("high") or 0) > 0 and (b.get("low") or 0) > 0]
    if not rows:
        raise ValueError("筹码分布需要同时有 high/low/close/turnover_pct 的 K 线，"
                         "当前有效行数为 0（检查是否全是停牌日）")
    rows.sort(key=lambda b: str(b["date"]))

    lo = min(float(b["low"]) for b in rows)
    hi = max(float(b["high"]) for b in rows)
    pad = (hi - lo) * 0.02 or max(lo * 0.02, 0.01)
    grid = [lo - pad + (hi + pad - (lo - pad)) * i / max(1, grid_size - 1)
            for i in range(grid_size)]

    chips: Optional[List[float]] = None
    for b in rows:
        t = float(b["turnover_pct"]) / 100.0 * float(decay or 1.0)
        t = min(max(t, 0.0), 1.0)             # 换手率兜到 [0,1]，防异常值一次清零
        avg = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        w = _triangular_weights(grid, float(b["low"]), float(b["high"]), avg)
        if sum(w) <= 0:
            continue
        if chips is None:
            chips = w[:]                      # 首日分布 = 期初全部流通筹码
            continue
        chips = [c * (1.0 - t) + x * t for c, x in zip(chips, w)]
    if chips is None:
        raise ValueError("所有交易日的价格区间都无效，无法构建筹码分布")
    total = sum(chips)
    if total <= 0:
        raise ValueError("筹码总量为 0，无法计算指标")
    chips = [c / total for c in chips]

    price = float(rows[-1]["close"])
    cum: List[float] = []
    acc = 0.0
    for c in chips:
        acc += c
        cum.append(acc)

    def price_at(q: float) -> float:
        """分位价（线性插值，与 numpy.interp 同口径）。"""
        if q <= cum[0]:
            return grid[0]
        for i in range(1, len(cum)):
            if cum[i] >= q:
                span = cum[i] - cum[i - 1]
                if span <= 1e-12:
                    return grid[i]
                ratio = (q - cum[i - 1]) / span
                return grid[i - 1] + (grid[i] - grid[i - 1]) * ratio
        return grid[-1]

    p05, p15, p85, p95 = (price_at(q) for q in (0.05, 0.15, 0.85, 0.95))
    peak = grid[max(range(len(chips)), key=lambda i: chips[i])]
    return {
        "price": price,
        "profit_ratio": sum(c for g, c in zip(grid, chips) if g <= price),
        "avg_cost": sum(g * c for g, c in zip(grid, chips)),
        "cost_90": (p05, p95),
        "cost_70": (p15, p85),
        "concentration_90": ((p95 - p05) / (p95 + p05)) if (p95 + p05) else None,
        "concentration_70": ((p85 - p15) / (p85 + p15)) if (p85 + p15) else None,
        "peak_price": peak,
        "bars": len(rows),
        "first_date": str(rows[0]["date"]),
        "last_date": str(rows[-1]["date"]),
        "cum_turnover_pct": sum(float(b["turnover_pct"]) for b in rows),
    }


# ---------------------------------------------------------------- 事件驱动


def earnings_forecast(code: Optional[str] = None, report_date: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
    """业绩预告（沪深京）。一次预告会拆成多行：`indicator` 是预告指标。"""
    parts: List[str] = []
    if code:
        parts.append(f'(SECURITY_CODE="{code}")')
    if report_date:
        parts.append(f"(REPORT_DATE='{report_date}')")
    rows = datacenter("RPT_PUBLIC_OP_NEWPREDICT", filter_str="".join(parts),
                      sort_columns="NOTICE_DATE,SECURITY_CODE,REPORT_DATE,PREDICT_FINANCE_CODE",
                      sort_types="-1,1,-1,1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "notice_date": _day(r.get("NOTICE_DATE")),
            "report_date": _day(r.get("REPORT_DATE")),
            "indicator": str(r.get("PREDICT_FINANCE") or ""),
            "type": str(r.get("PREDICT_TYPE") or ""),
            "amount_lower_yuan": _num(r.get("PREDICT_AMT_LOWER")),
            "amount_upper_yuan": _num(r.get("PREDICT_AMT_UPPER")),
            "change_pct_lower": _num(r.get("ADD_AMP_LOWER")),
            "change_pct_upper": _num(r.get("ADD_AMP_UPPER")),
            "prior_year_yuan": _num(r.get("PREYEAR_SAME_PERIOD")),
            "reason": str(r.get("CHANGE_REASON_EXPLAIN") or ""),
        })
    return out


def institution_survey(code: Optional[str] = None, days: int = 30,
                       limit: int = 30) -> List[Dict[str, Any]]:
    """机构调研（汇总自投资者关系活动记录表）。org_count 为参与机构家数。"""
    parts = ['(IS_SOURCE="1")', '(NUMBERNEW="1")']
    if code:
        parts.append(f'(SECURITY_CODE="{code}")')
    if days:
        start = (_today_cn() - dt.timedelta(days=max(1, int(days)))).isoformat()
        parts.append(f"(NOTICE_DATE>='{start}')")
    rows = datacenter("RPT_ORG_SURVEYNEW", filter_str="".join(parts),
                      sort_columns="NOTICE_DATE,SECURITY_CODE,RECEIVE_START_DATE",
                      sort_types="-1,1,-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "notice_date": _day(r.get("NOTICE_DATE")),
            "survey_date": _day(r.get("RECEIVE_START_DATE")),
            "org_count": _num(r.get("SUM")),
            "way": str(r.get("RECEIVE_WAY_EXPLAIN") or ""),
            "place": str(r.get("RECEIVE_PLACE") or ""),
            "receptionist": str(r.get("RECEPTIONIST") or ""),
        })
    return out


def holder_trades(code: Optional[str] = None, direction: Optional[str] = None,
                  days: int = 60, limit: int = 50) -> List[Dict[str, Any]]:
    """股东增减持。direction: None / '增持' / '减持'。股数单位万股。

    **符号校验**：东财同时给 `DIRECTION` 与带符号的 `CHANGE_NUM_SYMBOL`，
    两者方向相反时抛错——宁可报错也不要把「增持」渲染成负股数。
    """
    if direction not in (None, "", "增持", "减持"):
        raise ValueError("direction 只能是 增持 / 减持 / 留空")
    parts: List[str] = []
    if code:
        parts.append(f'(SECURITY_CODE="{code}")')
    if direction:
        parts.append(f'(DIRECTION="{direction}")')
    if days:
        start = (_today_cn() - dt.timedelta(days=max(1, int(days)))).isoformat()
        parts.append(f"(NOTICE_DATE>='{start}')")
    rows = datacenter("RPT_SHARE_HOLDER_INCREASE", filter_str="".join(parts),
                      sort_columns="NOTICE_DATE,SECURITY_CODE,HOLDER_NAME,START_DATE,END_DATE",
                      sort_types="-1,1,1,1,1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        signed = _num(r.get("CHANGE_NUM_SYMBOL"))
        side = str(r.get("DIRECTION") or "")
        if signed is not None and side in ("增持", "减持"):
            if (signed < 0) != (side == "减持"):
                raise DataError(
                    f"东财增减持方向与股数符号不一致：{r.get('SECURITY_CODE')} "
                    f"{r.get('HOLDER_NAME')}（{side} / {signed}）")
        out.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "holder": str(r.get("HOLDER_NAME") or ""),
            "direction": side,
            "change_shares_10k": signed,
            "after_pct_total": _num(r.get("HOLD_RATIO")),
            "avg_price": _num(r.get("TRADE_AVERAGE_PRICE")),
            "channel": str(r.get("MARKET") or ""),
            "start_date": _day(r.get("START_DATE")),
            "end_date": _day(r.get("END_DATE")),
            "notice_date": _day(r.get("NOTICE_DATE")),
        })
    return out


#: 东财回购进度码 → 中文。007/008 东财自己页面也不显示，保留原码。
BUYBACK_PROGRESS = {"001": "董事会预案", "002": "股东大会通过", "003": "股东大会否决",
                    "004": "实施中", "005": "停止实施", "006": "完成实施"}


def share_buyback(code: Optional[str] = None, progress: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
    """股票回购方案与实施进度（一个方案一行，按最新公告日倒序）。"""
    code_map = {v: k for k, v in BUYBACK_PROGRESS.items()}
    if progress and progress not in code_map:
        raise ValueError("progress 只能是 " + " / ".join(code_map))
    parts: List[str] = []
    if code:
        parts.append(f'(DIM_SCODE="{code}")')      # 这张表的代码字段叫 DIM_SCODE
    if progress:
        parts.append(f'(REPURPROGRESS="{code_map[progress]}")')
    rows = datacenter("RPTA_WEB_GETHGLIST_NEW", filter_str="".join(parts),
                      sort_columns="UPD,DIM_SCODE,REPURCODE", sort_types="-1,1,1",
                      limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        pcode = str(r.get("REPURPROGRESS") or "")
        out.append({
            "code": str(r.get("DIM_SCODE") or ""),
            "name": str(r.get("SECURITYSHORTNAME") or ""),
            "progress": BUYBACK_PROGRESS.get(pcode, ""),
            "progress_code": pcode,
            "plan_start": _day(r.get("REPURSTARTDATE")),
            "plan_end": _day(r.get("REPURENDDATE")),
            "price_cap": _num(r.get("REPURPRICECAP")),
            "amount_lower_yuan": _num(r.get("REPURAMOUNTLOWER")),
            "amount_upper_yuan": _num(r.get("REPURAMOUNTLIMIT")),
            "done_shares": _num(r.get("REPURNUM")),
            "done_amount_yuan": _num(r.get("REPURAMOUNT")),
            "latest_notice": _day(r.get("UPDATEDATE")),
        })
    return out


def equity_pledge(code: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """股权质押比例（中国结算按周统计）。code 给 → 该股历次；不给 → 最近统计日全市场。

    「无限售 + 限售 ≠ 合计」时抛错：这类数据拿去算质押风险会直接错一个量级。
    """
    report = "RPT_CSDC_LIST"
    if code:
        rows = datacenter(report, filter_str=f'(SECURITY_CODE="{code}")',
                          sort_columns="TRADE_DATE", sort_types="-1", limit=limit)
    else:
        latest = datacenter(report, sort_columns="TRADE_DATE", sort_types="-1", limit=1)
        if not latest:
            raise DataError("东财质押统计返回 0 行，接口口径可能已变")
        day = _day(latest[0].get("TRADE_DATE"))
        rows = datacenter(report, filter_str=f"(TRADE_DATE='{day}')",
                          sort_columns="PLEDGE_RATIO,SECURITY_CODE",
                          sort_types="-1,1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        total = _num(r.get("REPURCHASE_BALANCE"))
        free = _num(r.get("REPURCHASE_UNLIMITED_BALANCE"))
        locked = _num(r.get("REPURCHASE_LIMITED_BALANCE"))
        if None not in (total, free, locked) and abs(free + locked - total) > max(1.0, total * 0.001):
            raise DataError(f"东财质押数据「无限售 + 限售 ≠ 合计」："
                            f"{r.get('SECURITY_CODE')} {r.get('TRADE_DATE')}")
        out.append({
            "date": _day(r.get("TRADE_DATE")),
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "industry": str(r.get("INDUSTRY") or ""),
            "pledge_ratio_pct": _num(r.get("PLEDGE_RATIO")),
            "pledged_shares_10k": total,
            "pledged_mktcap_10k": _num(r.get("PLEDGE_MARKET_CAP")),
            "pledge_count": _num(r.get("PLEDGE_DEAL_NUM")),
        })
    return out


def ipo_calendar(limit: int = 50) -> List[Dict[str, Any]]:
    """新股申购日历（沪深京），按申购日倒序，含尚未申购的排期。"""
    rows = datacenter("RPTA_APP_IPOAPPLY", sort_columns="APPLY_DATE,SECURITY_CODE",
                      sort_types="-1,-1", limit=limit)
    out: List[Dict[str, Any]] = []
    for r in rows:
        price = _num(r.get("ISSUE_PRICE"))
        out.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME") or r.get("SECURITY_NAME_ABBR") or ""),
            "apply_code": str(r.get("APPLY_CODE") or ""),
            "exchange": str(r.get("TRADE_MARKET") or ""),
            "board": str(r.get("MARKET") or r.get("MARKET_TYPE_NEW") or ""),
            "apply_date": _day(r.get("APPLY_DATE")),
            "listing_date": _day(r.get("LISTING_DATE")),
            # 定价前东财填 null 或 0，0 不是真实发行价
            "issue_price": price if price else None,
            "issue_pe": _num(r.get("AFTER_ISSUE_PE")),
            "industry_pe": _num(r.get("INDUSTRY_PE")),
            "issue_shares_10k": _num(r.get("ISSUE_NUM")),
            "win_rate_pct": _num(r.get("ONLINE_ISSUE_LWR")),
            "first_close_chg_pct": _num(r.get("LD_CLOSE_CHANGE")),
        })
    return out


#: 可转债的行情字段要服务端按报价填，必须用 quoteColumns（不在 columns 里）
_CB_QUOTES = ("f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,"
              "f235~10~SECURITY_CODE~TRANSFER_PRICE,"
              "f236~10~SECURITY_CODE~TRANSFER_VALUE,"
              "f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,"
              "f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO")


def convertible_bonds(include_delisted: bool = False,
                      limit: int = 500) -> List[Dict[str, Any]]:
    """可转债全表：条款 + 最新转股价/债价/正股价/转股价值/溢价率。

    转股价值 = 100 / 转股价 × 正股价；premium_pct = 债价 / 转股价值 − 1。
    status 不猜：既没有上市日也没有申购日的一律 unknown。
    """
    rows = datacenter("RPT_BOND_CB_LIST",
                      sort_columns="PUBLIC_START_DATE,SECURITY_CODE",
                      sort_types="-1,1", page_size=500, limit=max(int(limit), 500),
                      extra={"quoteColumns": _CB_QUOTES, "quoteType": "0"})
    if not rows:
        raise DataError("东财可转债列表为空，接口可能改了")
    today = _today_cn().isoformat()          # 按北京时间，不看本机时区
    out: List[Dict[str, Any]] = []
    for r in rows:
        listing = _day(r.get("LISTING_DATE"))
        delist = _day(r.get("DELIST_DATE"))
        market = str(r.get("TRADE_MARKET") or "")
        if (delist and delist <= today) or market == "STAS00":   # STAS00 = 退市板块
            status = "delisted"
        elif market not in ("CNSESH", "CNSESZ"):
            status = "unknown"
        elif listing and listing <= today:
            status = "listed"
        elif listing or r.get("PUBLIC_START_DATE"):
            status = "upcoming"
        else:
            status = "unknown"
        if status == "delisted" and not include_delisted:
            continue
        out.append({
            "code": str(r.get("SECURITY_CODE") or ""),
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "status": status,
            "stock_code": str(r.get("CONVERT_STOCK_CODE") or ""),
            "stock_name": str(r.get("SECURITY_SHORT_NAME") or ""),
            "rating": str(r.get("RATING") or ""),
            "issue_size_100m": _num(r.get("ACTUAL_ISSUE_SCALE")),
            "listing_date": listing,
            "delist_date": delist,
            "expire_date": _day(r.get("EXPIRE_DATE")),
            "convert_start": _day(r.get("TRANSFER_START_DATE")),
            "initial_convert_price": _num(r.get("INITIAL_TRANSFER_PRICE")),
            "convert_price": _num(r.get("TRANSFER_PRICE")),
            "bond_price": _num(r.get("CURRENT_BOND_PRICE")),
            "stock_price": _num(r.get("CONVERT_STOCK_PRICE")),
            "convert_value": _num(r.get("TRANSFER_VALUE")),
            "premium_pct": _num(r.get("TRANSFER_PREMIUM_RATIO")),
        })
    return out


# ---------------------------------------------------------------- 研究资料


def cninfo_announcements(code: str, limit: int = 30,
                         category: str = "") -> List[Dict[str, Any]]:
    """巨潮公告（法定披露，可靠度最高）。

    巨潮**只认 POST**：GET 直接 500。且必须先经 topSearch 拿 `orgId`，
    少了它返回空 `announcements`——这不是"该股没有公告"。
    """
    hits = _post(CNINFO_SEARCH, {"keyWord": code, "maxNum": "10"})
    try:
        found = json.loads(hits) or []
    except ValueError as exc:
        raise DataError("巨潮代码检索返回的不是 JSON") from exc
    org_id = ""
    for item in found:
        if isinstance(item, dict) and str(item.get("code")) == code:
            org_id = str(item.get("orgId") or "")
            break
    if not org_id:
        raise DataError(f"巨潮没查到 {code} 的 orgId（代码错或该市场不覆盖）")
    form = {
        "pageNum": "1", "pageSize": str(max(1, min(int(limit), 50))),
        "column": "szse" if code[:1] in ("0", "3") else "sse",
        "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
        "searchkey": "", "secid": "", "category": category,
        "trade": "", "seDate": "", "sortName": "", "sortType": "",
        "isHLtitle": "true",
    }
    text = _post(CNINFO_QUERY, form)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise DataError("巨潮公告查询返回的不是 JSON（可能被反爬拦截）") from exc
    rows = (payload or {}).get("announcements") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        stamp = r.get("announcementTime")
        when = ""
        if isinstance(stamp, (int, float)):
            when = dt.datetime.fromtimestamp(stamp / 1000).strftime("%Y-%m-%d")
        title = re.sub(r"<[^>]+>", "", str(r.get("announcementTitle") or "")).strip()
        if not title:
            continue
        out.append({
            "date": when,
            "title": title,
            "type": str(r.get("announcementTypeName") or ""),
            "url": (f"http://static.cninfo.com.cn/{r.get('adjunctUrl')}"
                    if r.get("adjunctUrl") else ""),
        })
    return out


def eastmoney_reports(code: str, months: int = 12, limit: int = 20) -> List[Dict[str, Any]]:
    """个股研报列表（卖方观点，含三年度 EPS 预测与评级）。"""
    end = _today_cn()
    begin = end - dt.timedelta(days=max(30, int(months) * 31))
    params = {
        "industryCode": "*", "pageSize": str(max(1, min(int(limit), 50))),
        "industry": "*", "rating": "", "ratingChange": "",
        "beginTime": begin.isoformat(), "endTime": end.isoformat(),
        "pageNo": "1", "fields": "", "qType": "0", "orgCode": "",
        "code": code, "rcode": "", "p": "1", "pageNum": "1",
    }
    payload = _http_get_json(f"{EM_REPORT}?{urllib.parse.urlencode(params)}",
                             headers=_H_REPORT)
    rows = (payload or {}).get("data") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": str(r.get("title") or ""),
            "date": _day(r.get("publishDate")),
            "org": str(r.get("orgSName") or ""),
            "rating": str(r.get("emRatingName") or ""),
            "eps_this_year": _num(r.get("predictThisYearEps")),
            "eps_next_year": _num(r.get("predictNextYearEps")),
            "pe_this_year": _num(r.get("predictThisYearPe")),
            "industry": str(r.get("indvInduName") or ""),
        })
    return out


def sina_financial_report(code: str, venue: str = "sh",
                          report_type: str = "lrb", num: int = 8) -> List[Dict[str, Any]]:
    """新浪财报三表。report_type: fzb 资产负债 / lrb 利润 / llb 现金流。

    返回按报告期倒序，每期一条：`items` 是 {科目: 值}，`yoy` 是 {科目: 同比}。
    """
    if report_type not in ("fzb", "lrb", "llb"):
        raise ValueError("report_type 只能是 fzb / lrb / llb")
    params = {"paperCode": f"{venue}{code}", "source": report_type,
              "type": "0", "page": "1", "num": str(max(1, min(int(num), 20)))}
    payload = _http_get_json(f"{SINA_FIN}?{urllib.parse.urlencode(params)}",
                             headers={"User-Agent": data._HEADERS["User-Agent"]})
    report_list = ((payload or {}).get("result") or {}).get("data") or {}
    report_list = report_list.get("report_list") or {}
    if not isinstance(report_list, dict):
        raise DataError("新浪财报返回的 report_list 不是对象，格式可能已变")
    out: List[Dict[str, Any]] = []
    for period in sorted(report_list, reverse=True)[:max(1, min(int(num), 20))]:
        node = report_list[period] or {}
        items: Dict[str, Any] = {}
        yoy: Dict[str, Any] = {}
        for it in (node.get("data") or []):
            if not isinstance(it, dict):
                continue
            title = str(it.get("item_title") or "").strip()
            if not title or it.get("item_value") in (None, ""):
                continue
            items[title] = it.get("item_value")
            if it.get("item_tongbi") not in (None, ""):
                yoy[title] = it.get("item_tongbi")
        out.append({
            "period": f"{period[:4]}-{period[4:6]}-{period[6:8]}" if len(period) == 8 else period,
            "items": items,
            "yoy": yoy,
        })
    return out


# ---------------------------------------------------------------- 宏观 / 利率


def lpr_history(limit: int = 36) -> List[Dict[str, Any]]:
    """LPR 全历史（%）。旧贷款基准利率行也在这张表里，LPR1Y 为空已剔除。

    ⚠️ 东财对这张表的 `sortTypes` 不生效（实测传 1 仍返回旧→新），因此这里
    **自己按日期排序后再取尾部**。原实现直接 `[-limit:]` 拿到的是 2015 年的
    旧数据，看起来"有数"但完全不是当前利率——此类错比报错更难发现。
    """
    rows = datacenter("RPTA_WEB_RATE", sort_columns="TRADE_DATE", sort_types="1",
                      columns="TRADE_DATE,LPR1Y,LPR5Y", page_size=500, limit=2000)
    out: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("LPR1Y") is None:
            continue
        day = _day(r.get("TRADE_DATE"))
        if not day:
            continue
        out.append({"date": day, "lpr_1y": _num(r.get("LPR1Y")),
                    "lpr_5y": _num(r.get("LPR5Y"))})
    if not out:
        raise DataError("东财 LPR 报表里没有 LPR 数据")
    out.sort(key=lambda r: r["date"])
    return out[-max(1, int(limit)):]


def nbs_pmi() -> Dict[str, Any]:
    """国家统计局最新 PMI：制造业 / 非制造业 / 综合 + 大中小型企业。

    正文是**全角括号且括号内带空格**（`（ PMI ）为 49.2%`），必须把空白
    整个删掉再匹配；只做「压成单个空格」会一条都匹配不到。
    """
    ua = {"User-Agent": "Mozilla/5.0"}
    index_html = _http_get(NBS_INDEX, headers=ua)
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>\s*([^<]{6,80}?)\s*</a>', index_html)
    hit = next(((u, t) for u, t in links if "采购经理指数" in t), None)
    if not hit:
        raise DataError("国家统计局最新发布页未找到「采购经理指数」条目")
    href, title = hit
    url = href if href.startswith("http") else NBS_INDEX + href.lstrip("./")

    html = _http_get(url, headers=ua)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[\s\u3000\xa0]+", "", text)      # 空白整个删掉，见 docstring

    def grab(pattern: str) -> Optional[float]:
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    ym = re.search(r"(\d{4})年(\d{1,2})月", title)
    result: Dict[str, Any] = {
        "title": title.strip(),
        "period": f"{ym.group(1)}-{int(ym.group(2)):02d}" if ym else None,
        "manufacturing_pmi": grab(r"(?<!非)制造业采购经理指数（PMI）为([\d.]+)%"),
        "non_manufacturing_pmi": grab(r"非制造业商务活动指数为([\d.]+)%"),
        "composite_pmi": grab(r"综合PMI产出指数为([\d.]+)%"),
        "pmi_large": grab(r"大型企业PMI为([\d.]+)%"),
        "pmi_medium": grab(r"中型企业PMI为([\d.]+)%"),
        "pmi_small": grab(r"小型企业PMI为([\d.]+)%"),
        "source_url": url,
    }
    # 分档措辞统计局用过两种版式，逐层回退（解析不到留 None，属可选字段）：
    #   ① 全合并：大、中、小型企业PMI分别为 A%、B%和C%
    #   ② 大单列 + 中小合并：大型企业PMI为 A%…；中、小型企业PMI分别为 B%和C%
    # 单条正则 `大型企业PMI为` 不会误匹配 ② 里的「中、小型企业PMI分别为」。
    combined = re.search(r"大、中、小型企业PMI分别为([\d.]+)%、([\d.]+)%和([\d.]+)%", text)
    if combined:
        large, medium, small = (float(x) for x in combined.groups())
        result.update(pmi_large=large, pmi_medium=medium, pmi_small=small)
    else:
        m_ms = re.search(r"中、小型企业PMI分别为([\d.]+)%和([\d.]+)%", text)
        if m_ms:
            result["pmi_medium"], result["pmi_small"] = (float(x) for x in m_ms.groups())
    if any(result[k] is None for k in
           ("manufacturing_pmi", "non_manufacturing_pmi", "composite_pmi")):
        raise DataError(f"PMI 正文措辞可能已变更，三个主指标解析不全；请核对页面：{url}")
    return result


#: 中债曲线 id 与官方名称（名称必须逐字对上，页面按名称核对）
CHINABOND_CURVES = {"all": "ycqx", "treasury": "hzsylqx",
                    "bank_aaa": "syyhsylqx", "mtn_aaa": "zdqpjsylqx"}
CHINABOND_NAMES = {"treasury": "中债国债收益率曲线",
                   "bank_aaa": "中债商业银行普通债收益率曲线(AAA)",
                   "mtn_aaa": "中债中短期票据收益率曲线(AAA)"}
CHINABOND_FIRST_DAY = {"treasury": "2006-03-01", "mtn_aaa": "2006-12-25",
                       "bank_aaa": "2009-12-24"}
_CB_HEADER = ["曲线名称", "日期", "3月", "6月", "1年", "3年", "5年", "7年", "10年", "30年"]
_CB_TENORS = ["3m", "6m", "1y", "3y", "5y", "7y", "10y", "30y"]


def chinabond_yield(days: int = 30, curve: str = "treasury") -> Dict[str, Any]:
    """中债收益率曲线（中央结算公司官方）。收益率单位 %。

    官网单次查询超过 1 年会**静默返回 0 行**，因此按 360 天切片；
    这里只取最近 `days` 天，一次请求即可。表头必须逐字匹配——
    列序变了还按位置解析会把 10 年收益率读成 30 年的。
    """
    if curve not in CHINABOND_CURVES:
        raise ValueError(f"curve 只能是 {list(CHINABOND_CURVES)}")
    end = _today_cn()
    start = end - dt.timedelta(days=max(1, min(int(days), 360)))
    wanted = [k for k in CHINABOND_NAMES if curve in ("all", k)]
    begin = max(dt.date.fromisoformat(min(CHINABOND_FIRST_DAY[k] for k in wanted)), start)
    params = {"startDate": begin.isoformat(), "endDate": end.isoformat(),
              "gjqx": 0, "qxId": CHINABOND_CURVES[curve], "locale": "cn_ZH"}
    html = _http_get(f"{CHINABOND}?{urllib.parse.urlencode(params)}",
                     headers={"User-Agent": data._HEADERS["User-Agent"],
                              "Referer": "https://yield.chinabond.com.cn/"})
    body = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    tables = body.split("<table")
    if len(tables) < 3:
        raise DataError("中债收益率页面里没有数据表，接口口径可能已变")
    table_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tables[-1], re.S)
    cells = [[re.sub(r"<[^>]+>|\s+", "", c)
              for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
             for r in table_rows]
    if not cells or cells[0] != _CB_HEADER:
        raise DataError("中债收益率页面表头改变，不能按原列序解析")
    by_name = {CHINABOND_NAMES[k]: k for k in wanted}
    rows: List[Dict[str, Any]] = []
    for rec in cells[1:]:
        if len(rec) != len(_CB_HEADER):
            raise DataError(f"中债收益率行列数不对：{rec}")
        name = rec[0]
        if name not in by_name:
            raise DataError(f"中债返回了未请求的曲线「{name}」")
        row: Dict[str, Any] = {"date": rec[1], "curve": name,
                              "curve_key": by_name[name]}
        row.update({k: _num(v) for k, v in zip(_CB_TENORS, rec[2:])})
        rows.append(row)
    if not rows:
        raise DataError(f"{begin}~{end} 没有中债收益率（区间内无交易日或接口未发布）")
    rows.sort(key=lambda r: (r["date"], r["curve"]))
    return {"curve": curve, "rows": rows, "start": begin.isoformat(),
            "end": end.isoformat()}


def repo_fixing(kind: str = "FR") -> List[Dict[str, Any]]:
    """银行间回购定盘利率（中国货币网官方）。kind: FR（全市场）/ FDR（银银间）。"""
    names = {"FR": "frr", "FDR": "fdr"}
    key = str(kind or "FR").upper()
    if key not in names:
        raise ValueError("kind 只能是 FR / FDR")
    text = _http_get(CHINAMONEY.format(name=names[key]),
                     headers={"User-Agent": data._HEADERS["User-Agent"],
                              "Referer": "https://www.chinamoney.com.cn/chinese/bkfrr/"},
                     encoding="utf-8-sig")
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        # 每行是「日期,,,,,,隔夜,7天,14天」：中间 5 列恒为空，不为空说明格式改了
        if len(parts) != 9 or any(p.strip() for p in parts[1:6]):
            raise DataError(f"货币网定盘利率 CSV 格式改变：{line[:60]}")
        out.append({"date": parts[0].strip(),
                    f"{key.lower()}001": _num(parts[6]),
                    f"{key.lower()}007": _num(parts[7]),
                    f"{key.lower()}014": _num(parts[8])})
    if not out:
        raise DataError(f"货币网 {key} 定盘利率为空")
    out.sort(key=lambda r: r["date"])
    return out


def trading_calendar(year: int, month: int) -> Dict[str, Any]:
    """深交所完整自然月交易日历。未发布或缺日抛错——不能推断「全月休市」。"""
    if not isinstance(year, int) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("year/month 必须为整数，month 在 1–12 之间")
    import calendar as _cal
    last = _cal.monthrange(year, month)[1]
    payload = _http_get_json(
        f"{SZSE_CAL}?{urllib.parse.urlencode({'month': f'{year}-{month}'})}",
        headers={"User-Agent": data._HEADERS["User-Agent"],
                 "Referer": "https://www.szse.cn/"})
    rows = (payload or {}).get("data")
    if not isinstance(rows, list) or not rows:
        raise DataError("深交所尚未返回该月日历；不能推断全月休市")
    days: List[Dict[str, Any]] = []
    for rec in rows:
        if not isinstance(rec, dict) or str(rec.get("jybz")) not in ("0", "1"):
            raise DataError("深交所日历字段异常")
        day = _day(rec.get("jyrq"))
        if not day:
            raise DataError(f"深交所日历日期无法识别：{rec.get('jyrq')!r}")
        days.append({"date": day, "is_open": str(rec["jybz"]) == "1"})
    expected = {f"{year}-{month:02d}-{d:02d}" for d in range(1, last + 1)}
    got = {d["date"] for d in days}
    if got != expected:
        raise DataError(f"深交所日历月份错位或日期不完整（缺 {sorted(expected - got)[:5]}）")
    days.sort(key=lambda d: d["date"])
    return {"year": year, "month": month, "days": days,
            "open_count": sum(1 for d in days if d["is_open"])}
