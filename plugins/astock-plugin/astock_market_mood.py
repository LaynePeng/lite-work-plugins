"""大盘与市场情绪看板数据 · v2.5.0 新增。

────────────────────────────────────────────────────────────────────────
为什么需要
────────────────────────────────────────────────────────────────────────
持仓面板此前只回答「我的钱怎么样了」：总资产、浮盈、仓位、距止损。
但**不知道大盘和整体形势**就没法判断这些数字的成因：

  · 持仓跌 2%——是我的票变差了，还是全市场都在跌？
  · 现金占 70%——是该继续等，还是市场已经转暖而我在空仓踏空？
  · 大盘涨 1% 但持仓不涨——是弱势，还是只是今天风格不在我这边？

这三个问题**只有大盘+市场宽度能回答**。只看自己的持仓，会把
「市场普跌」误读成「我选错了」，也会把「普涨里的滞涨」当成正常。

────────────────────────────────────────────────────────────────────────
三条设计红线
────────────────────────────────────────────────────────────────────────
① **面板每 5 秒刷新，绝不能在渲染路径上做慢请求。**
   实测：全市场扫描（涨跌家数用 clist 全量算）要 5.3 秒——直接卡死面板。
   所以宽度数据必须走**单次请求**接口（实测 0.15 秒），且带 TTL 缓存。

② **指数与宽度是两回事，不能互相替代。**
   上证 +0.79% 但 4409 家上涨 / 790 家下跌 —— 指数被权重股压制，
   个股其实是普涨。只看指数会得出**完全相反的结论**。

③ **不做预测、不给"多空信号"。**
   本模块只输出**当下的事实分布**（涨了多少家、钱在往哪流、谁强谁弱）。
   "明天怎么走"没有数学基础，编出来比不给更危险。

────────────────────────────────────────────────────────────────────────
数据源与口径（重要，别混）
────────────────────────────────────────────────────────────────────────
| 数据 | 来源 | 口径 | 实测耗时 |
|---|---|---|---|
| 指数行情 | 腾讯 qt.gtimg.cn | 沪深主要指数实时/收盘 | 0.2s（批量）|
| 涨跌家数 | 东财 f104/f105/f106 | **沪市 + 深市分别统计后相加** | 0.15s |
| 成交额 | 指数行情的 amount 字段 | 单市场口径 | 随指数 |

⚠️ **f104/f105/f106 是"该指数所属市场"的统计，不是全市场**：
   上证指数 2356 家 + 深证成指 2938 家 = 5294 家（接近全市场 5576）。
   若只取上证一个 secid，得到的是**沪市 2356 家**，会被误当成全市场。
   早期差点踩这个坑：显示"上涨 1877 家"却标"全市场"，实际漏掉深市 2552 家。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import astock_data as data
import astock_market as mkt
import astock_formula as fx

logger = logging.getLogger("litework.astock.mood")

#: 大盘看板关注的指数（顺序即展示顺序：先看大势，再看风格）
#: (代码, 市场前缀, 展示名, 角色)
INDICES: List[Tuple[str, str, str, str]] = [
    ("000001", "sh", "上证指数", "沪深主板"),
    ("399001", "sz", "深证成指", "深市主板"),
    ("399006", "sz", "创业板指", "成长风格"),
    ("000688", "sh", "科创50", "硬科技"),
    ("000300", "sh", "沪深300", "大盘蓝筹"),
    ("000905", "sh", "中证500", "中盘"),
]

#: 涨跌家数接口（东财 ulist，单次请求返回指定指数的市场统计）
EM_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"

#: 宽度缓存 TTL（秒）。面板 5 秒刷一次，但 15 秒内的宽度数据不会变多少——
#: 而每次请求都要打公共接口，没必要跟着面板节奏打。
BREADTH_TTL = 15.0

#: 行情接口（腾讯），与 astock_data 保持一致
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


# ====================================================================== 市场宽度


def parse_breadth(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从东财 ulist 的 data.diff 解析单市场涨跌家数。

    字段：f104 上涨家数 / f105 下跌家数 / f106 平盘家数。
    **缺任何一个就返回 None**——不能把"没取到"当成 0 家。
    """
    if not isinstance(node, dict):
        return None
    up, down, flat = node.get("f104"), node.get("f105"), node.get("f106")
    if up is None or down is None:
        return None
    try:
        up_i, down_i = int(up), int(down)
        flat_i = int(flat) if flat is not None else 0
    except (TypeError, ValueError):
        return None
    return {"up": up_i, "down": down_i, "flat": flat_i,
            "total": up_i + down_i + flat_i}


def merge_breadth(parts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把多个市场的统计合并成全市场。

    ⚠️ **必须按市场分别统计再相加**：东财 f104/f105/f106 是
    "该指数所属市场"的家数，直接取上证一个 secid 只得到沪市 2356 家。
    """
    parts = [p for p in parts if p]
    if not parts:
        return None
    up = sum(p["up"] for p in parts)
    down = sum(p["down"] for p in parts)
    flat = sum(p["flat"] for p in parts)
    return {"up": up, "down": down, "flat": flat, "total": up + down + flat}


def advance_ratio(breadth: Optional[Dict[str, Any]]) -> Optional[float]:
    """上涨家数占比（%）＝ 涨 / (涨+跌)，平盘不计入分母。

    **为什么平盘不进分母**：停牌与持平不表达方向，算进去会稀释信号。
    返回 None 表示没有有效数据（不是 0——0% 意味着"全跌"，含义完全不同）。
    """
    if not breadth:
        return None
    dec = breadth.get("up", 0) + breadth.get("down", 0)
    if dec <= 0:
        return None
    return round(breadth["up"] / dec * 100.0, 2)


def breadth_tone(ratio: Optional[float]) -> str:
    """把上涨占比翻译成人话（只描述当下，不预测）。

    分档是**描述性**的，不是"买卖信号"。
    """
    if ratio is None:
        return "数据缺失"
    if ratio >= 70:
        return "普涨"
    if ratio >= 55:
        return "偏强"
    if ratio >= 45:
        return "分化"
    if ratio >= 30:
        return "偏弱"
    return "普跌"


# ====================================================================== 取数


def _http_json(url: str) -> Optional[Dict[str, Any]]:
    """取 JSON（复用数据层的限流与超时），失败返回 None 不抛。"""
    try:
        text = data._http_get(url, headers=_HEADERS)
        return json.loads(text)
    except Exception as exc:
        logger.info("[astock] 大盘数据请求失败: %s", exc)
        return None


def fetch_breadth() -> Optional[Dict[str, Any]]:
    """取全市场涨跌家数（沪深分别取后合并）。

    单次请求约 0.15 秒。失败返回 None（面板会显式标注"未取到"，
    **不静默隐藏**——用户需要知道这个数是缺失的还是真的为 0）。
    """
    secids = "1.000001,0.399001"        # 上证（沪市）+ 深证成指（深市）
    url = (f"{EM_ULIST}?fltt=2&secids={secids}"
           f"&fields=f104,f105,f106,f14")
    payload = _http_json(url)
    if not payload:
        return None
    diff = ((payload.get("data") or {}).get("diff") or [])
    parts = [parse_breadth(x) for x in diff]
    return merge_breadth(parts)


def fetch_indices(kind: str = "index") -> List[Dict[str, Any]]:
    """取主要指数行情（一次批量请求）。"""
    items = [(code, venue, kind) for code, venue, _n, _r in INDICES]
    try:
        quotes = data.fetch_quotes(items)
    except data.DataError as exc:
        logger.warning("[astock] 指数行情获取失败: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    by_code = {q.code: q for q in quotes}
    for code, _venue, name, role in INDICES:
        q = by_code.get(code)
        if not q:
            continue
        d = q.as_dict()
        out.append({
            "code": code,
            "name": d.get("name") or name,
            "role": role,
            "price": d.get("price"),
            "change_pct": d.get("change_pct"),
            "amount_yuan": d.get("amount_yuan"),
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "prev_close": d.get("prev_close"),
        })
    return out


# ====================================================================== 归因


def relative_strength(stock_pct: Optional[float],
                      index_pct: Optional[float]) -> Optional[float]:
    """个股/组合相对指数的超额涨跌（百分点）。

    这是判断"我的票行不行"的**唯一正确方式**：绝对涨跌说明不了问题。
    大盘跌 1%、持仓跌 0.5% = 跑赢 0.5pp（强）；大盘涨 2%、持仓涨 1% = 跑输。
    """
    if stock_pct is None or index_pct is None:
        return None
    return round(float(stock_pct) - float(index_pct), 2)


def rs_verdict(excess_pp: Optional[float]) -> str:
    """超额收益的人话判定（描述，不是建议）。"""
    if excess_pp is None:
        return "-"
    if excess_pp >= 2:
        return "显著跑赢"
    if excess_pp >= 0.5:
        return "跑赢"
    if excess_pp > -0.5:
        return "基本同步"
    if excess_pp > -2:
        return "跑输"
    return "显著跑输"


def market_summary(indices: List[Dict[str, Any]],
                   breadth: Optional[Dict[str, Any]],
                   total_amount: Optional[float] = None) -> Dict[str, Any]:
    """把指数与宽度合成一句可读的市场状态（纯描述）。

    输出字段：
      tone       一句话定性（普涨/分化/普跌 …）
      detail     带数字的展开说明
      sh_pct     上证涨跌幅
      breadth_up / breadth_down / ratio
      amount     两市成交额（亿）
      divergence 指数与宽度的**背离提示**（这是最有信息量的部分）
    """
    by_code = {i["code"]: i for i in indices}
    sh = by_code.get("000001") or {}
    sz = by_code.get("399001") or {}
    sh_pct = sh.get("change_pct")
    ratio = advance_ratio(breadth)
    tone = breadth_tone(ratio)

    out: Dict[str, Any] = {
        "sh_pct": sh_pct,
        "sz_pct": sz.get("change_pct"),
        "ratio": ratio,
        "breadth_up": (breadth or {}).get("up"),
        "breadth_down": (breadth or {}).get("down"),
        "breadth_flat": (breadth or {}).get("flat"),
        "breadth_total": (breadth or {}).get("total"),
        "amount_yi": round(total_amount / 1e8, 0) if total_amount else None,
        "tone": tone,
        "divergence": None,
        "detail": "",
    }

    # 背离提示：指数方向 vs 个股宽度方向不一致时，**指数会骗人**
    if sh_pct is not None and ratio is not None:
        if sh_pct > 0 and ratio < 45:
            out["divergence"] = (f"⚠️ 指数上涨 {sh_pct:+.2f}% 但只有 {ratio:.1f}% 个股上涨——"
                                 f"**指数被权重股拉动，多数个股在跌**，别把指数红盘当成普涨")
        elif sh_pct < 0 and ratio > 55:
            out["divergence"] = (f"⚠️ 指数下跌 {sh_pct:+.2f}% 但仍有 {ratio:.1f}% 个股上涨——"
                                 f"**是权重股拖累指数，多数个股在涨**，别被指数绿盘吓到")

    if ratio is not None and breadth:
        out["detail"] = (f"{breadth['up']} 家上涨 / {breadth['down']} 家下跌"
                         f"（上涨占比 {ratio:.1f}%，共 {breadth['total']} 家）")
    elif sh_pct is not None:
        out["detail"] = "涨跌家数未取到（接口限流或断网），仅显示指数"
    return out


def style_ranking(indices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按涨跌幅给指数排序 → 看出今天是什么风格在涨。"""
    rows = [i for i in indices if i.get("change_pct") is not None]
    return sorted(rows, key=lambda x: x["change_pct"], reverse=True)


def style_verdict(ranking: List[Dict[str, Any]]) -> str:
    """从指数强弱推断风格（描述当下，不预测）。

    只做**相对比较**：涨得最多的那个就是当下资金偏好，
    不给"应该买什么"的结论。
    """
    if len(ranking) < 2:
        return "-"
    best, worst = ranking[0], ranking[-1]
    spread = (best.get("change_pct") or 0) - (worst.get("change_pct") or 0)
    if spread < 0.8:
        return f"风格均衡（最强 {best['name']} 与最弱 {worst['name']} 仅差 {spread:.2f}pp）"
    return (f"**{best['name']}** 最强（{best['change_pct']:+.2f}%）、"
            f"**{worst['name']}** 最弱（{worst['change_pct']:+.2f}%），"
            f"分化 {spread:.2f}pp")


# ====================================================================== 缓存封装


class MoodCache:
    """大盘数据的进程内缓存（面板高频刷新用）。

    **为什么必须缓存**：面板每 5 秒刷一次，宽度接口虽然是 0.15 秒，
    但 5 秒打一次公共接口会被限流（实测东财 clist 就是这样挂的），
    而且盘后价格不变，重复请求纯属浪费。
    """

    def __init__(self, ttl: float = BREADTH_TTL) -> None:
        self.ttl = max(0.0, float(ttl))
        self._at: float = 0.0
        self._breadth: Optional[Dict[str, Any]] = None
        self._indices: List[Dict[str, Any]] = []
        self._amount: Optional[float] = None

    def get(self, now: float, force: bool = False) -> Dict[str, Any]:
        """取缓存；过期或 force 时刷新。**失败时保留上一次的有效值**。"""
        stale = (now - self._at) > self.ttl
        if force or stale or not self._at:
            self._refresh()
            self._at = now
        return {"breadth": self._breadth, "indices": self._indices,
                "amount": self._amount}

    def _refresh(self) -> None:
        idx = fetch_indices()
        if idx:
            self._indices = idx
            # 两市成交额：上证 + 深证（不含创业板/科创，避免重复计）
            by_code = {i["code"]: i for i in idx}
            sh = (by_code.get("000001") or {}).get("amount_yuan") or 0
            sz = (by_code.get("399001") or {}).get("amount_yuan") or 0
            if sh or sz:
                self._amount = float(sh) + float(sz)
        b = fetch_breadth()
        if b:
            self._breadth = b
        # 失败时不动缓存：宁可显示 15 秒前的数，也不显示空白

    def clear(self) -> None:
        self._at = 0.0
        self._breadth = None
        self._indices = []
        self._amount = None
