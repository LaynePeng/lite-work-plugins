"""jev 插件 · 配置快照 / 右栏「判断」面板 / 压缩判定器（从 plugin.py 拆出，0.9.2）。

⚠️ 多文件插件形态：宿主（litework/tools/plugin_loader.py）用 spec_from_file_location
**按文件**加载 plugin.py，并把**插件目录插入 sys.path**——所以同目录兄弟模块用
**裸 import + 唯一前缀**命名，不能用包相对导入（那不是包）。

本文件是 `JevPlugin` 的 mixin：方法通过 `self` 互相调用（由 plugin.py 组装成
单一类），mixin 之间**不互相 import**（避免循环）。
"""

from __future__ import annotations

import json
import os
from jev_config import JevConfig, PLUGIN_NAME, SOURCE_LABELS, logger, read_config
from jev_render import _prob_tone
from jev_transport import _RECENT, post_systemone_async
from typing import Any, Dict, List


class PanelMixin:
    def _effective_config(self, config: Any) -> "JevConfig":
        """面板/状态用的配置快照：按**完整分层链**解析，而不是只看传进来的内存快照。

        为什么必须分层：凭证可能来自 config.json / 环境变量 / 插件私有镜像。
        若面板只读内存快照，就会出现"插件实际在运行、面板却显示未启动"的矛盾
        （正是 0.9.1 修掉的那个不一致）。有 kernel 时委托 `read_config`；
        没有时（纯函数式调用）退回传入的裸配置。
        """
        kernel = getattr(self, "_kernel", None)
        if kernel is not None:
            try:
                return read_config(kernel)
            except Exception:  # noqa: BLE001 - 面板是只读展示，失败就退回裸配置
                logger.debug("[jev] 面板读分层配置失败，退回传入快照", exc_info=True)
        raw = dict((config or {}).get(PLUGIN_NAME) or {}) if isinstance(config, dict) else {}
        return JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))

    def status_from_config(self, config):
        """设置页显示「运行中 / 未启动 + 原因」（纯读：不发网络、不写盘）。"""
        cfg = self._effective_config(config)
        if not cfg.ready:
            return {"state": "not_started", "reason": cfg.reason}
        return {"state": "running", "reason": ""}

    def _credential_source(self, raw: Dict[str, Any]) -> str:
        """说明**凭证（api_key / 自定义头）到底来自哪一层**——配置语义可解释化的关键。

        历史问题：用户改了设置页却发现"改了又变回来"，因为插件会在读配置时
        顺手写回主配置。现在写回默认关闭，但"值来自哪里"仍需说清：
        面板直接展示命中的层，用户一眼能判断该去改内存/磁盘/环境变量/镜像。
        """
        kernel = getattr(self, "_kernel", None)
        if kernel is not None:
            try:
                _cfg, sources = read_config(kernel, with_sources=True)
            except Exception:  # noqa: BLE001 - 面板是只读展示，失败就退回简单推断
                sources = {}
            else:
                if sources.get("api_key") or sources.get("headers"):
                    layer = sources.get("api_key") or sources.get("headers")
                    return SOURCE_LABELS.get(layer, layer)
        # 无 kernel（或没读到）：按当前 raw 简单推断
        if raw.get("api_key") or raw.get("headers"):
            return SOURCE_LABELS["memory"]
        return ""

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """右栏「判断」面板：返回 JSON（前端渲染成 SVG 决策树）。"""
        if panel_id != "judgements":
            return ""
        raw = dict((config or {}).get(PLUGIN_NAME) or {}) if isinstance(config, dict) else {}
        cfg = self._effective_config(config)
        cred_source = self._credential_source(raw)

        status: Dict[str, Any] = {}
        if cfg.ready:
            chips = []
            if cfg.tools_enabled:
                chips.append("工具")
            if cfg.auto_gate_enabled:
                chips.append("门禁")
            if cfg.direct_answer:
                chips.append("直答")
            meta = [cfg.model, f"阈值 {cfg.threshold:.2f}"]
            if cred_source:
                meta.append(f"凭证：{cred_source}")
            status = {"text": "运行中", "tone": "ok",
                      "meta": meta, "chips": chips}
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

            title = {"gate": "门禁判定", "direct": "直答判定", "tool": "工具判定",
                     "verify": "完成核验", "screen": "注入护栏"}.get(point, "判定")
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

    @staticmethod
    def _block_fingerprint(block: List[Any]) -> str:
        """把一轮消息压成 <=200 字符的指纹（Jev 判定用）。"""
        parts: List[str] = []
        for m in block[:8]:
            role = str(getattr(m, "role", ""))
            content = str(getattr(m, "content", "") or "").replace("\n", " ").strip()
            if role == "tool":
                parts.append("tool: " + content[:80])
            elif role == "assistant":
                parts.append("asst: " + content[:60])
            elif role == "user":
                parts.append("user: " + content[:60])
        fp = " | ".join(parts)
        return fp[:200]

    def _make_compaction_decider(self):
        """返回核心 `app.compaction_decider` 调用的异步函数。

        签名：async (head: List[Message], system: Optional[Message]) -> Optional[List[Message]]
        返回保留后的消息列表；**任何退化都返回 None**（核心回退原有 LLM 摘要）。
        """
        async def _decider(head, system):
            kernel = getattr(self, "_kernel", None)
            if kernel is None:
                return None
            cfg = read_config(kernel)
            if not cfg.ready or not cfg.compact_decider:
                return None                              # D0 / D1
            body = [m for m in head if str(getattr(m, "role", "")) != "system"]
            has_tool = any(str(getattr(m, "role", "")) == "tool" for m in head)
            if not has_tool:
                return None                              # D5 纯对话 → LLM 摘要
            from litework.core.context_manager import ContextManager
            try:
                ranges = ContextManager._turn_ranges(body)
            except Exception:                            # noqa: BLE001
                return None                              # D2 解析失败
            if len(ranges) < 2:
                return None
            blocks = [body[r[0]:r[1]] for r in ranges]
            fps = [self._block_fingerprint(b) for b in blocks]
            kept: set = set(range(len(blocks)))
            batch = cfg.compact_decider_batch
            for start in range(0, len(fps), batch):
                chunk = fps[start:start + batch]
                questions: Dict[str, Any] = {}
                for i in range(len(chunk)):
                    questions[f"keep_{i}"] = {
                        "type": "noul",
                        "instructions": f"记录 {i} 对后续任务是否仍有保留价值？（工具执行记录 / 中间结论 / 已过期步骤）",
                    }
                payload = {"state": "\n".join(f"{i}: {fp}" for i, fp in enumerate(chunk)),
                           "model": cfg.model, "questions": questions}
                try:
                    resp = await post_systemone_async(cfg, payload,
                                                      timeout_ms=cfg.verify_timeout_ms)
                    answers = resp.get("answers") or {}
                except Exception:                        # noqa: BLE001
                    return None                          # D2 调用失败 → 回退
                for i in range(len(chunk)):
                    a = answers.get(f"keep_{i}") or {}
                    try:
                        noul_v = float(a.get("noul") or 0.5)
                    except (TypeError, ValueError):
                        noul_v = 0.5                     # D7 异常值按中性
                    if noul_v < cfg.compact_decider_threshold:
                        kept.discard(start + i)
            # D4 硬性下限：至少保留 min_keep_ratio（默认 1/3，取最近）
            min_keep = max(1, int(len(blocks) * cfg.compact_decider_min_keep_ratio))
            if len(kept) < min_keep:
                kept = set(range(len(blocks) - min_keep, len(blocks)))
            # D3 一条没丢 → 无意义
            if len(kept) == len(blocks):
                return None
            # D8 释放不足 20% → 压了没省
            if (len(blocks) - len(kept)) / len(blocks) < 0.2:
                return None
            kept_msgs: List[Any] = []
            for i in sorted(kept):
                kept_msgs.extend(blocks[i])
            return kept_msgs
        return _decider
