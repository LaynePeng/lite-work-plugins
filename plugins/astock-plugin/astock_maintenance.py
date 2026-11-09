"""台账导出 / 导入：让两边（本插件与 DSH 版 A 股助手 dsh-astock）互相认账。

────────────────────────────────────────────────────────────────────────
这份模块存在的唯一理由：**用户自己的数据必须能带走**
────────────────────────────────────────────────────────────────────────
换机器、重装插件、误删台账时，数据只活在 `ledger.sqlite` 里是不够的——
用户不会用 sqlite3 命令行，也不该被要求会。所以这里提供三种可读文件：

    astock-ledger-*.json     完整快照（资金 + 持仓 + 流水 + 自选）
    astock-trades-*.csv      成交 CSV（中文表头 + BOM，Excel 双击不乱码）
    astock-watchlist-*.json  自选股

格式**刻意与 dsh-astock 的 `lib/js/maintenance.js` 逐字对齐**
（`format: "dsh-astock-ledger/1"`、同名字段、同一套 CSV 表头与换行）。
两边共用同一个 SQLite 表结构，导出文件再同格式，才能做到「一边导出、
另一边直接导入」；格式只要差一个字段名，这套互通就是假的。

────────────────────────────────────────────────────────────────────────
导入的三条口径（改错任意一条都会静默污染台账）
────────────────────────────────────────────────────────────────────────
① **持仓以流水回放为准，不按文件里的持仓字段直接覆盖。**
   文件里的 positions 只在「该标的没有任何流水」时按手工持仓写回。
   若直接导入持仓，账面上看是对的，但下一次卖出算出的已实现盈亏一定是
   错的——成本基准与流水对不上，而且这种错在界面上看不出来。

② **重复流水按 (日期, 代码, 方向, 价格保留 3 位, 数量) 跳过。**
   同一份文件导入两次不会记两遍；价格取 3 位是因为 A 股成交价最小单位
   到厘，导出/导入两侧都按同一精度比较。

③ **资金不随成交自动扣减，只有全量/资金+自选两种范围会覆盖文件里的资金。**
   插件看不见银行转账与出入金，自动扣现金同样会算错。`scope=trades`
   只回放流水，资金保持不动，适合「补录历史成交」。

范围（scope）与 dsh-astock 完全一致：

    all        —— 成交 + 资金 + 自选（持仓由成交回放得出）
    watch_cash —— 只恢复资金与自选（不碰流水与持仓）
    trades     —— 只回放成交（资金不动）
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astock_ledger import Ledger, LedgerError
from astock_market import (
    board_of, calc_fee, check_order_size, normalize_code,
)

#: 完整台账 JSON 的格式标记（与 dsh-astock 的 EXPORT_FORMAT 逐字一致）。
EXPORT_FORMAT = "dsh-astock-ledger/1"

#: 自选 JSON 的格式标记（dsh-astock 的 watchlist 导出同样不校验它，仅作标识）。
WATCHLIST_FORMAT = "dsh-astock-watchlist/1"

#: 单次导入的行数上限：再多的成交应该分批，一次性回放既慢又难核对。
MAX_IMPORT_ROWS = 2000

#: 导入文件体积上限（20MB）。台账几千笔流水也远小于这个量级，
#: 超过多半是选错了文件（比如把 sqlite 当 JSON 传进来）。
MAX_IMPORT_BYTES = 20 * 1024 * 1024

#: 支持的导入范围（与 dsh-astock 的 scope 一致）。
SCOPES = ("all", "watch_cash", "trades")

#: CSV 表头（与 dsh-astock 的 `tradesCsv` 逐字一致，含顺序）：
#: 中文表头是为了用户双击用 Excel 打开时能直接看懂。
_CSV_HEADER = ("成交日期", "代码", "名称", "方向", "成交价", "数量",
               "成交金额", "费用", "已实现盈亏", "备注")

#: 与 dsh-astock 的 HEADER_ALIASES 一致（中英文都认，大小写/空格不敏感）。
_HEADER_ALIASES: Dict[str, Tuple[str, ...]] = {
    "trade_date": ("trade_date", "date", "成交日期", "交易日期", "日期"),
    "code": ("code", "symbol", "代码", "证券代码", "股票代码"),
    "side": ("side", "direction", "方向", "买卖", "买卖方向"),
    "price": ("price", "成交价", "成交价格", "价格"),
    "shares": ("shares", "quantity", "qty", "数量", "成交数量", "股数"),
    "fee": ("fee", "费用", "手续费", "佣金"),
    "name": ("name", "名称", "股票名称"),
    "note": ("note", "备注"),
}

_BUY_WORDS = frozenset(("buy", "b", "买入", "买"))
_SELL_WORDS = frozenset(("sell", "s", "卖出", "卖"))


# ====================================================================== 基础工具


def _now_text() -> str:
    """当前时间（本地时区），与 dsh-astock 的 `nowText()` 同格式。"""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    """文件名时间戳，与 dsh-astock 的 `stamp()` 同格式。"""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _num(value: Any) -> Optional[float]:
    """宽松转数字：None/布尔/空串一律 None，不用 0 冒充缺失值。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return n if _finite(n) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        n = float(text)
    except (TypeError, ValueError):
        return None
    return n if _finite(n) else None


def _finite(n: float) -> bool:
    return n == n and n not in (float("inf"), float("-inf"))


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return None if n is None else int(n)


def _round(value: Any, digits: int = 2) -> Optional[float]:
    n = _num(value)
    return None if n is None else round(n, digits)


def _json_loads(text: Any, default: Any = None) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) and text else default
    except (TypeError, ValueError):
        return default


def _normalize_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """导出前把 fee_detail 由 JSON 字符串还原成对象（与 dsh-astock 一致）。"""
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("fee_detail"), str):
            item["fee_detail"] = _json_loads(item["fee_detail"], {})
        out.append(item)
    return out


def _side_cn(side: str) -> str:
    """导出 CSV 时的方向文案（与 dsh-astock 一致：buy→买入，其余→卖出）。"""
    return "买入" if str(side).lower() == "buy" else "卖出"


def _norm_side(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in _BUY_WORDS:
        return "buy"
    if text in _SELL_WORDS:
        return "sell"
    return ""


def _norm_scope(scope: Any) -> str:
    text = str(scope or "all").strip().lower()
    return text if text in SCOPES else "all"


#: 对外公开的 scope 归一化入口（mixin 不该 call 带下划线的私有函数）。
normalize_scope = _norm_scope


def trade_key(trade_date: Any, code: Any, side: Any, price: Any,
              shares: Any) -> str:
    """流水去重键（与 dsh-astock 的 existingKeys 同口径）。

    价格保留 3 位：A 股成交价最小单位到厘，两侧用同一精度才认得出重复。
    """
    return "|".join((
        str(trade_date or ""),
        str(code or ""),
        str(side or ""),
        str(_round(price, 3) or 0.0),
        str(_int(shares) or 0),
    ))


# ====================================================================== 导出


def _csv_text(rows: Sequence[Dict[str, Any]]) -> str:
    """成交 CSV：中文表头 + UTF-8 BOM + CRLF（与 dsh-astock 逐字一致）。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_CSV_HEADER)
    for row in rows:
        realized = row.get("realized")
        writer.writerow([
            row.get("trade_date") or "",
            row.get("code") or "",
            row.get("name") or "",
            _side_cn(str(row.get("side") or "")),
            row.get("price") if row.get("price") is not None else "",
            row.get("shares") if row.get("shares") is not None else "",
            row.get("amount") if row.get("amount") is not None else "",
            row.get("fee") if row.get("fee") is not None else "",
            "" if realized is None else realized,
            row.get("note") or "",
        ])
    # Excel 只有看到 BOM 才按 UTF-8 打开；没有 BOM 的中文 CSV 双击会乱码。
    return "\ufeff" + buf.getvalue()


def export_payload(ledger: Ledger, kind: str = "ledger.json") -> Dict[str, Any]:
    """生成导出内容（不写盘；写盘由 `write_export` 做）。

    kind 与 dsh-astock 的 `exportPayload` 同一套别名：
        ledger.json / ledger            → 完整快照
        trades.csv / trades / csv       → 成交 CSV
        watchlist.json / watchlist / watch → 自选 JSON
    未知 kind 退回完整快照（与 dsh-astock 行为一致，避免用户拿不到数据）。
    """
    rows = ledger.export_rows()
    key = str(kind or "ledger.json").strip().lower()

    if key in ("trades.csv", "trades", "csv"):
        trade_rows = list(rows.get("trades") or [])
        return {
            "ok": True,
            "kind": "trades",
            "filename": f"astock-trades-{_stamp()}.csv",
            "content_type": "text/csv; charset=utf-8",
            "content": _csv_text(trade_rows),
            "rows": len(trade_rows),
            "summary": {"trades": len(trade_rows)},
        }

    if key in ("watchlist.json", "watchlist", "watch"):
        watch_rows = [{
            "code": str(w.get("code") or ""),
            "name": str(w.get("name") or ""),
            "note": str(w.get("note") or ""),
            "added_at": str(w.get("added_at") or ""),
        } for w in (rows.get("watchlist") or [])]
        payload = {
            "format": WATCHLIST_FORMAT,
            "exportedAt": _now_text(),
            "watchlist": watch_rows,
        }
        return {
            "ok": True,
            "kind": "watchlist",
            "filename": f"astock-watchlist-{_stamp()}.json",
            "content_type": "application/json; charset=utf-8",
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
            "rows": len(watch_rows),
            "summary": {"watchlist": len(watch_rows)},
        }

    cash_row = (rows.get("cash") or [{}])[0] or {}
    positions = _normalize_rows(rows.get("positions") or [])
    trades = _normalize_rows(rows.get("trades") or [])
    watchlist = _normalize_rows(rows.get("watchlist") or [])
    payload = {
        "format": EXPORT_FORMAT,
        "exportedAt": _now_text(),
        "cash": _round(cash_row.get("balance"), 2) or 0.0,
        "positions": positions,
        "trades": trades,
        "watchlist": watchlist,
    }
    return {
        "ok": True,
        "kind": "ledger",
        "filename": f"astock-ledger-{_stamp()}.json",
        "content_type": "application/json; charset=utf-8",
        "content": json.dumps(payload, ensure_ascii=False, indent=2),
        "rows": len(trades),
        "summary": {
            "cash": payload["cash"],
            "positions": len(positions),
            "trades": len(trades),
            "watchlist": len(watchlist),
        },
    }


def export_dir(ledger: Ledger) -> str:
    """导出目录：与台账同目录的 `exports/`（换个台账路径时不会散落别处）。"""
    return os.path.join(os.path.dirname(os.path.abspath(ledger.path)), "exports")


def write_export(ledger: Ledger, kind: str = "ledger.json") -> Dict[str, Any]:
    """导出到磁盘，返回带 `path` 的结果（对话里给出可执行的路径）。"""
    payload = export_payload(ledger, kind)
    if not payload.get("ok"):
        return payload
    try:
        out_dir = export_dir(ledger)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, str(payload["filename"]))
        # newline=""：不做换行转换，CSV 的 CRLF 原样落盘（Excel 才认）。
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(str(payload.get("content") or ""))
    except OSError as exc:
        return {"ok": False, "message": f"写入导出文件失败：{exc}"}
    payload["path"] = path
    return payload


# ====================================================================== 读入


def read_import_text(text: Any = "", path: Any = "") -> Dict[str, Any]:
    """取导入原文：优先 text（对话里直接粘），其次 path（读用户给的文件）。

    编码按 UTF-8（含 BOM）优先；CSV 若是 Excel 另存成 GBK，退回 gbk 再试——
    这两种是 Windows 用户最常遇到的保存方式。
    """
    if isinstance(text, str) and text.strip():
        return {"ok": True, "text": text}
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"ok": False,
                "message": "需要 text（文件内容）或 path（文件路径）二选一"}
    full = os.path.abspath(os.path.expanduser(raw_path))
    if not os.path.isfile(full):
        return {"ok": False, "message": f"找不到文件：{full}"}
    try:
        size = os.path.getsize(full)
        if size > MAX_IMPORT_BYTES:
            return {"ok": False,
                    "message": (f"文件过大（{size / 1024 / 1024:.1f}MB），"
                                f"上限 {MAX_IMPORT_BYTES // 1024 // 1024}MB；"
                                f"请确认选的是导出的 JSON/CSV，而不是 sqlite 台账")}
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"ok": False, "message": f"读取文件失败：{exc}"}
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return {"ok": True, "text": raw.decode(encoding), "file": full}
        except UnicodeDecodeError:
            continue
    return {"ok": False, "message": f"无法识别文件编码：{full}（请存成 UTF-8）"}


# ====================================================================== 台账 JSON


def parse_ledger_json(text: Any) -> Dict[str, Any]:
    """解析导出的台账 JSON（`astock-ledger-*.json`）。

    与 dsh-astock 的 `parseLedgerJson` 同语义：格式标记不符只给警告、不拒绝——
    用户手改过文件或从旧版本导出的数据，能救回来比严格校验更重要。
    """
    raw = str(text or "").strip().lstrip("\ufeff").strip()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError as exc:
        msg = f"不是合法 JSON：{exc}"
        return {"ok": False, "errors": [msg], "message": msg}
    if not isinstance(payload, dict):
        msg = "内容必须是一个 JSON 对象"
        return {"ok": False, "errors": [msg], "message": msg}

    errors: List[str] = []
    fmt = str(payload.get("format") or "")
    if fmt and fmt not in (EXPORT_FORMAT, WATCHLIST_FORMAT):
        errors.append(f"格式标记是 {fmt}，本插件期望 {EXPORT_FORMAT}（仍会尽力导入）")

    positions = payload.get("positions")
    trades = payload.get("trades")
    watchlist = payload.get("watchlist")
    positions = positions if isinstance(positions, list) else []
    trades = trades if isinstance(trades, list) else []
    watchlist = watchlist if isinstance(watchlist, list) else []
    cash = _round(payload.get("cash"), 2)

    if not positions and not trades and not watchlist and cash is None:
        msg = "文件里没有持仓 / 成交 / 自选 / 资金，无法导入"
        return {"ok": False, "errors": errors + [msg], "message": msg}
    return {
        "ok": True,
        "errors": errors,
        "payload": {
            "format": fmt,
            "exportedAt": str(payload.get("exportedAt") or ""),
            "cash": cash,
            "positions": positions,
            "trades": trades,
            "watchlist": watchlist,
        },
    }


def _valid_code(raw: Any) -> str:
    """任意写法 → 6 位代码；识别不了返回空串（由调用方记错误/跳过）。"""
    try:
        code, _ = normalize_code(str(raw or ""))
        return code
    except (TypeError, ValueError):
        return ""


def preview_ledger_import(ledger: Ledger, text: Any,
                          scope: Any = "all") -> Dict[str, Any]:
    """预览全量导入：把「会发生什么」算清楚，**不落账**。

    与 dsh-astock 的 `previewLedgerImport` 同口径：持仓按 incoming 与当前
    台账比对；成交按去重键分「新增 / 重复」；自选只算新增；资金给出前后值。
    """
    parsed = parse_ledger_json(text)
    if not parsed.get("ok"):
        return {"ok": False, "errors": parsed.get("errors") or [],
                "message": parsed.get("message") or "解析失败"}
    data = parsed["payload"]
    mode = _norm_scope(scope)

    current_positions = {str(p["code"]): p for p in ledger.list_positions()}
    current_watch = {str(w["code"]) for w in ledger.list_watch()}
    cash_before = float((ledger.get_cash() or {}).get("balance") or 0.0)

    incoming: Dict[str, Dict[str, Any]] = {}
    for pos in data["positions"]:
        if not isinstance(pos, dict):
            continue
        code = _valid_code(pos.get("code"))
        if code:
            incoming[code] = pos

    trades_in_file: List[Dict[str, Any]] = []
    for trade in data["trades"]:
        if not isinstance(trade, dict):
            continue
        code = _valid_code(trade.get("code"))
        if code:
            trades_in_file.append({**trade, "code": code})
    traded_codes = {str(t["code"]) for t in trades_in_file}

    add: List[Dict[str, Any]] = []
    update: List[Dict[str, Any]] = []
    keep: List[Dict[str, Any]] = []
    if mode != "watch_cash":
        for code, pos in incoming.items():
            before = current_positions.get(code)
            shares_after = _int(pos.get("shares")) or 0
            if before is None:
                add.append({"code": code, "shares": shares_after})
            elif (_int(before.get("shares")) or 0) != shares_after:
                update.append({
                    "code": code,
                    "shares_before": _int(before.get("shares")) or 0,
                    "shares_after": shares_after,
                })
        # ⚠️ 导入是**合并**，不是快照覆盖：台账里已有、文件里没有的持仓
        # **不会被删除**（删除不可逆，而"少了一份文件"最常见的后果是用户
        # 拿旧备份来合并，不是想清空账户）。这里单列为 keep，预览必须如实
        # 说"保留"，绝不能像早期设计那样写着"清 N 只"却什么都不删。
        for code in current_positions:
            if code not in incoming and code not in traded_codes:
                keep.append({"code": code,
                             "shares": _int(current_positions[code].get("shares")) or 0})

    existing = ledger.trade_dedupe_keys()
    duplicate = 0
    new_trades: List[Dict[str, Any]] = []
    if mode != "watch_cash":
        for trade in trades_in_file:
            key = trade_key(trade.get("trade_date"), trade["code"],
                            _norm_side(trade.get("side")) or trade.get("side"),
                            trade.get("price"), trade.get("shares"))
            if key in existing:
                duplicate += 1
            else:
                new_trades.append(trade)

    watch_add = []
    for item in data["watchlist"]:
        if not isinstance(item, dict):
            continue
        code = _valid_code(item.get("code"))
        if code and code not in current_watch:
            watch_add.append(code)

    # 文件里没带资金（例如纯自选 JSON）时导入**不会改**资金，
    # 预览必须显示"不变"而不是 0 —— 否则用户会以为余额要被清零。
    if mode == "trades" or data["cash"] is None:
        cash_after = cash_before
        cash_note = "资金不变" + ("（文件里没有资金）" if data["cash"] is None
                                  and mode != "trades" else "")
    else:
        cash_after = data["cash"]
        cash_note = f"资金 {cash_before} → {data['cash']}"
    scope_cn = {"all": "全量", "watch_cash": "仅资金+自选", "trades": "仅成交"}[mode]
    message = (f"预览（{scope_cn}）："
               f"持仓 +{len(add)} / 改 {len(update)} / 保留 {len(keep)}，"
               f"成交新增 {len(new_trades)}（重复 {duplicate}），"
               f"自选 +{len(watch_add)}，{cash_note}")
    return {
        "ok": True,
        "scope": mode,
        "exported_at": data["exportedAt"],
        "errors": parsed.get("errors") or [],
        "positions": {"add": add, "update": update, "keep": keep},
        "trades": {"in_file": len(trades_in_file),
                   "new": len(new_trades), "duplicate": duplicate},
        "watchlist": {"in_file": len(data["watchlist"]), "add": watch_add},
        "cash": {"before": cash_before, "after": cash_after},
        "message": message,
    }


def import_ledger(ledger: Ledger, text: Any, scope: Any = "all") -> Dict[str, Any]:
    """执行全量导入。`scope` 决定恢复哪些部分（与 dsh-astock 一致）。

    all        —— 成交 + 资金 + 自选（持仓由成交回放得出）
    watch_cash —— 只恢复资金与自选（不碰流水与持仓）
    trades     —— 只回放成交（资金不动，方便「流水补录」）
    """
    parsed = parse_ledger_json(text)
    if not parsed.get("ok"):
        return {"ok": False, "message": parsed.get("message") or "解析失败",
                "errors": parsed.get("errors") or []}
    data = parsed["payload"]
    mode = _norm_scope(scope)
    errors: List[str] = list(parsed.get("errors") or [])
    applied: Dict[str, Any] = {
        "trades": 0, "skipped": 0, "failed": [], "watch": 0,
        "cash": None, "positions": 0,
    }

    if mode != "watch_cash":
        existing = ledger.trade_dedupe_keys()
        rows = [t for t in data["trades"] if isinstance(t, dict) and t.get("code")]
        # 与 dsh-astock 一致：按日期正序重放（稳定排序，同日保持文件顺序），
        # 否则先卖后买会让「可卖数量」校验误报。
        rows.sort(key=lambda t: str(t.get("trade_date") or ""))
        for trade in rows:
            code = _valid_code(trade.get("code"))
            if not code:
                applied["failed"].append({
                    "code": str(trade.get("code") or ""),
                    "error": "代码无法识别",
                })
                continue
            key = trade_key(trade.get("trade_date"), code,
                            _norm_side(trade.get("side")) or trade.get("side"),
                            trade.get("price"), trade.get("shares"))
            if key in existing:
                applied["skipped"] += 1
                continue
            try:
                ledger.record_trade(
                    code=code,
                    side=_norm_side(trade.get("side")) or str(trade.get("side") or ""),
                    price=float(_num(trade.get("price")) or 0),
                    shares=int(_int(trade.get("shares")) or 0),
                    trade_date=str(trade.get("trade_date") or ""),
                    name=str(trade.get("name") or ""),
                    note=str(trade.get("note") or ""),
                    fee_override=_num(trade.get("fee")),
                )
            except (LedgerError, ValueError, TypeError) as exc:
                applied["failed"].append({"code": code, "error": str(exc)})
                continue
            existing.add(key)
            applied["trades"] += 1

        # 没有流水的持仓（手工维护的那种）按文件原样写回；有流水的以回放为准
        traded_codes = {_valid_code(t.get("code")) for t in rows}
        for pos in data["positions"]:
            if not isinstance(pos, dict):
                continue
            code = _valid_code(pos.get("code"))
            if not code or code in traded_codes:
                continue
            shares = int(_int(pos.get("shares")) or 0)
            if shares <= 0:
                continue
            try:
                ledger.upsert_manual_position(
                    code=code,
                    name=str(pos.get("name") or ""),
                    shares=shares,
                    cost_price=float(_num(pos.get("cost_price")) or 0.0),
                    opened_at=str(pos.get("opened_at") or _today_iso()),
                )
            except (LedgerError, ValueError, TypeError) as exc:
                applied["failed"].append({"code": code, "error": str(exc)})
                continue
            applied["positions"] += 1

        if mode == "all" and data["cash"] is not None:
            try:
                ledger.set_cash(float(data["cash"]))
                applied["cash"] = float(data["cash"])
            except (LedgerError, ValueError) as exc:
                errors.append(f"资金恢复失败：{exc}")

    if mode != "trades":
        for item in data["watchlist"]:
            if not isinstance(item, dict):
                continue
            code = _valid_code(item.get("code"))
            if not code:
                continue
            try:
                ledger.add_watch(code, str(item.get("name") or ""),
                                 str(item.get("note") or ""))
                applied["watch"] += 1
            except (LedgerError, ValueError):
                # 单个代码不合法只跳过该项，不让整批失败
                continue
        if data["cash"] is not None:
            try:
                ledger.set_cash(float(data["cash"]))
                applied["cash"] = float(data["cash"])
            except (LedgerError, ValueError) as exc:
                errors.append(f"资金恢复失败：{exc}")

    scope_cn = {"all": "全量", "watch_cash": "资金+自选", "trades": "仅成交"}[mode]
    parts = [f"导入完成（{scope_cn}）：成交 +{applied['trades']}"
             f"（重复跳过 {applied['skipped']}"
             + (f"，失败 {len(applied['failed'])}" if applied["failed"] else "")
             + "）"]
    if applied["positions"]:
        parts.append(f"手工持仓 +{applied['positions']}")
    if applied["watch"]:
        parts.append(f"自选 +{applied['watch']}")
    if applied["cash"] is not None:
        parts.append(f"资金 {applied['cash']:,.2f}")
    message = "，".join(parts) + "。"
    return {
        "ok": True,
        "scope": mode,
        "applied": applied,
        "errors": errors,
        "message": message,
    }


# ====================================================================== 成交 CSV


def _header_key(cell: Any) -> str:
    """表头单元格 → 语义键（去 BOM、大小写/空白不敏感）。"""
    text = str(cell if cell is not None else "").strip().lstrip("\ufeff")
    text = re.sub(r"\s+", "", text).lower()
    for key, aliases in _HEADER_ALIASES.items():
        if any(alias.lower() == text for alias in aliases):
            return key
    return ""


def _detect_delimiter(sample: str) -> str:
    counts = {",": 0, "\t": 0, ";": 0}
    in_quotes = False
    for ch in sample:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and ch in counts:
            counts[ch] += 1
    best = ","
    for delimiter, n in counts.items():
        if n > counts[best]:
            best = delimiter
    return best


def _norm_date(raw: Any) -> str:
    """日期归一化：空值按今天；支持 2026/10/20、20261020、2026.10.20。

    识别不了抛 ValueError（与 dsh-astock 一样**明确报错，不猜**——
    猜错日期会直接改变 T+1 可卖与已实现盈亏）。
    """
    text = str(raw or "").strip()
    if not text:
        return _today_iso()
    text = text.replace("/", "-").replace(".", "-")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"日期格式无法识别（{text!r}，应为 YYYY-MM-DD）") from exc
    return text


def parse_trade_rows(text: Any) -> Dict[str, Any]:
    """把粘贴/导入的 CSV/TSV 解析成成交行（与 dsh-astock 的 parseTradeRows 同口径）。

    识别不出的行**明确报错**，不猜——猜错方向或价格会直接污染台账。
    """
    raw = str(text or "").strip().lstrip("\ufeff").strip()
    if not raw:
        return {"rows": [], "errors": ["没有内容"]}

    lines = [ln for ln in re.split(r"\r?\n", raw) if ln.strip()]
    if len(lines) < 2:
        return {"rows": [], "errors": ["至少需要一行表头和一行数据"]}

    delimiter = _detect_delimiter("\n".join(lines[:5]))
    try:
        table = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
    except csv.Error as exc:
        return {"rows": [], "errors": [f"CSV 解析失败：{exc}"]}
    table = [row for row in table if any(str(c).strip() for c in row)]
    if not table:
        return {"rows": [], "errors": ["读不到表头"]}

    headers = [str(c or "").strip() for c in table[0]]
    mapping = [_header_key(c) for c in headers]
    known = {key for key in mapping if key}
    missing = [key for key in ("trade_date", "code", "side", "price", "shares")
               if key not in known]
    if missing:
        labels = {"trade_date": "成交日期", "code": "代码", "side": "方向",
                  "price": "成交价", "shares": "数量"}
        return {"rows": [],
                "errors": [f"表头缺少：{'、'.join(labels[k] for k in missing)}"]}

    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for index, record in enumerate(table[1:]):
        if index >= MAX_IMPORT_ROWS:
            errors.append(f"超过 {MAX_IMPORT_ROWS} 行，只解析前 {MAX_IMPORT_ROWS} 行")
            break
        item: Dict[str, Any] = {"_index": index}
        for column, key in enumerate(mapping):
            if key and column < len(record):
                item[key] = str(record[column] if record[column] is not None
                                else "").strip()
        try:
            item["trade_date"] = _norm_date(item.get("trade_date"))
        except ValueError as exc:
            errors.append(f"第 {index + 2} 行：{exc}")
            continue
        item["side"] = _norm_side(item.get("side"))
        if not item["side"]:
            errors.append(f"第 {index + 2} 行：方向只能是 买入/卖出（buy/sell）")
            continue
        price = _num(str(item.get("price") or "").replace(",", ""))
        shares = _num(str(item.get("shares") or "").replace(",", ""))
        if price is None or shares is None:
            errors.append(f"第 {index + 2} 行：价格/数量不是数字")
            continue
        item["price"] = round(price, 3)
        item["shares"] = int(shares)
        if item["price"] <= 0 or item["shares"] <= 0:
            errors.append(f"第 {index + 2} 行：价格与数量都必须大于 0")
            continue
        fee_raw = str(item.get("fee") or "").replace(",", "")
        item["fee"] = _round(fee_raw, 2) if fee_raw.strip() else None
        rows.append(item)
    return {"rows": rows, "errors": errors}


def preview_trade_rows(ledger: Ledger, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """预览 CSV 导入：逐行模拟持仓变化，标出可导入/重复/校验失败。"""
    state: Dict[str, Dict[str, Any]] = {}
    for pos in ledger.list_positions():
        state[str(pos["code"])] = {
            "shares": int(pos.get("shares") or 0),
            "cost": float(pos.get("cost_price") or 0.0),
            "available": int(pos.get("available_today") or pos.get("shares") or 0),
        }
    existing = ledger.trade_dedupe_keys()
    today = _today_iso()
    out: List[Dict[str, Any]] = []

    for row in rows:
        problems: List[str] = []
        code = _valid_code(row.get("code"))
        if not code:
            out.append({**row, "status": "error",
                        "problems": [f"代码无法识别：{row.get('code')}"]})
            continue
        price = float(_num(row.get("price")) or 0.0)
        shares = int(_int(row.get("shares")) or 0)
        side = str(row.get("side") or "")
        amount = round(price * shares, 2)
        fee_bd = calc_fee(amount, side, ledger.fee, board_of(code))
        fee = (float(row["fee"]) if row.get("fee") is not None
               else round(fee_bd.total, 2))

        current = state.get(code, {"shares": 0, "cost": 0.0, "available": 0})
        held = int(current["shares"])
        cost = float(current["cost"])
        available = int(current["available"])

        err = check_order_size(shares, code, side, held)
        if err:
            problems.append(err)
        if side == "buy":
            new_shares = held + shares
            new_cost = round((cost * held + amount + fee) / new_shares, 4) \
                if new_shares else 0.0
            realized: Optional[float] = None
        else:
            if held <= 0:
                problems.append(f"{code} 当前无持仓，无法卖出")
            elif shares > available:
                problems.append(f"可卖数量不足：持仓 {held} 股，"
                                f"可卖 {available} 股，本次 {shares} 股")
            new_shares = max(0, held - shares)
            new_cost = round(cost, 4) if new_shares else 0.0
            realized = round((price - cost) * shares - fee, 2) if held > 0 else None

        duplicate = trade_key(row.get("trade_date"), code, side,
                              price, shares) in existing
        status = "error" if problems else ("duplicate" if duplicate else "ok")
        # 重复行不参与模拟：导入时会跳过它，预览也必须按「跳过」推演，
        # 否则后面的卖出会按虚增的持仓算，看起来能过、落账却失败。
        if not problems and not duplicate:
            state[code] = {
                "shares": new_shares,
                "cost": new_cost,
                "available": new_shares if row.get("trade_date") != today else available,
            }
        out.append({
            "index": row.get("_index"),
            "trade_date": row.get("trade_date"),
            "code": code,
            "name": str(row.get("name") or ""),
            "side": side,
            "price": price,
            "shares": shares,
            "amount": amount,
            "fee": round(fee, 2),
            "realized": realized,
            "new_shares": new_shares,
            "new_cost": new_cost,
            "duplicate": duplicate,
            "status": status,
            "problems": problems,
        })

    summary = {"ok": 0, "duplicate": 0, "error": 0}
    for item in out:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {
        "ok": True,
        "committed": False,
        "rows": out,
        "summary": summary,
        "total_amount": round(sum((i["amount"] for i in out), 2), 2),
        "total_fee": round(sum((i["fee"] for i in out), 2), 2),
    }


def import_trades(ledger: Ledger, text: Any,
                  commit: bool = False) -> Dict[str, Any]:
    """导入成交 CSV：默认只预览；commit=True 才按日期顺序回放落账。"""
    parsed = parse_trade_rows(text)
    rows = parsed["rows"]
    errors = parsed["errors"]
    if not rows:
        return {"ok": False, "message": "没有可导入的成交行",
                "errors": errors, "rows": [], "summary": {}}

    if not commit:
        result = preview_trade_rows(ledger, rows)
        result["errors"] = errors
        summary = result["summary"]
        result["message"] = (f"预览：{summary.get('ok', 0)} 行可导入，"
                             f"{summary.get('duplicate', 0)} 行疑似重复，"
                             f"{summary.get('error', 0)} 行校验不通过")
        return result

    ordered = sorted(rows, key=lambda r: str(r.get("trade_date") or ""))
    existing = ledger.trade_dedupe_keys()
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for row in ordered:
        code = _valid_code(row.get("code"))
        if not code:
            failed.append({"code": str(row.get("code") or ""),
                           "error": "代码无法识别"})
            continue
        key = trade_key(row.get("trade_date"), code, row.get("side"),
                        row.get("price"), row.get("shares"))
        if key in existing:
            skipped.append({
                "trade_date": row.get("trade_date"), "code": code,
                "side": row.get("side"), "price": row.get("price"),
                "shares": row.get("shares"),
                "reason": "台账里已有同一笔（日期/代码/方向/价格/数量一致）",
            })
            continue
        try:
            saved = ledger.record_trade(
                code=code, side=str(row.get("side") or ""),
                price=float(_num(row.get("price")) or 0.0),
                shares=int(_int(row.get("shares")) or 0),
                trade_date=str(row.get("trade_date") or ""),
                name=str(row.get("name") or ""),
                note=str(row.get("note") or ""),
                fee_override=_num(row.get("fee")),
            )
        except (LedgerError, ValueError, TypeError) as exc:
            failed.append({"trade_date": row.get("trade_date"), "code": code,
                           "side": row.get("side"), "price": row.get("price"),
                           "shares": row.get("shares"), "error": str(exc)})
            continue
        existing.add(key)
        applied.append({
            "trade_date": saved.get("trade_date"),
            "code": saved.get("code"),
            "side": saved.get("side"),
            "price": saved.get("price"),
            "shares": saved.get("shares"),
            "fee": saved.get("fee"),
            "cost_after": saved.get("cost_price_after"),
            "shares_after": saved.get("shares_after"),
        })

    return {
        "ok": True,
        "committed": True,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "summary": {"applied": len(applied), "skipped": len(skipped),
                    "failed": len(failed)},
        "message": (f"已导入 {len(applied)} 笔成交"
                    + (f"，跳过 {len(skipped)} 笔重复" if skipped else "")
                    + (f"，{len(failed)} 笔失败（见明细）" if failed else "")),
    }


# ====================================================================== 兼容别名

#: 与 dsh-astock 的函数名对应，便于对照源码/文档时一眼找到。
exportPayload = export_payload
parseLedgerJson = parse_ledger_json
previewLedgerImport = preview_ledger_import
importLedger = import_ledger
parseTradeRows = parse_trade_rows
importTrades = import_trades
