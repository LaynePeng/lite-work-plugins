"""A 股市场规则：代码规范化 / 板块判定 / 涨跌停 / 费用 / 交易时段 / 数量单位。

**为什么这些必须是代码而不是让模型算**：
- 涨跌停价按板块不同（主板 10%、创业板·科创板 20%、北交所 30%、ST 5%），
  且"四舍五入到分"的口径决定委托价能不能成交——模型手算极易错一位；
- 佣金有"最低 5 元"、印花税卖出单边 0.05%、过户费沪深两市规则不同，
  算错会让成本价失真，进而让所有盈亏判断跟着错；
- 买入必须 100 股整数倍（科创板 200 股起、北交所 100 股起）、卖出可以
  零股一次性卖完——这些是硬约束，写错就是废单。

设计取舍：本模块**零网络、零第三方依赖**（纯标准库），因此可以被单测
完整覆盖，也不会给打包版引入 wheels 需求。
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 板块

BOARD_MAIN = "main"          # 沪深主板
BOARD_GEM = "gem"            # 创业板（300/301）
BOARD_STAR = "star"          # 科创板（688/689）
BOARD_BJ = "bj"              # 北交所（4xx/8xx/920）
BOARD_ETF = "etf"            # 场内基金（5xx 沪 / 15x·16x 深）
BOARD_INDEX = "index"        # 指数
BOARD_UNKNOWN = "unknown"

BOARD_LABELS: Dict[str, str] = {
    BOARD_MAIN: "沪深主板",
    BOARD_GEM: "创业板",
    BOARD_STAR: "科创板",
    BOARD_BJ: "北交所",
    BOARD_ETF: "场内基金",
    BOARD_INDEX: "指数",
    BOARD_UNKNOWN: "未知",
}

#: 各板块默认涨跌幅限制（百分比）。ST 单独按 _st_limit 处理。
_BOARD_LIMIT: Dict[str, float] = {
    BOARD_MAIN: 10.0,
    BOARD_GEM: 20.0,
    BOARD_STAR: 20.0,
    BOARD_BJ: 30.0,
    BOARD_ETF: 10.0,
    BOARD_INDEX: 0.0,      # 指数无涨跌停
    BOARD_UNKNOWN: 10.0,
}


def board_of(code: str) -> str:
    """6 位代码 → 板块。识别失败返回 BOARD_UNKNOWN（不抛错）。"""
    c = (code or "").strip()
    if not c.isdigit() or len(c) != 6:
        return BOARD_UNKNOWN
    if c.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return BOARD_MAIN
    if c.startswith(("300", "301")):
        return BOARD_GEM
    if c.startswith(("688", "689")):
        return BOARD_STAR
    if c.startswith(("430", "83", "87", "88", "889", "920")):
        return BOARD_BJ
    if c.startswith(("510", "511", "512", "513", "515", "518", "588", "159", "161", "162")):
        return BOARD_ETF
    if c.startswith(("000", "001", "399", "899", "999")):
        # 指数（000001 上证 / 399001 深证 / 899050 北证50）——与主板同码段，
        # 由调用方传入 kind="index" 时优先；纯代码无法可靠区分，按主板返回。
        return BOARD_MAIN
    if c.startswith(("5", "15", "16")):
        return BOARD_ETF
    return BOARD_UNKNOWN


def limit_pct(code: str, name: str = "", kind: str = "stock") -> float:
    """涨跌幅限制（百分比）：板块 + ST 双重判断。

    ST / *ST / SST / S*ST 一律 5%（主板）。注册制板块（创业/科创/北交）
    的 ST 也适用各自板块比例，但这类标的极少，这里统一按 5% 处理并
    由调用方通过 `limit_ratio` 返回值提示不确定性。
    """
    if kind == "index":
        return 0.0
    board = board_of(code)
    if kind == "etf" or board == BOARD_ETF:
        return 10.0
    up = (name or "").upper().replace(" ", "")
    # ST 判定：名称里出现 ST（含 *ST、SST、S*ST）即视为风险警示股
    if "ST" in up:
        return 5.0
    return _BOARD_LIMIT.get(board, 10.0)


def limit_prices(prev_close: float, code: str, name: str = "",
                 kind: str = "stock") -> Tuple[Optional[float], Optional[float], float]:
    """(涨停价, 跌停价, 限制比例)。四舍五入到分——与交易所口径一致。

    prev_close 缺失（<=0）时返回 (None, None, pct)。
    """
    pct = limit_pct(code, name, kind)
    if pct <= 0 or not prev_close or prev_close <= 0:
        return None, None, pct
    up = round(prev_close * (1 + pct / 100.0) + 1e-9, 2)
    down = round(prev_close * (1 - pct / 100.0) + 1e-9, 2)
    return up, down, pct


# ---------------------------------------------------------------- 费用


@dataclass
class FeeConfig:
    """费用口径（默认值对齐 2026 年主流券商，用户可在设置页改）。

    - commission_rate：佣金率，万 2.5
    - min_commission：单笔最低佣金 5 元（多数券商）
    - stamp_tax_rate：印花税，卖出单边 0.05%
    - transfer_fee_rate：过户费，沪深统一 0.001%（双向）
    - other_rate：其他杂费（可选，默认 0）
    """

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    other_rate: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "other_rate": self.other_rate,
        }


@dataclass
class FeeBreakdown:
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    other: float = 0.0

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax +
                     self.transfer_fee + self.other, 4)

    def as_dict(self) -> Dict[str, float]:
        return {
            "commission": round(self.commission, 4),
            "stamp_tax": round(self.stamp_tax, 4),
            "transfer_fee": round(self.transfer_fee, 4),
            "other": round(self.other, 4),
            "total": self.total,
        }


def calc_fee(amount: float, side: str, cfg: Optional[FeeConfig] = None,
             board: str = BOARD_MAIN) -> FeeBreakdown:
    """按成交金额算费用。side: "buy" | "sell"。

    指数不算费（不可交易）；ETF 免印花税、免过户费（场内基金口径）。
    """
    cfg = cfg or FeeConfig()
    if amount <= 0:
        return FeeBreakdown()
    if board == BOARD_INDEX:
        return FeeBreakdown()
    commission = max(amount * cfg.commission_rate, cfg.min_commission)
    stamp = 0.0
    transfer = 0.0
    if board != BOARD_ETF:
        if side == "sell":
            stamp = amount * cfg.stamp_tax_rate
        transfer = amount * cfg.transfer_fee_rate
    other = amount * cfg.other_rate
    return FeeBreakdown(commission=commission, stamp_tax=stamp,
                        transfer_fee=transfer, other=other)


# ---------------------------------------------------------------- 数量单位


def lot_size(code: str) -> int:
    """最小买入单位（股）。科创板 200 股起、北交所 100 股起，其余 100 股。"""
    board = board_of(code)
    if board == BOARD_STAR:
        return 200
    return 100


def round_lot(shares: float, code: str) -> int:
    """把股份数向下取整到合法买入单位（科创板 200 的整数倍，允许 1 股递增
    的规则自 2023 年起已取消，统一按整手）。"""
    step = lot_size(code)
    if shares <= 0:
        return 0
    return int(shares // step) * step


def check_order_size(shares: int, code: str, side: str,
                     holding: int = 0) -> Optional[str]:
    """校验申报数量；合法返回 None，不合法返回中文原因。"""
    if shares <= 0:
        return "数量必须大于 0"
    if side == "buy":
        step = lot_size(code)
        if shares % step != 0:
            return f"买入数量必须是 {step} 股的整数倍（本次 {shares} 股）"
        if shares < step:
            return f"买入数量不足最小单位 {step} 股"
    else:
        if holding and shares > holding:
            return f"卖出数量 {shares} 股超过持仓 {holding} 股"
        # 卖出允许零股，但只有在一次性清掉剩余零股时才合法
        if shares % 100 != 0 and shares != holding:
            return (f"卖出零股（{shares % 100} 股部分）只能在一次性卖出全部 "
                    f"剩余持仓时申报")
    return None


# ---------------------------------------------------------------- 交易时段

#: 连续竞价 + 集合竞价时段（本地时区，A 股为 UTC+8）
SESSIONS: List[Tuple[str, str, str]] = [
    ("09:15", "09:25", "开盘集合竞价"),
    ("09:25", "09:30", "集合竞价撮合"),
    ("09:30", "11:30", "上午连续竞价"),
    ("13:00", "14:57", "下午连续竞价"),
    ("14:57", "15:00", "收盘集合竞价"),
]


def _now_cn() -> _dt.datetime:
    """北京时间（UTC+8，不依赖 tzdata）。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=8)))


def is_trading_day(day: Optional[_dt.date] = None) -> bool:
    """粗判交易日：仅排除周六日。法定节假日/调休需要外部日历，
    因此行情返回的 `trade_time` 才是"是否最新"的最终依据（见 astock_data）。"""
    day = day or _now_cn().date()
    return day.weekday() < 5


def should_watch_now(now: Optional[_dt.datetime] = None,
                     lead_minutes: int = 5,
                     lag_minutes: int = 5) -> Tuple[bool, str]:
    """盯盘时刻判断：现在该不该让盯梢源真的去查行情。

    **这个函数是"何时开始盯"的答案**，也是省 token 的关键：非交易时段直接
    返回 False，盯梢源立刻返回"无变化"，不会唤醒 LLM。

    返回 (是否盯, 原因说明)。

    口径：
    - 只在**交易日**盯（周末不算；法定节假日靠行情返回的 trade_time 兜底，
      这里宁可多跑一次也不擅自 skip——漏报比多一次请求更糟）；
    - 时段覆盖 09:15–15:00 并各留 `lead/lag` 分钟缓冲：集合竞价（9:15）与
      尾盘（14:57–15:00）正是条件单容易触发的时刻，掐掉会漏报；
    - 午休 11:30–13:00 不盯（价格不动，盯了纯浪费）。
    """
    now = now or _now_cn()
    day = now.date()
    if day.weekday() >= 5:
        return False, "周末休市，无需盯盘"
    hhmm = now.strftime("%H:%M")
    # 边界：集合竞价 09:15 开始（提前量只在"刚好卡点"时生效，不能把 9:10
    # 也算成盘中——那是错误信号，会让用户在开盘前收到"盘中异动"）；
    # 收盘 15:00 之后一律不盯（尾盘集合竞价到 15:00 结束，15:00 后价格冻结）。
    start = f"{9:02d}:{max(0, 15 - max(0, lead_minutes)):02d}"
    if hhmm < start:
        return False, f"开盘前（早于 {start}）"
    if hhmm >= "15:00":
        return False, "已收盘（15:00 后价格不再变动）"
    if "11:30" <= hhmm < "13:00":
        return False, "午间休市"
    st = trading_status(now)
    return True, f"{st['phase']}"


def next_watch_window(now: Optional[_dt.datetime] = None) -> str:
    """下一次该盯盘的时间（给人看的说明，用于面板/状态展示）。"""
    now = now or _now_cn()
    today = now.date()
    hhmm = now.strftime("%H:%M")
    if is_trading_day(today):
        if hhmm < "09:15":
            return f"今天 09:15"
        if "11:30" <= hhmm < "13:00":
            return "今天 13:00"
        if hhmm < "15:00":
            return "现在（盘中）"
    # 找下一个工作日
    d = today + _dt.timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5:
            return f"{d.isoformat()} 09:15"
        d += _dt.timedelta(days=1)
    return "下一个交易日 09:15"


def trading_status(now: Optional[_dt.datetime] = None) -> Dict[str, object]:
    now = now or _now_cn()
    hhmm = now.strftime("%H:%M")
    phase = "休市"
    is_open = False
    if is_trading_day(now.date()):
        for start, end, label in SESSIONS:
            if start <= hhmm < end:
                phase = label
                is_open = True
                break
        else:
            if hhmm < "09:15":
                phase = "开盘前"
            elif hhmm >= "15:00":
                phase = "已收盘"
            elif "11:30" <= hhmm < "13:00":
                phase = "午间休市"
    else:
        phase = "非交易日（周末）"
    return {
        "open": is_open,
        "phase": phase,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "is_trading_day": is_trading_day(now.date()),
    }


# ---------------------------------------------------------------- 代码规范化

_VENUE_PREFIX = {"sh": "sh", "sz": "sz", "bj": "bj"}
_CODE_RE = re.compile(r"(?P<code>\d{6})")


def guess_venue(code: str, kind: str = "stock") -> str:
    """6 位代码 → 交易所前缀（sh / sz / bj），用于腾讯/新浪行情接口。

    指数与个股同码段冲突（000001 既是平安银行也是上证指数），因此指数
    调用方必须显式传 kind="index"，否则按个股处理。
    """
    c = (code or "").strip()
    if kind == "index":
        if c.startswith(("000", "880", "999", "5")):
            return "sh"
        if c.startswith(("399", "899")):
            return "sz" if c.startswith("399") else "bj"
    board = board_of(c)
    if board == BOARD_BJ:
        return "bj"
    if c.startswith(("6", "5", "9")):
        return "sh"
    return "sz"


def normalize_code(raw: str, kind: str = "stock") -> Tuple[str, str]:
    """任意输入 → (6 位代码, 交易所前缀)。

    接受 "600000" / "sh600000" / "SH600000" / "600000.SH" / "sz.000001"。
    无法识别时抛 ValueError（报错信息给出可操作提示）。
    """
    text = str(raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("代码为空")
    # 600000.sh / 000001.sz / sz.000001 三种写法都要认
    if "." in text:
        left, _, right = text.rpartition(".")
        if right in _VENUE_PREFIX:          # 000001.sz
            text = right + left
        elif left in _VENUE_PREFIX:         # sz.000001
            text = left + right
        else:
            text = left
    prefix = ""
    for p in _VENUE_PREFIX:
        if text.startswith(p):
            prefix, text = p, text[len(p):]
            break
    m = _CODE_RE.search(text)
    if not m:
        raise ValueError(f"无法识别代码 {raw!r}，请用 6 位代码（如 600000）或带交易所前缀"
                         f"（如 sh600000 / 000001.sz）")
    code = m.group("code")
    venue = prefix or guess_venue(code, kind)
    # 校验交易所与代码是否矛盾（sh300xxx 这类输入多半是手误）
    expect = guess_venue(code, kind)
    if prefix and prefix != expect and board_of(code) != BOARD_INDEX:
        raise ValueError(f"代码 {code} 与交易所前缀 {prefix} 不匹配（应为 {expect}）")
    return code, venue


def tencent_symbol(code: str, venue: str) -> str:
    """腾讯行情接口的标的形式：sh600000 / sz000001 / bj430047。"""
    return f"{venue}{code}"


# ---------------------------------------------------------------- 价格处理


def to_float(value: object, default: Optional[float] = 0.0) -> Optional[float]:
    """宽容的数值转换（接口常返回 "" / "-" / "0.00" 占位）。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else default
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "null", "N/A"}:
        return default
    try:
        f = float(text)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def round_price(p: float) -> float:
    """A 股报价最小变动单位 0.01 元。"""
    return round(float(p) + 1e-9, 2)


def pct_change(new: float, old: float) -> Optional[float]:
    if not old:
        return None
    return round((new - old) / old * 100.0, 2)
