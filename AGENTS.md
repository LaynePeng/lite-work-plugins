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
3. **`office-plugin` / `ocr-plugin` / `webfetch-plugin` 以本仓库为源**：
   直接在这三个插件里开发改动（本仓库是上游事实源）；需要进主程序内置时，
   再**按需同步回** lite-work 的 `litework/tools/{office,ocr,web}.py`。
   不要反向操作（不要改主仓库后往回同步）。
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

- 技能 = 目录 + `SKILL.md`（frontmatter：name / description / triggers / version）
- **版本号（关键）**：`SKILL.md` frontmatter 的**顶层 `version`** 必须与
  `manifest.json` 中该技能的 `version` 保持一致。主程序「检查社区更新 →
  社区技能」就是拿这两者比对：已安装副本读的是 SKILL.md 的 `version`
  （`litework/tools/skills.py` → `meta.get("version")`），缺失则更新永不显示。
  写成嵌套的 `metadata: version:` **无效**（后端只读顶层）。
- 技能的 `scripts/` / `themes/` / `assets/` 等子目录会随 GitHub 导入一起复制
  （导入走 git clone 整仓 + 子路径过滤），无需额外登记
- Python/Node 依赖走 `requirements.txt` / `package.json`（技能由系统
  Python / npm 子进程执行，不受 frozen 限制；导入时会自动 pip/npm 安装）
- 新技能登记进 `manifest.json` 的 `skills` 段

### ppt-master（上游同步型技能，特殊规则）

- 事实源是上游 `hugohe3/ppt-master` 的 `skills/ppt-master/`；本仓库是
  **精简收录 + overlay 增量**，**禁止手改 `skills/ppt-master/` 内容**——
  下次同步会全部覆盖。升级只走：
  `python scripts/sync_ppt_master.py --tag v<版本>`
- 精简排除清单（ai-image-comparison / sounds wav / tests）与 overlay
  （SKILL.md 顶层 version+triggers、精简 requirements.txt、
  fetch_optional_assets.py）的权威副本在 `scripts/ppt_master_assets/`，
  改 overlay 必须改那里再重放，不要直接改技能目录
- 技能自带 `attribution_guard.py` 防篡改门（嵌套 metadata / LICENSE /
  SPONSORS / gate 脚本动一个就拒绝运行）；任何改动后必须
  `python3 skills/ppt-master/scripts/attribution_guard.py` 验证 exit 0
- 触发词分工：裸词 PPT/ppt/幻灯片/演示文稿 归 ppt-master；
  `presentation` 只保留 PPT初稿/快速PPT 等意图词（避免双注入冲突）

## 提交规范

- commit message 用中文，格式 `<类型>: <摘要>`（feat / fix / docs / sync）
- 触及 `plugins/**` 的提交必须先过审计脚本（CI 红了不允许合并）
