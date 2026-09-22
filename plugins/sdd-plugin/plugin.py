# lite-work 社区插件：SDD 规格驱动开发（Spec-Driven Development，GitHub Spec Kit 风格）
#
# 工作流：clarify → spec（需求规格，EARS）→ plan（技术方案）→ tasks（任务清单）
#         → implement（实现，勾选任务）→ done（sdd_check 复核交付）。
# 工件落盘到工作区 specs/<feature>/：
#   spec.md    需求规格（R1/R2… 编号 + EARS 句式）
#   plan.md    技术方案（选型/接口/数据结构/风险）
#   tasks.md   任务清单（T<x.y> [R…] 引用需求编号，完成打勾）
#   state.json 阶段状态机 + 历史留痕
#
# 设计要点：
# - 硬门禁：plan 前必须有合格 spec，tasks 前必须有 plan，且需求-任务追溯
#   矩阵必须全绿；force=true 可显式跳过（记录进 state.json，不推进阶段）
# - 纯 stdlib（os/json/re/datetime），零第三方依赖，无需 wheels
# - 工具返回值自带「下一步」引导，LLM 无需 SKILL.md 即可走完流程
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SPECS_DIR_NAME = "specs"

# state.json 中持久化的工件里程碑（implement/done 由任务完成度动态推导）
STORED_PHASES = ["clarify", "spec", "plan", "tasks"]

SPEC_FILE = "spec.md"
PLAN_FILE = "plan.md"
TASKS_FILE = "tasks.md"
STATE_FILE = "state.json"

# feature 目录名：字母/数字/中文/下划线/连字符，1-64 字符，不以连字符开头
_FEATURE_RE = re.compile(r"^\w[\w\-]{0,63}$", re.UNICODE)

# 需求编号：标题（### R1: xxx）优先，正文提及（含验收标准里的引用）也计入
_R_TOKEN_RE = re.compile(r"\bR(\d+)\b")

# EARS 句式（中英双语）
_EARS_CN_RE = re.compile(r"当\s*.+?时[，,]?\s*系统应当\s*.+")
_EARS_EN_RE = re.compile(r"\bWHEN\b.+\bTHE SYSTEM SHALL\b.+", re.IGNORECASE)

# 任务行：- [ ] / - [x] T1.1 [R1,R2] 描述
_TASK_LINE_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s*(.*)$")
_TASK_ID_RE = re.compile(r"\bT(\d+(?:\.\d+)?)\b")

# 章节完整性检查（缺失 → 警告，不阻断）
SPEC_REQUIRED_SECTIONS = ["目的", "范围", "非目标"]
PLAN_REQUIRED_SECTIONS = ["技术选型", "接口", "数据结构", "风险"]


class SDDValidationError(Exception):
    """输入/门禁校验失败（execute 捕获后转 [Error] 返回）。"""


# ---------------------------------------------------------------------------
# 模板（中文 + EARS 说明）
# ---------------------------------------------------------------------------

SPEC_TEMPLATE = """# 规格说明：{feature}

> sdd-plugin 生成于 {ts}（阶段：clarify）。填写完成后用 `sdd_spec_save` 保存并校验。

## 1. 目的

（本特性解决什么问题、为谁解决、价值是什么）

## 2. 范围

（本特性包含什么）

## 3. 非目标

（明确不做什么，防止范围蔓延）

## 4. 需求（EARS 格式）

> 每条需求编号 R<n>，用 EARS 句式书写：
> 中文：当 <触发条件> 时，系统应当 <系统响应>。
> 英文：WHEN <trigger> THE SYSTEM SHALL <response>。

### R1: <需求标题>

当 <触发条件> 时，系统应当 <系统响应>。

## 5. 验收标准

（可测试的验收条件，与需求编号对应）

## 6. 开放问题

（待澄清事项，澄清后删除或标记已解决）
"""

PLAN_TEMPLATE_HINT = (
    "# 技术方案：{feature}\n\n"
    "> 基于 spec.md 撰写，用 `sdd_plan_save` 保存。建议章节：\n"
    "> ## 1. 技术选型（含理由）\n"
    "> ## 2. 架构与模块划分\n"
    "> ## 3. 接口约定（公共接口先定，防接口漂移）\n"
    "> ## 4. 数据结构 / 存储设计\n"
    "> ## 5. 错误处理与边界\n"
    "> ## 6. 风险与对策\n"
)

TASKS_TEMPLATE_HINT = (
    "# 任务清单：{feature}\n\n"
    "> 由 plan.md 拆解，用 `sdd_tasks_save` 保存。任务行格式：\n"
    "> `- [ ] T1.1 [R1,R2] 任务描述`（完成打勾改为 `- [x]`；每个需求 R 至少被一个任务引用）\n"
)


# ---------------------------------------------------------------------------
# 核心逻辑（不依赖 litework，便于独立测试）
# ---------------------------------------------------------------------------


class SDDCore:
    def __init__(self, workspace: Optional[str]) -> None:
        # 桌面应用未打开项目（workspace=None）时回落用户目录，
        # 真正的 specs 目录在每次工具调用时以当前 workspace 重建
        self.workspace = os.path.abspath(workspace) if workspace else os.path.expanduser("~")

    # ----- 路径与状态 -----

    @property
    def specs_dir(self) -> str:
        return os.path.join(self.workspace, SPECS_DIR_NAME)

    def _feature_dir(self, feature: str) -> str:
        if not feature or not _FEATURE_RE.match(feature):
            raise SDDValidationError(
                f"feature 名称非法：{feature!r}"
                "（允许字母/数字/中文/下划线/连字符，1-64 字符，不以连字符开头）"
            )
        base = os.path.realpath(self.specs_dir)
        path = os.path.realpath(os.path.join(base, feature))
        if not path.startswith(base + os.sep):
            raise SDDValidationError(f"路径越界：{feature!r}")
        return path

    @staticmethod
    def _read(fdir: str, name: str) -> Optional[str]:
        path = os.path.join(fdir, name)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _write(fdir: str, name: str, content: str) -> None:
        os.makedirs(fdir, exist_ok=True)
        with open(os.path.join(fdir, name), "w", encoding="utf-8") as f:
            f.write(content)

    def _load_state(self, fdir: str) -> Optional[Dict[str, Any]]:
        raw = self._read(fdir, STATE_FILE)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _save_state(fdir: str, state: Dict[str, Any]) -> None:
        state["updated_at"] = _now()
        SDDCore._write(fdir, STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))

    def _require_state(self, fdir: str, feature: str) -> Dict[str, Any]:
        state = self._load_state(fdir)
        if state is None:
            raise SDDValidationError(
                f"特性 {feature} 尚未初始化（specs/{feature}/{STATE_FILE} 不存在）。先调用 sdd_init"
            )
        return state

    @staticmethod
    def _advance_phase(state: Dict[str, Any], target: str) -> None:
        cur = state.get("phase", "clarify")
        if STORED_PHASES.index(target) > STORED_PHASES.index(cur):
            state.setdefault("history", []).append(
                {"ts": _now(), "event": "phase", "from": cur, "to": target}
            )
            state["phase"] = target

    # ----- 解析器 -----

    @staticmethod
    def _requirement_ids(spec_text: str) -> List[int]:
        ids = {int(m.group(1)) for m in _R_TOKEN_RE.finditer(spec_text or "")}
        return sorted(ids)

    @staticmethod
    def _ears_count(spec_text: str) -> int:
        cn = len(_EARS_CN_RE.findall(spec_text or ""))
        en = len(_EARS_EN_RE.findall(spec_text or ""))
        return cn + en

    @staticmethod
    def _parse_tasks(tasks_text: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for line in (tasks_text or "").splitlines():
            m = _TASK_LINE_RE.match(line)
            if not m:
                continue
            done = m.group(1).lower() == "x"
            rest = m.group(2)
            tid_m = _TASK_ID_RE.search(rest)
            refs = sorted({int(x) for x in _R_TOKEN_RE.findall(rest)})
            items.append(
                {
                    "done": done,
                    "id": f"T{tid_m.group(1)}" if tid_m else None,
                    "refs": refs,
                    "text": rest.strip(),
                }
            )
        return items

    # ----- 校验器（返回 errors / warnings）-----

    def _validate_spec(self, content: str) -> Tuple[List[str], List[str]]:
        errors: List[str] = []
        warnings: List[str] = []
        if not (content or "").strip():
            return (["spec 内容为空"], [])
        reqs = self._requirement_ids(content)
        if not reqs:
            errors.append(
                "未找到需求编号（R1、R2…）：请在「需求」章节用「### R1: 标题」为每条需求编号"
            )
        if self._ears_count(content) == 0:
            errors.append(
                "未找到 EARS 需求语句：中文「当 <触发条件> 时，系统应当 <响应>」"
                "或英文「WHEN … THE SYSTEM SHALL …」，每条 R 至少一句"
            )
        missing = [s for s in SPEC_REQUIRED_SECTIONS if s not in content]
        if missing:
            warnings.append(f"spec 缺少建议章节：{'、'.join(missing)}")
        if reqs and self._ears_count(content) < len(reqs):
            warnings.append(
                f"EARS 语句数（{self._ears_count(content)}）少于需求数（{len(reqs)}），"
                "建议每条 R 至少一句 EARS"
            )
        return errors, warnings

    def _validate_plan(self, content: str) -> Tuple[List[str], List[str]]:
        if not (content or "").strip():
            return (["plan 内容为空"], [])
        errors: List[str] = []
        warnings: List[str] = []
        if not re.search(r"^#{1,6}\s+\S+", content, re.MULTILINE):
            errors.append("plan 缺少 Markdown 章节结构（# 标题）")
        missing = [s for s in PLAN_REQUIRED_SECTIONS if s not in content]
        if missing:
            warnings.append(f"plan 缺少建议章节：{'、'.join(missing)}")
        return errors, warnings

    def _validate_tasks(
        self, content: str, spec_text: Optional[str]
    ) -> Tuple[List[str], List[str], Dict[str, Any]]:
        errors: List[str] = []
        warnings: List[str] = []
        items = self._parse_tasks(content)
        if not (content or "").strip():
            return (["tasks 内容为空"], [], {"items": []})
        if not items:
            errors.append(
                "未找到任务条目：任务行格式「- [ ] T1.1 [R1,R2] 任务描述」"
            )
            return errors, warnings, {"items": items}

        # 门禁：spec 必须存在且有需求编号
        if spec_text is None:
            errors.append("缺少 spec.md：先 sdd_spec_save 保存需求规格")
            spec_reqs: List[int] = []
        else:
            spec_reqs = self._requirement_ids(spec_text)
            if not spec_reqs:
                errors.append("spec.md 中没有需求编号（R1、R2…），请先修订 spec")

        covered = {r for it in items for r in it["refs"]}
        uncovered = [r for r in spec_reqs if r not in covered]
        if spec_reqs and uncovered:
            errors.append(
                "需求未被任务覆盖（每个 R 至少被一个任务引用）："
                + "、".join(f"R{r}" for r in uncovered)
            )
        invalid_refs = sorted({r for it in items for r in it["refs"] if r not in spec_reqs})
        if invalid_refs:
            errors.append(
                "任务引用了 spec 中不存在的需求："
                + "、".join(f"R{r}" for r in invalid_refs)
            )
        if any(it["id"] is None for it in items):
            warnings.append("存在未编号任务（建议 T1.1 / T1.2 格式，便于勾选与追溯）")
        if any(not it["refs"] for it in items):
            warnings.append("存在未引用需求编号的任务（建议补 [R…] 标注）")
        info = {
            "items": items,
            "spec_reqs": spec_reqs,
            "covered": sorted(covered),
        }
        return errors, warnings, info

    # ----- 工具实现 -----

    def init_feature(self, feature: str, description: str) -> str:
        fdir = self._feature_dir(feature)
        existing = self._load_state(fdir)
        if existing is not None:
            return (
                f"[SDD OK]: 特性 {feature} 已初始化（阶段 {self._dynamic_phase(fdir)}），"
                f"无需重复初始化。下一步：{self._next_step(fdir)}"
            )
        os.makedirs(fdir, exist_ok=True)
        self._write(fdir, SPEC_FILE, SPEC_TEMPLATE.format(feature=feature, ts=_now()))
        state = {
            "feature": feature,
            "description": (description or "").strip(),
            "phase": "clarify",
            "created_at": _now(),
            "updated_at": _now(),
            "history": [{"ts": _now(), "event": "init", "detail": (description or "").strip()[:200]}],
            "forced": [],
        }
        self._save_state(fdir, state)
        return (
            f"[SDD OK]: 已初始化 specs/{feature}/\n"
            f"- spec.md（需求规格模板，含 EARS 说明）\n"
            f"- state.json（阶段：clarify）\n"
            f"下一步：与用户澄清需求后填写 spec.md（每条需求编号 R<n>、EARS 句式），"
            f"再调用 sdd_spec_save 保存校验"
        )

    def save_spec(self, feature: str, content: str, force: bool) -> str:
        fdir = self._feature_dir(feature)
        state = self._require_state(fdir, feature)
        errors, warnings = self._validate_spec(content)
        if errors and not force:
            raise SDDValidationError(
                "spec 校验未通过：\n- " + "\n- ".join(errors)
                + "\n修复后重试；用户明确要求跳过时可 force=true 强制保存（不推进阶段，留痕）"
            )
        self._write(fdir, SPEC_FILE, content)
        event = {"ts": _now(), "event": "spec_save", "valid": not errors}
        if errors:  # force 路径
            state["forced"].append({"ts": _now(), "tool": "sdd_spec_save", "errors": errors})
        else:
            self._advance_phase(state, "spec")
        state.setdefault("history", []).append(event)
        self._save_state(fdir, state)
        reqs = self._requirement_ids(content)
        out = [f"[SDD OK]: spec.md 已保存（需求 {len(reqs)} 条：{self._fmt_reqs(reqs)}）"]
        if errors:
            out.append(f"[SDD WARN]: force 保存，存在未修复问题：\n- " + "\n- ".join(errors))
        if warnings:
            out.append("[SDD WARN]: " + "；".join(warnings))
        out.append(f"下一步：撰写技术方案 plan.md（选型/接口/数据结构/风险），再调用 sdd_plan_save")
        return "\n".join(out)

    def save_plan(self, feature: str, content: str, force: bool) -> str:
        fdir = self._feature_dir(feature)
        state = self._require_state(fdir, feature)
        spec_text = self._read(fdir, SPEC_FILE)
        gate_errors: List[str] = []
        if spec_text is None:
            gate_errors.append("缺少 spec.md：先 sdd_spec_save 保存需求规格（SDD 门禁：先规格后方案）")
        elif not self._requirement_ids(spec_text):
            gate_errors.append("spec.md 中没有需求编号（R1、R2…），请先修订 spec")
        errors, warnings = self._validate_plan(content)
        errors = gate_errors + errors
        if errors and not force:
            raise SDDValidationError(
                "plan 保存被门禁拦截：\n- " + "\n- ".join(errors)
                + "\n修复后重试；用户明确要求跳过时可 force=true 强制保存（不推进阶段，留痕）"
            )
        self._write(fdir, PLAN_FILE, content)
        state.setdefault("history", []).append({"ts": _now(), "event": "plan_save", "valid": not errors})
        if errors:
            state["forced"].append({"ts": _now(), "tool": "sdd_plan_save", "errors": errors})
        else:
            self._advance_phase(state, "plan")
        self._save_state(fdir, state)
        out = ["[SDD OK]: plan.md 已保存"]
        if errors:
            out.append("[SDD WARN]: force 保存，存在未修复问题：\n- " + "\n- ".join(errors))
        if warnings:
            out.append("[SDD WARN]: " + "；".join(warnings))
        out.append(
            "下一步：把方案拆解为任务清单 tasks.md"
            "（- [ ] T1.1 [R1,R2] 描述，每个 R 至少被一个任务引用），再调用 sdd_tasks_save"
        )
        return "\n".join(out)

    def save_tasks(self, feature: str, content: str, force: bool) -> str:
        fdir = self._feature_dir(feature)
        state = self._require_state(fdir, feature)
        spec_text = self._read(fdir, SPEC_FILE)
        errors, warnings, info = self._validate_tasks(content, spec_text)
        if self._read(fdir, PLAN_FILE) is None:
            errors = ["缺少 plan.md：先 sdd_plan_save 保存技术方案（SDD 门禁：先方案后任务）"] + errors
        if errors and not force:
            raise SDDValidationError(
                "tasks 校验未通过：\n- " + "\n- ".join(errors)
                + "\n修复后重试；用户明确要求跳过时可 force=true 强制保存（不推进阶段，留痕）"
            )
        self._write(fdir, TASKS_FILE, content)
        state.setdefault("history", []).append(
            {"ts": _now(), "event": "tasks_save", "valid": not errors}
        )
        if errors:
            state["forced"].append({"ts": _now(), "tool": "sdd_tasks_save", "errors": errors})
        else:
            self._advance_phase(state, "tasks")
        self._save_state(fdir, state)
        items = info["items"]
        total = len(items)
        done = sum(1 for it in items if it["done"])
        out = [f"[SDD OK]: tasks.md 已保存（任务 {total} 条，已完成 {done}）"]
        if info.get("spec_reqs"):
            out.append(f"需求覆盖：{self._fmt_reqs(info['spec_reqs'])} 全部覆盖" if not errors else "")
        out = [x for x in out if x]
        if errors:
            out.append("[SDD WARN]: force 保存，存在未修复问题：\n- " + "\n- ".join(errors))
        if warnings:
            out.append("[SDD WARN]: " + "；".join(warnings))
        out.append(
            "下一步：按任务清单实现（完成一条勾一条，直接编辑 tasks.md 的 [x]），"
            "随时 sdd_status 看进度；全部完成后 sdd_check 一致性复核"
        )
        return "\n".join(out)

    def check(self, feature: str) -> str:
        fdir = self._feature_dir(feature)
        self._require_state(fdir, feature)
        spec_text = self._read(fdir, SPEC_FILE)
        plan_text = self._read(fdir, PLAN_FILE)
        tasks_text = self._read(fdir, TASKS_FILE)
        if spec_text is None:
            raise SDDValidationError(f"缺少 spec.md，无法校验。下一步：sdd_spec_save")

        lines = [f"[SDD Check] 特性 {feature} 一致性审计："]
        failures: List[str] = []
        warnings: List[str] = []

        spec_reqs = self._requirement_ids(spec_text)
        if not spec_reqs:
            failures.append("spec 无需求编号")
        else:
            lines.append(f"- 需求：{self._fmt_reqs(spec_reqs)}（EARS 语句 {self._ears_count(spec_text)} 句）")

        if plan_text is None:
            warnings.append("plan.md 未创建")
        else:
            plan_missing = [s for s in PLAN_REQUIRED_SECTIONS if s not in plan_text]
            if plan_missing:
                warnings.append(f"plan 缺少建议章节：{'、'.join(plan_missing)}")

        if tasks_text is None:
            warnings.append("tasks.md 未创建")
            verdict = "BLOCKED"
            lines.append("- 结论：BLOCKED（缺少任务清单，不能进入实现）")
        else:
            items = self._parse_tasks(tasks_text)
            total = len(items)
            done = sum(1 for it in items if it["done"])
            if not items:
                failures.append("tasks 无任务条目")
            covered = {r for it in items for r in it["refs"]}
            uncovered = [r for r in spec_reqs if r not in covered]
            invalid = sorted({r for it in items for r in it["refs"] if r not in spec_reqs})
            if uncovered:
                failures.append("需求未覆盖：" + self._fmt_reqs(uncovered))
            if invalid:
                failures.append("任务引用了不存在的需求：" + self._fmt_reqs(invalid))
            # 覆盖矩阵
            if spec_reqs:
                matrix = []
                for r in spec_reqs:
                    tids = [it["id"] or "?" for it in items if r in it["refs"]]
                    mark = "✓" if tids else "✗"
                    matrix.append(f"  - {mark} R{r} ← {', '.join(tids) if tids else '（无任务覆盖）'}")
                lines.append("- 需求-任务追溯矩阵：\n" + "\n".join(matrix))
            lines.append(f"- 任务完成度：{done}/{total}" + (f"（{done * 100 // total}%）" if total else ""))
            if total and done == total and not failures:
                verdict = "READY_TO_DONE"
            elif not failures:
                verdict = "READY_TO_IMPLEMENT" if done < total else "READY_TO_DONE"
            else:
                verdict = "BLOCKED"

        for w in warnings:
            lines.append(f"- ⚠ {w}")
        for f_ in failures:
            lines.append(f"- ✗ {f_}")

        if verdict == "READY_TO_DONE":
            lines.append("- 结论：READY_TO_DONE（追溯矩阵全绿、任务全部完成，可交付）")
            lines.append("下一步：sdd_status 汇总交付")
        elif verdict == "READY_TO_IMPLEMENT":
            lines.append("- 结论：READY_TO_IMPLEMENT（追溯矩阵全绿，按 tasks.md 实现）")
            lines.append("下一步：实现并逐条勾选任务，完成后再次 sdd_check")
        else:
            lines.append("- 结论：BLOCKED（存在阻断问题，先修复上方 ✗ 项）")
        return "\n".join(lines)

    def status(self, feature: Optional[str]) -> str:
        if feature:
            return self._status_one(feature)
        if not os.path.isdir(self.specs_dir):
            return (
                "[SDD Status] 工作区尚无 SDD 工件（specs/ 不存在）。"
                "下一步：确定特性名后调用 sdd_init 初始化"
            )
        rows = []
        for name in sorted(os.listdir(self.specs_dir)):
            fdir = os.path.join(self.specs_dir, name)
            if not os.path.isdir(fdir) or self._load_state(fdir) is None:
                continue
            phase = self._dynamic_phase(fdir)
            total, done = self._task_progress(fdir)
            desc = (self._load_state(fdir) or {}).get("description", "")
            rows.append((name, phase, done, total, desc))
        if not rows:
            return (
                f"[SDD Status] specs/ 下没有有效特性（缺 state.json）。下一步：sdd_init 初始化"
            )
        lines = ["[SDD Status] 全部 SDD 特性："]
        for name, phase, done, total, desc in rows:
            pct = f"{done * 100 // total}%" if total else "—"
            lines.append(
                f"- {name}｜阶段 {phase}｜任务 {done}/{total}（{pct}）"
                + (f"｜{desc[:40]}" if desc else "")
            )
        lines.append("下一步：对单个特性调用 sdd_status(feature=…) 看详情，或 sdd_check 一致性复核")
        return "\n".join(lines)

    def _status_one(self, feature: str) -> str:
        fdir = self._feature_dir(feature)
        state = self._require_state(fdir, feature)
        phase = self._dynamic_phase(fdir)
        total, done = self._task_progress(fdir)
        forced = state.get("forced", [])
        lines = [
            f"[SDD Status] 特性 {feature}：",
            f"- 阶段：{phase}",
        ]
        if state.get("description"):
            lines.append(f"- 描述：{state['description']}")
        lines.append(f"- 任务：{done}/{total}" + (f"（{done * 100 // total}%）" if total else ""))
        files = [
            n for n in (SPEC_FILE, PLAN_FILE, TASKS_FILE)
            if os.path.isfile(os.path.join(fdir, n))
        ]
        lines.append(f"- 工件：specs/{feature}/（{'、'.join(files)}）")
        if forced:
            lines.append(f"- ⚠ 有 {len(forced)} 次 force 跳过记录（state.json 留痕）")
        lines.append(f"下一步：{self._next_step(fdir)}")
        return "\n".join(lines)

    # ----- 辅助 -----

    def _task_progress(self, fdir: str) -> Tuple[int, int]:
        text = self._read(fdir, TASKS_FILE)
        if text is None:
            return (0, 0)
        items = self._parse_tasks(text)
        return (sum(1 for it in items if it["done"]), len(items))

    def _dynamic_phase(self, fdir: str) -> str:
        state = self._load_state(fdir) or {}
        phase = state.get("phase", "clarify")
        if phase != "tasks":
            return phase
        done, total = self._task_progress(fdir)
        if total and done == total:
            return "done"
        if done > 0:
            return "implement"
        return "tasks"

    def _next_step(self, fdir: str) -> str:
        phase = self._dynamic_phase(fdir)
        feature = os.path.basename(fdir)
        return {
            "clarify": f"澄清需求并填写 specs/{feature}/spec.md，然后 sdd_spec_save",
            "spec": "撰写技术方案 plan.md，然后 sdd_plan_save",
            "plan": "拆解任务清单 tasks.md（每条引用 R 编号），然后 sdd_tasks_save",
            "tasks": "sdd_check 通过后开始实现，完成一条勾一条 tasks.md",
            "implement": "完成剩余任务（勾选 tasks.md），然后 sdd_check 复核",
            "done": "全部完成；如需变更，修订 spec 后重跑受影响阶段",
        }[phase]

    @staticmethod
    def _fmt_reqs(reqs: List[int]) -> str:
        return "、".join(f"R{r}" for r in reqs)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 插件入口（与内置同名 → 覆盖内置版；卸载自动回退）。无参构造（插件加载器
# 约定），workspace 在 install(kernel) 时捕获，支持项目热切换。
# ---------------------------------------------------------------------------


class SDDPlugin(ToolPlugin):
    name = "sdd-plugin"
    version = "1.0.0"
    description = (
        "SDD 规格驱动开发：spec→plan→tasks→implement 四件套工件落盘 specs/<feature>/，"
        "EARS 需求格式、需求-任务追溯矩阵与阶段门禁校验，跨会话可续接"
    )

    def __init__(self) -> None:
        self._app = None

    def install(self, kernel) -> None:
        """install 时从内核服务捕获 app 引用（每次新 kernel 都会重新调用）。"""
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:
            self._app = None
        super().install(kernel)

    def _core(self) -> SDDCore:
        workspace = getattr(self._app, "workspace", None) if self._app else None
        return SDDCore(workspace)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="sdd_init",
                description=(
                    "SDD 规格驱动开发-初始化：为特性创建 specs/<feature>/ 工件目录"
                    "（spec.md 需求模板 + state.json 状态机，阶段 clarify）。"
                    "开始 SDD 流程的第一步总是它；已存在则返回当前状态"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {
                            "type": "string",
                            "description": "特性标识（目录名）：字母/数字/中文/下划线/连字符，如 user-auth、导出报告",
                        },
                        "description": {
                            "type": "string",
                            "description": "一句话特性描述（可选，写入状态与状态总览）",
                        },
                    },
                    "required": ["feature"],
                },
            ),
            ToolDefinition(
                name="sdd_spec_save",
                description=(
                    "SDD-保存需求规格 spec.md：校验需求编号（### R1: 标题）、EARS 句式"
                    "（中文「当…时，系统应当…」/ 英文「WHEN … THE SYSTEM SHALL …」）与"
                    "章节（目的/范围/非目标）。通过则阶段推进到 spec；未通过报错，"
                    "force=true 可强制保存（留痕、不推进阶段）"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {"type": "string", "description": "特性标识（sdd_init 时所用）"},
                        "content": {"type": "string", "description": "spec.md 完整 Markdown 内容"},
                        "force": {"type": "boolean", "description": "校验失败时强制保存（默认 false）"},
                    },
                    "required": ["feature", "content"],
                },
            ),
            ToolDefinition(
                name="sdd_plan_save",
                description=(
                    "SDD-保存技术方案 plan.md：门禁要求 spec.md 已存在且有需求编号；"
                    "建议章节：技术选型/架构模块/接口约定/数据结构/风险。"
                    "通过则阶段推进到 plan；force=true 可强制跳过门禁（留痕）"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {"type": "string", "description": "特性标识"},
                        "content": {"type": "string", "description": "plan.md 完整 Markdown 内容"},
                        "force": {"type": "boolean", "description": "门禁/校验失败时强制保存（默认 false）"},
                    },
                    "required": ["feature", "content"],
                },
            ),
            ToolDefinition(
                name="sdd_tasks_save",
                description=(
                    "SDD-保存任务清单 tasks.md：门禁要求 plan.md 已存在；任务行格式"
                    "「- [ ] T1.1 [R1,R2] 描述」，需求-任务追溯矩阵必须全绿"
                    "（每个 R 至少被一个任务引用，且不得引用不存在的 R）。"
                    "通过则阶段推进到 tasks；force=true 可强制跳过（留痕）"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {"type": "string", "description": "特性标识"},
                        "content": {"type": "string", "description": "tasks.md 完整 Markdown 内容"},
                        "force": {"type": "boolean", "description": "门禁/校验失败时强制保存（默认 false）"},
                    },
                    "required": ["feature", "content"],
                },
            ),
            ToolDefinition(
                name="sdd_check",
                description=(
                    "SDD-一致性审计（只读）：需求-任务追溯矩阵、任务引用有效性、"
                    "任务完成度、章节完整性交叉检查，输出结论"
                    "BLOCKED / READY_TO_IMPLEMENT / READY_TO_DONE。实现前与交付前各跑一次"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {"type": "string", "description": "特性标识"},
                    },
                    "required": ["feature"],
                },
            ),
            ToolDefinition(
                name="sdd_status",
                description=(
                    "SDD-状态总览：不传 feature 列出工作区全部 SDD 特性"
                    "（阶段/任务完成率/下一步）；传 feature 看单特性详情"
                    "（工件清单/force 留痕/下一步）。跨会话恢复现场用"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "feature": {
                            "type": "string",
                            "description": "特性标识（可选；缺省列全部）",
                        },
                    },
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        core = self._core()
        feature = str(args.get("feature") or "").strip()
        force = bool(args.get("force", False))
        try:
            if name == "sdd_init":
                return core.init_feature(feature, str(args.get("description") or ""))
            if name == "sdd_spec_save":
                return core.save_spec(feature, str(args.get("content") or ""), force)
            if name == "sdd_plan_save":
                return core.save_plan(feature, str(args.get("content") or ""), force)
            if name == "sdd_tasks_save":
                return core.save_tasks(feature, str(args.get("content") or ""), force)
            if name == "sdd_check":
                return core.check(feature)
            if name == "sdd_status":
                return core.status(feature or None)
            return f"[Error]: 未知工具 {name}"
        except SDDValidationError as e:
            return f"[Error]: {e}"
