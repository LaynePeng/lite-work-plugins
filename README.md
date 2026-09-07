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
    ├── data-analysis/
    ├── diagram-to-office/
    ├── document-writing/
    ├── meeting-notes/
    ├── presentation/
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

## 版本策略

- `manifest.json` 顶层 `version`：本仓库清单版本
- 每个插件 / 技能独立 semver；与主程序内置版同源时版本号保持同步起点，
  之后独立演进
- `min_app_version`：本仓库内容要求的最低 lite-work 版本

## License

MIT
