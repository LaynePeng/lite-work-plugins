# lite-work-plugins

[lite-work](https://github.com/LaynePeng/lite-work) 的社区插件与技能仓库：
**独立于主程序发版的工具扩展分发源**。

主程序内置了一批 Cordis 插件与技能（随主程序版本发布）；本仓库提供同名的
**社区独立版**——版本独立演进，安装即覆盖主程序内置版，卸载自动回退内置版。
也可以发布主程序没有的新插件 / 新技能。

## 仓库结构

```text
lite-work-plugins/
├── manifest.json            # 清单：全部可安装项（名称/版本/路径）
├── plugins/                 # Cordis 工具插件
│   ├── office-plugin/       #   办公生产力（16 个工具：生成/读取/格式化编辑/查找替换）
│   │   └── plugin.py
│   ├── ocr-plugin/          #   OCR 识别（图片/PDF/PPT 内嵌图片）
│   │   └── plugin.py
│   ├── webfetch-plugin/     #   Web 抓取（三级反爬对抗）
│   │   └── plugin.py
│   └── example-greeting/    #   示例插件（演示编写格式）
│       └── plugin.py
└── skills/                  # 技能（SKILL.md 指令文档）
    ├── academic-paper-composer/     #   英文论文写作（按大纲逐章写作 + 质量门槛）
    ├── academic-paper-engineering/  #   学术论文工程化排版（→ 期刊模板 LaTeX 工程 / PDF）
    ├── academic-paper-strategist/   #   英文论文选题规划（文献/缺口/大纲 + 评审自评）
    ├── data-analysis/
    ├── diagram-to-office/
    ├── document-styling/
    ├── document-writing/
    ├── meeting-notes/
    ├── academic-diagram/             #   论文配图（上游同步型：TikZ 架构/算法图，OpenTikZ 库）
    ├── patent-disclosure-skill/     #   中国专利技能（交底书/申请文件/检索/解读/地图/审查答复）
    ├── ppt-master/                  #   PPT 大师（上游同步型：专业级 PPT 工作流，精简收录）
    ├── presentation/                #   演示文稿快速初稿（轻量零依赖；专业版用 ppt-master）
    ├── research-report/
    └── weekly-report/
```

> 需要第三方依赖的插件：依赖以 `wheels/*.whl` 放在插件目录内分发
> （见「插件依赖」），运行时自动解压生效；渲染引擎（node_modules 等
> 大体积依赖）不进 git，技能运行时按需自动安装。

## 使用方式

在 lite-work 中：**设置 → Plugins → 检查社区更新**，即可看到本仓库的
插件与技能，逐个安装 / 更新。也可以在导入框直接粘贴任意插件目录的
GitHub URL（支持子目录）：

```text
https://github.com/laynepeng/lite-work-plugins/tree/main/plugins/ocr-plugin
```

安装位置：插件 `~/.lite-work/plugins/`，技能 `~/.agents/skills/`。

## 编写插件

一个插件 = 一个 `ToolPlugin` 子类，放在 `plugins/<插件名>/plugin.py`：

```python
from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class MyPlugin(ToolPlugin):
    name = "my-plugin"          # 与内置同名 → 覆盖内置版（卸载回退）
    version = "1.0.0"           # semver；高于内置版本时出现「更新」
    description = "我的工具集"
    removed_tools = []          # 可声明移除其他插件的工具

    def __init__(self) -> None:  # 构造必须无参
        self._app = None

    def install(self, kernel) -> None:
        # 从内核服务捕获 app：workspace 热切换后自动刷新
        if kernel.has_service("app"):
            self._app = kernel.get_service("app")
        super().install(kernel)

    def get_tools(self):
        return [ToolDefinition(
            name="my_tool",
            description="工具描述（给 LLM 看）",
            parameters={"type": "object", "properties": {...}, "required": [...]},
        )]

    async def execute(self, name, args):
        return "工具结果"
```

要点：

- **自包含**：实现代码放在插件里（不要 `from litework.tools.xxx import` 复用
  内部实现——那不是独立更新，只是薄包装）
- **无参构造**：workspace 等运行时状态在 `install(kernel)` 时从
  `kernel.get_service("app")` 捕获
- **覆盖**：`get_tools()` 返回与内置同名的工具即完成替换；
  `removed_tools` 声明移除
- **依赖**：优先用 `wheels/` 目录分发（见下节「插件依赖」）；
  `requirements.txt` 仅开发态可用（打包版会跳过并告警）
- 新插件记得登记进 `manifest.json`
- 主程序已捆绑的库可直接 import：docx / openpyxl / pptx / reportlab /
  pandas / matplotlib / pypdf / rapidocr / pymupdf / curl_cffi /
  httpx / numpy / PIL——**这些不要打进 wheels**（体积巨大且版本可能冲突）

## 插件依赖（wheels 分发）

打包版 lite-work 是 PyInstaller frozen 进程：site-packages 已固化，
`pip install` 装到任何位置 frozen 进程都无法 import。因此插件依赖的
**唯一分发方式是自带 wheels**：

```text
plugins/my-plugin/
├── plugin.py
└── wheels/                  # 安装时自动解压到 libs/ 并加入 sys.path
    └── some_pkg-1.0.0-py3-none-any.whl
```

安装时主程序自动把 `wheels/*.whl` 解压到插件目录 `libs/`（whl 即 zip，
包结构在根目录），并把 `libs/` 加入 `sys.path`——插件代码直接
`import some_pkg` 即可。解压幂等（stamp 记录 wheel 清单，更新才重解压）。

**制作 wheels**（在插件目录下执行）：

```bash
# 当前平台（本机测试用）
pip download some_pkg -d wheels/

# 跨平台分发：按目标平台下载二进制 wheel（纯 Python 包无需指定平台）
pip download some_pkg -d wheels/ \
  --platform win_amd64 --python-version 3.12 --only-binary=:all:
```

约束与建议：

- **纯 Python 依赖**（jinja2、pyyaml、markdown 等）：任意平台通用，首选
- **带原生扩展的依赖**（lxml、pillow 等）：whl 与平台/Python 版本强绑定，
  需为每个目标平台各放一个 wheel（文件名含平台标签，自动全部解压）
- 超大依赖（>50MB）不建议打进 wheels——考虑改为技能（外部进程执行，
  可用系统 pip/npm）或让用户预装
- `requirements.txt` 保留作开发态便利（`npm run dev` 时 venv pip 安装），
  但**打包版安装会跳过并记录告警**，社区发布必须带 wheels
- **更新插件引入新包时先跑审计**（CI 强制执行，规则见 [AGENTS.md](AGENTS.md)）：
  ```bash
  python scripts/check_plugin_deps.py
  ```

## 编写技能

技能 = 一个目录 + `SKILL.md`（frontmatter + 正文指令）：

```markdown
---
name: my-skill
description: 一句话描述（显示在技能索引，Agent 据此决定是否加载）
triggers: 关键词1,关键词2
---

# 我的技能

（工作流程、规则、示例——Agent 加载后按此执行）
```

## 上游同步型技能

### ppt-master

`skills/ppt-master` 源自上游 [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master)
（MIT），是本仓库唯一的上游同步型技能：**禁止手改其内容**，升级只走
`python scripts/sync_ppt_master.py --tag v<版本>`（克隆 → 精简排除 → 重放
overlay 补丁 → 完整性校验 → 同步 manifest 版本）。

收录时的受控差异（overlay 权威副本在 `scripts/ppt_master_assets/`）：

- **精简收录**（社区安装是整仓 git clone，控体积）：剔除 AI 生图风格参考图
  （43MB）、音效库（12MB）、tests；保留全部 12000+ 图标与核心链路。
  需要被剔资源时：`python3 scripts/fetch_optional_assets.py`
  （从上游 Releases 按需补全；国内网络可先手动下载 zip 再 `--zip` 离线补）
- **SKILL.md frontmatter** 追加顶层 `version` + `triggers`（lite-work 社区
  更新检查读顶层 version；上游嵌套 metadata 块受防篡改门保护，原样保留）
- **requirements.txt** 为精简核心版（SVG→PPTX 主链路依赖随导入自动安装；
  PDF 转源 / AI 生图 / 视频旁白等可选依赖按功能现场 pip install）

技能自带 `scripts/attribution_guard.py` 防篡改门（fail-closed）：改动
LICENSE / SPONSORS / gate 脚本 / 嵌套 metadata 会使技能立即拒绝运行，
同步脚本每步都以该校验收尾。

### academic-diagram

`skills/academic-diagram`（原名 `opentikz`，v0.1.0 时改名）源自上游
[opentikz/opentikz](https://github.com/opentikz/opentikz)（Code MIT / 内容 CC0），
专门为**学术论文配图**而生（TikZ 生成：61 个可复制图标（含 41 个品牌 logo）+
9 个参数化模板 + 3 个整图示例，模板带 edit_contract 供 Agent 安全编辑）。
**禁止手改其内容**，升级只走 `python scripts/sync_academic_diagram.py`（克隆 →
合并上游 skills/ 与库资源为自包含单目录 → 重放 SKILL.md overlay →
更新 manifest 版本）。

收录差异（overlay 权威副本在 `scripts/academic_diagram_assets/`）：

- 上游技能在 `skills/using-opentikz/` 而库资源在仓库根——本仓库把两者
  合并进 `skills/academic-diagram/` 单目录（lite-work 技能必须自包含），
  SKILL.md 的 OTROOT 定位规则已相应扩展（同级目录含 catalog.json 即库根）
- SKILL.md 补丁：frontmatter（name=academic-diagram + version/triggers +
  论文定位与负向路由）、OTROOT 自包含规则、无本地 LaTeX 时的降级交付
- 不带 requirements.txt（出图零 Python 依赖）；编译验证需本机 LaTeX

## 版本策略

- `manifest.json` 顶层 `version`：本仓库清单版本
- 每个插件 / 技能独立 semver；与主程序内置版同源时版本号保持同步起点，
  之后独立演进
- `min_app_version`：本仓库内容要求的最低 lite-work 版本

## License

MIT

## 协作模式（kind: collab）

lite-work v1.5.0 起，**7 个协作模式已内置随主程序发布**（v1.0.0）：
编排-工人 / 流水线 / 头脑风暴 / 互批 / 辩论 / 会议 / 测试驱动接力。

本仓库的 `collab-*` 包是它们的**独立更新通道**（与 office-plugin 等内置插件
同一机制）：

- lite-work 内置 v1.0.0 ↔ 社区同版本 → 设置面板显示「已是最新」
- 社区发布新版（如 v1.1.0）→ 显示「可更新」，安装后**覆盖内置版**
- 删除本地版 → 自动回退内置版
- 安装/覆盖判定按插件包名（`collab-meeting` 等）匹配，`kind: "collab"`
  标记协作模式类别（lite-work 前端据此归入协作模式安装面板与标签）

### 包格式

```
plugins/collab-<mode>/
  plugin.py    # CollabModePlugin 子类（元信息与内置版保持一致）
  recipe.md    # 模式配方（须与内置 RECIPE 常量同步——本仓库是上游事实源）
  icon.svg     # logo（48×48；对话框选择器与安装面板展示）
```

`plugin.py` 模板（元信息必须与内置版一致，否则覆盖机制失效）：

```python
from litework.orchestration.collab_policy import CollabModePlugin

class MeetingCollabMode(CollabModePlugin):
    name = "collab-meeting"          # 插件包名（与内置一致 → 可覆盖更新）
    version = "1.0.0"
    description = "协作模式：多 Agent 围绕议题轮流发言、收敛共识"
    mode_name = "meeting"            # 模式标识（会话选择器取值）
    display_name = "会议（群聊共识）"   # 选择器显示名

    # 可选行为钩子（异常自动隔离，不影响任务执行）：
    # async def on_agent_spawned(self, ctx): ...
    # async def on_agent_complete(self, ctx): ...
    # async def on_task_done(self, ctx): ...
```

`manifest.json` 条目：

```json
{
  "name": "collab-meeting",
  "version": "1.0.0",
  "description": "协作模式：多 Agent 围绕议题轮流发言、收敛共识",
  "path": "plugins/collab-meeting",
  "kind": "collab",
  "icon": "plugins/collab-meeting/icon.svg"
}
```

### 新增协作模式（内置没有的）

内置只含上述 7 种；社区可发布全新模式（如 `collab-swarm`），用户安装后
即出现在对话框协作模式选择器中。
