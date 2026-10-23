"""jev 插件 · 工具面（jev_score / jev_choice）与用户直问短路（从 plugin.py 拆出，0.9.2）。

⚠️ 多文件插件形态：宿主（litework/tools/plugin_loader.py）用 spec_from_file_location
**按文件**加载 plugin.py，并把**插件目录插入 sys.path**——所以同目录兄弟模块用
**裸 import + 唯一前缀**命名，不能用包相对导入（那不是包）。

本文件是 `JevPlugin` 的 mixin：方法通过 `self` 互相调用（由 plugin.py 组装成
单一类），mixin 之间**不互相 import**（避免循环）。
"""

from __future__ import annotations

import time
from jev_config import JevConfig, logger, read_config
from jev_render import _approx_tokens, _parse_direct_command, render_card
from jev_transport import STATS, _build_questions, post_systemone_async, record_judgement
from litework.core.kernel import Kernel
from litework.core.types import ToolDefinition
from typing import Any, Dict, List


class ToolsMixin:
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
            data = await post_systemone_async(cfg, payload)
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
        data = await post_systemone_async(cfg, payload)
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
