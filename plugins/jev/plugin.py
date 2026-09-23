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
DEFAULT_TIMEOUT_MS = 800

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
                 "auto_gate_enabled", "reason")

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
        self.headers = ({str(k): str(v) for k, v in raw_headers.items()}
                        if isinstance(raw_headers, dict) else {})
        # 启动门通过之后才受这两个开关控制（成本模型不同，见文档 §12.7）
        self.tools_enabled = bool(raw.get("tools_enabled", True))
        self.auto_gate_enabled = bool(raw.get("auto_gate_enabled", False))

        if not self.api_key:
            self.reason = "缺少 API Key（设置 → System One，或环境变量 TYPESAFE_API_KEY）"
        elif not self.model:
            self.reason = "模型未设置（设置 → System One → 模型版本）"
        else:
            self.reason = ""

    @property
    def ready(self) -> bool:
        return not self.reason

    @property
    def timeout_s(self) -> float:
        return max(0.05, self.timeout_ms / 1000.0)


def read_config(kernel: Kernel) -> JevConfig:
    """从 app 服务读 `config.jev`；无 app 服务时退化为只看环境变量。

    密钥优先级：`config.jev.api_key` > 环境变量 `TYPESAFE_API_KEY`。
    """
    raw: Dict[str, Any] = {}
    if kernel.has_service("app"):
        try:
            app = kernel.get_service("app")
            raw = (app.config.get("jev") or {}) if getattr(app, "config", None) else {}
        except Exception:  # noqa: BLE001 - 配置读取失败不应影响内核
            logger.debug("[jev] 读取 app.config 失败", exc_info=True)
            raw = {}
    return JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))


# ---------------------------------------------------------------- HTTP（可 mock）

def post_systemone(cfg: JevConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
    """单次调用 System One。独立函数，便于测试 monkeypatch 与调用计数。"""
    STATS["judgements"] += 1
    with httpx.Client(timeout=cfg.timeout_s) as client:
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        headers.update(cfg.headers)      # 自定义头可覆盖默认 Authorization
        resp = client.post(cfg.base_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


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
                probabilities: Optional[Dict[str, float]] = None, questions: Optional[List[str]] = None,
                evidence: str = "", accepted: Optional[bool] = None) -> str:
    """Markdown 降级版判断卡（零核心改动即可用；设计文档 §7.6）。

    只使用纯文本 + 表格 + emoji + 字符条：lite-work 的 Markdown 渲染器未开
    rehype-raw，内联 HTML 与样式不生效，因此**不要**输出颜色块或内联样式。
    """
    if accepted is None:
        accepted = confidence >= threshold
    mark = {"allow": "🟢 放行", "confirm": "🟡 需确认", "block": "🔴 拦截"}.get(verdict, verdict)
    lines = [
        f"🧠 **Jev 判断** · {model} · {latency_ms}ms",
        "",
        f"- 结论：{mark}",
        f"- 置信：`{confidence:.2f}` / 阈值 `{threshold:.2f}` → "
        + ("**已采信**" if accepted else "**未采信**（交回基础规则）"),
    ]
    if score is not None:
        filled = max(0, min(10, int(score)))
        lines.append(f"- 风险：`{filled}/10` {'█' * filled}{'░' * (10 - filled)}"
                     + (f"　{legend}" if legend else ""))
    if probabilities:
        lines.append("- 行动概率：")
        for name, p in sorted(probabilities.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {name}：`{p * 100:.0f}%` {'█' * int(round(p * 20))}")
    if questions:
        lines.append("- 问了什么：")
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q}")
    if evidence:
        lines.append(f"- 依据：{evidence}")
    lines.append("")
    lines.append("_判断结果可被用户覆盖；Jev 只做建议，不替代基础安全规则。_")
    return "\n".join(lines)


# ---------------------------------------------------------------- 插件

class JevPlugin(ToolPlugin):
    name = PLUGIN_NAME
    version = "0.2.2"
    description = "Jev 判定层：System One 结构化判断（工具 + 工具执行前单向升级器）"

    #: 通用插件 UI 协议：设置页据此自动渲染表单（含自定义 Base URL 与请求头）
    contributes = {
        "settings": [
            {"key": "base_url", "type": "str", "label": "Base URL",
             "default": DEFAULT_BASE_URL,
             "hint": "System One 兼容端点；可指向自建网关 / 代理"},
            {"key": "headers", "type": "map", "label": "自定义请求头",
             "hint": 'JSON 对象，例如 {"x-api-key":"...","X-Api-Version":"1"}；与默认 Authorization 合并（同名覆盖）'},
            {"key": "api_key", "type": "secret", "label": "API Key",
             "hint": "默认用于 Authorization: Bearer；若网关要求别的头，请把 key 放进自定义请求头"},
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
        ],
        "panels": [
            {"id": "judgements", "title": "判断", "icon": "🧠"},
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
                    "用 Jev（System One 判定模型）对一段内容按有序量表打分，返回分数与校准置信度。"
                    "适合：风险分级、质量评估、严重度判定。不返回自然语言解释。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "description": "要判断的内容（精简摘要，建议 < 8k token）"},
                        "instructions": {"type": "string", "description": "判断指令，例如「这段改动有多危险」"},
                        "criteria": {
                            "type": "array", "items": {"type": "string"},
                            "description": "量表各级含义，从低到高（2–10 级）",
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
            data = post_systemone(cfg, payload)
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
        score = ans.get("score")
        conf = float(ans.get("confidence") or 0.0)
        return render_card(model=str(data.get("model") or cfg.model), latency_ms=latency_ms,
                           verdict="confirm", confidence=conf, threshold=cfg.threshold,
                           score=int(score) if score is not None else None,
                           legend=str(ans.get("legend") or ""), questions=[instructions],
                           evidence=f"state 摘要 {len(instructions)} 字")

    # -------------------------------------------------- 门禁（单向升级器）
    def _install_gate(self, kernel: Kernel) -> None:
        @kernel.before_tool.use
        async def _jev_gate(ctx, data, next):
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
            if conf < cfg.threshold:
                STATS["skipped_untrusted"] += 1     # 未采信：不参与决策
                return await next(data)
            record_judgement({
                "kind": "gate",
                "confidence": conf,
                "choice": "拦截" if str(verdict.get("choice")) == "block" else str(verdict.get("choice")),
                "tool": tool,
                "point": "gate",
                "at": time.time(),
            })
            if str(verdict.get("choice")) == "block":
                STATS["blocked"] += 1
                data["cancel"] = True
                data["reason"] = (
                    f"[Jev] 判定该操作风险高（置信 {conf:.2f}）：{verdict.get('why') or '不可逆或触及生产数据'}。"
                    "此调用不会执行。若认为误判，请让用户确认后再执行（或调整 Jev 阈值）。"
                )
            # 其余结论一律让行：本插件从**不**放行、也从不把 cancel 置回 False
            return await next(data)

    # -------------------------------------------------- 右栏「判断」面板

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """右栏「判断」面板的 Markdown（纯读、不联网）。"""
        if panel_id != "judgements":
            return ""
        raw = dict((config or {}).get(PLUGIN_NAME) or {}) if isinstance(config, dict) else {}
        cfg = JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))
        out = ["### 🧠 Jev 判断", ""]
        if cfg.ready:
            out.append(f"**● 运行中** · 模型 `{cfg.model}` · 阈值 `{cfg.threshold:.2f}` · "
                       f"自动门禁 {'开' if cfg.auto_gate_enabled else '关'}")
        else:
            out.append(f"**⏸ 未启动** —— {cfg.reason}")
        out.append("")
        out.append(f"端点：`{cfg.base_url}`")
        if cfg.headers:
            out.append(f"自定义请求头：`{', '.join(cfg.headers.keys())}`")
        out.append("")
        out.append("| 指标 | 值 |")
        out.append("| --- | --- |")
        out.append(f"| 判断次数 | {STATS['judgements']} |")
        out.append(f"| 门禁拦截 | {STATS['blocked']} |")
        out.append(f"| 未采信跳过 | {STATS['skipped_untrusted']} |")
        out.append(f"| 调用失败 | {STATS['errors']} |")
        out.append("")
        if not _RECENT:
            out.append("_暂无判断记录（本进程内）。判断发生后会显示在这里。_")
            return "\n".join(out)
        out.append(f"#### 最近 {len(_RECENT)} 次判断（新→旧）")
        out.append("")
        out.append("| 来源 | 结论 | 置信 | 阈值 | 采信 |")
        out.append("| --- | --- | --- | --- | --- |")
        for item in reversed(_RECENT[-15:]):
            conf = float(item.get("confidence") or 0.0)
            accepted = conf >= cfg.threshold
            verdict = str(item.get("choice") or item.get("score") or "-")
            point = "门禁" if item.get("point") == "gate" else "工具"
            tool = f" `{item.get('tool')}`" if item.get("tool") else ""
            out.append(f"| {point}{tool} | {verdict} | `{conf:.2f}` | `{cfg.threshold:.2f}` | "
                       f"{'✅' if accepted else '⚪ 未采信'} |")
        out.append("")
        out.append("_记录仅存于本进程内存，重启即清空；不写盘、不外发。_")
        return "\n".join(out)

    def judge(self, cfg: JevConfig, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
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
        data = post_systemone(cfg, payload)
        ans = (data.get("answers") or {}).get("q1") or {}
        return {
            "choice": {"放行": "allow", "请人确认": "confirm", "拦截": "block"}.get(
                str(ans.get("choice") or ""), "confirm"),
            "confidence": float(ans.get("confidence") or 0.0),
            "why": str(ans.get("why") or ""),
        }


# ---------------------------------------------------------------- 小工具

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
