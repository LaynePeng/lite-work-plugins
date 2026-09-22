# 规格驱动开发技能（SDD，多 Agent 特化）

四件套工作流：**spec（需求规格，EARS）→ plan（技术方案）→ tasks（任务清单）→
implement（实现与验证）**。工件落盘在 `specs/<feature>/`，统一用 sdd-plugin
工具集管理（sdd_init / sdd_spec_save / sdd_plan_save / sdd_tasks_save /
sdd_check / sdd_status）——工具自带阶段门禁与需求-任务追溯校验，跨会话可续接。

## 阶段 0：澄清（clarify）

1. 与用户确认特性名 feature（短横线命名，如 user-auth）与一句话描述
2. `sdd_init` 初始化，读生成的 spec.md 模板结构
3. 需求歧义点逐条问用户（一次最多 3-5 个问题）；未决事项记入 spec.md
   「开放问题」章节，不要自行脑补

## 阶段 1：规格（spec）

1. 起草需求：每条编号 `R<n>`，EARS 句式（当 <触发条件> 时，系统应当 <响应>；
   英文 WHEN … THE SYSTEM SHALL …）；「范围」「非目标」必须明确
2. 复杂特性可 spawn spec-writer（general，fork_turns 继承澄清上下文）起草，
   你负责审校
3. spawn role=critic 评审 spec：「逐条检查歧义、不可测试、缺失边界与错误
   路径、范围蔓延」，产出按严重度排序的问题清单
4. 按清单修订，直至 critic 无严重问题 → `sdd_spec_save` 落盘过门禁
   （校验失败就改到过，不要 force）

## 阶段 2：方案（plan）

1. spawn planner（general）基于 spec.md 产出 plan.md：技术选型（含理由）、
   架构与模块划分、接口约定、数据结构、风险与对策
2. critic 复审：接口完整性、与 spec 的冲突、技术风险 → 修订
3. `sdd_plan_save` 落盘（门禁：spec 必须已有编号需求）

## 阶段 3：任务（tasks）

1. 把 plan 拆成可独立验收的任务清单，每条 `- [ ] T<x.y> [R…] 描述`
   ——追溯矩阵必须全绿（每个 R 至少被一个任务引用）
2. `sdd_tasks_save` 落盘；未覆盖需求会直接报错，回到拆分

## 阶段 4：实现（implement）

- 单模块 / 质量敏感 → **测试驱动接力**：spawn 实现者（allowed_dirs 限定
  目标模块）→ 完成后 spawn 测试者跑测试并**以 spec 的 EARS 条目为验收
  标准**逐条核对 → 有问题 followup_task 唤醒实现者修复，循环到全绿
- 多模块独立 → **模块领地并行**：按 write set 拆分，allowed_dirs 显式声明
  且互不相交；公共接口先定并写入文件，各 implementer 读取遵守
- 任务完成一条勾一条（直接编辑 tasks.md 的 `[x]`）；随时 `sdd_status` 看进度

## 收尾

1. `sdd_check` 一致性复核：追溯矩阵全绿、任务全部完成才算完
2. `sdd_status(feature=…)` 汇总交付：工件路径 + 需求完成率 + 验证结果

## 纪律

- **规格是唯一事实源**：实现与 spec 冲突时，要么改实现，要么走变更
  （改 spec 必须重跑受影响的后续阶段）
- 门禁不达标不要 force 跳过（force 会留痕，仅用于用户明确要求的紧急场景）
- 每阶段产物落盘后才进入下一阶段；中断后用 `sdd_status` 恢复现场
- 测试 agent 只读代码 + 跑命令，不修实现（角色分离）；close_agent 释放
  完成的 agent，进度卡住用 list_agents 检查 + send_message 督促
