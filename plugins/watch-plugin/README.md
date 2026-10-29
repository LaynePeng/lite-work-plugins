# watch-plugin — 盯梢 / 定时计划的工具面

给 lite-work 的**盯梢（watch & schedule）**子系统补上 Agent 工具。

## 为什么需要它

盯梢在 lite-work 里是一个完整子系统（`litework/watch/`）：调度循环、指纹比对、
跨进程触发租约、通知总线、无人值守权限档一应俱全。但它对模型**只有一个 REST 面**
（`/api/schedules`），没有任何 Agent 工具。结果是：

- 界面上能建计划、能改 `probe_config`，**人**点得动；
- 模型既看不见「现在盯的是什么」，也没法「改成盯别的」——只能回一句
  「请到右栏盯梢页手动新建」。

第三方插件（如 astock）为了绕开这个缺口，直接在进程内读写
`app.schedule_store`。那条路能用，但每个插件都要重写一遍，且只能管自己那条计划。
本插件把它变成**通用工具面**：任何盯梢源的计划都能被列出、创建、修改、立即跑一次。

## 工具

| 工具 | 作用 |
|---|---|
| `schedule_sources` | 可用数据源 + 各源的 `probe_config` 字段说明（**配置前先调它**） |
| `schedule_list` | 计划清单：探针参数、最近几次运行结果、启用状态 |
| `schedule_get` | 单条计划的完整内容（含原始 `probe_config`） |
| `schedule_upsert` | 创建 / 修改计划；`probe_config` 支持整体替换、按键合并、删键 |
| `schedule_run` | 立即触发一次（走与定时触发完全相同的探针 → 指纹 → 按需唤 Agent 路径） |
| `schedule_delete` | 删除计划（**默认只预览**，`dry_run=false` 才真删） |

典型用法：

```
# 1. 有哪些数据源、各自认什么参数
schedule_sources

# 2. 现在盯的是什么
schedule_list
schedule_list source=astock include_other_workspaces=true

# 3. 只改一个探针参数（其余键保留）
schedule_upsert id=sch_1a48d329ba merge_probe_config={"min_level": "urgent"}

# 4. 新建一条盯网页的计划
schedule_upsert name="盯发布页" source=url interval_seconds=600 \
                probe_config={"url": "https://example.com/releases"}

# 5. 立即验证一次配置是否可用
schedule_run id="盯发布页"
```

## 安全边界

这些不是可选项，每一条都对应一种真实误操作：

1. **`workspace` 钉死为当前工作区**——计划里的 `workspace` 决定探针盯哪个目录
   （`ProbeContext.workspace`）。允许调用方指定，模型就能把探针指到任意路径。
   传了也会被忽略。
2. **权限档白名单 `read` / `read+web`**——与宿主 `ALLOWED_PERMISSIONS` 一致。
   无人值守时没人点审批，写/执行类必须给不出去。
3. **`source` 写入前校验**——对照 `watch_registry` 检查。写错源名字时宿主
   `fire()` 只会记一条 error，计划永远跑不出结果，且没有任何提示。
4. **跨工作区保护**——列出全部计划是只读无害，但**改/删**别人的计划要显式
   `force=true`。否则症状是"那个项目突然不提醒了"，且无从排查。
5. **删除默认预览**——删除会丢运行历史与指纹基线（恢复时会把当前所有信号当
   "新变化"重报一遍）。只想暂停请用 `schedule_upsert enabled=false`。
6. **先校验后写盘**——`source` / `permissions` / `schedule_mode` / `catch_up` /
   `interval_seconds` / `probe_config` 全部先校验，任一不合法就整条拒绝，
   不让半截配置落盘。

## 权限域与审批

新工具名（`schedule_*`）不在主程序的 `TOOL_DOMAIN` 映射里，因此归入 `misc` 域，
默认动作是 **`ask`**——即每次调用都会弹审批。这是有意的：它们能创建/修改/删除
计划，属于写入类操作。

想免审批又不想全放开，可在「设置 → Agent」里把 `misc` 域设为 `allow`，或在
自定义 Agent 的 `extra_tools` 里单独放行。无人值守的盯梢任务本身（`read` 档）
**不会**拿到这些工具——`misc` 在 `UNATTENDED_PROFILES` 之外，这是正确的。

## 与宿主 REST 的关系

两者走**同一条数据通路**：`app.schedule_store`（`upsert` 内部即原子写盘）+
`app.watch_scheduler.reschedule()`。不是去改 `schedules.json`，因此不会与界面上
的操作互相覆盖。宿主改内部结构时，最坏结果是插件如实降级（返回
「拿不到宿主的计划存储」），而不是插件加载失败。

## 安装

```
~/.lite-work/plugins/watch-plugin/plugin.py    # 从本仓库 plugins/watch-plugin/ 复制
```

零第三方依赖（纯标准库 + 宿主稳定接口）。
