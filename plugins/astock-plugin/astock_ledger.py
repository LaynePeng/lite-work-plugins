"""手工交易台账：持仓 / 流水 / 资金 / 自选股，落盘 SQLite（纯标准库 sqlite3）。

**定位**：用户自己在券商 App 下单，插件负责**记账 + 计算 + 跟踪 + 提示**。
因此这里没有任何委托、撤单、券商对接逻辑——只有"用户成交之后录进来"。

核心口径（A 股特有，算错会让后续所有判断连锁错误）：

- **移动加权平均成本**：买入按 `(原金额 + 本次金额 + 本次费用) / 总股数` 重算；
  卖出**不改变成本价**，只减股数并把 `(卖出价 - 成本价) * 股数 - 费用` 计入已实现盈亏；
- **T+1**：当日买入的股份当日不可卖。台账按"买入日期"维护可用股数，
  卖出时先扣可用（T+1 解禁的），不足则拒绝并说明还差多少、哪天解禁；
- **清仓后成本归零**：持仓为 0 时删除该条，避免残留 0 股记录污染面板；
- **已实现盈亏累计**：卖出时累加到 ledger 层的 `realized_pnl`，与浮动盈亏分开统计。

数据文件：`<workspace>/.astock/ledger.sqlite`（默认），可由设置页指定绝对路径。
写入用 SQLite 事务，天然抗半写；不使用 WAL 之外的花哨特性，便于用户自己用
DB 工具查看/备份。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from astock_market import (
    BOARD_LABELS, FeeConfig, board_of, calc_fee, check_order_size,
    normalize_code, round_price, to_float,
)

_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,             -- YYYY-MM-DD（成交日，决定 T+1 解禁）
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL,             -- buy / sell
    price       REAL NOT NULL,
    shares      INTEGER NOT NULL,
    amount      REAL NOT NULL,             -- price * shares
    fee         REAL NOT NULL DEFAULT 0,   -- 佣金+印花税+过户费合计
    fee_detail  TEXT NOT NULL DEFAULT '',  -- JSON
    realized    REAL,                      -- 卖出时的已实现盈亏（买入为 NULL）
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code, trade_date);

CREATE TABLE IF NOT EXISTS positions (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    shares      INTEGER NOT NULL DEFAULT 0,
    cost_price  REAL NOT NULL DEFAULT 0,   -- 移动加权平均成本（含买入费用）
    opened_at   TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cash (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    balance     REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS watchlist (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _today() -> str:
    return _dt.date.today().isoformat()


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: str, default: Any = None) -> Any:
    import json
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


class LedgerError(RuntimeError):
    """台账操作失败（消息面向 Agent，说明原因与可行动作）。"""


class Ledger:
    """交易台账。所有方法同步；插件层用 asyncio.to_thread 调用。"""

    def __init__(self, path: str, fee: Optional[FeeConfig] = None) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.fee = fee or FeeConfig()
        self._lock = threading.RLock()
        self._init_db()

    # -------------------------------------------------- 基础设施

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """连接上下文：**保证 close**。

        为什么不能直接用 `with self._connect() as conn`：sqlite3.Connection 的
        上下文管理器只负责事务提交/回滚，**不会关闭连接**。每次调用泄漏一个
        连接 + WAL 句柄，长跑会累积到 `too many open files`（单测里表现为大量
        ResourceWarning）。这里显式 close，且异常时 rollback 后仍 close。
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.executescript(_SQL_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO cash(id, balance, updated_at) VALUES (1, 0, ?)",
                (_now(),))

    def refresh_fee(self, fee: FeeConfig) -> None:
        self.fee = fee

    # -------------------------------------------------- 资金

    def get_cash(self) -> Dict[str, Any]:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT balance, updated_at FROM cash WHERE id=1").fetchone()
            return {"balance": round(row["balance"], 2) if row else 0.0,
                    "updated_at": row["updated_at"] if row else ""}

    def set_cash(self, balance: float) -> Dict[str, Any]:
        """设置可用资金余额（用户录入，不自动推算——银行转账/出入金不可见）。"""
        if balance < 0:
            raise LedgerError("资金余额不能为负数")
        with self._lock, self._db() as conn:
            conn.execute("UPDATE cash SET balance=?, updated_at=? WHERE id=1",
                         (round(float(balance), 2), _now()))
        return self.get_cash()

    def add_cash(self, delta: float) -> Dict[str, Any]:
        cur = self.get_cash()
        return self.set_cash(cur["balance"] + float(delta))

    # -------------------------------------------------- 自选股

    def add_watch(self, code: str, name: str = "", note: str = "") -> Dict[str, Any]:
        code, _ = normalize_code(code)
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO watchlist(code, name, note, added_at) VALUES (?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, note=excluded.note",
                (code, name, note, _now()))
        return {"code": code, "name": name, "note": note}

    def remove_watch(self, code: str) -> bool:
        code, _ = normalize_code(code)
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
            return cur.rowcount > 0

    def list_watch(self) -> List[Dict[str, Any]]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT code, name, note, added_at FROM watchlist ORDER BY added_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------- 持仓

    def _position(self, conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM positions WHERE code=?", (code,)).fetchone()

    def list_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT code, name, shares, cost_price, opened_at, updated_at, note "
                "FROM positions WHERE shares > 0 ORDER BY code").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["board"] = BOARD_LABELS.get(board_of(d["code"]), "")
                d["cost_amount"] = round(d["shares"] * d["cost_price"], 2)
                d["available_today"] = self._available(conn, d["code"], d["shares"])
                out.append(d)
            return out

    def _available(self, conn: sqlite3.Connection, code: str,
                   total: Optional[int] = None) -> int:
        """T+1 可用股数 = 总持仓 - 今日买入且尚未卖出的部分。

        用「今日买入股数 - 今日卖出股数」估算，而不是逐笔 FIFO 配对：
        A 股卖出不区分批次，只要当日净卖出不超过昨日持仓即合法，
        这个口径与券商一致且足够简单。
        """
        today = _today()
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN shares ELSE 0 END),0) AS b, "
            "       COALESCE(SUM(CASE WHEN side='sell' THEN shares ELSE 0 END),0) AS s "
            "FROM trades WHERE code=? AND trade_date=?", (code, today)).fetchone()
        bought = int(row["b"] or 0)
        sold = int(row["s"] or 0)
        if total is None:
            prow = self._position(conn, code)
            total = int(prow["shares"]) if prow else 0
        # 今日净买入的部分不可卖；今日卖出已经消耗掉旧股，故 bought - sold 可能为负
        locked = max(0, bought - sold)
        return max(0, int(total) - locked)

    # -------------------------------------------------- 录入成交

    def record_trade(self, code: str, side: str, price: float, shares: int,
                     trade_date: str = "", name: str = "", note: str = "",
                     fee_override: Optional[float] = None) -> Dict[str, Any]:
        """录入一笔**已成交**的委托（用户自己在券商 App 完成的结果）。

        返回录入后的持仓快照 + 本次费用明细 + 成本变化，供 Agent 复述给用户。
        """
        side = (side or "").strip().lower()
        if side not in ("buy", "sell"):
            raise LedgerError("side 只能是 buy 或 sell")
        code, _ = normalize_code(code)
        price = round_price(to_float(price, 0.0) or 0.0)
        if price <= 0:
            raise LedgerError("成交价格必须大于 0")
        shares = int(shares or 0)
        trade_date = (trade_date or "").strip() or _today()
        try:
            _dt.date.fromisoformat(trade_date)
        except ValueError as exc:
            raise LedgerError(f"成交日期格式应为 YYYY-MM-DD，收到 {trade_date!r}") from exc
        if trade_date > _today():
            raise LedgerError("成交日期不能晚于今天（未来日期会让 T+1 判断失真）")

        amount = round(price * shares, 2)
        board = board_of(code)
        fee_bd = calc_fee(amount, side, self.fee, board)
        fee = round(fee_bd.total, 2) if fee_override is None else round(float(fee_override), 2)

        with self._lock, self._db() as conn:
            pos = self._position(conn, code)
            cur_shares = int(pos["shares"]) if pos else 0
            cur_cost = float(pos["cost_price"]) if pos else 0.0
            cur_name = (pos["name"] if pos else "") or name

            if side == "buy":
                err = check_order_size(shares, code, "buy", cur_shares)
                if err:
                    raise LedgerError(err)
                new_shares = cur_shares + shares
                # 移动加权平均：本次总成本含费用，均摊到总股数
                total_cost = cur_cost * cur_shares + amount + fee
                new_cost = round(total_cost / new_shares, 4) if new_shares else 0.0
                realized = None
            else:
                if cur_shares <= 0:
                    raise LedgerError(f"{code} 当前无持仓，无法录入卖出")
                available = self._available(conn, code, cur_shares)
                if shares > available:
                    locked = cur_shares - available
                    hint = (f"（另有 {locked} 股为今日买入，T+1 明日才可卖）"
                            if locked > 0 else "")
                    raise LedgerError(
                        f"可卖数量不足：{code} 持仓 {cur_shares} 股，"
                        f"今日可卖 {available} 股，本次想卖 {shares} 股{hint}")
                err = check_order_size(shares, code, "sell", cur_shares)
                if err:
                    raise LedgerError(err)
                new_shares = cur_shares - shares
                # 卖出不改成本价，只把差额计入已实现盈亏
                realized = round((price - cur_cost) * shares - fee, 2)
                new_cost = round(cur_cost, 4) if new_shares else 0.0

            conn.execute(
                "INSERT INTO trades(trade_date, code, name, side, price, shares, amount,"
                " fee, fee_detail, realized, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_date, code, cur_name or name, side, price, shares, amount,
                 fee, _json_dumps(fee_bd.as_dict()), realized, note, _now()))

            if new_shares > 0:
                conn.execute(
                    "INSERT INTO positions(code, name, shares, cost_price, opened_at,"
                    " updated_at, note) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
                    " shares=excluded.shares, cost_price=excluded.cost_price,"
                    " updated_at=excluded.updated_at",
                    (code, cur_name or name, new_shares, new_cost,
                     (pos["opened_at"] if pos else trade_date), _now(),
                     (pos["note"] if pos else "")))
            else:
                conn.execute("DELETE FROM positions WHERE code=?", (code,))

            if realized is not None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('realized_pnl', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=?",
                    (str(realized), str(realized)))

        return {
            "code": code,
            "name": cur_name or name,
            "side": side,
            "price": price,
            "shares": shares,
            "amount": amount,
            "trade_date": trade_date,
            "fee": fee,
            "fee_detail": fee_bd.as_dict(),
            "cost_price_before": round(cur_cost, 4) if cur_shares else None,
            "cost_price_after": new_cost,
            "shares_after": new_shares,
            "realized_pnl": realized,
            "cash_delta": round(-(amount + fee) if side == "buy" else amount - fee, 2),
        }

    # -------------------------------------------------- 查询

    def trades(self, code: str = "", limit: int = 50,
               start: str = "", end: str = "") -> List[Dict[str, Any]]:
        sql = ("SELECT id, trade_date, code, name, side, price, shares, amount, fee,"
               " realized, note, created_at FROM trades WHERE 1=1")
        params: List[Any] = []
        if code:
            code, _ = normalize_code(code)
            sql += " AND code=?"
            params.append(code)
        if start:
            sql += " AND trade_date>=?"
            params.append(start)
        if end:
            sql += " AND trade_date<=?"
            params.append(end)
        sql += " ORDER BY trade_date DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 500)))
        with self._lock, self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["fee_detail"] = _json_loads(d.get("fee_detail"), {})
            out.append(d)
        return out

    def realized_pnl(self, start: str = "", end: str = "") -> Dict[str, Any]:
        """已实现盈亏合计（按卖出笔累加）。"""
        sql = "SELECT COALESCE(SUM(realized),0) AS total, COUNT(*) AS n FROM trades WHERE realized IS NOT NULL"
        params: List[Any] = []
        if start:
            sql += " AND trade_date>=?"
            params.append(start)
        if end:
            sql += " AND trade_date<=?"
            params.append(end)
        with self._lock, self._db() as conn:
            row = conn.execute(sql, params).fetchone()
        return {"realized_pnl": round(float(row["total"] or 0), 2),
                "sell_count": int(row["n"] or 0)}

    def total_fee(self, start: str = "", end: str = "") -> float:
        sql = "SELECT COALESCE(SUM(fee),0) AS total FROM trades WHERE 1=1"
        params: List[Any] = []
        if start:
            sql += " AND trade_date>=?"
            params.append(start)
        if end:
            sql += " AND trade_date<=?"
            params.append(end)
        with self._lock, self._db() as conn:
            row = conn.execute(sql, params).fetchone()
        return round(float(row["total"] or 0), 2)

    def export_rows(self) -> Dict[str, List[Dict[str, Any]]]:
        """整表导出（用于备份 / 让用户拿 Excel 自己看）。"""
        with self._lock, self._db() as conn:
            return {
                "positions": [dict(r) for r in conn.execute(
                    "SELECT * FROM positions ORDER BY code").fetchall()],
                "trades": [dict(r) for r in conn.execute(
                    "SELECT * FROM trades ORDER BY trade_date, id").fetchall()],
                "watchlist": [dict(r) for r in conn.execute(
                    "SELECT * FROM watchlist ORDER BY code").fetchall()],
                "cash": [dict(r) for r in conn.execute("SELECT * FROM cash").fetchall()],
            }

    def delete_trade(self, trade_id: int, rebuild: bool = True) -> Dict[str, Any]:
        """删除一笔录入错误的成交，并按剩余流水**重算全部持仓**。

        重算而不是"反向冲销"：用户手滑录错价格/数量时，冲销会留下错误的
        成本基准；按全量流水按序重放才能得到与券商一致的移动加权成本。
        """
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT id, code FROM trades WHERE id=?",
                               (int(trade_id),)).fetchone()
            if row is None:
                raise LedgerError(f"流水 {trade_id} 不存在")
            code = str(row["code"])
            conn.execute("DELETE FROM trades WHERE id=?", (int(trade_id),))
        result: Dict[str, Any] = {"deleted": int(trade_id), "code": code}
        if rebuild:
            result.update(self.rebuild_position(code))
        return result

    def rebuild_position(self, code: str) -> Dict[str, Any]:
        """按该标的的全部流水按时间重放，重建持仓与成本（数据自愈入口）。"""
        code, _ = normalize_code(code)
        with self._lock:
            rows = self.trades(code=code, limit=500)
            rows.reverse()      # 时间正序
            shares = 0
            cost = 0.0
            name = ""
            opened = ""
            for t in rows:
                if t["name"]:
                    name = t["name"]
                if t["side"] == "buy":
                    if not opened:
                        opened = t["trade_date"]
                    total = cost * shares + float(t["amount"]) + float(t["fee"])
                    shares += int(t["shares"])
                    cost = round(total / shares, 4) if shares else 0.0
                else:
                    shares -= int(t["shares"])
                    if shares <= 0:
                        shares = 0
                        cost = 0.0
                        opened = ""
            with self._db() as conn:
                if shares > 0:
                    conn.execute(
                        "INSERT INTO positions(code, name, shares, cost_price, opened_at,"
                        " updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET"
                        " name=excluded.name, shares=excluded.shares,"
                        " cost_price=excluded.cost_price, updated_at=excluded.updated_at",
                        (code, name, shares, cost, opened, _now()))
                else:
                    conn.execute("DELETE FROM positions WHERE code=?", (code,))
        return {"code": code, "shares": shares, "cost_price": round(cost, 4),
                "name": name}


def default_ledger_path(workspace: str = "") -> str:
    """默认台账位置：<workspace>/.astock/ledger.sqlite；无 workspace 时落到家目录。"""
    base = (workspace or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".lite-work")
    return os.path.join(os.path.abspath(os.path.expanduser(base)),
                        ".astock", "ledger.sqlite")
