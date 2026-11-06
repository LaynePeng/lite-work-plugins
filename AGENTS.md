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
   与主程序内置版同源的插件（office / ocr / webfetch），版本号跟随主程序起点后独立演进。
   **`manifest.json` 顶层 `version` 也要一起 bump**（仓库内容版本，随任一新插件/技能
   发布或插件重大改动递增）。历史坑：2.1.3→2.3.1 连续多次发版都只 bump 了插件版本、
   顶层停在 `1.0.33`，导致清单首行读起来像是"仓库没变"。它目前不参与任何判定
   （「检查社区更新」用的是**逐插件** `version` 对比，见
   `web/src/components/SettingsModal.tsx` 的 `communityUpdates`），但作为仓库元信息
   必须准确
3. **`office-plugin` / `ocr-plugin` / `webfetch-plugin` 以本仓库为源**：
   直接在这三个插件里开发改动（本仓库是上游事实源）；需要进主程序内置时，
   再**按需同步回** lite-work 的 `litework/tools/{office,ocr,web}.py`。
   不要反向操作（不要改主仓库后往回同步）。
4. **`astock-plugin`（A 股助手）不内置，只在本仓库独立发布**：
   **禁止**把它同步进 lite-work 的 `litework/tools/` 或 `builtin_plugins/`。
   它是「个人自用型」插件——依赖用户自己的券商习惯、台账数据与邮箱配置，
   进内置只会增加主程序的维护面与体积，且没有内置的必要性。
   后续如需内置其他插件，须逐项确认，不要套用第 3 条的同步流程。
5. **manifest.json**：新增插件必须登记（name / version / description / path）；
   **工具型插件**（`kind` 缺省或 `tool`）另需登记 `tools`，且与实际 `get_tools()` 一致；
   **模式型插件**（`kind: collab`）与无工具的数据源插件（如 `pricing-plugin` 的
   `get_tools()` 返回空）不需要 `tools`，登记空数组亦可
6. **禁止提交 0 字节隐藏文件**（`.gitkeep` 类占位）：仓库不接受 0 字节的隐藏文件
   （CI `.github/workflows/hidden-file-check.yml` 会强制检查）；上游同步型技能的
   同步脚本已自动清理（见 `sync_academic_diagram.py` 的 `.gitkeep` 清理），
   手改时也不要引入这类占位

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
- 精简排除清单在 `scripts/sync_ppt_master.py` 的 `RSYNC_EXCLUDES`；文件类
  overlay（精简 `requirements.txt`、`fetch_optional_assets.py`）的权威副本在
  `scripts/ppt_master_assets/`；SKILL.md 的顶层 `version`/`triggers` **不是文件**，
  由同步脚本从上游 `metadata.version` + 脚本内 `TRIGGERS` 常量注入。
  三类要改都在上述位置改完再重放，不要直接改技能目录
- 技能自带 `attribution_guard.py` 防篡改门（嵌套 metadata / LICENSE /
  SPONSORS / gate 脚本动一个就拒绝运行）；任何改动后必须
  `python3 skills/ppt-master/scripts/attribution_guard.py` 验证 exit 0
- 触发词分工：裸词 PPT/ppt/幻灯片/演示文稿 归 ppt-master；
  `presentation` 只保留 PPT初稿/快速PPT 等意图词（避免双注入冲突）

### academic-diagram（上游同步型技能，特殊规则）

- 事实源是上游 `opentikz/opentikz`（Code MIT / 内容 CC0）；上游技能在
  仓库 `skills/using-opentikz/`、库资源在仓库根，本仓库**合并为
  `skills/academic-diagram/` 自包含单目录**收录（v0.1.0 时从 opentikz 更名为
  academic-diagram，定位学术论文配图）。**禁止手改其内容**——升级只走：
  `python scripts/sync_academic_diagram.py [--ref <tag|main>]`
- SKILL.md overlay（name=academic-diagram + 顶层 version/triggers、OTROOT 自包含
  定位规则、无 LaTeX 降级交付）的权威副本在 `scripts/academic_diagram_assets/`
  （含 upstream-SKILL.md 漂移检测基准）；改 overlay 必须改那里再重放；
- **漂移基准取自上游 `main` 快照，不是 tag**：上游 tag 可能落后（品牌图标在
  v0.1.0 之后才并入 main），钉旧 tag 会被误报基线漂移且会拉到较旧内容。
  升级用 `python scripts/sync_academic_diagram.py --ref main`（默认即 main）；
- 不带 requirements.txt（出图零 Python 依赖）；编译验证依赖本机 LaTeX
- 触发词分工：tikz/架构图/算法图/论文配图/科研绘图/框图 归 academic-diagram；
  `diagram-to-office` 保留 plantuml/mermaid/时序图/流程图/类图（转
  Office 图片场景），避免双注入冲突；**专利附图由 patent-disclosure-skill
  自带黑白引擎负责**，不走 academic-diagram

## 提交规范

- commit message 用中文，格式 `<类型>: <摘要>`（feat / fix / docs / sync）
- 触及 `plugins/**` 的提交必须先过审计脚本（CI 红了不允许合并）

### commit message 写什么、不写什么

**摘要（首行）**：直接说结果，一个动作一句陈述。不要叙事转折、不要自我点评。

```text
✅ fix(astock): 修 run_all 读错策略表导致对比推荐率永远缺失
✅ fix: 覆盖安装后插件兄弟模块未重新加载
❌ fix: 插件覆盖安装仍不生效——v2.1.3 漏了「装完没有 __pycache__」这条路径
   （转折叙事，只讲来历不讲结果）
```

**正文**：只写「现象 → 根因 → 改动 → 验证」四类事实，用 `-` 列表分行。
本项目既有提交正文在 17–36 行，超过 50 行基本是注水。

允许写进正文：

- 根因代码位置与错误机制（`_refresh_plugin_bytecode` 里 `os.listdir` 抛
  `OSError` 就 `return False`，上层据此不 purge）
- 改动清单（`- 无 __pycache__ 时返回 True`）
- 回归用例路径与「移除修复后该用例失败」这类**可复现**的验证方式
- 兼容性/风险提示（如需用户操作、影响哪些场景）

**禁止写进正文**（以下是本仓库明确不要的“聊天式”内容）：

| 禁止 | 例子 |
| --- | --- |
| 心路历程 / 排查日志 | 「发现过程（值得记一笔）：…」「我一开始以为是…」 |
| 自我点评 / 感慨 | 「**无法证明是新的不能当成确认是旧的**」「这个坑实际踩过一次」 |
| 口语化转折与强调 | 「卧槽」「其实」「说白了」「一模一样——」「最主流的那条路径」 |
| 反问、命令式语气 | 「谁再写 `bt.STRATEGIES` 立刻红」 |
| 无信息量的粗体强调 | 正文里满屏 `**…**`；强调应由结构表达，不靠加粗 |
| 版本号塞进摘要凑长度 | 摘要写清结果即可，版本号有 CHANGELOG 与 manifest 承载 |
| revert/reapply 噪音 | 反复 `git revert` 同一提交再 reapply，会污染历史（详见下条） |

### 历史卫生（必须遵守）

- **同一改动的反复提交必须压成一条**：用 `git commit --amend` 或
  `git reset --soft <base> && git commit`，不要把 `commit → revert → reapply
  → amend` 的中间态留在历史里。`git log` 应当读起来像一次做完的。
- **提交前自查**：`git log --oneline -5` 看一遍，出现 `Revert "..."` /
  `Reapply "..."` / 连续多条同一主题，就是没压干净。
- **改写 message 用 `--amend`**，不要新开一条「修正提交信息」的提交。
- **未经用户明确要求不要 `git push`**；`reset --hard`、`reflog expire`、
  `gc --prune=now` 属破坏性操作，执行前必须得到用户确认。
- **不要在隔离工作区之外直接写主仓库**：宿主提示「主工作区不允许读写」时，
  改代码应走用户指定的目标仓库；确实需要动主仓库时先问，不要用
  `execute_command` 绕过隔离。
