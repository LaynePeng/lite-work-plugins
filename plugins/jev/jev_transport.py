"""jev 插件 · 传输层（从 plugin.py 拆出）。

⚠️ 命名与导入方式同 `jev_config.py`：同目录、裸 import（宿主把插件目录插进
`sys.path`）、**唯一前缀**命名，不能用包相对导入。

内容：观测计数、判定结果缓存、后台任务调度（不阻塞关键路径）、
HTTP 传输（AsyncClient）、以及"问一个 noul / choice 问题"的高层封装。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx                      # 主程序已捆绑；禁止为此打 wheels（AGENTS.md）

from jev_config import JevConfig

logger = logging.getLogger("litework.plugins.jev")


# ---------------------------------------------------------------- 观测计数

#: 观测计数（测试与 UI 都会读）
STATS: Dict[str, int] = {"judgements": 0, "errors": 0, "blocked": 0,
                         "skipped_untrusted": 0, "cache_hits": 0,
                         "dropped": 0, "notes": 0}

#: 最近判断记录（进程内环形缓冲，供右栏「判断」面板展示；不落盘、不外发）
_RECENT: List[Dict[str, Any]] = []
_RECENT_MAX = 50


def record_judgement(entry: Dict[str, Any]) -> None:
    """记录一次判断（仅本进程内存态；面板展示用）。"""
    _RECENT.append(entry)
    if len(_RECENT) > _RECENT_MAX:
        del _RECENT[0: len(_RECENT) - _RECENT_MAX]


# ---------------------------------------------------------------- 判定缓存
#
# 「同样的内容只判一次」：screen 会反复判同一个文件的不同片段、verify 会在
# before_llm 与 before_finish 两次遇到同一条完成声称。按内容哈希缓存判定结果，
# 命中即零网络、零延迟（原实现对每次调用各发一次 HTTP）。

_CACHE_MAX = 256
_CACHE: "Dict[str, Tuple[float, Dict[str, Any]]]" = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(kind: str, state: str, model: str = "") -> str:
    """判定缓存键：内容 + 模型（换模型后旧结论不复用）。"""
    digest = hashlib.sha256(state.encode("utf-8", "replace")).hexdigest()[:32]
    return f"{kind}|{model}|{digest}"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        expire_at, value = hit
        if expire_at <= now:                     # 过期即失效（不做 LRU 提升，够用）
            _CACHE.pop(key, None)
            return None
        return dict(value)


def _cache_put(key: str, value: Dict[str, Any], ttl_s: float) -> None:
    if ttl_s <= 0:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:            # 简易淘汰：丢最早插入的一条
            try:
                _CACHE.pop(next(iter(_CACHE)))
            except (StopIteration, KeyError):
                pass
        _CACHE[key] = (time.time() + ttl_s, dict(value))


def cache_clear() -> None:
    """清空判定缓存（测试与设置页改配置后可用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------- 后台判定（不阻塞关键路径）
#
# P0：Screen 的 after_tool 判定原本同步串在工具返回路径上（一次 HTTP 阻塞工具返回）。
# 现在改为 fire-and-forget：结论经 _queue_note 暂存，由下一次 before_llm 注入上下文。

_BG_TASKS: "set[asyncio.Task]" = set()
_BG_MAX_INFLIGHT = 8

#: 跨 hook 传给下一轮的待注入提示（session_id → [文本]）
_PENDING_NOTES: Dict[str, List[str]] = {}
_NOTES_MAX = 20
_NOTES_LOCK = threading.Lock()


def _queue_note(session_id: str, text: str, *, limit: int = _NOTES_MAX) -> None:
    """把一条判定结论排入"下一轮注入"队列（同会话 FIFO，超限丢最旧）。"""
    text = (text or "").strip()
    if not text:
        return
    with _NOTES_LOCK:
        notes = _PENDING_NOTES.setdefault(str(session_id or ""), [])
        notes.append(text)
        if len(notes) > limit:
            del notes[0:len(notes) - limit]
    STATS["notes"] += 1


def _drain_notes(session_id: str) -> List[str]:
    """取出并清空某会话的待注入提示。"""
    with _NOTES_LOCK:
        return _PENDING_NOTES.pop(str(session_id or ""), [])


def pending_note_count(session_id: str = "") -> int:
    with _NOTES_LOCK:
        if session_id:
            return len(_PENDING_NOTES.get(str(session_id), []))
        return sum(len(v) for v in _PENDING_NOTES.values())


async def _guarded(coro: Any, label: str) -> None:
    """后台判定统一兜底：任何异常只记日志（fail-open，绝不影响主链路）。"""
    try:
        await coro
    except Exception:  # noqa: BLE001
        STATS["errors"] += 1
        logger.warning("[jev] 后台判定失败（%s），已按 fail-open 放行", label, exc_info=True)


def _spawn_bg(coro: Any, *, label: str = "bg", limit: Optional[int] = None) -> bool:
    """把判定协程丢到后台执行，立即返回（不阻塞调用方）。

    - 无运行中的事件循环 / 在飞任务已达上限 → 丢弃（fail-open，绝不排队等待）；
    - 返回是否真的派出（测试可据此断言"关键路径没有被阻塞"）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                         # 同步上下文（极少见）
        coro.close()
        STATS["dropped"] += 1
        return False
    cap = int(limit) if isinstance(limit, int) and limit > 0 else _BG_MAX_INFLIGHT
    if len(_BG_TASKS) >= cap:
        coro.close()
        STATS["dropped"] += 1
        logger.debug("[jev] 后台判定已达并发上限（%s），丢弃（%s）", cap, label)
        return False
    task = loop.create_task(_guarded(coro, label))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return True


async def wait_bg_tasks(timeout: float = 3.0) -> int:
    """等待当前在飞的后台判定结束（测试/收尾用）。返回完成的任务数。"""
    tasks = [t for t in list(_BG_TASKS) if not t.done()]
    if not tasks:
        return 0
    await asyncio.wait(tasks, timeout=timeout)
    return sum(1 for t in tasks if t.done())


# ---------------------------------------------------------------- HTTP（可 mock）

_TRANSIENT_EXC: "Tuple[type, ...]" = tuple(
    exc for exc in (
        getattr(httpx, "ConnectError", None),
        getattr(httpx, "ConnectTimeout", None),
        getattr(httpx, "ReadError", None),
        getattr(httpx, "ReadTimeout", None),
        getattr(httpx, "TimeoutException", None),
    ) if isinstance(exc, type)
) or (OSError,)

#: 可重试的 HTTP 状态码（限流 / 网关抖动）
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 529})


def _client_factory(timeout_s: float) -> Any:
    """构造 HTTP 客户端工厂（测试可替换成内存替身）。

    System One 调用是**同步语义的远程判定**；0.9.0 起改走 httpx.AsyncClient，
    避免 0.8.x 的「asyncio.to_thread + 同步 time.sleep 退避」占用线程池
    （线程池容量 min(32, cpu+4)，多会话并发时排队 → "偶发特别慢"）。
    """
    return httpx.AsyncClient(timeout=timeout_s)


async def post_systemone_async(cfg: JevConfig, payload: Dict[str, Any],
                               timeout_ms: Optional[int] = None) -> Dict[str, Any]:
    """异步调用 System One（瞬时错误最多 `cfg.max_attempts` 次）。

    与 0.8.x 的差别（P2 超时/重试收紧 + P0 不占线程池）：

    - 走 `httpx.AsyncClient`（原生异步），不再 `asyncio.to_thread` 包同步 client；
    - 退避用 `asyncio.sleep`（不阻塞工作线程）；上限 1 次重试，退避 0.25s；
    - 超时可单独指定（Verify 用更宽松的 `verify_timeout_ms`），判定超时即抛错，
      由调用方 fail-open 让行——**等待时间有硬上限**。
    """
    STATS["judgements"] += 1
    headers = dict(cfg.headers)          # 自定义头优先（网关常用 x-api-key）
    if cfg.api_key:
        headers.setdefault("Authorization", f"Bearer {cfg.api_key}")
    if not headers:
        raise RuntimeError("未配置任何凭证（api_key 或自定义请求头）")

    timeout_s = max(0.05, (timeout_ms or cfg.timeout_ms) / 1000.0)
    attempts = max(1, int(cfg.max_attempts))
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            client = _client_factory(timeout_s)
            try:
                resp = await client.post(cfg.base_url, json=payload, headers=headers)
            finally:
                aclose = getattr(client, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001 - 关闭失败无关判定结果
                        logger.debug("[jev] 关闭 HTTP 客户端失败", exc_info=True)
            resp.raise_for_status()
            return resp.json()
        except _TRANSIENT_EXC as exc:            # 瞬时网络错误 → 重试一次
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(0.25)
                continue
        except httpx.HTTPStatusError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status in _RETRY_STATUS:
                last_exc = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.25)
                    continue
            raise
    STATS["errors"] += 1
    assert last_exc is not None
    raise last_exc


def _build_questions(kind: str, instructions: str, criteria: Any) -> Dict[str, Any]:
    """构造 questions（以真实网关契约实测为准）。

    - choice：criteria 必须是**映射** {选项: 说明}（传数组会 422）
    - score ：criteria 是**有序数组** [各级含义]
    - noul ：无 criteria
    """
    q: Dict[str, Any] = {"type": kind, "instructions": instructions}
    if criteria:
        q["criteria"] = dict(criteria) if isinstance(criteria, dict) else list(criteria)
    return {"q1": q}


async def _judge_noul(cfg: JevConfig, state: str, instructions: str, *,
                      cache_kind: str, timeout_ms: Optional[int] = None,
                      ) -> Tuple[Optional[float], str, bool]:
    """判一个 noul 问题 → (noul, why, 是否命中缓存)；失败抛错（调用方 fail-open）。

    带内容缓存：同样的 state 不重复问（P1）。缓存 key 含 model，换模型自动失效。
    """
    key = _cache_key(cache_kind, state, cfg.model)
    cached = _cache_get(key)
    if cached is not None:
        STATS["cache_hits"] += 1
        return float(cached.get("noul", 0.5)), str(cached.get("why") or ""), True
    resp = await post_systemone_async(
        cfg, {"state": state, "model": cfg.model,
              "questions": _build_questions("noul", instructions, None)},
        timeout_ms=timeout_ms)
    ans = (resp.get("answers") or {}).get("q1") or {}
    raw = ans.get("noul")
    if raw is None:
        return None, "", False               # 缺字段 = 无效响应（不能当 0.0 用）
    noul = float(raw)
    why = str(ans.get("why") or "")
    _cache_put(key, {"noul": noul, "why": why}, cfg.cache_ttl_s)
    return noul, why, False


async def _answer_choice(cfg: JevConfig, state: str, instructions: str,
                         criteria: Dict[str, str], *,
                         cache_kind: str = "choice") -> Dict[str, Any]:
    """判一个 choice 问题 → {choice, confidence, why, probabilities}（带内容缓存）。"""
    key = _cache_key(cache_kind, state + "|" + "|".join(sorted(criteria or ())), cfg.model)
    cached = _cache_get(key)
    if cached is not None:
        STATS["cache_hits"] += 1
        return cached
    resp = await post_systemone_async(
        cfg, {"state": state, "model": cfg.model,
              "questions": _build_questions("choice", instructions, criteria)})
    ans = (resp.get("answers") or {}).get("q1") or {}
    if not isinstance(ans, dict):
        raise RuntimeError("响应格式不符合预期")
    raw_probs = ans.get("probabilities") or {}
    probs = ({str(k): round(float(v), 4) for k, v in raw_probs.items()}
             if isinstance(raw_probs, dict) else {})
    out = {
        "choice": str(ans.get("choice") or ""),
        "confidence": float(ans.get("confidence") or 0.0),
        "why": str(ans.get("why") or ""),
        "probabilities": probs,
        "model": str(resp.get("model") or cfg.model),
    }
    _cache_put(key, out, cfg.cache_ttl_s)
    return out
