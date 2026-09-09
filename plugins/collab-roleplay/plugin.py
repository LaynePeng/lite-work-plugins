# lite-work 协作模式插件：角色扮演（跨角色评审）
# 安装后在 设置 → 多智能体 → 协作模式 中选择启用；
# recipe.md 为派生指引（注入 spawn_agent 描述），可选实现
# on_agent_spawned / on_agent_complete / on_task_done 行为钩子。
from __future__ import annotations

from litework.orchestration.collab_policy import CollabModePlugin


class RoleplayCollabMode(CollabModePlugin):
    name = "collab-roleplay"
    version = "1.0.0"
    description = "协作模式：多 Agent 扮演不同利益相关者角色，从各自立场审视方案，产出多角色风险清单与综合评审"
    mode_name = "roleplay"
    display_name = "角色扮演（跨角色评审）"
