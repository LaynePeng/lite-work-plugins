"""jev 插件 · 渲染与纯函数小工具（从 plugin.py 拆出）。

⚠️ 命名与导入方式同 `jev_config.py`：同目录、裸 import、唯一前缀命名。

内容：无副作用的纯函数——判断卡（Markdown）渲染、`/jev` 直答命令解析、
工具调用摘要、token 粗估、完成声称识别、选项语义色。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 「声称完成」话术（Verify 的 before_llm 路径与 Stop 钩子共用）
DONE_MARKERS = ("完成了", "已完成", "测试通过", "全部通过", "已修复",
                "done", "all tests pass", "fixed")


def claims_done(text: str) -> bool:
    """这段文本是否在声称"已经完成"（大小写不敏感）。"""
    lowered = (text or "").lower()
    return any(h in lowered for h in DONE_MARKERS)


def render_card(*, model: str, latency_ms: int, verdict: str, confidence: float,
                threshold: float, score: Optional[int] = None, legend: str = "",
                scale_total: Optional[int] = None,
                probabilities: Optional[Dict[str, float]] = None,
                questions: Optional[List[str]] = None,
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
