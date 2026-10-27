"""本地历史行情存档：让回测不再受"只有 2.6 年"的接口限制。

**为什么必须存档**：
免费行情接口的历史深度是**硬上限**（腾讯 641 根日 K ≈ 2.6 年，东财同量级）。
这个长度**不足以覆盖一个完整牛熊周期**，所以回测结论只能说"这两年有效"。
唯一不依赖付费数据源的办法，是**自己每天把当天的 K 线存下来**，慢慢积累。

**它不会立刻变长**——存档是时间的函数。今天开始存，一年后多一年。
这一点必须写在界面上，否则用户会以为"点了存档就有 10 年历史"。

────────────────────────────────────────────────────────────────────────
两个关键设计（做错就会让存档悄悄失效）
────────────────────────────────────────────────────────────────────────

① **必须存「不复权」原始价格，不能存前复权。**

   前复权价格会随每次分红/送股**重算整段历史**——今天存的 2024 年价格，
   明年分红后会变成另一个数字。若直接存前复权：
     · 存档里同一日期会有两个不同价格（今天存的 vs 明年补的），
       而代码无法分辨哪个是"当时的真相"；
     · 回测收益率还算得出来（前复权内部一致），但**跨期拼接后自相矛盾**；
     · 最坏的情况是静默错误：数据看着有、结果是错的。

   所以存档一律存**不复权**（raw）价，另存 `adjust` 字段标明口径。
   回测要用复权价时，由读取方在**同一口径内**自行处理；跨存档拼接永远
   只拼 raw。

② **口径隔离：(code, date, adjust) 三段主键，两口径并存互不污染。**

   同一日期在 raw 与 qfq 下是**两个不同的价格序列**（前复权价会随分红重算），
   所以它们必须各自独立存储、各自独立查询：
     · 写 raw 不影响已有的 qfq，写 qfq 也不影响已有的 raw；
     · 同口径重复写入 → 覆盖（当日数据可能有盘中/收盘差异）；
     · 读取时必须指定口径，绝不跨口径混拼（混拼后收益率毫无意义）。

   **早期实现的错误（留作教训）**：一开始写的是"已有 raw 就跳过 qfq"，
   看似在保护事实基准，实际导致「先存 raw 再存 qfq」时 qfq 静默丢失——
   两个口径本该并存，却互相阻挡。单测 `test_raw_and_qfq_coexist_per_date`
   用来锁住这个行为。

────────────────────────────────────────────────────────────────────────
数据量与清理
────────────────────────────────────────────────────────────────────────

一只股票一年约 244 根日 K，一行约 100 字节 → **约 24KB/只/年**。
500 只存 5 年 ≈ 60MB，SQLite 完全扛得住。仍提供按标的/按时间清理。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

logger = logging.getLogger("litework.plugins.astock.archive")

#: 存档表结构。
#:
#: 为什么把 adjust 放进主键：同一日期在不复权/前复权下是**两个不同的价格序列**，
#: 混存会让"某天收盘价"出现二义性。放进主键后两者各自独立、互不污染。
_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,             -- YYYY-MM-DD
    adjust      TEXT NOT NULL DEFAULT 'raw', -- raw / qfq / hfq（见模块注释①）
    open        REAL NOT NULL DEFAULT 0,
    close       REAL NOT NULL DEFAULT 0,
    high        REAL NOT NULL DEFAULT 0,
    low         REAL NOT NULL DEFAULT 0,
    volume_lot  REAL NOT NULL DEFAULT 0,   -- 手
    amount      REAL,                      -- 元（腾讯源可能没有）
    source      TEXT NOT NULL DEFAULT '',  -- tencent / eastmoney
    saved_at    TEXT NOT NULL,
    PRIMARY KEY (code, date, adjust)
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date);
CREATE INDEX IF NOT EXISTS idx_bars_code ON bars(code, adjust, date);

-- 存档覆盖情况速查（避免每次 COUNT 全表）
CREATE TABLE IF NOT EXISTS archive_meta (
    code        TEXT NOT NULL,
    adjust      TEXT NOT NULL DEFAULT 'raw',
    first_date  TEXT NOT NULL DEFAULT '',
    last_date   TEXT NOT NULL DEFAULT '',
    bar_count   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, adjust)
);
"""


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return _dt.date.today().isoformat()


def default_archive_path(workspace: str = "") -> str:
    """存档默认位置：与台账同目录（`.astock/`），便于一起备份。"""
    if workspace:
        return os.path.join(workspace, ".astock", "archive.sqlite")
    return os.path.join(os.path.expanduser("~"), ".lite-work", "astock-data",
                        "archive.sqlite")


class Archive:
    """本地日 K 存档。所有方法同步（插件层用 asyncio.to_thread 调用）。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lock = threading.RLock()
        self._init_db()

    # -------------------------------------------------- 基础设施

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """连接上下文，**保证 close**（与 astock_ledger 同一教训：
        `with sqlite3.connect(...)` 只提交事务、不关连接，长跑会耗尽句柄）。"""
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

    # -------------------------------------------------- 写入

    def save_bars(self, code: str, bars: Sequence[Dict[str, Any]],
                  adjust: str = "raw", source: str = "") -> Dict[str, Any]:
        """批量写入日 K。返回 {saved, skipped, first_date, last_date}。

        `adjust` 默认 **raw**（不复权）——见模块注释①，这是存档的事实基准。
        """
        adjust = (adjust or "raw").lower()
        if adjust not in ("raw", "qfq", "hfq"):
            adjust = "raw"
        rows = []
        now = _now()
        for b in bars or []:
            date = str(b.get("date") or "").strip()
            close = b.get("close")
            if not date or close is None or close <= 0:
                continue
            rows.append((
                code, date, adjust,
                float(b.get("open") or close),
                float(close),
                float(b.get("high") or close),
                float(b.get("low") or close),
                float(b.get("volume_lot") or 0.0),
                (float(b["amount_yuan"]) if b.get("amount_yuan") is not None
                 else (float(b["amount"]) if b.get("amount") is not None
                       else None)),
                source, now,
            ))
        if not rows:
            return {"saved": 0, "skipped": 0, "first_date": "", "last_date": ""}

        saved = 0
        with self._lock, self._db() as conn:
            for row in rows:
                # 去重规则（主键含 adjust，故两口径**可以共存**）：
                #   ① 同口径重复 → 覆盖（当日数据可能有盘中/收盘差异）
                #   ② 该日已有 raw、这次写 qfq → 照常写入（两口径各自独立）
                #   ③ 该日已有 qfq、这次写 raw → 也照常写入（raw 是事实基准）
                # 换句话说：**不跨口径互相覆盖，也不跨口径互相阻挡**。
                # 早期实现错在"见到已有 raw 就跳过 qfq"，导致
                # 「先存 raw 再存 qfq」时 qfq 静默丢失——两个口径本该并存。
                cur = conn.execute(
                    "SELECT 1 FROM bars WHERE code=? AND date=? AND adjust=?",
                    (code, row[1], adjust))
                if cur.fetchone() is not None:
                    pass        # 同口径 → 下面 INSERT OR REPLACE 覆盖
                conn.execute(
                    "INSERT OR REPLACE INTO bars"
                    "(code,date,adjust,open,close,high,low,volume_lot,amount,"
                    " source,saved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
                saved += 1
            self._refresh_meta(conn, code, adjust)

        dates = sorted(r[1] for r in rows)
        return {"saved": saved, "skipped": len(rows) - saved,
                "first_date": dates[0], "last_date": dates[-1]}

    def _refresh_meta(self, conn: sqlite3.Connection, code: str,
                      adjust: str) -> None:
        row = conn.execute(
            "SELECT MIN(date) AS f, MAX(date) AS l, COUNT(*) AS n "
            "FROM bars WHERE code=? AND adjust=?", (code, adjust)).fetchone()
        if not row or not row["n"]:
            return
        conn.execute(
            "INSERT OR REPLACE INTO archive_meta"
            "(code,adjust,first_date,last_date,bar_count,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (code, adjust, row["f"] or "", row["l"] or "", int(row["n"]), _now()))

    # -------------------------------------------------- 读取

    def load_bars(self, code: str, adjust: str = "raw",
                  start: str = "", end: str = "",
                  limit: int = 0) -> List[Dict[str, Any]]:
        """读存档（升序）。limit>0 时取**最近** limit 根。"""
        adjust = (adjust or "raw").lower()
        sql = ("SELECT date,open,close,high,low,volume_lot,amount "
               "FROM bars WHERE code=? AND adjust=?")
        params: List[Any] = [code, adjust]
        if start:
            sql += " AND date>=?"
            params.append(start)
        if end:
            sql += " AND date<=?"
            params.append(end)
        if limit and limit > 0:
            sql += " ORDER BY date DESC LIMIT ?"
            params.append(int(limit))
        else:
            sql += " ORDER BY date ASC"
        with self._lock, self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = [{"date": r["date"], "open": r["open"], "close": r["close"],
                "high": r["high"], "low": r["low"],
                "volume_lot": r["volume_lot"],
                "amount_yuan": r["amount"]} for r in rows]
        if limit and limit > 0:
            out.reverse()          # DESC 取最近 N 根后再翻回升序
        return out

    def load_many(self, codes: Sequence[str], adjust: str = "raw",
                  limit: int = 0) -> Dict[str, List[Dict[str, Any]]]:
        return {c: self.load_bars(c, adjust=adjust, limit=limit) for c in codes}

    # -------------------------------------------------- 统计与清理

    def stats(self) -> Dict[str, Any]:
        """存档概览：多少只、总根数、时间跨度、库文件大小。"""
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT code) AS codes, COUNT(*) AS bars, "
                "MIN(date) AS f, MAX(date) AS l FROM bars").fetchone()
            per = conn.execute(
                "SELECT code, adjust, first_date, last_date, bar_count "
                "FROM archive_meta ORDER BY bar_count DESC LIMIT 50").fetchall()
        size = 0
        try:
            size = os.path.getsize(self.path)
        except OSError:
            pass
        return {
            "path": self.path,
            "codes": int(row["codes"] or 0) if row else 0,
            "bars": int(row["bars"] or 0) if row else 0,
            "first_date": (row["f"] or "") if row else "",
            "last_date": (row["l"] or "") if row else "",
            "size_bytes": size,
            "size_human": _human_size(size),
            "detail": [dict(r) for r in per],
        }

    def coverage(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """指定标的的覆盖情况（每只多少根、起止日期）。"""
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock, self._db() as conn:
            for c in codes:
                r = conn.execute(
                    "SELECT adjust,first_date,last_date,bar_count "
                    "FROM archive_meta WHERE code=?", (c,)).fetchall()
                if not r:
                    out[c] = {"bar_count": 0, "first_date": "", "last_date": ""}
                    continue
                best = max(r, key=lambda x: x["bar_count"])
                out[c] = {"adjust": best["adjust"],
                          "bar_count": int(best["bar_count"]),
                          "first_date": best["first_date"],
                          "last_date": best["last_date"]}
        return out

    def prune(self, before: str = "", codes: Optional[Sequence[str]] = None
              ) -> Dict[str, Any]:
        """清理：删 before 之前的数据 / 指定标的的全部数据。

        **两个都为空时拒绝执行**——避免面板上误点一下就把几年存档清光。
        """
        if not before and not codes:
            raise ValueError("必须指定 before 日期或 codes，否则会清空全部存档")
        conds, params = [], []
        if before:
            conds.append("date<?")
            params.append(before)
        if codes:
            ph = ",".join("?" for _ in codes)
            conds.append(f"code IN ({ph})")
            params.extend(codes)
        sql = "DELETE FROM bars WHERE " + " AND ".join(conds)
        with self._lock, self._db() as conn:
            cur = conn.execute(sql, params)
            deleted = cur.rowcount or 0
            if codes:
                for c in codes:
                    conn.execute("DELETE FROM archive_meta WHERE code=?", (c,))
            elif before:
                conn.execute("DELETE FROM archive_meta")
        return {"deleted": deleted}


def _human_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ====================================================================== 增量更新


def update_from_provider(archive: Archive, codes: Sequence[str],
                         fetch_fn: Any, adjust: str = "raw",
                         limit: int = 640) -> Dict[str, Any]:
    """对一批标的做增量更新。

    `fetch_fn(code, adjust, limit)` → bars 列表（由调用方注入，便于单测）。

    **增量策略**：先看存档里每只已有的最后日期，只请求"可能的新数据"。
    但免费接口没有"按日期区间取增量"的能力（都是取最近 N 根），所以实际
    做法是**每次取最近 N 根再按主键去重覆盖**——写入是幂等的，
    代价只是多传一些已存过的数据。这样比"漏掉某天"安全得多。
    """
    result: Dict[str, Any] = {"updated": [], "failed": [], "total_saved": 0}
    for code in codes:
        try:
            bars = fetch_fn(code, adjust, limit)
        except Exception as exc:
            logger.warning("[astock] 存档更新失败 %s: %s", code, exc)
            result["failed"].append({"code": code, "error": str(exc)[:120]})
            continue
        if not bars:
            result["failed"].append({"code": code, "error": "无数据返回"})
            continue
        info = archive.save_bars(code, bars, adjust=adjust)
        result["total_saved"] += info["saved"]
        result["updated"].append({"code": code, **info})
    return result


def merged_bars(archive: Archive, code: str, adjust: str = "raw",
                fallback: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """把存档与在线数据**按口径合并**（存档优先，在线补缺口）。

    为什么不是简单 concat：同一日期两边都有时必须只保留一份，
    否则回测会重复计算那一天。存档优先——它记录的是"当时的事实"，
    在线的前复权价会随分红重算。
    """
    archived = {b["date"]: b for b in archive.load_bars(code, adjust=adjust)}
    for b in fallback or []:
        d = str(b.get("date") or "")
        if d and d not in archived:
            archived[d] = b
    return [archived[d] for d in sorted(archived)]


def merged_bars_dicts(archived: Sequence[Dict[str, Any]],
                      online: Sequence[Dict[str, Any]]
                      ) -> List[Dict[str, Any]]:
    """纯列表版本的合并（不依赖 Archive 实例，便于单测与工具层复用）。

    `archived` 优先：同一日期两边都有时保留存档那份。
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for b in online or []:
        d = str(b.get("date") or "")
        if d:
            by_date[d] = b
    for b in archived or []:
        d = str(b.get("date") or "")
        if d:
            by_date[d] = b        # 存档覆盖在线
    return [by_date[d] for d in sorted(by_date)]
