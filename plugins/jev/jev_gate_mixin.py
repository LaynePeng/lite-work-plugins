"""jev 插件 · 工具执行前门禁（从 plugin.py 拆出，0.9.2）。

⚠️ 多文件插件形态：宿主（litework/tools/plugin_loader.py）用 spec_from_file_location
**按文件**加载 plugin.py，并把**插件目录插入 sys.path**——所以同目录兄弟模块用
**裸 import + 唯一前缀**命名，不能用包相对导入（那不是包）。

本文件是 `JevPlugin` 的 mixin：方法通过 `self` 互相调用（由 plugin.py 组装成
单一类），mixin 之间**不互相 import**（避免循环）。
"""

from __future__ import annotations

import time
from jev_config import JevConfig, logger, read_config
from jev_render import _summarize_action
from jev_transport import STATS, _answer_choice, record_judgement
from litework.core.kernel import Kernel
from typing import Any, Dict


GATED_TOOLS = frozenset({
    "execute_command",
    "delete_file",
    "write_file",
    "apply_search_replace",
    "apply_unified_diff",
})


class GateMixin:
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

    async def judge(self, cfg: JevConfig, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """对一次工具调用做判断（可被测试 monkeypatch）。

        注意：这是**唯一仍在关键路径上的判定**——门禁必须在工具执行**之前**给出
        结论（要么让行、要么升级为审批/拦截），无法先返回后补判。因此这里：

        - 走异步传输（不占线程池）；
        - 命中内容缓存时零网络（同样的"工具+参数"从不重复问）；
        - 超时用 `cfg.timeout_ms`（默认 1200ms，硬上限），判不出就让行。

        成本模型上它只在写/执行类工具上触发（`GATED_TOOLS`），且 `auto_gate_enabled`
        默认关闭——开启前请知悉"每个写操作 +1.2s 上限"。
        """
        state = _summarize_action(tool, args)
        criteria = {"放行": "无副作用或完全可逆，风险低",
                    "请人确认": "有中等风险或不可逆，但可能是合理操作",
                    "拦截": "不可逆或触及生产数据，风险高"}
        try:
            ans = await _answer_choice(
                cfg, state, f"该工具调用（{tool}）应当放行、需人确认，还是拦截？",
                criteria, cache_kind="gate")
        except Exception:  # noqa: BLE001 - 保留原语义：异常向上抛，由调用方 fail-open
            raise
        return {
            "choice": {"放行": "allow", "请人确认": "confirm", "拦截": "block"}.get(
                str(ans.get("choice") or ""), "confirm"),
            "confidence": float(ans.get("confidence") or 0.0),
            "why": str(ans.get("why") or ""),
            # 概率分布：供审批卡展示"拦截 88% / 放行 9% / 请人确认 3%"
            "probabilities": ans.get("probabilities") or {},
        }
