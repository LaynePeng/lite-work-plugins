# lite-work 协作模式插件：规格驱动开发（SDD）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
# 工件与门禁由 sdd-plugin 的工具集（sdd_init / sdd_spec_save / sdd_plan_save /
# sdd_tasks_save / sdd_check / sdd_status）提供，建议与本模式配套安装。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class SDDCollabMode(CollabModePlugin):
    name = "collab-sdd"
    version = "1.0.0"
    description = "协作模式：规格驱动开发（SDD）——spec→plan→tasks→implement 四件套工件落盘 specs/，critic 评审规格与方案，实现与测试以 EARS 需求为验收标准（配套 sdd-plugin 工具集）"
    mode_name = "sdd"
    display_name = "规格驱动开发（SDD）"
