"""成交回单解析：把券商 App 的成交回报变成结构化成交列表。

**为什么需要它**：用户自己在券商下单后，把回单逐笔手抄进 `astock_record`
既慢又容易错（价格/数量/费用/日期四个字段），而"没时间盯盘"的人更不会
花五分钟录一笔。真实用法是**从券商 App 复制一段文字，或导入导出的 CSV**，
一次把当天所有成交登记完。

**设计取向：宽容解析 + 保守落账**
- 格式差异极大（各家列名、分隔符、中文/英文、全角/半角、千分位、括号负数），
  因此先归一化再按"多套候选策略"匹配，而不是死磕某一家；
- **识别不出的行不猜**，明确列进 `unparsed` 让用户看到（宁可少记一笔，
  也不能记错一笔——记错的成本价会污染后续所有盈亏判断）；
- 每笔都带 `confidence` 与 `evidence`（命中了哪些策略），便于核对。

支持的三类输入：
    ① 粘贴文本（一行一笔，字段用空白/逗号/制表符分隔）
    ② 券商导出 CSV/TSV（按表头列名自动映射）
    ③ 单行自然语言（"买了200股寒武纪900.5"）——仅作兜底，命中率低
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astock_market import normalize_code, round_price, to_float

#: 列名 → 语义（覆盖华泰/中信/东财/同花顺/国泰君安等常见导出命名）
_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "code": ("证券代码", "股票代码", "代码", "证券编码", "code", "stock_code", "symbol"),
    "name": ("证券名称", "股票名称", "名称", "name", "stock_name"),
    "side": ("买卖标志", "买卖方向", "操作", "委托类别", "业务名称", "side",
             "direction", "买卖", "交易类别"),
    "price": ("成交价格", "成交均价", "价格", "成交价", "price", "avg_price", "deal_price"),
    "shares": ("成交数量", "成交股数", "数量", "股数", "shares", "volume",
               "qty", "quantity", "成交笔数"),
    "amount": ("成交金额", "金额", "amount", "turnover", "发生金额"),
    "fee": ("手续费", "佣金", "费用", "交易费用", "fee", "commission", "总费用"),
    "date": ("成交日期", "发生日期", "日期", "date", "委托日期", "trade_date"),
    "time": ("成交时间", "时间", "time"),
    "stamp": ("印花税",),
    "transfer": ("过户费",),
}

_BUY_WORDS = ("买入", "买", "证券买入", "buy", "b", "担保品买入")
_SELL_WORDS = ("卖出", "卖", "证券卖出", "sell", "s", "担保品卖出")


@dataclass
class ParsedTrade:
    """解析出的一笔成交（字段可能缺失，缺失项让上层提示补全）。"""

    code: str = ""
    name: str = ""
    side: str = ""             # buy / sell
    price: Optional[float] = None
    shares: Optional[int] = None
    amount: Optional[float] = None
    fee: Optional[float] = None
    trade_date: str = ""
    raw: str = ""
    confidence: str = "high"   # high / medium / low
    evidence: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code, "name": self.name, "side": self.side,
            "price": self.price, "shares": self.shares,
            "amount": self.amount, "fee": self.fee,
            "trade_date": self.trade_date,
            "confidence": self.confidence, "problems": list(self.problems),
        }


# ---------------------------------------------------------------- 归一化


def _norm(text: str) -> str:
    """全角→半角、去千分位、统一符号（解析前必做）。"""
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:                       # 全角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:           # 全角 ASCII
            out.append(chr(code - 0xFEE0))
        elif ch in "，、":
            out.append(",")
        elif ch in "：":
            out.append(":")
        else:
            out.append(ch)
    s = "".join(out)
    s = s.replace("−", "-").replace("—", "-").replace("＋", "+")
    return s.strip()


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(text: Any) -> Optional[float]:
    """从任意文本取第一个数（支持 "+1,234.50"、"1.23万"）。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = _norm(str(text)).replace(",", "")
    if not s:
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    val = float(m.group(0))
    if "万" in s:
        val *= 10000
    return val


def _side_of(text: str) -> str:
    t = _norm(text).lower()
    if not t:
        return ""
    # 先判"卖"再看"买"：'担保品卖出' 同时含 '出'，但先匹配长词更稳
    for w in _SELL_WORDS:
        if w in t:
            return "sell"
    for w in _BUY_WORDS:
        if w in t:
            return "buy"
    return ""


_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)


def _date_of(text: str) -> str:
    s = _norm(text or "")
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if m:
            y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y}-{mo:02d}-{d:02d}"
    return ""


# ---------------------------------------------------------------- CSV/TSV


def _split_table(text: str) -> Tuple[List[str], List[List[str]]]:
    """识别分隔符并切表；返回 (表头, 数据行)。"""
    raw = _norm(text)
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []
    # 分隔符取样：制表符 / 逗号 / 连续空格
    sample = lines[0]
    for delim in ("\t", ",", "|"):
        if sample.count(delim) >= 2:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
            header = [c.strip() for c in rows[0]]
            return header, [[c.strip() for c in r] for r in rows[1:] if any(x.strip() for x in r)]
    # 空白分隔：表头按 2+ 空格或单空格切
    header = re.split(r"\s{2,}|\s+", sample.strip())
    rows = []
    for ln in lines[1:]:
        cells = re.split(r"\s{2,}|\s+", ln.strip())
        if len(cells) >= max(3, len(header) - 2):
            rows.append(cells)
    return header, rows


def _map_columns(header: Sequence[str]) -> Dict[str, int]:
    """表头 → {语义: 列号}。按别名最长匹配，避免"成交金额"被"金额"重复占位。"""
    idx: Dict[str, int] = {}
    norm_header = [_norm(h).lower() for h in header]
    # 先长别名，避免"成交金额"被"金额"抢
    pairs: List[Tuple[int, str, str]] = []
    for semantic, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            for i, h in enumerate(norm_header):
                if h and alias.lower() in h:
                    pairs.append((len(alias), semantic, str(i)))
    pairs.sort(reverse=True)
    for _l, semantic, i in pairs:
        col = int(i)
        if semantic in idx:
            continue
        if col in idx.values():
            continue
        idx[semantic] = col
    return idx


def parse_csv(text: str) -> List[ParsedTrade]:
    """从带表头的表格文本解析（券商导出的 CSV/TSV 直接粘进来即可）。"""
    header, rows = _split_table(text)
    if not header or not rows:
        return []
    col = _map_columns(header)
    if "code" not in col:
        return []
    out: List[ParsedTrade] = []
    for r in rows:
        def cell(key: str) -> str:
            i = col.get(key)
            return r[i].strip() if i is not None and i < len(r) else ""
        code = _clean_code(cell("code"))
        if not code:
            continue
        t = ParsedTrade(code=code, name=cell("name"), side=_side_of(cell("side")),
                        price=_num(cell("price")), shares=_to_int(cell("shares")),
                        amount=_num(cell("amount")), trade_date=_date_of(cell("date")),
                        raw=",".join(r), confidence="high",
                        evidence=["csv:" + ",".join(sorted(col))])
        fee_bits: List[float] = []
        for key in ("fee", "stamp", "transfer"):
            v = _num(cell(key))
            if v:
                fee_bits.append(v)
        if fee_bits:
            t.fee = round(sum(fee_bits), 2)
        _finalize(t)
        out.append(t)
    return out


def _clean_code(raw: str) -> str:
    s = _norm(raw or "")
    m = re.search(r"\d{6}", s)
    if m:
        return m.group(0)
    # 港股/美股等非 6 位代码：本插件只处理 A 股，直接丢弃
    return ""


def _to_int(raw: Any) -> Optional[int]:
    v = _num(raw)
    if v is None:
        return None
    return int(abs(v))


# ---------------------------------------------------------------- 粘贴文本


def parse_lines(text: str) -> List[ParsedTrade]:
    """按行解析（无表头场景）：每行取代码 + 方向词 + 两个数（价、量）。

    兜底策略：一行里可能有多个数字，用"哪个像价格、哪个像股数"来分配——
    价格一般带小数点，股数是整数且通常是整手（100 的倍数）。
    """
    out: List[ParsedTrade] = []
    for line in _norm(text).splitlines():
        ln = line.strip()
        if not ln or len(ln) < 6:
            continue
        # 跳过明显是表头的行
        if re.search(r"证券代码|成交|买卖", ln) and not re.search(r"\d{6}", ln):
            continue
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", ln)
        if not m:
            continue
        t = ParsedTrade(code=m.group(1), raw=ln, confidence="medium",
                        evidence=["line"])
        t.side = _side_of(ln)
        t.trade_date = _date_of(ln)
        # 去掉代码/日期后再抽数字，避免把代码当金额
        rest = ln.replace(m.group(1), " ")
        if t.trade_date:
            rest = rest.replace(t.trade_date, " ").replace(t.trade_date.replace("-", ""), " ")
        # 去掉明显的方向词/名称，避免中文被当成数字干扰位置
        tokens = [x for x in re.split(r"[\s,]+", rest) if x]
        nums = [(i, _num(x)) for i, x in enumerate(tokens)]
        nums = [(i, v) for i, v in nums if v is not None]
        t.price, t.shares = _pick_price_shares(nums)
        if t.price is None or t.shares is None:
            _finalize(t)
            out.append(t)
            continue
        _finalize(t)
        out.append(t)
    return out


def _pick_price_shares(nums: List[Tuple[int, Optional[float]]]) -> Tuple[Optional[float], Optional[int]]:
    """从一行数字里挑出「价格」和「股数」。

    难点：`9.53 2000 19060 5.10` 里 5.10 是费用、19060 是金额，都不是股数；
    而 `11.20 500 5600.00 5.00` 里若取最后一个整数会拿到 5600（金额）当股数。

    策略（按优先级，命中即用）：
      ① 价格 = 第一个带小数的数（成交价总是小数，且通常在最前面）
      ② 股数 = 价格之后、且是 100 整数倍的最小的数（金额一般远大于股数，
         且费用金额也常带小数 → 取"最小整手"比取最大更稳）
      ③ 兜底：价格之后第一个整数
    """
    if not nums:
        return None, None
    price_idx: Optional[int] = None
    price: Optional[float] = None
    for i, v in nums:
        if v is not None and abs(v - round(v)) > 1e-9 and v > 0:
            price_idx, price = i, v
            break
    if price is None:
        # 全是整数（价格写成 10 这类）：第一个数当价格
        price_idx, price = nums[0][0], nums[0][1]
    after = [(i, v) for i, v in nums if price_idx is not None and i > price_idx
             and v is not None and v > 0]
    lots = [(i, int(v)) for i, v in after if v == int(v) and v >= 100 and int(v) % 100 == 0]
    shares: Optional[int] = None
    if lots:
        # 最小整手：金额(19060)/费用(5.10) 都不是 100 倍数的"小"整数
        shares = min(lots, key=lambda x: x[1])[1]
    elif after:
        shares = int(after[0][1])
    return price, shares


# ---------------------------------------------------------------- 校验


def _finalize(t: ParsedTrade) -> None:
    """补齐/校验单笔成交。问题写进 problems（不抛错，让调用方决定）。"""
    if not t.side:
        t.problems.append("识别不出买卖方向")
    if t.shares is None or t.shares <= 0:
        t.problems.append("识别不出成交数量")
    if t.price is None or t.price <= 0:
        t.problems.append("识别不出成交价格")
    if t.shares and t.price and t.amount:
        calc = round(t.shares * t.price, 2)
        # 成交额与价*量差异 >2% 说明解析配错（如把费用当价格）
        if calc > 0 and abs(calc - t.amount) / max(calc, 1) > 0.02:
            t.problems.append(
                f"金额校验不符：{t.shares}×{t.price}={calc:.2f} 但回单金额 {t.amount:.2f}")
            t.confidence = "low"
    if t.price:
        t.price = round_price(t.price)
    if not t.trade_date:
        t.trade_date = ""      # 由调用方填今天


def parse(text: str) -> Dict[str, Any]:
    """统一入口：自动判断表格 / 逐行，返回 {trades, unparsed, method}。

    `unparsed` 是识别失败的原行——**必须展示给用户**，让他知道漏了什么，
    而不是静默丢弃（漏一笔持仓就少一笔，后面对账全乱）。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"trades": [], "unparsed": [], "method": "empty"}

    trades: List[ParsedTrade] = []
    method = ""
    if "\n" in raw:
        csv_trades = parse_csv(raw)
        if csv_trades:
            trades, method = csv_trades, "csv"
    if not trades:
        line_trades = parse_lines(raw)
        if line_trades:
            trades, method = line_trades, "lines"

    # 同一笔重复出现（粘贴两次）→ 去重，但保留计数提示
    seen: Dict[Tuple[str, str, float, int], int] = {}
    deduped: List[ParsedTrade] = []
    dups = 0
    for t in trades:
        key = (t.code, t.side, t.price or 0, t.shares or 0)
        if key in seen:
            dups += 1
            continue
        seen[key] = 1
        deduped.append(t)

    bad = [t for t in deduped if not t.ok]
    unparsed = [t.raw for t in bad]
    return {
        "trades": deduped,
        "unparsed": unparsed,
        "method": method or "none",
        "duplicates_removed": dups,
        "usable": [t for t in deduped if t.ok],
    }
