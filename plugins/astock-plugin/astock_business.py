"""主营构成（分部收入占比）——让选股方法论第②道门槛可自动化判定。

**为什么需要这个模块**（2026-11-04 立项）

《选股方法论》四道硬门槛的第②道要求「相关业务收入占营收 >= 5%」，
此前被登记为「工具链无分部财报字段 -> 永久缺口」，导致每次盘前简报都只能写
「②无法核实 -> 只观察」。结果是这道门槛事实上不存在：
回购/增持类事件讲不清"哪块业务受益"，于是所有标的都卡在同一句免责声明上。

数据源是东财 F10「主营构成」，返回每年中报/年报两个报告期的分部明细，
字段 `MBI_RATIO` 就是该项收入占营业收入的比例——**这正是门槛②要的数字**。

**判定不是「取个数跟 5% 比」那么简单**，真实情况有四类，必须分开：

| 结果 | 条件 | 对门槛②的意义 |
|---|---|---|
| `pass` | 相关分部占比 >= 阈值 | 通过 |
| `below` | 找到相关分部，占比 < 阈值 | 不通过 |
| `absent` | 分部表里根本没有相关业务 | **不通过，且比 below 更确定** |
| `na` | 事件本身不涉及"某块业务"（回购/增持/分红） | 不适用，不据此否决 |

`absent` 这一档是踩出来的关键认识：分部披露有**重要性门槛**，进不了分部表
说明该业务占比远低于披露线，必然远小于 5%。襄阳轴承（000678）就是活例——
它的分部表只有「轴承 83.7% / 等速万向节 12.9% / 其他 3.4%」，**没有机器人**。
这比事后查到"机器人业务占营收 0.023%"更早、也更少歧义地证明了同一件事。

**为什么 `na` 必须单独存在**：回购、增持、分红是全体股东受益的资本运作，
不对应任何具体产品线。若把它算成「查不到 -> 不通过」，门槛②会变成
"任何资本运作类标的都永远过不了"，等于自己废掉一条通道。

⚠️ 数据边界（实测 2026-11-04）：
  · 沪深主板 / 创业板 / 科创板 -> 正常返回
  · 北交所（BJ 前缀）/ ETF -> 返回 `{"status": -1, "message": "股票代码不合法"}`
  · 报告期滞后：最新一般是最近一期中报或年报，**不是实时**。
    因此本模块给出的是"最近披露的业务结构"，不是"今天的业务结构"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import astock_data as data

logger = logging.getLogger("litework.astock.business")

#: 东财 F10 主营构成接口
_URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/"
        "PageAjax?code={prefix}{code}")

#: 门槛②阈值（方法论：相关业务收入占营收 >= 5%）
THRESHOLD_PCT = 5.0

#: 判定结果
PASS = "pass"        # 占比达标
BELOW = "below"      # 有该业务，但占比不足
ABSENT = "absent"    # 分部表里没有该业务（未达披露门槛）
NA = "na"            # 事件不涉及具体业务，门槛② 本就不适用
UNKNOWN = "unknown"  # 数据取不到（北交所/ETF/接口异常），**不构成通过**

#: 资本运作类事件：不涉及"某块业务收入占比"，不据此否决
_CAPITAL_EVENT_WORDS = (
    "回购", "增持", "分红", "派息", "红利", "股权激励", "注销", "质押",
    "股东会", "股东大会", "章程", "注册资本",
)

#: 业务关键词同义词表：事件里的行业词 -> 分部表里可能叫的名字。
#: 只放**确实需要桥接**的（差太远会误匹配）；命中不到就用原始词直配。
_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "机器人": ("机器人", "减速器", "谐波", "RV", "精密传动", "智能装备"),
    "人形机器人": ("机器人", "减速器", "谐波", "精密传动"),
    "算力": ("算力", "IDC", "数据中心", "服务器", "云计算"),
    "人工智能": ("人工智能", "算力", "AI"),
    "光伏": ("光伏", "硅片", "电池片", "组件", "逆变器"),
    "储能": ("储能", "电池"),
    "锂电": ("锂离子电池", "动力电池", "电池", "正极", "负极", "隔膜"),
    "半导体": ("半导体", "芯片", "集成电路", "晶圆", "封装"),
    "军工": ("军工", "国防", "航空航天", "兵器"),
    "汽车": ("汽车", "整车", "零部件", "汽零"),
    "医药": ("医药", "药品", "制剂", "原料药", "医疗器械"),
    "白酒": ("白酒", "酒类", "酒"),
    "黄金": ("黄金", "贵金属", "矿产金"),
    "稀土": ("稀土", "永磁", "磁材"),
    # 商品/资源类：事件常指向某个金属品种而非"业务线"，但分部表正是按品种列
    "电池": ("电池", "锂离子", "动力电池", "储能电池"),
    "锡": ("锡锭", "锡材", "锡产品", "锡"),
    "铜": ("铜产品", "铜", "阴极铜"),
    "铝": ("铝", "电解铝", "铝锭", "铝材"),
    "锌": ("锌产品", "锌", "锌锭"),
    "镍": ("镍", "镍产品"),
    "钴": ("钴", "钴产品"),
    # 光电/精密制造
    "光学": ("光学", "镜片", "镜头", "光学元件"),
    "激光": ("激光", "光子"),
    "显示": ("显示", "面板", "液晶", "OLED"),
    "消费电子": ("消费电子", "智能终端", "精密组件"),
    "连接器": ("连接器", "线缆", "互连"),
    # 医药细分
    "创新药": ("创新药", "药品", "制剂", "生物药"),
    "医疗器械": ("医疗器械", "器械", "耗材"),
    "原料药": ("原料药", "中间体", "api"),
}

#: 分部项的"杂项"，不参与业务匹配（否则「其他」会命中一切）
_NOISE_ITEMS = ("其他", "其它", "合计", "抵销", "分部间")


@dataclass
class Segment:
    """一个分部项（某报告期、某口径下的一条收入构成）。"""

    item: str               # 名称，如「激光光学产品」
    ratio_pct: float        # 收入占比 %
    income: Optional[float] = None       # 收入金额（元）
    gross_pct: Optional[float] = None    # 毛利率 %
    rank: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"item": self.item, "ratio_pct": self.ratio_pct,
                "income": self.income, "gross_pct": self.gross_pct,
                "rank": self.rank}


@dataclass
class BusinessMix:
    """一家公司某报告期的主营构成。"""

    code: str
    name: str = ""
    report_date: str = ""                        # 如 2026-06-30
    by_product: List[Segment] = field(default_factory=list)   # 按产品（最细）
    by_industry: List[Segment] = field(default_factory=list)  # 按行业
    scope: str = ""                              # 经营范围（核对业务是否沾边用）

    @property
    def items(self) -> List[Segment]:
        """优先按产品口径；没有则回退按行业。"""
        return self.by_product or self.by_industry

    def top(self, n: int = 5) -> List[Segment]:
        return sorted(self.items, key=lambda s: -s.ratio_pct)[:n]


@dataclass
class Threshold2Result:
    """门槛②判定结果（带证据，便于人复核）。"""

    code: str
    result: str = UNKNOWN
    ratio_pct: Optional[float] = None       # 命中的占比
    matched: str = ""                       # 命中的分部名
    keywords: Tuple[str, ...] = ()          # 用到的关键词
    report_date: str = ""
    reason: str = ""
    candidates: List[Segment] = field(default_factory=list)  # 全部分部（供人判断）

    @property
    def passed(self) -> Optional[bool]:
        """True/False/None（None = 不适用或未知，不能当作通过）。"""
        if self.result == PASS:
            return True
        if self.result in (BELOW, ABSENT):
            return False
        return None

    @property
    def verdict(self) -> str:
        return {
            PASS: "PASS 过",
            BELOW: "FAIL 不过（占比不足）",
            ABSENT: "FAIL 不过（无此业务）",
            NA: "NA 不适用",
            UNKNOWN: "WARN 未核实",
        }.get(self.result, "WARN 未核实")


# ---------------------------------------------------------------- 取数

def _prefix(venue: str) -> str:
    return {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(venue.lower(), "SZ")


def _num(raw: Any) -> Optional[float]:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val


def _segments(rows: Sequence[Dict[str, Any]], mainop_type: str) -> List[Segment]:
    out: List[Segment] = []
    for row in rows:
        if str(row.get("MAINOP_TYPE") or "") != mainop_type:
            continue
        item = str(row.get("ITEM_NAME") or "").strip()
        ratio = _num(row.get("MBI_RATIO"))
        if not item or ratio is None:
            continue
        gross = _num(row.get("GROSS_RPOFIT_RATIO"))
        rank = _num(row.get("RANK"))
        # 接口给的是小数（0.9313），转成百分数
        out.append(Segment(
            item=item, ratio_pct=round(ratio * 100, 4),
            income=_num(row.get("MAIN_BUSINESS_INCOME")),
            gross_pct=(None if gross is None else round(gross * 100, 2)),
            rank=(None if rank is None else int(rank)),
        ))
    return out


def fetch_mix(code: str, name: str = "") -> BusinessMix:
    """取一家公司最新报告期的主营构成。

    取不到时抛 `DataError`（由 `check()` 收敛成 `unknown`）：
    **绝不能把"没查到"当成"通过"**。北交所/ETF 会返回「股票代码不合法」，
    这属于已知边界，单独在 reason 里说明。
    """
    import astock_market as mkt

    norm, venue = mkt.normalize_code(code)
    url = _URL.format(prefix=_prefix(venue), code=norm)

    headers = dict(getattr(data, "_HEADERS", {}) or {})
    headers["Referer"] = "https://emweb.securities.eastmoney.com/"
    raw = data._http_get_json(url, headers=headers)

    if not isinstance(raw, dict):
        raise data.DataError("主营构成接口返回结构异常（非对象）")
    if raw.get("status") == -1:
        # 北交所 / ETF / 退市：接口明确说代码不合法，不是网络问题
        raise data.DataError(
            f"主营构成接口不支持 {code}（{raw.get('message') or '代码不合法'}）"
            f"——北交所与 ETF 无分部披露，门槛② 无法据此核实")

    rows = raw.get("zygcfx") or []
    if not isinstance(rows, list) or not rows:
        raise data.DataError("主营构成为空（该公司可能未披露分部数据）")

    # 取最新报告期：接口已按期倒序，但显式比较更稳
    dates = [str(r.get("REPORT_DATE") or "")[:10] for r in rows]
    latest = max((d for d in dates if d), default="")
    rows_at = [r for r in rows if str(r.get("REPORT_DATE") or "")[:10] == latest]

    scope = ""
    for z in (raw.get("zyfw") or []):
        if isinstance(z, dict) and z.get("BUSINESS_SCOPE"):
            scope = str(z["BUSINESS_SCOPE"])
            break

    return BusinessMix(
        code=norm, name=name, report_date=latest,
        by_product=_segments(rows_at, "2"),
        by_industry=_segments(rows_at, "1"),
        scope=scope,
    )


# ---------------------------------------------------------------- 判定

def keywords_from(text: str) -> Tuple[str, ...]:
    """从事件文本里提取业务关键词（用于与分部名做包含匹配）。

    只做**显式同义词桥接**，不做分词猜测——猜错的代价是给出错误的门槛结论，
    比"未核实"更糟。
    """
    hits: List[str] = []
    for key, alts in _SYNONYMS.items():
        if key in text:
            for alt in alts:
                if alt not in hits:
                    hits.append(alt)
    return tuple(hits)


def _is_noise(item: str) -> bool:
    return any(n in item for n in _NOISE_ITEMS)


def judge(mix: BusinessMix, event_text: str = "",
          keywords: Sequence[str] = ()) -> Threshold2Result:
    """判定门槛②。

    判定顺序（不可调）：
      1. 资本运作类事件（回购/增持/分红…）-> `na`，不据此否决
      2. 有可用关键词 -> 在分部表里找；找到则按 5% 判 pass/below，
         **找不到 -> absent**（分部披露有重要性门槛，没有 = 远低于 5%）
      3. 连关键词都没有 -> `unknown`（不猜）

    ⚠️ 第 2 步的 `absent` 是"否定证据"，不是"数据缺失"。二者区别：
    前者是"报表说你没有这块业务"，后者是"我没查到报表"。混淆会得出相反结论。
    """
    res = Threshold2Result(
        code=mix.code, report_date=mix.report_date,
        candidates=mix.top(8),
    )

    # **先把公司名从事件文本里剔掉再提关键词**（2026-11-04 实测踩到）
    # 「锡业股份」里含「锡」，若不剔除，任何关于锡业的新闻都会命中「锡」这个
    # 商品词——问锌业务也会被判成锡业务。同理「洛阳钼业」「北方稀土」等。
    text = f"{event_text}"
    clean = text
    for token in (mix.name,):
        if token and len(token) >= 2:
            clean = clean.replace(token, " ")
    kws = tuple(keywords) or keywords_from(clean)
    # 显式传入的关键词不参与公司名剔除（调用方明确指定就是要用它）
    if keywords:
        kws = tuple(keywords)
    res.keywords = kws

    # ① 资本运作类：不涉及具体业务线
    capital_hit = [w for w in _CAPITAL_EVENT_WORDS if w in text]
    if not kws and capital_hit:
        res.result = NA
        res.reason = (f"事件属资本运作（{'/'.join(capital_hit[:3])}），"
                      f"不涉及某块业务收入占比——门槛② 不适用，"
                      f"应以资金（门槛③）与止损（门槛④）为准")
        return res

    if not kws:
        res.result = UNKNOWN
        res.reason = ("未能从事件文本中提取出业务关键词，无法定位对应分部。"
                      "请补充关键词（如 event_text 写明「机器人业务」）或人工核对")
        return res

    items = mix.items
    if not items:
        res.result = UNKNOWN
        res.reason = "该公司未披露分部构成，门槛② 无法核实"
        return res

    # ② 关键词匹配（跳过"其他/合计"等杂项）
    #
    # **两个坑都踩过，这里同时规避**：
    #
    # 坑 A（漏判）：早先「任一关键词命中即取占比最大者」。
    #   事件说「电池材料回收」，泛词「电池」命中了 69% 的「动力电池系统」
    #   → 判 PASS。但问的是"回收"，跟动力电池是两回事——**泛词把不达标误判成通过**。
    #
    # 坑 B（误判）：改成「最长关键词优先」后又过头——「锡锭」(2字) 命中的是
    #   「其中:供应链业务-锡产品 4.94%」这种**带前缀的子项**，把 50% 的锡锭
    #   主业判成了「占比不足」。
    #
    # 现在的规则：
    #   1. 排除带「其中:」的细分项（它们是父项的拆分，会和父项重复计数）
    #   2. 关键词必须**整词命中分部名的主体**，优先选与事件文本贴合度更高的
    #   3. 同一关键词命中多项时取占比最大的（那是该业务的主口径）
    hits: List[Segment] = []
    for kw in kws:
        if not kw:
            continue
        cand = [seg for seg in items
                if not _is_noise(seg.item)
                and not seg.item.startswith("其中")
                and kw in seg.item]
        if cand:
            hits = cand
            break               # 按 kws 顺序（同义词表定义顺序 = 由具体到泛）取第一个有命中的

    if not hits:
        # 兜底：经营范围里出现该关键词 -> 业务沾边但未单独披露，
        # 说明占比低于披露门槛，仍是 absent（不给"未核实"的模糊结论）
        in_scope = [k for k in kws if k and k in mix.scope]
        res.result = ABSENT
        top = "、".join(f"{s.item} {s.ratio_pct:.1f}%" for s in mix.top(3))
        if in_scope:
            res.reason = (f"分部表中无「{'/'.join(in_scope)}」相关项，但经营范围含该业务——"
                          f"说明其收入低于分部披露门槛（远<{THRESHOLD_PCT:.0f}%）。"
                          f"现有分部：{top}")
        else:
            res.reason = (f"分部表与经营范围中均无「{'/'.join(kws[:3])}」相关业务。"
                          f"现有分部：{top}")
        return res

    best = max(hits, key=lambda s: s.ratio_pct)
    res.ratio_pct = best.ratio_pct
    res.matched = best.item
    if best.ratio_pct >= THRESHOLD_PCT:
        res.result = PASS
        res.reason = (f"「{best.item}」占营收 {best.ratio_pct:.2f}% >= {THRESHOLD_PCT:.0f}%，"
                      f"该业务变好会进报表（报告期 {mix.report_date}）")
    else:
        res.result = BELOW
        res.reason = (f"「{best.item}」占营收仅 {best.ratio_pct:.2f}% < {THRESHOLD_PCT:.0f}%，"
                      f"即使翻倍对报表影响也在四舍五入范围（报告期 {mix.report_date}）")
    return res


def check(code: str, event_text: str = "", name: str = "",
          keywords: Sequence[str] = ()) -> Threshold2Result:
    """一站式：取分部构成 + 判定门槛②。取数失败收敛成 `unknown`，不抛给上层。"""
    try:
        mix = fetch_mix(code, name=name)
    except data.DataError as exc:
        return Threshold2Result(code=code, result=UNKNOWN, reason=str(exc))
    except Exception as exc:            # 拿不到数据不该把工具打挂
        logger.warning("[astock] 主营构成取数失败 %s: %s", code, exc)
        return Threshold2Result(code=code, result=UNKNOWN,
                                reason=f"主营构成取数失败：{type(exc).__name__}: {exc}")
    return judge(mix, event_text=event_text, keywords=keywords)
