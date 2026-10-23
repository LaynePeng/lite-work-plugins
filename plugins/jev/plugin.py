"""jev 插件 · 装配层（Jev 判定层）。

把 TypeSafe 的 Jev（System One 判定模型）接进 lite-work，作为一层"判定器"：
工具（jev_score / jev_choice）+ 工具执行前的单向升级器（before_tool）。

设计约束（lite-work 侧设计文档 §6.1「三条不可让步的原则」与 §12.7「未启动」）：

1. **单向棘轮**：本插件只能把"本会放行"的调用**升级**为拦截；结构上不可能放行
   任何东西——它没有审批卡 API，也从不把 cancel 置回 False。
2. **fail-open**：判定失败（超时/429/529/schema 不符/未采信）一律**让行**，
   交给排在后面的 SecurityPlugin 按基础规则处理，绝不因为"判不了"而阻断。
3. **配置缺失 = 未启动**：装配期（install）判定一次，不通过就立即返回——
   不注册工具、不挂钩子、零网络，只记一条 info。补齐配置后下一条消息生效。
4. **不引官方 SDK / 不打 wheels**：只用 lite-work 内置的 httpx，保证打包态可用。

配置（lite-work `config.json` 顶层 `jev` 对象；密钥优先 config，其次环境变量 `TYPESAFE_API_KEY`）

```
键                      默认                 说明
api_key                 空                   缺失即"未启动"；生产建议用环境变量
model                   jev-1.13            固定版本，别用会移动的别名
base_url                官方端点             可指向自建网关 / 代理
confidence_threshold    0.60                 低于此值不采信，交回基础规则
size_gate_tokens        8192                 超过则不送 Jev
timeout_ms              1200                 超时视为未采信（fail-open 让行）
tools_enabled           true                 jev_* 工具是否暴露（按需付费）
auto_gate_enabled       false                自动门禁（每次写/执行类工具一次）
```

迟延代价与"不阻塞关键路径"约定（0.9.0 起，见 `jev_guards_mixin`）：

- **判定不阻塞用户可见链路**：Screen 的 after_tool 判定是 fire-and-forget——
  工具结果**立刻**返回，判定在后台跑，命中则**下一轮**以 system 提示送达。
- **同一内容判定一次**：按内容哈希缓存（默认 TTL 600s），重复内容零网络。
- **超时收紧**：默认 `timeout_ms=1200`、最多 2 次尝试；服务不可用时每轮付
  O(1.2s) 而不是 O(10s)。

配置优先级与写盘作用域（0.9.1 起，见 `jev_config.read_config`）：

```
第 1 层  设置页（内存 app.config["jev"]）     ← 优先级最高
第 2 层  <config_dir>/config.json 的 jev 段
第 3 层  环境变量 JEV_BASE_URL / JEV_MODEL / JEV_HEADERS / TYPESAFE_API_KEY
第 4 层  插件私有镜像 plugins/jev/config.local.json
```

**各层只补空缺**（前层有非空值即生效），右栏「判断」面板会显示凭证实际来自哪一层。
写盘只发生在一处：把有效凭证镜像到**插件自己目录**下的 `config.local.json`（0600）。
历史问题（0.9.0 及以前）：读配置的路径上会顺手 `app.save_config(...)` 把凭证**写回
用户主配置**，代价是用户改了设置页却发现"改了又变回来"。0.9.1 起默认**关闭**，
仅由 `config_self_heal_enabled=true` 显式开启，且每次写回都记 WARNING 日志。

未启动语义：配置不完整 = 未启动，与"未启用"等价——不注册工具、不挂钩子、零网络。
补齐配置后：钩子类能力下一条消息即生效；**工具面**受核心 60s 缓存约束，最长 60 秒
进入模型工具表。手动复制插件目录需重启应用（核心缓存插件实例），经设置页安装则无需重启。

## 文件结构（0.9.2 起拆分，各文件头有职责说明）

多文件插件形态：宿主（`litework/tools/plugin_loader.py`）用 `spec_from_file_location`
**按文件**加载本 `plugin.py`，并**把插件目录插入 `sys.path`**——因此同目录兄弟模块
必须用**裸 import + 唯一前缀**命名，不能用包相对导入（那不是包，会 ImportError）。

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 装配层：元信息、`contributes`、`__init__`、`install`（启动门 + 钩子装配） |
| `jev_config.py` | 配置常量 / `JevConfig` / 四层配置解析 / 凭证私有镜像 |
| `jev_transport.py` | 观测计数 / 判定缓存 / 后台任务 / HTTP 传输 |
| `jev_render.py` | 判断卡渲染 / `/jev` 命令解析 / 纯函数小工具 |
| `jev_tools_mixin.py` | 工具面（jev_score / jev_choice）+ 用户直问短路 |
| `jev_guards_mixin.py` | Screen 护栏与 Verify 核验 |
| `jev_gate_mixin.py` | 工具执行前门禁 |
| `jev_panel_mixin.py` | 配置快照 / 右栏「判断」面板 / 压缩判定器 |

行为按上述 mixin 切分：方法通过 `self` 互相调用，mixin 之间**不互相 import**
（避免环）；本文件只导入它们并组装成单一 `JevPlugin` 类。

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
from typing import Any, Dict, List, Optional

from litework.core.kernel import Kernel
from litework.tools.plugin import ToolPlugin

# 同目录兄弟模块：宿主把插件目录插入 sys.path，故用裸 import（唯一前缀命名）。
# 只导入本文件自己用到的名字——各 mixin 负责自己的 import（拆分时的教训：
# 漏一个名字静态检查看不出来，只在真机加载时才炸）。
from jev_config import (PLUGIN_NAME, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_THRESHOLD,
                        DEFAULT_SIZE_GATE_TOKENS, DEFAULT_TIMEOUT_MS,
                        DEFAULT_VERIFY_TIMEOUT_MS, DEFAULT_MAX_ATTEMPTS,
                        DEFAULT_CACHE_TTL_S, DEFAULT_MAX_CONCURRENCY, read_config)
from jev_gate_mixin import GateMixin
from jev_guards_mixin import GuardsMixin
from jev_panel_mixin import PanelMixin
from jev_tools_mixin import ToolsMixin

logger = logging.getLogger("litework.plugins.jev")


class JevPlugin(ToolsMixin, GuardsMixin, GateMixin, PanelMixin, ToolPlugin):
    """Jev 判定层插件：装配 + 启动门。

    行为分散在四个 mixin（各自文件头说明）；本类只声明元信息、`contributes`
    与 `install`。MRO：JevPlugin → ToolsMixin → GuardsMixin → GateMixin →
    PanelMixin → ToolPlugin。各 mixin 通过 `self` 互相调用，不彼此 import。
    """

    name = PLUGIN_NAME
    version = "0.9.2"
    description = "Jev 判定层：System One 结构化判断（工具 + 工具执行前单向升级器）"

    contributes = {
        "settings": [
            {"key": "base_url", "type": "str", "label": "Base URL",
             "default": DEFAULT_BASE_URL,
             "hint": "System One 兼容端点；可指向自建网关 / 代理"},
            {"key": "headers", "type": "map", "label": "自定义 Header（每行一个，可留空）",
             "hint": "格式 Key: Value 或 Key=Value（按第一个分隔符切分，值可含冒号）；"
                     "# 开头忽略；可覆盖默认 Authorization；这里带凭证也算已配置"},
            {"key": "api_key", "type": "secret", "label": "API Key",
             "hint": "默认用于 Authorization: Bearer；也可留空、改把 key 放进自定义请求头。"
                     "留空的键会按「设置页 → config.json → 环境变量 → 插件私有镜像」"
                     "逐层兜底（面板会显示当前生效的层）"},
            {"key": "model", "type": "str", "label": "模型版本", "default": DEFAULT_MODEL,
             "hint": "建议固定版本 ID，别名会移动"},
            {"key": "confidence_threshold", "type": "number", "label": "置信度阈值",
             "default": DEFAULT_THRESHOLD},
            {"key": "size_gate_tokens", "type": "number", "label": "size-gate（token）",
             "default": DEFAULT_SIZE_GATE_TOKENS},
            {"key": "timeout_ms", "type": "number", "label": "单次判定超时（毫秒）",
             "default": DEFAULT_TIMEOUT_MS,
             "hint": "每轮/每次卡点的等待上限。默认 1200ms：判不出就让行（fail-open）。"
                     "调大 = 判定更可能成功，但卡顿也更久"},
            {"key": "tools_enabled", "type": "boolean",
             "label": "暴露 jev_* 工具（0 轮代价）", "default": True,
             "hint": "不挂钩子、不自动发请求；只在 Agent 主动调用 jev_score/jev_choice 时付费"},
            {"key": "auto_gate_enabled", "type": "boolean",
             "label": "自动门禁（每个写/执行类工具 +1 次判定）", "default": False,
             "hint": "代价：execute_command / write_file / apply_* 每次执行前最多等 timeout_ms"
                     "（唯一无法异步化的卡点：必须在执行前给结论）。默认关"},
            {"key": "direct_answer_enabled", "type": "boolean",
             "label": "用户直问短路（跳过 LLM）", "default": False,
             "hint": "代价：仅当消息以 /jev 是否|打分|选 开头时触发一次判定；命中则跳过主模型（反而省一次 LLM）"},
            {"key": "direct_answer_confidence", "type": "number",
             "label": "直答置信阈值", "default": 0.85,
             "hint": "低于此值不直答，改为正常交给主模型（避免误抢答）"},
            {"key": "skill_recommend_enabled", "type": "boolean",
             "label": "技能推荐（Jev 选技能）", "default": False,
             "hint": "代价：仅首次消息一次判定；命中则注入 system 提示（不阻断）"},
            {"key": "screen_enabled", "type": "boolean",
             "label": "Screen 护栏（防提示注入）", "default": False,
             "hint": "代价：读文件/抓网页结果**后台**判定（不阻塞工具返回）；"
                     "每轮用户消息与 Verify 合并成 1 次请求。命中则在下一轮提示模型"},
            {"key": "async_screen_enabled", "type": "boolean",
             "label": "Screen 异步判定（不阻塞工具返回）", "default": True,
             "hint": "开（推荐）：工具结果立即返回，判定在后台跑，命中结论注入下一轮；"
                     "关：退回 0.8.x 行为——当场等一次判定，命中则截断隔离"},
            {"key": "screen_skip_self", "type": "boolean",
             "label": "跳过自有内容（本仓代码/文档）", "default": True,
             "hint": "代价：命中路径含 plugins/ skills/ scripts/ 等标记时零判定。"
                     "若你把它当通用护栏，可关闭以全量判定"},
            {"key": "screen_max_per_turn", "type": "number",
             "label": "每轮最多判定几个工具结果", "default": 3,
             "hint": "成本闸门：一轮里读 10 个文件也只判前 N 个（0 = 不限）"},
            {"key": "verify_enabled", "type": "boolean",
             "label": "Verify 核验（防假完成）", "default": False,
             "hint": "代价：仅当回复里出现「已完成/测试通过」时触发；与 Screen 合并为 1 次请求；"
                     "收尾那一刻（Stop 钩子）再核验一次，同一声称同结论只真判一次"},
            {"key": "verify_threshold", "type": "number",
             "label": "Verify 通过阈值", "default": 0.5,
             "hint": "证据支撑度低于此值判定为假完成"},
            {"key": "verify_finish_enforce", "type": "boolean",
             "label": "Verify 阻断收尾（Stop 钩子强制）", "default": False,
             "hint": "代价：证据不足时 block_finish，**多一整轮 LLM 往返**。"
                     "默认关（Jev 只做顾问）；且同一声称只拦一次"},
            {"key": "verify_timeout_ms", "type": "number",
             "label": "Verify 超时（毫秒）", "default": DEFAULT_VERIFY_TIMEOUT_MS,
             "hint": "收尾/轮次边界的核验可比护栏宽松些（默认 2500ms）"},
            {"key": "cache_ttl_s", "type": "number",
             "label": "判定缓存有效期（秒）", "default": DEFAULT_CACHE_TTL_S,
             "hint": "同样的内容（同一文件、同一声称、同一消息）在 TTL 内直接复用结论，零网络。0 = 关闭缓存"},
            {"key": "max_attempts", "type": "number",
             "label": "单次判定最大尝试次数", "default": DEFAULT_MAX_ATTEMPTS,
             "hint": "默认 2（1 次 + 1 次瞬时错误重试）。调大更抗抖动，代价是最坏等待成倍"},
            {"key": "max_concurrency", "type": "number",
             "label": "后台判定并发上限", "default": DEFAULT_MAX_CONCURRENCY,
             "hint": "同时最多几个后台判定；超出的直接丢弃（fail-open，宁可漏判不排队）"},
            {"key": "config_self_heal_enabled", "type": "boolean",
             "label": "允许插件写回主配置（不推荐）", "default": False,
             "hint": "代价/风险：开启后，当设置页的凭证为空而插件能从别处读到有效凭证时，"
                     "会把它写回你的 config.json（写回时会记一条 WARNING）。"
                     "默认关——插件只写自己目录下的镜像文件；关闭不影响可用性"},
            {"key": "compact_decider_enabled", "type": "boolean",
             "label": "Jev 压缩判定器", "default": False,
             "hint": "代价：仅在上下文压缩时批量判定（每批 1 次请求）。"
                     "关 = 用原有 LLM 摘要"},
            {"key": "compact_decider_threshold", "type": "number",
             "label": "保留阈值", "default": 0.6,
             "hint": "noul 低于此值的记录会被丢弃"},
            {"key": "compact_decider_batch", "type": "number",
             "label": "每批判定条数", "default": 20},
            {"key": "compact_decider_min_keep_ratio", "type": "number",
             "label": "最少保留比例", "default": 0.33,
             "hint": "防止判太激进全丢（硬性下限，保最近的记录）"},
        ],
        "panels": [
            {"id": "judgements", "title": "判断"},
        ],
        "commands": [
            {"name": "jev",
             "description": "Jev 直答：跳过主模型，由 System One 直接判断",
             "argsHint": "是否 <陈述> · 打分 <内容> · 选 A|B|C | <内容>"},
        ],
    }

    def __init__(self) -> None:
        self._kernel: Optional[Kernel] = None
        self.status = "not_installed"        # not_started | running
        self.status_reason = ""

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
        if cfg.direct_answer:
            self._install_direct_answer(kernel)
        if cfg.skill_recommend:
            self._install_skill_recommend(kernel)
        if cfg.screen or cfg.verify:
            # 单轮只判一次：Screen（用户消息注入）与 Verify（完成声称）合并为一次请求
            self._install_turn_guard(kernel)
        if cfg.screen:
            self._install_screen(kernel)     # after_tool 结果判定（默认异步）
        if cfg.verify:
            # 兼容守卫：Stop 钩子需要 core ≥ 1.10.3（kernel.before_finish）。
            # 老核心上**只停用这一项能力**，绝不抛错——否则插件 install 失败会
            # 让整次工具装配失败（/api/tools 与任务启动都受影响）。
            # 老核心下轮次核验（turn guard）依然有效，只是抓不到"声称完成即结束"。
            if hasattr(kernel, "before_finish"):
                self._install_finish(kernel)
            else:
                logger.warning(
                    "[jev] 当前核心没有 before_finish（Stop 钩子）——"
                    "收尾核验不可用，其余能力照常；升级 lite-work ≥ 1.10.3 后自动生效")
        # #1 Jev 压缩判定器：注册到 app 服务（核心 compact_session 查询）
        # D0：功能关 → 注销，压缩走默认 LLM 摘要（零残留）
        app = kernel.get_service("app") if kernel.has_service("app") else None
        if app is not None:
            if cfg.compact_decider:
                app.compaction_decider = self._make_compaction_decider()
            else:
                app.compaction_decider = None

        self.status = "running"
        self.status_reason = ""
        logger.info("[jev] 已启动（tools=%s, auto_gate=%s, model=%s）",
                    cfg.tools_enabled, cfg.auto_gate_enabled, cfg.model)
