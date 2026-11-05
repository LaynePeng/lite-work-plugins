"""A 股行情数据访问层：腾讯行情/K线 + 东方财富检索/选股/资金流。

三条设计约束（都是踩坑换来的）：

1. **零第三方依赖**：只用标准库 `urllib`。打包版 lite-work 是 PyInstaller
   frozen 进程，能少一个依赖就少一个 wheels/跨平台风险；实测 urllib + 浏览器
   指纹头足以打通腾讯/东财全部所需接口。

2. **节流 + 缓存 + 换源**：行情接口对高频请求会直接掐连接（实测东财
   `push2his` 在秒级连发时返回 `RemoteDisconnected`，非 UA 问题）。因此：
   - 全局令牌桶限速（默认 5 次/秒，可配）；
   - 行情快照 3 秒 TTL 缓存、K 线 60 秒 TTL 缓存（同一轮对话里 Agent
     反复调用不会重复打接口）；
   - 单接口失败自动换备源（腾讯 ↔ 新浪），全失败才报错，且报错信息里
     说明"是网络还是接口变更"。

3. **不做真实判断**：本层只负责"取到干净的数据"，涨跌停/费用/盈亏
   一律由 `astock_market` / `astock_ledger` 计算。
"""
from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.error
import zlib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from astock_market import (
    BOARD_LABELS, board_of, guess_venue, limit_prices, normalize_code,
    round_price, to_float, tencent_symbol,
)

logger = logging.getLogger("litework.astock")

#: 浏览器指纹头。裸 UA 或不带 Referer 会被部分接口 403（新浪 hq 接口强制校验 Referer）。
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://gu.qq.com/",
}
_EM_HEADERS = dict(_HEADERS, Referer="https://www.eastmoney.com/")

TENCENT_QUOTE = "https://qt.gtimg.cn/q="
SINA_QUOTE = "https://hq.sinajs.cn/list="
#: 腾讯 K 线**域名链**（同一接口的多个入口）。web.ifzq 会被限流成 501，
#: 换域名常能绕过——2026-11 实测 web.ifzq 挂了一整天而 ifzq / proxy.finance
#: 正常，写成单域名就等于 K 线全废（回测、均线、选股一起停摆）。
TENCENT_KLINE_HOSTS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
)
TENCENT_MKLINE_HOSTS = (
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
)
#: 兼容旧引用（代码里有按模块常量取 URL 的地方）
TENCENT_KLINE = TENCENT_KLINE_HOSTS[0]
TENCENT_MKLINE = TENCENT_MKLINE_HOSTS[0]

#: 雪球 K 线（**不同厂商**的备源）。腾讯全家桶同源限流时它仍可用，
#: 且是唯一同时支持 前复权/后复权/不复权 + 日周月分钟 的免费源。
XQ_KLINE = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
EM_SUGGEST = "https://searchapi.eastmoney.com/api/suggest/get"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_FFLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

#: 东财 suggest 返回的 SecurityTypeName 里属于"A 股个股"的取值
_A_STOCK_TYPES = {"沪A", "深A", "京A", "科创板", "创业板", "北证A股", "A股"}

SINA_RANK = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData")
SINA_COUNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeStockCount")

#: 新浪排行接口的 node 参数（与东财 universe 对齐）
SINA_NODE: Dict[str, str] = {
    "all": "hs_a",
    "main": "hs_a",
    "gem": "cyb",
    "star": "kcb",
    "bj": "bj_a",
    "etf": "etf_hq_fund",
}

#: 新浪排行 sort 字段（东财字段名 → 新浪字段名）
_SINA_SORT: Dict[str, str] = {
    "change_pct": "changepercent", "price": "trade", "amount": "amount",
    "volume": "volume", "turnover": "turnoverratio", "mcap": "mktcap",
    "pe": "per", "main_net": "amount",
}

#: 东财选股的市场范围（fs 参数）。
UNIVERSE_FS: Dict[str, str] = {
    "all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "main": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "gem": "m:0+t:80",
    "star": "m:1+t:23",
    "bj": "m:0+t:81+s:2048",
    "etf": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
}


# ---------------------------------------------------------------- 限速与缓存


class RateLimiter:
    """令牌桶：A 股接口是**公共免费接口**，限速是礼貌也是稳定性前提。"""

    def __init__(self, rate_per_sec: float = 5.0, burst: int = 5) -> None:
        self.rate = max(0.5, float(rate_per_sec))
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


class TTLCache:
    """极简 TTL 缓存（线程安全）。"""

    def __init__(self, max_items: int = 512) -> None:
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._max = max_items
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, value = item
            if time.monotonic() - ts > ttl:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # 简单淘汰：清掉最早写入的一批
                for k in list(self._data)[: self._max // 4]:
                    self._data.pop(k, None)
            self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CACHE = TTLCache()
_LIMITER = RateLimiter()


def configure(rate_per_sec: float = 5.0, timeout: float = 10.0,
              cache_ttl: float = 3.0, kline_ttl: float = 60.0) -> None:
    """由插件在 install 时按设置页配置注入。"""
    global _LIMITER, _TIMEOUT, _QUOTE_TTL, _KLINE_TTL
    _LIMITER = RateLimiter(rate_per_sec)
    _TIMEOUT = max(3.0, float(timeout))
    _QUOTE_TTL = max(0.0, float(cache_ttl))
    _KLINE_TTL = max(0.0, float(kline_ttl))


_TIMEOUT = 10.0
_QUOTE_TTL = 3.0
_KLINE_TTL = 60.0


class DataError(RuntimeError):
    """取数失败（网络 / 接口变更）。消息面向 Agent，说明原因与可行动作。"""


def _decompress(raw: bytes, content_encoding: Optional[str]) -> bytes:
    """按 Content-Encoding 解压响应体。

    **为什么必须有这一步**（2026-11-04 实测踩到）：
    东财 F10 系接口（emweb.securities.eastmoney.com）即使请求头里没有
    `Accept-Encoding`，也会返回 **gzip 压缩体**。不解压的话拿到的是
    `b'\\x1f\\x8b...'` 二进制，`json.loads` 直接抛
    「接口返回的不是 JSON（可能被反爬拦截）」——**错误信息完全指向了错误的
    方向**（以为是反爬，实际只是没解压），排查花了不少时间。

    除 gzip 外一并处理 deflate（标准库 zlib 即可，无需第三方包）。
    未知编码原样返回，让上层按原逻辑报错，不吞掉异常。
    """
    if not raw or not content_encoding:
        # 没有头信息时用 magic 兜底判断：gzip 固定以 1f 8b 开头
        if len(raw) > 2 and raw[0] == 0x1F and raw[1] == 0x8B:
            try:
                return gzip.decompress(raw)
            except OSError:
                return raw
        return raw

    enc = content_encoding.lower().strip()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
        # 不处理 br：brotli 是第三方包，本插件坚持零依赖（AGENTS.md 硬约束），
        # 且东财/腾讯/新浪实测都只回 gzip 或 identity，不会有 br。
    except Exception as exc:       # 解压失败不该让整个请求挂掉
        logger.warning("[astock] 解压失败(%s)：%s，按原文返回", content_encoding, exc)
    return raw


def _http_get(url: str, headers: Optional[Dict[str, str]] = None,
              encoding: str = "utf-8", retries: int = 2,
              backoff: float = 0.6) -> str:
    """带重试的 GET。

    东财 push2 系接口对连续请求会直接掐连接（`RemoteDisconnected`，实测与
    UA/Referer 无关，等十几秒即恢复）。因此只对「连接被断开/超时」这类
    **瞬时错误**做有限重试（指数退避），4xx 立刻失败（接口变更重试无意义）。
    仍失败时把"是限流还是网络"说清楚，并把可行动作写进错误信息。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        if not _LIMITER.acquire():
            raise DataError("行情请求排队超时（本地限速器拥塞），请稍后重试")
        req = urllib.request.Request(url, headers=headers or _HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
                raw = _decompress(raw, resp.headers.get("Content-Encoding"))
            try:
                return raw.decode(encoding, "replace")
            except (LookupError, UnicodeDecodeError):
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise DataError(f"HTTP {exc.code}：{url.split('?')[0]} 返回错误，"
                            f"可能是接口变更或被限流") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
    reason = getattr(last_exc, "reason", last_exc)
    raise DataError(
        f"接口连接被断开（{type(last_exc).__name__}: {reason}）：{url.split('?')[0]}。"
        f"公共行情接口对高频请求会掐连接，通常等 10-30 秒、"
        f"或调低设置页「请求速率上限」后即可恢复")


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
    text = _http_get(url, headers=headers)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DataError(f"接口返回的不是 JSON（可能被反爬拦截）：{url.split('?')[0]}") from exc


# ---------------------------------------------------------------- 行情快照

#: 腾讯行情 88 字段的下标（实测 sh600000，字段顺序稳定）
_T = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume_lot": 6, "time": 30, "change": 31, "change_pct": 32,
    "high": 33, "low": 34, "amount_wan": 37, "turnover": 38, "pe": 39,
    "amplitude": 43, "float_mcap": 44, "total_mcap": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "volume_ratio": 49,
}


@dataclass
class Quote:
    """一条行情快照（字段已归一化，空缺为 None 而非 0——避免把"没有"当"零"）。"""

    code: str
    venue: str
    name: str = ""
    price: Optional[float] = None
    prev_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    volume_lot: Optional[float] = None          # 成交量（手）
    amount: Optional[float] = None              # 成交额（元）
    turnover: Optional[float] = None            # 换手率 %
    volume_ratio: Optional[float] = None        # 量比
    amplitude: Optional[float] = None           # 振幅 %
    pe: Optional[float] = None
    pb: Optional[float] = None
    total_mcap: Optional[float] = None          # 总市值（元）
    float_mcap: Optional[float] = None          # 流通市值（元）
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    trade_time: str = ""
    board: str = ""
    source: str = "tencent"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return f"{self.venue}{self.code}"

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "code": self.code, "symbol": self.symbol, "name": self.name,
            "board": BOARD_LABELS.get(self.board, self.board),
            "price": self.price, "prev_close": self.prev_close,
            "change": self.change, "change_pct": self.change_pct,
            "open": self.open, "high": self.high, "low": self.low,
            "volume_lot": self.volume_lot, "amount_yuan": self.amount,
            "turnover_pct": self.turnover, "volume_ratio": self.volume_ratio,
            "amplitude_pct": self.amplitude,
            "pe": self.pe, "pb": self.pb,
            "total_mcap_yuan": self.total_mcap,
            "float_mcap_yuan": self.float_mcap,
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "trade_time": self.trade_time, "source": self.source,
        }
        out.update(self.extra)
        return {k: v for k, v in out.items() if v is not None or k in
                ("name", "code", "symbol", "board", "trade_time", "source")}


def _parse_tencent_line(payload: str, venue: str) -> Optional[Quote]:
    """解析 `v_sh600000="1~浦发银行~600000~9.53~...";` 一行。"""
    if '="' not in payload:
        return None
    body = payload.split('="', 1)[1].rstrip('";\n\r ')
    f = body.split("~")
    if len(f) < 50:
        return None
    code = f[_T["code"]].strip()
    if not code:
        return None

    def num(key: str) -> Optional[float]:
        idx = _T.get(key)
        if idx is None or idx >= len(f):
            return None
        return to_float(f[idx], None)

    board = board_of(code)
    q = Quote(
        code=code, venue=venue, board=board,
        name=f[_T["name"]].strip(),
        price=num("price"), prev_close=num("prev_close"), open=num("open"),
        high=num("high"), low=num("low"),
        change=num("change"), change_pct=num("change_pct"),
        volume_lot=num("volume_lot"),
        amount=(num("amount_wan") or 0) * 10000 or None,
        turnover=num("turnover"), pe=num("pe"), pb=num("pb"),
        amplitude=num("amplitude"),
        float_mcap=(num("float_mcap") or 0) * 1e8 or None,
        total_mcap=(num("total_mcap") or 0) * 1e8 or None,
        # 腾讯对**指数**在涨跌停字段返回 -1（表示"不适用"），
        # 直接透传会让 astock_quote 显示"跌停/涨停 -1.00 / -1.00"这种
        # 不存在的数值（v2.8.4 修）。判据：非正数一律视为"无涨跌停"。
        limit_up=_positive_or_none(num("limit_up")),
        limit_down=_positive_or_none(num("limit_down")),
        volume_ratio=num("volume_ratio"),
        trade_time=_fmt_tencent_time(f[_T["time"]] if len(f) > _T["time"] else ""),
        source="tencent",
    )
    # 停牌/未开盘时 price 可能为 0——用昨收兜底并把状态说清楚，而不是返回 0 元
    if not q.price and q.prev_close:
        q.price = q.prev_close
        q.extra["price_is_prev_close"] = True
        q.extra["note"] = "当前无成交价（停牌/未开盘），已用昨收填充"
    if q.price and q.board and q.board != "index":
        up, down, pct = limit_prices(q.prev_close or 0, code, q.name)
        q.limit_up = q.limit_up or up
        q.limit_down = q.limit_down or down
        q.extra.setdefault("limit_pct", pct)
    return q


def _positive_or_none(value: Optional[float]) -> Optional[float]:
    """涨跌停价：非正数视为"无"（指数/异常源），避免把 -1 当真价格显示。"""
    try:
        v = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if v is None or v <= 0:
        return None
    return v


def _fmt_tencent_time(raw: str) -> str:
    """20261023161458 → 2026-10-23 16:14:58。"""
    t = (raw or "").strip()
    if len(t) == 14 and t.isdigit():
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
    if len(t) == 12 and t.isdigit():     # 分钟线：202610231455
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}"
    return t


def _parse_sina_line(payload: str, code: str, venue: str) -> Optional[Quote]:
    """新浪备源：var hq_str_sh600000="名称,今开,昨收,现价,最高,最低,...,日期,时间";"""
    if '="' not in payload:
        return None
    body = payload.split('="', 1)[1].rstrip('";\n\r ')
    f = body.split(",")
    if len(f) < 32:
        return None
    price = to_float(f[3], None)
    prev = to_float(f[2], None)
    q = Quote(
        code=code, venue=venue, board=board_of(code), name=f[0].strip(),
        price=price, prev_close=prev, open=to_float(f[1], None),
        high=to_float(f[4], None), low=to_float(f[5], None),
        volume_lot=(to_float(f[8], 0) or 0) / 100 or None,
        amount=to_float(f[9], None),
        trade_time=f"{f[30]} {f[31]}" if len(f) > 31 else "",
        source="sina",
    )
    if price and prev:
        q.change = round_price(price - prev)
        q.change_pct = round((price - prev) / prev * 100, 2)
    up, down, pct = limit_prices(prev or 0, code, q.name)
    q.limit_up, q.limit_down = up, down
    q.extra["limit_pct"] = pct
    return q


def fetch_quotes(items: Sequence[Tuple[str, str, str]],
                 use_cache: bool = True) -> List[Quote]:
    """批量取行情。items = [(code, venue, kind)]，一次请求最多 50 个标的。

    单批失败自动换新浪源；两个源都失败才抛 DataError。
    """
    if not items:
        return []
    key = "q:" + ";".join(f"{v}{c}" for c, v, _ in items)
    if use_cache:
        cached = _CACHE.get(key, _QUOTE_TTL)
        if cached is not None:
            return cached

    symbols = [tencent_symbol(c, v) for c, v, _ in items]
    out: List[Quote] = []
    try:
        text = _http_get(TENCENT_QUOTE + ",".join(symbols), encoding="gbk")
        by_sym: Dict[str, Quote] = {}
        for line in text.split(";"):
            line = line.strip()
            if not line:
                continue
            sym = line.split("=", 1)[0].replace("v_", "").strip()
            venue = sym[:2] if sym[:2] in ("sh", "sz", "bj") else ""
            q = _parse_tencent_line(line, venue)
            if q is not None:
                by_sym[q.symbol] = q
        out = [by_sym[s] for s in symbols if s in by_sym]
        if out:
            if use_cache:
                _CACHE.set(key, out)
            return out
    except DataError as exc:
        logger.warning("[astock] 腾讯行情失败，尝试新浪备源: %s", exc)

    # 备源：新浪（必须带 Referer，否则 403）
    try:
        text = _http_get(SINA_QUOTE + ",".join(symbols),
                         headers=dict(_HEADERS, Referer="https://finance.sina.com.cn"),
                         encoding="gbk")
    except DataError as exc:
        raise DataError(f"行情获取失败（腾讯与新浪均不可用）：{exc}") from exc
    by_sym = {}
    for line in text.split(";"):
        line = line.strip()
        if not line or '="' not in line:
            continue
        sym = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        for code, venue, _ in items:
            if sym == tencent_symbol(code, venue):
                q = _parse_sina_line(line, code, venue)
                if q is not None:
                    by_sym[q.symbol] = q
    out = [by_sym[tencent_symbol(c, v)] for c, v, _ in items
           if tencent_symbol(c, v) in by_sym]
    if not out:
        raise DataError("行情接口没有返回任何有效数据（代码可能不存在或已退市）")
    if use_cache:
        _CACHE.set(key, out)
    return out


def quote_one(raw_code: str, kind: str = "stock",
              use_cache: bool = True) -> Quote:
    """单标的行情（自动规范化代码）。"""
    code, venue = normalize_code(raw_code, kind)
    quotes = fetch_quotes([(code, venue, kind)], use_cache=use_cache)
    if not quotes:
        raise DataError(f"未取到 {code} 的行情")
    return quotes[0]


# ---------------------------------------------------------------- K 线

_PERIODS = {
    "day": ("day", "qfqday", "day"),
    "week": ("week", "qfqweek", "week"),
    "month": ("month", "qfqmonth", "month"),
}
_MINUTE_PERIODS = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}
_ADJUST = {"qfq": "qfq", "hfq": "hfq", "none": ""}


@dataclass
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume_lot: float
    amount: Optional[float] = None      # 部分周期不返回成交额

    def as_dict(self) -> Dict[str, Any]:
        out = {"date": self.date, "open": self.open, "close": self.close,
               "high": self.high, "low": self.low, "volume_lot": self.volume_lot}
        if self.amount is not None:
            out["amount_yuan"] = self.amount
        return out


#: 东财 K 线（备用源）。腾讯 web.ifzq 被限流时会 501（实测高发，尤其批量
#: 回测时连发几十次），没有备用源就等于 K 线与回测全部不可用。
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
#: 东财 klt 周期映射（分钟线 1/5/15/30/60，日/周/月 101/102/103）
_EM_KLT = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60",
           "day": "101", "week": "102", "month": "103"}
#: 东财 fqt：0 不复权 / 1 前复权 / 2 后复权
_EM_FQT = {"none": "0", "qfq": "1", "hfq": "2"}


def _em_secid(code: str, venue: str) -> str:
    """东财 secid：沪市 1.xxxxxx / 深市与北交所 0.xxxxxx。"""
    return f"{'1' if venue == 'sh' else '0'}.{code}"


def _fetch_klines_em(code: str, venue: str, period: str, limit: int,
                     adjust: str) -> List[Bar]:
    """东财 K 线备用源。

    与腾讯的**字段口径差异**（写在这里防止以后改错）：
      · 东财 klines 每条是逗号分隔字符串：日期,开,收,高,低,成交量(手),成交额(元)
        —— 注意是「开,收」不是「开,收,高」的顺序习惯，第 3 位是收盘价；
      · 成交额直接给出，比腾讯（只有量）更完整，因此顺序也是 开收高低。
    """
    klt = _EM_KLT.get(period)
    if not klt:
        return []
    fqt = _EM_FQT.get((adjust or "qfq").lower(), "1")
    params = urllib.parse.urlencode({
        "secid": _em_secid(code, venue),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": klt, "fqt": fqt, "end": "20500101", "lmt": str(limit),
    })
    data = _http_get_json(f"{EM_KLINE}?{params}", headers=_EM_HEADERS)
    rows = ((data or {}).get("data") or {}).get("klines") or []
    out: List[Bar] = []
    for raw in rows:
        parts = str(raw).split(",")
        if len(parts) < 6:
            continue
        # 顺序：date, open, close, high, low, volume(手), amount(元)
        out.append(Bar(
            date=str(parts[0]),
            open=to_float(parts[1], 0.0) or 0.0,
            close=to_float(parts[2], 0.0) or 0.0,
            high=to_float(parts[3], 0.0) or 0.0,
            low=to_float(parts[4], 0.0) or 0.0,
            volume_lot=to_float(parts[5], 0.0) or 0.0,
            amount=to_float(parts[6], None) if len(parts) > 6 else None,
        ))
    return out


def _kline_source_fns() -> Tuple[Tuple[str, Any], ...]:
    """K 线源链（顺序即优先级）。三家**不同厂商**，任一家被限流不会连带全挂：
    腾讯（域名链内再轮换）→ 雪球 → 东财（push2his 常年 Empty reply，仅兜底）。"""
    return (
        ("腾讯", _fetch_klines_tencent),
        ("雪球", _fetch_klines_xueqiu),
        ("东财", _fetch_klines_em),
    )


#: 源失败记忆：某个源刚刚整体失败过就在一段时间内排到末尾，避免每次都先
#: 在挂掉的源上白等一轮超时（回测要连发几十次，这个开销会被放大）。
_SOURCE_COOLDOWN = 120.0
_SOURCE_DOWN: Dict[str, float] = {}


def _ordered_sources() -> Tuple[Tuple[str, Any], ...]:
    now = time.monotonic()
    fns = _kline_source_fns()

    def key(item: Tuple[str, Any]) -> Tuple[int, float]:
        name = item[0]
        down_at = _SOURCE_DOWN.get(name)
        if down_at is None or now - down_at > _SOURCE_COOLDOWN:
            return (0, 0.0)
        return (1, down_at)

    return tuple(sorted(fns, key=key))


def fetch_klines(raw_code: str, period: str = "day", limit: int = 120,
                 adjust: str = "qfq", kind: str = "stock") -> List[Bar]:
    """取 K 线（前复权默认）。period: day/week/month/1m/5m/15m/30m/60m。

    **多源链**：腾讯（3 个域名轮换）→ 雪球 → 东财。设计依据是 2026-11 的
    真实故障：`web.ifzq` 501 + `push2his.eastmoney` Empty reply 同时发生，
    而当时只有这两家做源，结果 K 线、均线、选股、回测**全天不可用**。
    同厂商的多个域名不算备源（会被一起限流），所以第二家必须换厂商。

    复权口径：`qfq/hfq/none` 三档都会透传给各源。注意腾讯前复权只有约
    641 根上限，要更长历史请用 `none`（存档层即按不复权落库）。
    """
    code, venue = normalize_code(raw_code, kind)
    limit = max(1, min(int(limit or 120), 640))
    period = (period or "day").lower()
    adj_key = (adjust or "qfq").lower()
    cache_key = f"k:{venue}{code}:{period}:{limit}:{adj_key}"
    cached = _CACHE.get(cache_key, _KLINE_TTL)
    if cached is not None:
        return cached

    errors: List[str] = []
    bars: List[Bar] = []
    for name, fn in _ordered_sources():
        try:
            bars = fn(code, venue, period, limit, adj_key) or []
        except Exception as exc:        # 单源失败不影响后续源
            errors.append(f"{name}: {exc}")
            _SOURCE_DOWN[name] = time.monotonic()
            logger.warning("[astock] K 线源「%s」失败 %s %s: %s",
                           name, code, period, exc)
            continue
        if bars:
            _SOURCE_DOWN.pop(name, None)
            break
        errors.append(f"{name}: 返回空")

    if not bars:
        detail = "；".join(errors) if errors else "无可用源"
        raise DataError(
            f"{code} {period} K 线获取失败（已尝试腾讯/雪球/东财：{detail}）。"
            f"若三家同时失败通常是网络或全部限流，稍后重试；"
            f"若长期如此请检查设置页「请求速率上限」是否过高")

    bars.sort(key=lambda b: b.date)
    # 去重（同一交易日被两个源拼进来时保留最后一条）
    deduped: List[Bar] = []
    seen: set = set()
    for b in reversed(bars):
        if b.date in seen:
            continue
        seen.add(b.date)
        deduped.append(b)
    deduped.reverse()
    # 各源返回根数不一致（腾讯某域名会多给 1 根），统一截断到 limit，
    # 否则「换源前后均线值变化」会被误读成行情漂移。
    if len(deduped) > limit:
        deduped = deduped[-limit:]
    _CACHE.set(cache_key, deduped)
    return deduped


#: 腾讯 K 线专用头（Referer 必须是腾讯自己的域名之一）
_KLINE_HEADERS = dict(_HEADERS, Referer="https://gu.qq.com/")


def _parse_tencent_rows(node: Dict[str, Any], keys: Sequence[str]) -> List[Bar]:
    """从腾讯返回的 node 里按 keys 顺序找第一个非空数组并解析。

    腾讯同一份数据在不同域名/不同复权下会挂在 `qfqday` / `day` / `qfqweek`
    等不同键上，键名不稳定，所以按候选键依次探测（这是历史踩坑点）。
    """
    for key in keys:
        rows = node.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        out: List[Bar] = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            out.append(Bar(date=str(r[0]),
                           open=to_float(r[1], 0.0) or 0.0,
                           close=to_float(r[2], 0.0) or 0.0,
                           high=to_float(r[3], 0.0) or 0.0,
                           low=to_float(r[4], 0.0) or 0.0,
                           volume_lot=to_float(r[5], 0.0) or 0.0))
        if out:
            return out
    return []


def _fetch_klines_tencent(code: str, venue: str, period: str, limit: int,
                          adjust: str) -> List[Bar]:
    """腾讯 K 线（主源，**逐个域名重试**）。

    单个域名 501 不代表厂商挂了——web.ifzq 被限流时 ifzq / proxy 往往仍通。
    全部域名失败才抛错，把最后一次异常带出去供上层汇总。
    """
    sym = tencent_symbol(code, venue)
    minute = period in _MINUTE_PERIODS
    if minute:
        mcode = _MINUTE_PERIODS[period]
        urls = [f"{h}?param={sym},{mcode},,{limit}"
                for h in TENCENT_MKLINE_HOSTS]
    else:
        api_period, adj_key, plain_key = _PERIODS.get(period, _PERIODS["day"])
        adj = _ADJUST.get((adjust or "qfq").lower(), "qfq")
        param = (f"{sym},{api_period},,,{limit},{adj}" if adj
                 else f"{sym},{api_period},,,{limit}")
        urls = [f"{h}?param={param}" for h in TENCENT_KLINE_HOSTS]

    last_exc: Optional[Exception] = None
    for url in urls:
        try:
            data = _http_get_json(url, headers=_KLINE_HEADERS)
        except Exception as exc:        # 501/超时/断连 → 换下一个域名
            last_exc = exc
            logger.warning("[astock] 腾讯 K 线域名失败，换下一个 %s @%s: %s",
                           code, urllib.parse.urlsplit(url).netloc, exc)
            continue
        node = ((data or {}).get("data") or {}).get(sym) or {}
        if not isinstance(node, dict):
            continue
        keys = ([_MINUTE_PERIODS[period]] if minute
                else [adj_key, plain_key, api_period, "day"])
        bars = _parse_tencent_rows(node, keys)
        if bars:
            return bars
    if last_exc is not None:
        raise last_exc
    return []


# ---------------------------------------------------------------- 雪球 K 线
#: 雪球需要先访问首页拿 xq_a_token cookie，否则 400。cookie 进程内复用。
_XQ_COOKIE: Dict[str, Any] = {"jar": None, "ts": 0.0}
_XQ_LOCK = threading.Lock()
_XQ_COOKIE_TTL = 1800.0

#: 雪球 period 取值（分钟线写作 1m/5m/…，日周月为 day/week/month）
_XQ_PERIOD = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
              "60m": "60m", "day": "day", "week": "week", "month": "month"}
#: 雪球 type：before 前复权 / after 后复权 / bfq 不复权
_XQ_TYPE = {"qfq": "before", "hfq": "after", "none": "bfq"}



def _xq_opener() -> Any:
    """取（或刷新）带 xq_a_token 的 opener。cookie 过期会自动重建。"""
    import http.cookiejar
    now = time.monotonic()
    with _XQ_LOCK:
        if (_XQ_COOKIE["jar"] is not None
                and now - _XQ_COOKIE["ts"] < _XQ_COOKIE_TTL):
            return _XQ_COOKIE["jar"]
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        try:
            req = urllib.request.Request(
                "https://xueqiu.com/",
                headers=dict(_HEADERS, Referer="https://xueqiu.com/"))
            with opener.open(req, timeout=_TIMEOUT) as resp:
                resp.read()
        except Exception as exc:
            raise DataError(f"雪球 cookie 获取失败：{exc}") from exc
        _XQ_COOKIE["jar"] = opener
        _XQ_COOKIE["ts"] = now
        return opener


def _xq_symbol(code: str, venue: str) -> str:
    prefix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(venue, "SH")
    return f"{prefix}{code}"


def _fetch_klines_xueqiu(code: str, venue: str, period: str, limit: int,
                         adjust: str) -> List[Bar]:
    """雪球 K 线（异地备源）。

    返回列：timestamp, volume, open, high, low, close, chg, percent,
    turnoverrate, amount, ...（**是 open/high/low/close 顺序**，与腾讯的
    open/close/high/low 不同，别抄错）。volume 单位是**股**，本层统一用「手」，
    故除以 100；amount 单位是元（雪球给的浮点科学计数串）。

    count 传负数表示「取到 begin 之前的 N 根」，这是雪球的分页语义。
    """
    xq_period = _XQ_PERIOD.get(period)
    if not xq_period:
        return []
    xq_type = _XQ_TYPE.get((adjust or "qfq").lower(), "before")
    sym = _xq_symbol(code, venue)
    begin_ms = int(time.time() * 1000)
    params = urllib.parse.urlencode({
        "symbol": sym, "begin": begin_ms, "period": xq_period,
        "type": xq_type, "count": -max(1, int(limit)),
        "indicator": "kline",
    })
    opener = _xq_opener()
    req = urllib.request.Request(
        f"{XQ_KLINE}?{params}",
        headers=dict(_HEADERS, Referer=f"https://xueqiu.com/S/{sym}"))
    try:
        with opener.open(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            raw = _decompress(raw, resp.headers.get("Content-Encoding"))
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise DataError(f"雪球 K 线请求失败：{exc}") from exc
    if payload.get("error_code"):
        # token 失效：清掉缓存，下次重建
        _XQ_COOKIE["jar"] = None
        raise DataError(f"雪球返回错误 {payload.get('error_code')}: "
                        f"{payload.get('error_description')}")
    item = ((payload.get("data") or {}).get("item")) or []
    out: List[Bar] = []
    for r in item:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts, vol, op_, hi, lo, cl = r[0], r[1], r[2], r[3], r[4], r[5]
        try:
            dt = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(int(ts) / 1000))
        except (ValueError, OSError, TypeError):
            continue
        date = dt[:10] if xq_period in ("day", "week", "month") else dt[:16]
        amt = to_float(r[9], None) if len(r) > 9 else None
        out.append(Bar(
            date=date,
            open=to_float(op_, 0.0) or 0.0,
            close=to_float(cl, 0.0) or 0.0,
            high=to_float(hi, 0.0) or 0.0,
            low=to_float(lo, 0.0) or 0.0,
            # 雪球给「股」，本层统一「手」；与腾讯对齐取整（腾讯给整手，
            # 不 round 的话同一根 K 线换源后量能会差 0.5 手，看起来像数据漂移）
            # 用 int(x+0.5) 而非 round()：round 是银行家舍入，1150268.5 会
            # 变 1150268 而腾讯给 1150269，同一根 K 线换源后量能不一致
            volume_lot=float(int((to_float(vol, 0.0) or 0.0) / 100.0 + 0.5)),
            amount=amt,
        ))
    return out


def fetch_minute_bars(raw_code: str, period: str = "5m", limit: int = 120,
                      kind: str = "stock") -> List[Bar]:
    """分钟线快捷入口（补齐 1m 支持）。"""
    return fetch_klines(raw_code, period=period, limit=limit, kind=kind)


# ---------------------------------------------------------------- 检索


def search(keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    """按代码/名称/拼音检索标的（东财 suggest 接口）。

    返回 [{code, name, symbol, board, kind, market}]；`kind` 用于后续取行情时
    区分指数（000001 上证 vs 平安银行）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    params = urllib.parse.urlencode({
        "input": kw, "type": "14",
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": max(1, min(int(limit), 30)),
    })
    data = _http_get_json(f"{EM_SUGGEST}?{params}", headers=_EM_HEADERS)
    rows = ((data or {}).get("QuotationCodeTable") or {}).get("Data") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        code = str(r.get("Code") or "").strip()
        name = str(r.get("Name") or "").strip()
        classify = str(r.get("Classify") or "")
        sec_type = str(r.get("SecurityTypeName") or "")
        mkt = str(r.get("MktNum") or "")
        if not code or not name:
            continue
        if classify in ("Index",) or sec_type == "指数":
            kind, venue = "index", ("sh" if mkt == "1" else "sz")
        elif classify in ("Fund", "ETF") or sec_type.startswith("基金"):
            kind, venue = "etf", ("sh" if mkt == "1" else "sz")
        elif classify in ("AStock",) or sec_type in _A_STOCK_TYPES:
            kind = "stock"
            venue = guess_venue(code)
            if code.startswith(("43", "83", "87", "88", "920")):
                venue = "bj"
        else:
            # 债券 / 权证 / 港股 / 美股等：保留在结果里但标注 other，
            # 由调用方决定是否过滤（炒股场景通常只要 stock/index/etf）
            kind, venue = "other", ("sh" if mkt == "1" else "sz")
        out.append({
            "code": code, "name": name, "symbol": f"{venue}{code}",
            "venue": venue, "kind": kind,
            "board": BOARD_LABELS.get(board_of(code), ""),
            "market": sec_type or classify,
        })
    return out


# ---------------------------------------------------------------- 资金流


SINA_FLOW = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "MoneyFlow.ssl_qsfx_zjlrqs")


def _flow_via_sina(symbol: str, days: int) -> List[Dict[str, Any]]:
    """新浪资金流备源。

    字段口径与东财不同：新浪给 `netamount`（净流入，元）与 `r0_net`
    （超大单净额）。东财的口径是"主力 = 超大单 + 大单"，新浪没有直接
    对应字段，因此这里只填充能确认的项，其余留 None——**宁可缺字段，
    也不把不同口径的数字硬塞进同一个列**（那会让 Agent 讲错结论）。
    """
    params = urllib.parse.urlencode({
        "page": 1, "num": max(1, min(int(days), 60)), "sort": "opendate",
        "asc": 0, "daima": symbol, "_s_r_a": "page",
    })
    text = _http_get(f"{SINA_FLOW}?{params}",
                     headers=dict(_HEADERS, Referer="https://finance.sina.com.cn"))
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in raw:
        out.append({
            "date": str(r.get("opendate") or ""),
            "main_net": to_float(r.get("netamount"), None),
            "small_net": None,
            "mid_net": None,
            "large_net": None,
            "super_net": to_float(r.get("r0_net"), None),
            "source": "sina",
        })
    out.sort(key=lambda x: x["date"])
    return out


def fetch_money_flow(raw_code: str, days: int = 10,
                     kind: str = "stock") -> List[Dict[str, Any]]:
    """个股资金流（主力/超大单/大单/中单/小单 净流入，单位元）。

    东财为主源、新浪为备源（东财 push2his 与选股接口同域，常一起被限流）。
    """
    code, venue = normalize_code(raw_code, kind)
    secid = f"{'1' if venue == 'sh' else '0'}.{code}"
    days = max(1, min(int(days or 10), 120))
    params = urllib.parse.urlencode({
        "lmt": days, "klt": "101", "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    })
    rows: List[str] = []
    try:
        data = _http_get_json(f"{EM_FFLOW}?{params}", headers=_EM_HEADERS)
        rows = ((data or {}).get("data") or {}).get("klines") or []
    except DataError as exc:
        logger.warning("[astock] 东财资金流失败，改用新浪备源: %s", exc)
        return _flow_via_sina(tencent_symbol(code, venue), days)

    out: List[Dict[str, Any]] = []
    for line in rows:
        parts = str(line).split(",")
        if len(parts) < 5:
            continue
        out.append({
            "date": parts[0],
            "main_net": to_float(parts[1], None),
            "small_net": to_float(parts[2], None),
            "mid_net": to_float(parts[3], None),
            "large_net": to_float(parts[4], None),
            "super_net": to_float(parts[5], None) if len(parts) > 5 else None,
            "source": "eastmoney",
        })
    return out


# ---------------------------------------------------------------- 选股


_CLIST_FIELDS = ("f2,f3,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62,f115")


def _screen_via_sina(universe: str, sort_by: str, ascending: bool,
                     limit: int, min_change, max_change, min_amount,
                     max_amount, min_turnover, max_turnover, min_mcap,
                     max_mcap, exclude_st) -> Tuple[List[Dict[str, Any]], int]:
    """新浪排行备源（东财 push2 被限流时使用）。

    新浪返回的是**全字段快照数组**且只按一个字段排序，所以这里按同样的
    过滤/排序逻辑本地处理，保证 `screen()` 对上层结构完全一致。

    已知差异（写在这里而不是藏着）：新浪的 `mktcap` 单位是**万元**，
    东财 `f20` 是元——归一化时统一乘 1e4。
    """
    node = SINA_NODE.get((universe or "all").lower(), "hs_a")
    sort_field = _SINA_SORT.get((sort_by or "change_pct").lower(), "changepercent")
    try:
        total_text = _http_get(SINA_COUNT + f"?node={node}",
                               headers=dict(_HEADERS, Referer="https://finance.sina.com.cn"))
        universe_total = int(to_float(total_text.strip().strip('"'), 0) or 0)
    except (DataError, ValueError):
        universe_total = 0

    params = urllib.parse.urlencode({
        "page": 1, "num": 200, "sort": sort_field,
        "asc": 1 if ascending else 0, "node": node, "symbol": "", "_s_r_a": "page",
    })
    text = _http_get(f"{SINA_RANK}?{params}",
                     headers=dict(_HEADERS, Referer="https://finance.sina.com.cn"))
    try:
        raw = json.loads(text) if text.strip().startswith("[") else []
    except ValueError as exc:
        raise DataError("新浪排行接口返回无法解析（接口可能变更）") from exc

    def num(d: Dict[str, Any], key: str) -> Optional[float]:
        return to_float(d.get(key), None)

    def ok(v: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
        if v is None:
            return lo is None and hi is None
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
        return True

    out: List[Dict[str, Any]] = []
    for r in raw:
        name = str(r.get("name") or "")
        if exclude_st and "ST" in name.upper():
            continue
        price = num(r, "trade")
        chg = num(r, "changepercent")
        amount = num(r, "amount")
        turnover = num(r, "turnoverratio")
        mcap_wan = num(r, "mktcap")
        mcap = mcap_wan * 1e4 if mcap_wan is not None else None
        if not price or chg is None:
            continue
        if not ok(chg, min_change, max_change):
            continue
        if not ok(amount, min_amount, max_amount):
            continue
        if not ok(turnover, min_turnover, max_turnover):
            continue
        if not ok(mcap, min_mcap, max_mcap):
            continue
        volume = num(r, "volume")          # 新浪为股数
        out.append({
            "code": str(r.get("code") or ""), "name": name,
            "price": price, "change_pct": chg,
            "volume_lot": round(volume / 100, 2) if volume else None,
            "amount_yuan": amount, "turnover_pct": turnover,
            "pe": num(r, "per"), "pb": num(r, "pb"),
            "total_mcap_yuan": mcap,
            "float_mcap_yuan": (num(r, "nmc") * 1e4
                                if num(r, "nmc") is not None else None),
            "main_net_inflow_yuan": None,   # 新浪排行不含资金流
            "volume_ratio": None,
            "high": num(r, "high"), "low": num(r, "low"),
            "open": num(r, "open"), "prev_close": num(r, "settlement"),
        })
        if len(out) >= max(1, min(int(limit or 20), 200)):
            break
    return out, universe_total


def screen(universe: str = "all", sort_by: str = "change_pct",
           ascending: bool = False, limit: int = 20,
           min_change: Optional[float] = None,
           max_change: Optional[float] = None,
           min_amount: Optional[float] = None,
           max_amount: Optional[float] = None,
           min_turnover: Optional[float] = None,
           max_turnover: Optional[float] = None,
           min_mcap: Optional[float] = None,
           max_mcap: Optional[float] = None,
           exclude_st: bool = True) -> Dict[str, Any]:
    """条件选股（东财 clist 全市场快照）。金额单位：元。

    返回 {total, rows:[...], universe, filters}。单页最多 100 条，本地过滤
    后再截断——先取全市场（分页）再过滤保证 `total` 是真实数量。
    """
    fs = UNIVERSE_FS.get((universe or "all").lower(), UNIVERSE_FS["all"])
    sort_map = {
        "change_pct": "f3", "price": "f2", "amount": "f6", "volume": "f5",
        "turnover": "f8", "mcap": "f20", "pe": "f9", "main_net": "f62",
    }
    fid = sort_map.get((sort_by or "change_pct").lower(), "f3")

    rows: List[Dict[str, Any]] = []
    total = 0
    page = 1
    page_size = 100
    max_pages = 60          # 6000 条上限，覆盖全市场
    try:
        while page <= max_pages:
            params = urllib.parse.urlencode({
                "pn": page, "pz": page_size, "po": "0" if ascending else "1",
                "np": 1, "fltt": 2, "invt": 2, "fid": fid, "fs": fs,
                "fields": _CLIST_FIELDS,
            })
            data = _http_get_json(f"{EM_CLIST}?{params}", headers=_EM_HEADERS)
            node = (data or {}).get("data") or {}
            if page == 1:
                total = int(node.get("total") or 0)
            diff = node.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            rows.extend(diff)
            if len(rows) >= total or len(diff) < page_size:
                break
            page += 1
    except DataError as exc:
        # 东财 push2 被限流时（实测高发）退回新浪排行接口：
        # 字段口径不同，因此走独立的归一化函数，保证下游结构一致
        logger.warning("[astock] 东财选股失败，改用新浪排行备源: %s", exc)
        rows, total = _screen_via_sina(universe, sort_by, ascending, limit,
                                       min_change, max_change, min_amount,
                                       max_amount, min_turnover, max_turnover,
                                       min_mcap, max_mcap, exclude_st)
        return {"total_matched": len(rows), "universe_total": total,
                "universe": universe, "rows": rows,
                "filters": {"sort_by": sort_by, "ascending": bool(ascending),
                            "source": "sina"}}

    def ok(v: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
        if v is None:
            return lo is None and hi is None
        if lo is not None and v < lo:
            return False
        if hi is not None and v > hi:
            return False
        return True

    picked: List[Dict[str, Any]] = []
    for r in rows:
        name = str(r.get("f14") or "")
        if exclude_st and "ST" in name.upper():
            continue
        price = to_float(r.get("f2"), None)
        chg = to_float(r.get("f3"), None)
        amount = to_float(r.get("f6"), None)
        turnover = to_float(r.get("f8"), None)
        mcap = to_float(r.get("f20"), None)
        if price in (None, 0) or chg is None:      # 停牌（无价）剔除
            continue
        if not ok(chg, min_change, max_change):
            continue
        if not ok(amount, min_amount, max_amount):
            continue
        if not ok(turnover, min_turnover, max_turnover):
            continue
        if not ok(mcap, min_mcap, max_mcap):
            continue
        picked.append({
            "code": str(r.get("f12") or ""), "name": name,
            "price": price, "change_pct": chg,
            "volume_lot": to_float(r.get("f5"), None),
            "amount_yuan": amount, "turnover_pct": turnover,
            "pe": to_float(r.get("f9"), None), "pb": to_float(r.get("f23"), None),
            "total_mcap_yuan": mcap,
            "float_mcap_yuan": to_float(r.get("f21"), None),
            "main_net_inflow_yuan": to_float(r.get("f62"), None),
            "volume_ratio": to_float(r.get("f10"), None),
            "high": to_float(r.get("f15"), None),
            "low": to_float(r.get("f16"), None),
            "open": to_float(r.get("f17"), None),
            "prev_close": to_float(r.get("f18"), None),
        })
        if len(picked) >= max(1, min(int(limit or 20), 200)):
            break

    return {
        "total_matched": len(picked),
        "universe_total": total,
        "universe": universe,
        "rows": picked,
        "filters": {
            "sort_by": sort_by, "ascending": bool(ascending),
            "min_change": min_change, "max_change": max_change,
            "min_amount": min_amount, "max_amount": max_amount,
            "min_turnover": min_turnover, "max_turnover": max_turnover,
            "min_mcap": min_mcap, "max_mcap": max_mcap,
            "exclude_st": bool(exclude_st),
        },
    }


# ---------------------------------------------------------------- 便捷


def fetch_index_snapshot() -> List[Quote]:
    """常用指数一览（上证/深成/创业板/沪深300/科创50/北证50）。"""
    items = [("000001", "sh", "index"), ("399001", "sz", "index"),
             ("399006", "sz", "index"), ("000300", "sh", "index"),
             ("000688", "sh", "index"), ("899050", "bj", "index")]
    try:
        return fetch_quotes(items)
    except DataError:
        return []


def clear_cache() -> None:
    _CACHE.clear()
