# AGENTS.md — lite-work-plugins 仓库工作规则

本仓库是 [lite-work](https://github.com/LaynePeng/lite-work) 的社区插件/技能分发源。
任何在本仓库工作的 Agent / 贡献者必须遵守以下规则。

## 插件更新 Checklist（每次改动必须逐项执行）

1. **新依赖检查（最重要）**：
   对比 `plugin.py` 的**全部** import（含函数体内的延迟 import）与下方
   「主程序已捆绑的包」清单：
   - 已捆绑 → 直接 import 即可，**禁止**打进 wheels（体积浪费 + 版本冲突）
   - 未捆绑 → **必须**放 wheel 分发：
     ```bash
     pip download <pkg> -d plugins/<插件名>/wheels/
     # 原生扩展依赖需按目标平台各放一个 wheel（win_amd64 / macOS arm64 …）
     ```
   - 跑审计脚本确认（CI 会强制执行，红了就修）：
     ```bash
     python scripts/check_plugin_deps.py
     ```
2. **版本号**：插件类 `version` 与 `manifest.json` 同步 bump（semver）；
   与主程序内置版同源的插件（office / ocr / webfetch），版本号跟随主程序起点后独立演进
3. **同步生成的插件勿手改**：`office-plugin` / `ocr-plugin` / `webfetch-plugin`
   由主仓库脚本生成（`litework/tools/{office,ocr,web}.py` → 社区分发包装），
   改动请去主仓库改源文件后重新同步——手改会在下次同步时被覆盖
4. **manifest.json**：新增插件必须登记（name / version / description / path / tools），
   工具名列表保持与实际 `get_tools()` 一致

## 主程序已捆绑的包（禁止打进 wheels）

| 类别 | import 名 |
| --- | --- |
| Web/服务 | `fastapi` `uvicorn` `starlette` `pydantic` `anyio` `httpx` `httpcore` |
| 解析器 | `tree_sitter` `tree_sitter_typescript` `tree_sitter_java` `tree_sitter_go` `pathspec` |
| 办公/文档 | `docx` `openpyxl` `pptx` `reportlab` `pypdf` `lxml` `xlsxwriter` `dateutil` |
| 数据/图表 | `pandas` `matplotlib` `numpy` `PIL` |
| OCR/PDF/反爬 | `rapidocr_onnxruntime` `onnxruntime` `cv2` `pymupdf` `fitz` `curl_cffi` |
| 传递依赖 | `yaml` `shapely` `pyclipper` `tqdm` `click` `certifi` `idna` |

> 清单与主仓库 `pyproject.toml` + PyInstaller collect 参数保持同步。
> **存疑时**：以主程序打包产物 `_internal/` 目录（二进制包）或 PYZ 归档
> （纯 Python 包）为准；或先跑审计脚本看报什么。

## wheels 规则（打包版唯一可离线安装方式）

- 打包版 lite-work 是 PyInstaller frozen 进程：site-packages 固化，
  `pip install` 装到任何位置都 import 不到 → **依赖只有 wheels 一条路**
- 纯 Python 依赖任意平台通用；原生扩展依赖按目标平台各放一个 wheel
- 超过 50MB 的依赖不要打 wheels → 改为技能形态（外部进程执行，可用
  系统 pip/npm）或文档注明让用户预装
- `requirements.txt` 仅开发态便利（`npm run dev` 时 venv pip 安装），
  打包版安装会**跳过并记录告警**——社区发布以 wheels 为准

## 技能规则

- 技能 = 目录 + `SKILL.md`（frontmatter：name / description / triggers）
- Python/Node 依赖走 `requirements.txt` / `package.json`（技能由系统
  Python / npm 子进程执行，不受 frozen 限制）
- 新技能登记进 `manifest.json` 的 `skills` 段

## 提交规范

- commit message 用中文，格式 `<类型>: <摘要>`（feat / fix / docs / sync）
- 触及 `plugins/**` 的提交必须先过审计脚本（CI 红了不允许合并）
