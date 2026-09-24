"""lite-work 社区插件：jev（Jev 判定层）。

把 TypeSafe 的 Jev（System One 判定模型）接进 lite-work，作为一层"判定器"：
工具（jev_score / jev_choice）+ 工具执行前的单向升级器（before_tool）。

设计约束（详见 lite-work 侧设计文档 §6.1「三条不可让步的原则」与 §12.7「未启动」）：

1. **单向棘轮**：本插件只能把"本会放行"的调用**升级**为拦截；结构上不可能放行
   任何东西——它没有审批卡 API，也从不把 cancel 置回 False。
2. **fail-closed**：判定失败（超时/429/529/schema 不符/未采信）一律**让行**，
   即交给排在后面的 SecurityPlugin 按基础规则处理，绝不因为"判不了"而放行。
3. **配置缺失 = 未启动**：装配期（install）判定一次，不通过就立即返回——
   不注册工具、不挂钩子、零网络，只记一条 info。补齐配置后下一条消息生效。
4. **不引官方 SDK / 不打 wheels**：只用 lite-work 内置的 httpx，保证打包态可用。

配置（lite-work `config.json` 顶层 `jev` 对象；密钥优先 config，其次环境变量 `TYPESAFE_API_KEY`）

```
键                      默认                 说明
api_key                 空                   缺失即"未启动"；生产建议用环境变量
model                   jev-1.13.0           固定版本，别用会移动的别名
base_url                官方端点             可指向自建网关 / 代理
confidence_threshold    0.60                 低于此值不采信，交回基础规则
size_gate_tokens        8192                 超过则不送 Jev
timeout_ms              800                  超时视为未采信（fail-closed）
tools_enabled           true                 jev_* 工具是否暴露（按需付费）
auto_gate_enabled       false                自动门禁（每轮成本），默认关
```

未启动语义：配置不完整 = 未启动，与"未启用"等价——不注册工具、不挂钩子、零网络。
补齐配置后：钩子类能力下一条消息即生效；**工具面**受 `_all_tool_names()` 的
`TOOL_NAMES_TTL`（60s）缓存约束，最长 60 秒进入模型工具表。手动复制插件目录需重启应用
（`_ensure_local_plugins()` 缓存实例），经设置页安装则无需重启。

许可与治理（AGENTS.md §6）：本文件是**社区插件**源码（目标仓 laynepeng/lite-work-plugins，
整体 MIT），因此**不添加任何 Apache SPDX 头**，与内置 office.py / ocr.py 同口径。

兼容性：**设置表单**（Base URL / 自定义请求头 / 阈值等）需要 lite-work ≥ 1.10.0 的
通用插件 UI 协议；在更老的核心上插件依然可用（工具 + 工具执行前升级器），只是设置页
不会出现配置表单，此时请手改 config.json 顶层的 `jev` 对象。

注意：HTTP 端点与问题字段依据官方文档整理（docs.typesafe.ai），**尚未真机联调**
（需要 API Key）。首次联调请核对设计文档 §2.2 的 API 契约再改 `_build_questions`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from litework.core.kernel import Kernel
from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

logger = logging.getLogger("litework.plugins.jev")

PLUGIN_NAME = "jev"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13"            # 网关实际可用名（官方别名 jev-latest 会移动，不建议）
DEFAULT_THRESHOLD = 0.60
DEFAULT_SIZE_GATE_TOKENS = 8192
DEFAULT_TIMEOUT_MS = 3000        # 真机实测单次约 1.3s（800ms 会频繁超时）

#: 只对"有副作用"的工具做门禁评估；其余工具直接让行（零成本、零延迟）
GATED_TOOLS = frozenset({
    "execute_command",
    "delete_file",
    "write_file",
    "apply_search_replace",
    "apply_unified_diff",
})

#: 观测计数（测试与 UI 都会读）
STATS: Dict[str, int] = {"judgements": 0, "errors": 0, "blocked": 0, "skipped_untrusted": 0}

#: 最近判断记录（进程内环形缓冲，供右栏「判断」面板展示；不落盘、不外发）
_RECENT: List[Dict[str, Any]] = []
_RECENT_MAX = 50


def record_judgement(entry: Dict[str, Any]) -> None:
    """记录一次判断（仅本进程内存态；面板展示用）。"""
    _RECENT.append(entry)
    if len(_RECENT) > _RECENT_MAX:
        del _RECENT[0: len(_RECENT) - _RECENT_MAX]


# ---------------------------------------------------------------- 配置

class JevConfig:
    """插件配置快照。每次装配/调用都**现读**（app.config 是内存 dict 原地更新）。"""

    __slots__ = ("api_key", "model", "base_url", "headers", "threshold",
                 "size_gate_tokens", "timeout_ms", "tools_enabled",
                 "auto_gate_enabled", "has_header_credential",
                 "direct_answer", "direct_answer_confidence", "reason")

    def __init__(self, raw: Dict[str, Any], env_key: str = "") -> None:
        self.api_key = str(raw.get("api_key") or env_key or "").strip()
        model = raw.get("model", DEFAULT_MODEL)
        self.model = str(model or "").strip()
        self.base_url = str(raw.get("base_url") or DEFAULT_BASE_URL).strip()
        self.threshold = float(raw.get("confidence_threshold") or DEFAULT_THRESHOLD)
        self.size_gate_tokens = int(raw.get("size_gate_tokens") or DEFAULT_SIZE_GATE_TOKENS)
        self.timeout_ms = int(raw.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        # 自定义请求头（网关 / 代理常用）：键值都强制为字符串
        raw_headers = raw.get("headers")
        if isinstance(raw_headers, str):        # 容错：被存成了 JSON 字符串
            try:
                raw_headers = json.loads(raw_headers)
            except Exception:  # noqa: BLE001
                raw_headers = None
        self.headers = ({str(k): str(v) for k, v in raw_headers.items()}
                        if isinstance(raw_headers, dict) else {})
        # 用户直问短路（opt-in，默认关）：命中 /jev 语法时由 Jev 直接作答、跳过 LLM
        self.direct_answer = bool(raw.get("direct_answer_enabled", False))
        self.direct_answer_confidence = float(
            raw.get("direct_answer_confidence") or 0.85)
        # 启动门通过之后才受这两个开关控制（成本模型不同，见文档 §12.7）
        self.tools_enabled = bool(raw.get("tools_enabled", True))
        self.auto_gate_enabled = bool(raw.get("auto_gate_enabled", False))

        # 凭证来源：API Key 字段，或自定义请求头里带的凭证（网关常用 x-api-key / token）
        _CRED_HEADS = {"authorization", "x-api-key", "api-key", "apikey", "token", "x-token"}
        self.has_header_credential = any(
            str(k).strip().lower() in _CRED_HEADS and str(v).strip()
            for k, v in self.headers.items()
        )
        if not self.api_key and not self.has_header_credential:
            self.reason = ("缺少凭证（在「API Key」填 key，或在「自定义请求头」里带 x-api-key / token 等）")
        elif not self.model:
            self.reason = "模型未设置（填固定版本，如 jev-1.13）"
        else:
            self.reason = ""

    @property
    def ready(self) -> bool:
        return not self.reason

    @property
    def timeout_s(self) -> float:
        return max(0.05, self.timeout_ms / 1000.0)


#: 磁盘 jev 段缓存（path+mtime → 内容），避免每次 hook 都读盘
_DISK_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}


_PRIVATE_NAME = "config.local.json"
def _private_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _PRIVATE_NAME)


def _read_private() -> Dict[str, Any]:
    """读插件私有配置（首配置成功后镜像下来的凭证快照）。"""
    try:
        with open(_private_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_private(data: Dict[str, Any]) -> None:
    """写插件私有配置（0600；原子替换）。"""
    path = _private_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[jev] 写私有配置失败", exc_info=True)


_CRED_KEYS = ("api_key", "base_url", "headers", "model", "direct_answer_enabled")


def _self_heal(app: Any, raw: Dict[str, Any]) -> None:
    """把有效凭证镜像到私有文件；若主 config 被抹过，顺手修回来。"""
    if app is None or not getattr(app, "config_dir", None):
        return          # 没有 config_dir = 非真 app（测试替身）→ 不镜像，避免污染
    snapshot = {k: raw[k] for k in _CRED_KEYS if raw.get(k)}
    if not snapshot:
        return
    priv = _read_private()
    if any(priv.get(k) != snapshot[k] for k in snapshot):
        _write_private({**priv, **snapshot})
    # 主 config 缺 api_key 而我们有 → 修回（让设置页也正常）
    # 自限：修回成功后 cur 就有 api_key，下次不再进入，不会形成写盘循环
    if app is None or not snapshot.get("api_key"):
        return
    try:
        cur = dict(getattr(app, "config", {}).get(PLUGIN_NAME) or {})
        if not cur.get("api_key"):
            app.save_config({PLUGIN_NAME: {**cur, **snapshot}})
            logger.info("[jev] 已用私有快照修复被抹掉的配置")
    except Exception:  # noqa: BLE001
        logger.debug("[jev] 自愈写回失败", exc_info=True)


def _read_disk_jev(config_dir: str) -> Dict[str, Any]:
    """读 `<config_dir>/config.json` 里的 `jev` 段（按 mtime 缓存）。"""
    path = os.path.join(config_dir, "config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _DISK_CACHE["path"] == path and _DISK_CACHE["mtime"] == mtime:
        return _DISK_CACHE["data"]
    data: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and isinstance(cfg.get(PLUGIN_NAME), dict):
            data = dict(cfg[PLUGIN_NAME])
    except Exception:  # noqa: BLE001 - 读不到就当没有
        data = {}
    _DISK_CACHE.update({"path": path, "mtime": mtime, "data": data})
    return data


def read_config(kernel: Kernel) -> JevConfig:
    """读配置：内存 `app.config["jev"]` 为主，**磁盘 config.json 兜底**，环境变量再兜底。

    为什么要有磁盘/环境兜底：app 的配置只在启动时读一次，内存是一份可能过期的快照；
    实测事故：用户配好的凭证被其他保存动作从内存抹掉后，插件一直报「缺少凭证」。
    这里对**内存缺失的键**用磁盘补齐（内存有值仍以内存为准），环境变量则永远有效。
    """
    raw: Dict[str, Any] = {}
    app = None
    if kernel.has_service("app"):
        try:
            app = kernel.get_service("app")
            cfg = getattr(app, "config", None)
            if isinstance(cfg, dict):
                raw = dict(cfg.get(PLUGIN_NAME) or {})
        except Exception:  # noqa: BLE001
            logger.debug("[jev] 读取 app.config 失败", exc_info=True)
            raw = {}
    if app is not None:
        cfg_dir = getattr(app, "config_dir", None)
        if cfg_dir:
            for k, v in _read_disk_jev(str(cfg_dir)).items():
                if k not in raw or raw.get(k) in (None, "", {}, []):
                    raw[k] = v
    # 环境变量兜底：配置被抹也不受影响
    if os.environ.get("JEV_BASE_URL") and not raw.get("base_url"):
        raw["base_url"] = os.environ["JEV_BASE_URL"]
    if os.environ.get("JEV_MODEL") and not raw.get("model"):
        raw["model"] = os.environ["JEV_MODEL"]
    if os.environ.get("JEV_HEADERS") and not raw.get("headers"):
        raw["headers"] = os.environ["JEV_HEADERS"]
    # 私有文件兜底（首配置成功后的镜像）：比内存/主 config 都更能扛"被抹"
    priv = _read_private()
    for k, v in priv.items():
        if k not in raw or raw.get(k) in (None, "", {}, []):
            raw[k] = v
    # 自愈：镜像当前有效凭证 + 必要时修回主 config
    _self_heal(app, raw)
    return JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))


# ---------------------------------------------------------------- HTTP（可 mock）

def post_systemone(cfg: JevConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
    """调用 System One，**对瞬时错误重试**（最多 3 次：1 次 + 2 次重试）。

    实测：opencode 网关偶发 TLS 断开（UNEXPECTED_EOF，走代理时尤其明显）。
    瞬时错误（连接/读/超时/429/5xx）重试可吸收抖动；判定失败仍是 fail-closed。
    独立函数，便于测试 monkeypatch 与调用计数。
    """
    STATS["judgements"] += 1
    headers = dict(cfg.headers)          # 自定义头优先（网关常用 x-api-key）
    if cfg.api_key:
        headers.setdefault("Authorization", f"Bearer {cfg.api_key}")
    if not headers:
        raise RuntimeError("未配置任何凭证（api_key 或自定义请求头）")

    transient = (
        httpx.ConnectError, httpx.ConnectTimeout,
        httpx.ReadError, httpx.ReadTimeout,
    )
    last_exc: Optional[BaseException] = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=cfg.timeout_s) as client:
                resp = client.post(cfg.base_url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except transient as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))     # 0.4s / 0.8s 退避
                continue
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code in (429, 500, 502, 503, 529):
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
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


# ---------------------------------------------------------------- 判断卡渲染

def render_card(*, model: str, latency_ms: int, verdict: str, confidence: float,
                threshold: float, score: Optional[int] = None, legend: str = "",
                scale_total: Optional[int] = None,
                probabilities: Optional[Dict[str, float]] = None, questions: Optional[List[str]] = None,
                evidence: str = "", accepted: Optional[bool] = None,
                direct: bool = False, note: str = "") -> str:
    """Markdown 判断卡（纯文本）。

    为什么不用富组件：实测富卡片在会话流里的观感**不如纯 Markdown 列表**（用户结论），
    故回滚。只使用纯文本 + 列表 + 行内 code + ASCII 条形图——ChatView 的 Markdown
    渲染未开 rehype-raw，内联 HTML / 样式一律无效。
    """
    if accepted is None:
        accepted = confidence >= threshold
    mark = {"allow": "放行", "confirm": "需确认", "block": "拦截"}.get(verdict, verdict)
    head = "**Jev 直答（未使用 LLM）**\n\n" if direct else ""
    lines = [
        f"{head}**Jev 判断** · {model} · {latency_ms}ms",
        "",
        f"- 结论：{mark}",
        f"- 置信：`{confidence:.2f}` / 阈值 `{threshold:.2f}` → "
        + ("**已采信**" if accepted else "**未采信**（交回基础规则）"),
    ]
    if score is not None and scale_total:
        # 真机事实：score 是 criteria 的**档位下标**（0 基，可为小数），legend 是 {下标: 标签}
        pos = max(0, min(int(scale_total) - 1, int(round(float(score)))))
        bars = "█" * (pos + 1) + "░" * (int(scale_total) - pos - 1)
        lines.append(f"- 档位：`{pos + 1}/{int(scale_total)}` {bars}"
                     + (f"　**{legend}**" if legend else ""))
    elif score is not None:
        filled = max(0, min(10, int(score)))
        lines.append(f"- 风险：`{filled}/10` {'█' * filled}{'░' * (10 - filled)}"
                     + (f"　{legend}" if legend else ""))
    if probabilities:
        lines.append("- 行动概率：")
        for name, p in sorted(probabilities.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {name}：`{p * 100:.0f}%` {'█' * int(round(p * 20))}")
    if questions:
        lines.append("- 问了什么：")
        for n, q in enumerate(questions, 1):
            lines.append(f"  {n}. {q}")
    if evidence:
        lines.append(f"- 依据：{evidence}")
    if note:
        lines.append(f"- 提示：{note}")
    lines.append("")
    lines.append("_判断结果可被用户覆盖；Jev 只做建议，不替代基础安全规则。_")
    return "\n".join(lines)


# ---------------------------------------------------------------- 插件

class JevPlugin(ToolPlugin):
    name = PLUGIN_NAME
    version = "0.6.5"
    description = "Jev 判定层：System One 结构化判断（工具 + 工具执行前单向升级器）"

    #: 通用插件 UI 协议：设置页据此自动渲染表单（含自定义 Base URL 与请求头）
    contributes = {
        "settings": [
            {"key": "base_url", "type": "str", "label": "Base URL",
             "default": DEFAULT_BASE_URL,
             "hint": "System One 兼容端点；可指向自建网关 / 代理"},
            {"key": "headers", "type": "map", "label": "自定义 Header（每行一个，可留空）",
             "hint": "格式 Key: Value 或 Key=Value（按第一个分隔符切分，值可含冒号）；"
                     "# 开头忽略；可覆盖默认 Authorization；这里带凭证也算已配置"},
            {"key": "api_key", "type": "secret", "label": "API Key",
             "hint": "默认用于 Authorization: Bearer；也可留空、改把 key 放进自定义请求头"},
            {"key": "model", "type": "str", "label": "模型版本", "default": DEFAULT_MODEL,
             "hint": "建议固定版本 ID，别名会移动"},
            {"key": "confidence_threshold", "type": "number", "label": "置信度阈值",
             "default": DEFAULT_THRESHOLD},
            {"key": "size_gate_tokens", "type": "number", "label": "size-gate（token）",
             "default": DEFAULT_SIZE_GATE_TOKENS},
            {"key": "timeout_ms", "type": "number", "label": "超时（毫秒）",
             "default": DEFAULT_TIMEOUT_MS},
            {"key": "tools_enabled", "type": "boolean", "label": "暴露 jev_* 工具",
             "default": True},
            {"key": "auto_gate_enabled", "type": "boolean", "label": "自动门禁（每轮成本）",
             "default": False},
            {"key": "direct_answer_enabled", "type": "boolean",
             "label": "用户直问短路（跳过 LLM）", "default": False,
             "hint": "开启后：消息以 /jev 是否|打分|选 开头时，由 Jev 直接作答、不调用主模型"},
            {"key": "direct_answer_confidence", "type": "number",
             "label": "直答置信阈值", "default": 0.85,
             "hint": "低于此值不直答，改为正常交给主模型（避免误抢答）"},
        ],
        "panels": [
            {"id": "judgements", "title": "判断"},
        ],
        "commands": [
            {"name": "jev",
             "description": "Jev 直答：跳过主模型，由 System One 直接判断",
             "argsHint": "是否 <陈述> · 打分 <内容> · 选 A|B|C | <内容>"},
        ],
    }

    def __init__(self) -> None:
        self._kernel: Optional[Kernel] = None
        self.status = "not_installed"        # not_started | running
        self.status_reason = ""

    # -------------------------------------------------- 装配（启动门）
    def install(self, kernel: Kernel) -> None:
        """启动门：配置不完整 = 未启动（不注册工具、不挂钩子、零网络）。"""
        self._kernel = kernel
        cfg = read_config(kernel)

        if not cfg.ready:
            self.status = "not_started"
            self.status_reason = cfg.reason
            # 只记一条 info：这不是错误，也不该污染插件列表的"加载失败"
            logger.info("[jev] 未启动：%s", cfg.reason)
            return

        if cfg.tools_enabled:
            super().install(kernel)          # 注册 jev_score / jev_choice
        if cfg.auto_gate_enabled:
            self._install_gate(kernel)
        if cfg.direct_answer:
            self._install_direct_answer(kernel)

        self.status = "running"
        self.status_reason = ""
        logger.info("[jev] 已启动（tools=%s, auto_gate=%s, model=%s）",
                    cfg.tools_enabled, cfg.auto_gate_enabled, cfg.model)

    def status_from_config(self, config):
        """设置页显示「运行中 / 未启动 + 原因」（纯读：不发网络、不写盘）。"""
        raw = dict(config.get(PLUGIN_NAME) or {}) if isinstance(config, dict) else {}
        cfg = JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))
        if not cfg.ready:
            return {"state": "not_started", "reason": cfg.reason}
        return {"state": "running", "reason": ""}

    # -------------------------------------------------- 工具
    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="jev_score",
                description=(
                    "用 Jev（System One 判定模型）把一段内容归入给定的**有序档位**，"
                    "返回选中的档位（含标签）与校准置信度。适合：风险分级、质量评估、严重度判定。"
                    "不返回自然语言解释。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "description": "要判断的内容（精简摘要，建议 < 8k token）"},
                        "instructions": {"type": "string", "description": "判断指令，例如「这段改动有多危险」"},
                        "criteria": {
                            "type": "array", "items": {"type": "string"},
                            "description": "有序档位标签，从低到高（越靠后档位越高）；Jev 返回档位下标",
                        },
                    },
                    "required": ["state", "instructions"],
                },
            ),
            ToolDefinition(
                name="jev_choice",
                description=(
                    "用 Jev 在给定选项中选择最可能的一项，返回选择、各选项概率与置信度。"
                    "适合：归类、二选一、优先级排序。不返回自然语言解释。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "description": "要判断的内容（精简摘要）"},
                        "instructions": {"type": "string", "description": "判断指令，例如「这属于哪一类」"},
                        "options": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "object", "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"}}},
                                ]
                            },
                            "description": "候选选项（≤255）：字符串，或 {'label','description'}；描述能显著提升判断质量",
                        },
                    },
                    "required": ["state", "instructions", "options"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        """工具执行入口。任何失败都返回**可读错误**，不抛异常。"""
        kernel = self._kernel
        cfg = read_config(kernel) if kernel is not None else JevConfig({})
        if not cfg.ready:
            # 理论上不会发生（未启动就不会注册工具），防御性兜底
            return f"Jev 未启动：{cfg.reason}"

        state = str(args.get("state") or "")
        instructions = str(args.get("instructions") or "")
        if not state.strip() or not instructions.strip():
            return "参数不全：需要 state（要判断的内容）与 instructions（判断指令）。"
        if _approx_tokens(state) > cfg.size_gate_tokens:
            return (f"内容超出 size-gate（约 {_approx_tokens(state)} > {cfg.size_gate_tokens} token），"
                    "未送 Jev。请先用摘要，或缩小范围。")

        if name == "jev_score":
            criteria = [str(c) for c in (args.get("criteria") or []) if str(c).strip()]
            kind = "score"
            if not criteria:
                criteria = ["1 无害", "5 中等", "10 破坏性"]
        elif name == "jev_choice":
            kind = "choice"
            options = args.get("options") or []
            criteria = {}
            for idx, opt in enumerate(options):
                if isinstance(opt, dict):
                    label = str(opt.get("label") or opt.get("value") or f"option{idx + 1}")
                    desc = str(opt.get("description") or "") or label
                else:
                    label = str(opt)
                    desc = label
                if label:
                    criteria[label] = desc
            if not criteria:
                return "参数不全：jev_choice 需要 options（候选选项，字符串或 {label, description}）。"
            if len(criteria) > 255:
                return f"选项过多（{len(criteria)} > 255），请先收敛候选。"
        else:
            return f"未知工具：{name}"

        payload = {"state": state, "model": cfg.model,
                   "questions": _build_questions(kind, instructions, criteria)}
        started = time.time()
        try:
            data = await asyncio.to_thread(post_systemone, cfg, payload)
        except Exception as exc:  # noqa: BLE001 - 一律降级为可读错误
            STATS["errors"] += 1
            logger.warning("[jev] 调用失败：%s", exc)
            return (f"Jev 暂不可用（{type(exc).__name__}）：已回退到基础规则，"
                    "请由你自行判断，或稍后重试。")
        latency_ms = int((time.time() - started) * 1000)
        return self._format(data, kind, cfg, latency_ms, instructions)

    def _format(self, data: Dict[str, Any], kind: str, cfg: JevConfig,
                latency_ms: int, instructions: str) -> str:
        answers = data.get("answers") or {}
        ans = answers.get("q1") if isinstance(answers, dict) else None
        if not isinstance(ans, dict) and isinstance(answers, dict) and answers:
            # 网关可能按我们给的 key 原样回；取第一个答案对象即可
            first = next(iter(answers.values()))
            ans = first if isinstance(first, dict) else None
        if not isinstance(ans, dict):
            return "Jev 返回格式不符合预期，已按未采信处理（fail-closed）。"

        record_judgement({
            "kind": kind,
            "confidence": float(ans.get("confidence") or 0.0),
            "choice": str(ans.get("choice") or ""),
            "score": ans.get("score"),
            "noul": ans.get("noul"),
            "probabilities": ans.get("probabilities") if isinstance(ans.get("probabilities"), dict) else {},
            "question": instructions,
            "context": f"Agent 调用 jev_{kind}",
            "point": "tool",
            "at": time.time(),
        })
        if kind == "choice":
            probs = ans.get("probabilities") or {}
            probs = {str(k): float(v) for k, v in probs.items()} if isinstance(probs, dict) else {}
            conf = float(ans.get("confidence") or 0.0)
            chosen = str(ans.get("choice") or "")
            verdict = {"放行": "allow", "请人确认": "confirm", "拦截": "block"}.get(chosen, chosen)
            return render_card(model=str(data.get("model") or cfg.model), latency_ms=latency_ms,
                               verdict=verdict, confidence=conf, threshold=cfg.threshold,
                               probabilities=probs, questions=[instructions],
                               evidence=f"state 摘要 {len(instructions)} 字")
        raw_score = ans.get("score")
        conf = float(ans.get("confidence") or 0.0)
        legend_map = ans.get("legend") if isinstance(ans.get("legend"), dict) else {}
        total = len(legend_map) or None
        pos = None
        if raw_score is not None:
            try:
                pos = int(round(float(raw_score)))
            except (TypeError, ValueError):
                pos = None
        label = ""
        if legend_map and pos is not None:
            label = str(legend_map.get(str(pos), legend_map.get(pos, "")))
        return render_card(model=str(data.get("model") or cfg.model), latency_ms=latency_ms,
                           verdict="confirm", confidence=conf, threshold=cfg.threshold,
                           score=pos, legend=label, scale_total=total,
                           questions=[instructions],
                           evidence=f"state 摘要 {len(instructions)} 字")

    # -------------------------------------------------- 用户直问短路（跳过 LLM）

    def _install_direct_answer(self, kernel: Kernel) -> None:
        """挂 LLM 调用前钩子：命中 /jev 语法且置信达标 → 直接作答、跳过主模型。

        与内核约定：答案放进 `ctx.metadata["final_answer"]`（AgentLoop 取用后即收尾）。
        异常/低置信一律**让行**（正常交给 LLM），绝不阻断、绝不臆造答案。
        """
        @kernel.before_llm.use
        async def _jev_direct(ctx, data, next):
            cfg = read_config(kernel)
            if not cfg.ready or not cfg.direct_answer:
                return await next(data)
            if ctx.metadata.get("final_answer"):
                return await next(data)          # 已被别的插件抢占
            turns = data if isinstance(data, list) else None
            if not turns:
                return await next(data)
            if str(getattr(turns[-1], "role", "")) != "user":
                return await next(data)          # 只在"最后一条是用户消息"时尝试
            parsed = _parse_direct_command(str(getattr(turns[-1], "content", "") or ""))
            if not parsed:
                return await next(data)
            try:
                answer = await self._answer_direct(cfg, parsed)
            except Exception:  # noqa: BLE001 - fail-closed：照常走 LLM
                STATS["errors"] += 1
                logger.warning("[jev] 直答失败，交回 LLM", exc_info=True)
                return await next(data)
            if answer:
                ctx.metadata["final_answer"] = {"content": answer, "source": "jev"}
            return await next(data)

    async def _answer_direct(self, cfg: JevConfig, parsed: Dict[str, Any]) -> str:
        """用 Jev 直接回答一条显式命令（Markdown）；低置信返回空串 → 交回 LLM。"""
        kind = parsed["kind"]
        payload = {"state": parsed["state"], "model": cfg.model,
                   "questions": _build_questions(kind, parsed["instructions"],
                                                 parsed["criteria"])}
        started = time.time()
        data = await asyncio.to_thread(post_systemone, cfg, payload)
        latency_ms = int((time.time() - started) * 1000)
        answers = data.get("answers") or {}
        ans = answers.get("q1") if isinstance(answers, dict) else None
        if not isinstance(ans, dict):
            return ""
        # noul 的语义是"陈述为真的概率"，不是置信度：
        #   noul=0.95 → 高置信"是"；noul=0.05 → 高置信"否"；noul≈0.5 → 抛硬币（低置信）。
        # 正确校准：置信 = |noul - 0.5| * 2（离抛硬币的距离）。
        if kind == "noul":
            noul_raw = ans.get("noul")
            if noul_raw is None:
                return ""          # 缺 noul 字段 = 无效响应 → 让行（不能当 0.0 → 假高置信"否"）
            noul_val = float(noul_raw)
            conf = abs(noul_val - 0.5) * 2.0
            ans["choice"] = "是" if noul_val >= 0.5 else "否"
        else:
            conf = float(ans.get("confidence") or 0.0)
        if conf < cfg.direct_answer_confidence:
            STATS["skipped_untrusted"] += 1
            return ""                              # 未达直答阈值 → 交回主模型
        record_judgement({"kind": kind, "confidence": conf,
                          "choice": str(ans.get("choice") or ""), "score": ans.get("score"),
                          "noul": ans.get("noul"),
                          "probabilities": ans.get("probabilities") if isinstance(ans.get("probabilities"), dict) else {},
                          "question": parsed["instructions"],
                          "context": f"用户直问 /jev {parsed['kind']}",
                          "point": "direct", "at": time.time()})
        head = ""   # 标识由 render_card(direct=True) 输出
        if kind == "choice":
            raw_probs = ans.get("probabilities") or {}
            probs = {str(k): float(v) for k, v in raw_probs.items()} \
                if isinstance(raw_probs, dict) else {}
            return head + render_card(
                model=str(data.get("model") or cfg.model), latency_ms=latency_ms,
                verdict=str(ans.get("choice") or ""), confidence=conf,
                threshold=cfg.direct_answer_confidence, probabilities=probs,
                questions=[parsed["instructions"]], evidence="来自消息内 /jev 命令",
                direct=True)
        legend_map = ans.get("legend") if isinstance(ans.get("legend"), dict) else {}
        total = len(legend_map) or None
        pos = None
        if ans.get("score") is not None:
            try:
                pos = int(round(float(ans["score"])))
            except (TypeError, ValueError):
                pos = None
        label = ""
        if legend_map and pos is not None:
            label = str(legend_map.get(str(pos), legend_map.get(pos, "")))
        return head + render_card(
            model=str(data.get("model") or cfg.model), latency_ms=latency_ms,
            verdict="noul" if kind == "noul" else "score", confidence=conf,
            threshold=cfg.direct_answer_confidence,
            score=pos, legend=label, scale_total=total,
            questions=[parsed["instructions"]], evidence="来自消息内 /jev 命令",
            direct=True)

    # -------------------------------------------------- 门禁（单向升级器）
    def _install_gate(self, kernel: Kernel) -> None:
        @kernel.before_tool.use
        async def _jev_gate(ctx, data, next):
            """门禁：**不拦截**，只附加风险意见到 data["jev_risk"]。

            拦截权始终在 SecurityPlugin / 用户手里。Jev 的角色是"安全顾问"——
            在审批卡弹出之前，多给用户一条 AI 风险判断，辅助决策。
            唯一例外：Jev 判定「拦截」且置信度极高（≥ 0.95）时才 cancel（防灾难性操作）。
            """
            cfg = read_config(kernel)
            if not cfg.ready or not cfg.auto_gate_enabled:
                return await next(data)
            tool = str(data.get("toolName") or "")
            # 只评估有副作用的工具；不评估自己（避免递归判定）
            if tool not in GATED_TOOLS or tool.startswith("jev_"):
                return await next(data)
            try:
                verdict = await self.judge(cfg, tool, data.get("args") or {})
            except Exception:  # noqa: BLE001 - fail-closed：判不了就让行
                STATS["errors"] += 1
                logger.warning("[jev] 门禁判定失败，交回基础规则", exc_info=True)
                return await next(data)

            conf = float(verdict.get("confidence") or 0.0)
            choice = str(verdict.get("choice") or "")
            if conf < cfg.threshold:
                STATS["skipped_untrusted"] += 1     # 未采信：不参与决策
                return await next(data)
            record_judgement({
                "kind": "gate",
                "confidence": conf,
                "choice": choice,
                "tool": tool,
                "probabilities": verdict.get("probabilities") if isinstance(verdict.get("probabilities"), dict) else {},
                "question": f"该工具调用（{tool}）应当放行、需人确认，还是拦截？",
                "context": f"工具执行前 {tool}",
                "point": "gate",
                "at": time.time(),
            })
            # 附加**结构化**风险意见：SecurityPlugin 会把它带进审批事件，
            # 前端在审批卡上渲染成"AI 风险意见"面板（只给建议，不代替用户决策）
            risk_label = {"allow": "低风险", "confirm": "中风险", "block": "高风险"}.get(choice, choice)
            data["judge_opinion"] = {
                "source": "Jev",
                "level": risk_label,
                "choice": choice,
                "confidence": round(conf, 2),
                "threshold": cfg.threshold,
                "why": str(verdict.get("why") or ""),
                "probabilities": verdict.get("probabilities") or {},
                "model": cfg.model,
            }
            # 唯一拦截条件：极高置信的「拦截」判定（≥ 0.95）
            if choice == "block" and conf >= 0.95:
                STATS["blocked"] += 1
                data["cancel"] = True
                data["reason"] = (
                    f"[Jev] 判定该操作风险极高（置信 {conf:.2f}）："
                    f"{verdict.get('why') or '不可逆或触及生产数据'}。"
                    "此调用已被 AI 安全判定阻断。"
                )
            # 其余结论一律让行：审批卡照常弹出，用户最终决定
            return await next(data)

    # -------------------------------------------------- 右栏「判断」面板

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """右栏「判断」面板：返回 JSON（前端渲染成 SVG 决策树）。"""
        if panel_id != "judgements":
            return ""
        raw = dict((config or {}).get(PLUGIN_NAME) or {}) if isinstance(config, dict) else {}
        cfg = JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))

        status: Dict[str, Any] = {}
        if cfg.ready:
            chips = []
            if cfg.tools_enabled:
                chips.append("工具")
            if cfg.auto_gate_enabled:
                chips.append("门禁")
            if cfg.direct_answer:
                chips.append("直答")
            status = {"text": "运行中", "tone": "ok",
                      "meta": [cfg.model, f"阈值 {cfg.threshold:.2f}"], "chips": chips}
        else:
            status = {"text": "未启动", "tone": "warn", "reason": cfg.reason}

        trees = []
        for item in reversed(_RECENT[-8:]):
            conf = float(item.get("confidence") or 0.0)
            accepted = conf >= cfg.threshold
            point = str(item.get("point", ""))
            kind = str(item.get("kind", ""))
            choice = str(item.get("choice") or "")
            score = item.get("score")
            noul = item.get("noul")
            probs = item.get("probabilities") or {}
            probs = {str(k): float(v) for k, v in probs.items()} if isinstance(probs, dict) else {}

            title = {"gate": "门禁判定", "direct": "直答判定", "tool": "工具判定"}.get(point, "判定")
            context = str(item.get("context") or "")
            question = str(item.get("question") or "")

            # 决策树的选项分支：标签 / 概率 / 语义色 / 是否选中
            opts: List[Dict[str, Any]] = []
            if kind == "choice" and probs:
                for k, v in sorted(probs.items(), key=lambda kv: -kv[1]):
                    opts.append({"label": str(k), "prob": round(float(v), 4),
                                 "tone": _prob_tone(str(k)), "chosen": (str(k) == choice)})
            elif kind == "score" and probs:
                try:
                    chosen_idx = int(round(float(score or 0)))
                except (TypeError, ValueError):
                    chosen_idx = 0
                for k, v in sorted(probs.items(), key=lambda kv: -float(kv[1])):
                    idx = int(float(k))
                    opts.append({"label": f"档位 {idx + 1}", "prob": round(float(v), 4),
                                 "tone": "alt" if idx == chosen_idx else "",
                                 "chosen": (idx == chosen_idx)})
            elif kind == "noul" and noul is not None:
                noul_v = float(noul)
                opts = [{"label": "是", "prob": round(noul_v, 4), "tone": "ok", "chosen": noul_v >= 0.5},
                        {"label": "否", "prob": round(1.0 - noul_v, 4), "tone": "",
                         "chosen": noul_v < 0.5}]
            if not opts:
                opts = [{"label": choice or str(score or "—"), "prob": round(conf, 4),
                         "tone": "alt", "chosen": True}]

            trees.append({"title": title, "context": context, "question": question,
                          "kind": kind, "options": opts,
                          "confidence": round(conf, 4), "threshold": cfg.threshold,
                          "accepted": accepted})

        data = {"type": "lite-tree", "status": status,
                "trees": trees, "total": len(_RECENT)}
        return json.dumps(data, ensure_ascii=False)

    async def judge(self, cfg: JevConfig, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """对一次工具调用做判断（可被测试 monkeypatch）。"""
        state = _summarize_action(tool, args)
        payload = {
            "state": state,
            "model": cfg.model,
            "questions": _build_questions(
                "choice",
                f"该工具调用（{tool}）应当放行、需人确认，还是拦截？",
                {"放行": "无副作用或完全可逆，风险低",
                 "请人确认": "有中等风险或不可逆，但可能是合理操作",
                 "拦截": "不可逆或触及生产数据，风险高"},
            ),
        }
        data = await asyncio.to_thread(post_systemone, cfg, payload)
        ans = (data.get("answers") or {}).get("q1") or {}
        raw_probs = ans.get("probabilities") or {}
        probs = ({str(k): round(float(v), 4) for k, v in raw_probs.items()}
                 if isinstance(raw_probs, dict) else {})
        return {
            "choice": {"放行": "allow", "请人确认": "confirm", "拦截": "block"}.get(
                str(ans.get("choice") or ""), "confirm"),
            "confidence": float(ans.get("confidence") or 0.0),
            "why": str(ans.get("why") or ""),
            # 概率分布：供审批卡展示"拦截 88% / 放行 9% / 请人确认 3%"
            "probabilities": probs,
        }


# ---------------------------------------------------------------- 小工具

def _parse_direct_command(text: str) -> Optional[Dict[str, Any]]:
    """解析显式直答命令（不命中返回 None → 正常交给 LLM）。

    /jev 是否 <判断语句>       → noul
    /jev 打分 <内容>           → score（1–10）
    /jev 选 A|B|C | <内容>     → choice
    """
    body = (text or "").strip()
    if not body.startswith("/jev"):
        return None
    rest = body[4:].strip()
    if not rest:
        return None
    if rest.startswith("是否"):
        statement = rest[2:].strip()
        if not statement:
            return None
        return {"kind": "noul", "state": statement,
                "instructions": "该情况/陈述是否属实或成立？请基于常识直接判断",
                "criteria": None}
    if rest.startswith("打分"):
        content = rest[2:].strip()
        if not content:
            return None
        return {"kind": "score", "state": content,
                "instructions": "按下面档位评估该内容的整体质量",
                "criteria": ["很差", "较差", "一般", "较好", "很好"]}
    if rest.startswith("选"):
        rest2 = rest[1:].strip()
        # 选项之间用 |，选项与内容之间用 " | "（空格竖线空格）分隔 ——
        # 必须按【最后一个】" | " 切分，否则 "A|B|C | 内容" 会被切坏。
        if " | " not in rest2:
            return None
        head_opts, _, tail = rest2.rpartition(" | ")
        options = [o.strip() for o in head_opts.split("|") if o.strip()]
        state = tail.strip()
        if len(options) < 2 or not state:
            return None
        return {"kind": "choice", "state": state,
                "instructions": "在这些选项中选最合适的一个",
                "criteria": {o: o for o in options}}
    return None


def _prob_tone(key: str) -> str:
    """选项语义 → 语义色（danger/ok/warn/空=中性）。"""
    if any(x in key for x in ("拦截", "否", "高", "危险")):
        return "danger"
    if any(x in key for x in ("放行", "是", "低", "安全")):
        return "ok"
    if any(x in key for x in ("确认", "中")):
        return "warn"
    return ""


def _approx_tokens(text: str) -> int:
    """粗估 token（不引 tokenizer，够用于 size-gate）。"""
    return max(1, len(text) // 3)


def _summarize_action(tool: str, args: Dict[str, Any]) -> str:
    """把一次工具调用压成简短 state（避免把大文件正文送去第三方）。"""
    parts = [f"工具: {tool}"]
    for key in ("command", "filePath", "path", "query"):
        val = args.get(key)
        if val:
            parts.append(f"{key}: {str(val)[:200]}")
    extra = {k: str(v)[:80] for k, v in args.items()
             if k not in {"command", "filePath", "path", "query", "content",
                          "searchBlock", "replaceBlock"}}
    if extra:
        parts.append("其他参数: " + ", ".join(f"{k}={v}" for k, v in list(extra.items())[:5]))
    return "\n".join(parts)
