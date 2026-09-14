---
name: academic-paper-style-docx
description: 中文学术论文风格docx：把 Markdown 论文（LaTeX 公式、三线表、图片、[n] 引用）转成符合国内高校规范的 .docx，也可解包/编辑/重打包已有 Word（三线表、公式、批注、修订）。适用于课程论文、毕业设计/学位论文、数学建模论文、期刊投稿等场景。
triggers: 论文,课程论文,毕业论文,学位论文,数学建模,学术论文,期刊投稿,三线表,参考文献,LaTeX公式,公式编号,上标引用,论文格式,GB/T 7714,docx排版,word排版,查重,摘要,关键词
version: "1.0.0"
---

# 中文学术论文风格docx

面向**中文学术论文**的 Word 生成 / 编辑技能。当用户要写论文、把 Markdown 论文转成
Word，或提出「三线表 / 带编号公式 / [n] 上标引用 / 论文格式 / 摘要关键词」等要求时，
加载本技能。

核心价值：

- 内置中国大陆高校常见的论文格式规范（A4 + 2.5cm 页边距、宋体小四正文、黑体标题、
  三线表、公式居中右编号、[n] 上标引用、页脚页码、参考文献分页）；
- `scripts/new_doc.js` 把 **Markdown 论文一键转成 .docx**，LaTeX 公式经
  temml → MathML → OMML 转为 **Word 原生公式**（可在 Word 里继续编辑）；
- 附带 DOCX 解包 / 编辑 / 打包 / schema 校验工具，可对**已有** Word 做 XML 级精修
  （插入三线表、公式、批注、修订留痕）。

## ⚠️ 与内置 office-plugin 的分工

| 需求 | 用什么 |
| --- | --- |
| 普通文档 / 报告 / 通知 → Word | 内置 `docx_create` + `document-writing` / `document-styling` 技能 |
| **中文学术论文**（公式/三线表/参考文献/论文格式） | **本技能** |

本技能是「论文专用排版器」，不是通用文档工具；非论文场景优先用内置工具，避免过度工程。

## 上游来源与许可

移植自 [Gostyan/docx-skill-4-cn-paper](https://github.com/Gostyan/docx-skill-4-cn-paper)
的 `docx-editor-cn`（MIT License，Copyright (c) 2025-2026 Gostyan，见同级
`LICENSE.txt`）。本地适配内容：

1. 脚本调用路径统一改为相对**技能目录**（下文 `${SKILL_DIR}`）；
2. 补充依赖安装说明与 Node ≥ 18 / Python ≥ 3.9 要求；
3. **修复上游 bug**：`table.py` / `formula.py` 生成的 `<w:pPr>` 子元素顺序违反
   OOXML schema（`<w:jc>` 被放在 `<w:spacing>` / `<w:ind>` 之前），导致
   `pack.py` 自动校验报 `spacing/ind: This element is not expected`。已调整为先
   `spacing → ind → jc`；
4. **修复上游 bug**：单行块公式 `$$...$$` 被误当成行内公式 —— 正文字面残留 `$`、
   `\tag{n}` 未剥离、公式整条丢失。现支持单行块公式，并**复用**同一套 `\tag{n}`
   剥离逻辑（`splitTag`）；
5. **修复上游 bug**：`unicodeToLatex` 把裸 `*` 一律提升为 `^*`，令 `{Q}^{ * }` 变成
   `{Q}^{ ^* }` 非法嵌套 → temml 能渲染但 OMML 转换结果为空，**公式整条丢失**。
   现改为「只提升裸星号」；
6. **修复上游 bug**：Markdown 强调（`**粗体**` / `*斜体*`）从不解析，正文里原样输出
   `**程序级**` 这类标记。现由统一的 `buildInlineRuns` 处理（含表格单元格 / 列表 / 关键词）；
7. **改为响亮失败**：块公式解析失败不再静默回退成「原始 LaTeX 文本」，而是直接报错
   （含公式原文与原因）；行内公式失败会汇总告警并置退出码 1；
8. **新增** `scripts/check_formulas.py`：与 schema 校验互补的**内容**健康检查
   （残留 `$` / 未转换的 LaTeX / 未解析的强调标记）；
9. **Python 3.9 兼容**：`office/*.py`、`table.py`、`formula.py`、`comment.py` 在
   3.9 下也能运行（原先 `match` 语句与 `X | Y` 注解要求 3.10+）。

完整上游正文（XML 参考、11 个已解决问题的细节等）原样保留在同级 `REFERENCE.md`。

## 依赖安装

```bash
# Node 依赖：docx / temml / fast-xml-parser（Node ≥ 18）
cd "${SKILL_DIR}" && npm install

# Python 依赖：仅解包/校验/批注脚本需要（Python ≥ 3.9）
python3 -m pip install -r "${SKILL_DIR}/requirements.txt"
# lxml 由宿主 lite-work 提供（内置包），无需额外安装
```

> 技能导入时 lite-work 会依据 `package.json` / `requirements.txt` 自动安装依赖；
> 若沙箱内未自动装好，按上面命令手动补齐。
> 三条外部工具链按需使用：`pandoc`（`formula.py` 的 LaTeX→OMML 转换）、
> LibreOffice/`soffice`（.doc→.docx、转 PDF）、`pdftoppm`（PDF→图片）。

## 目录结构

```text
academic-paper-style-docx/
├── SKILL.md                 # 本文件（Agent 加载的指令）
├── REFERENCE.md             # 上游完整正文（深入查阅）
├── README.md                # 上游说明（含格式模板示例）
├── LICENSE.txt              # MIT（上游版权）
├── package.json / package-lock.json
├── requirements.txt
├── example/markdown论文/     # 可运行的示例论文（md + images + 参考成品 docx）
├── tests/                   # 回归套件（fixture + 断言运行器）
└── scripts/
    ├── new_doc.js           # ★ Markdown → 论文 docx（主入口）
    ├── check_formulas.py    # ★ 交付前内容健康检查（见「完成后必须校验」）
    ├── convert_paper.js     # 内置内容写死的示例（排版函数参考）
    ├── mathml-to-docx.js    # MathML → docx Math(OMML) 转换器
    ├── table.py             # 向解包目录插入三线表（XML 级）
    ├── formula.py           # 向解包目录插入带编号公式（XML 级，需 pandoc）
    ├── comment.py           # 插入批注
    ├── accept_changes.py    # 接受全部修订（需 LibreOffice）
    └── office/
        ├── unpack.py        # docx → 解包 XML（美化 + 合并 run）
        ├── pack.py          # 解包 XML → docx（含自动修复与校验）
        ├── validate.py      # 按 OOXML schema 校验
        ├── soffice.py       # LibreOffice 包装（.doc→.docx、转 PDF）
        ├── helpers/ validators/ schemas/
        └── templates/       # 批注相关 XML 模板
```

## 工作流 A：Markdown 论文 → Word（主路径）

```bash
node "${SKILL_DIR}/scripts/new_doc.js" "论文.md" "产出物/论文.docx"
```

- 第 1 个参数：输入 Markdown；第 2 个参数（可选）：输出 docx，
  缺省时与 md 同目录同名 `.docx`。
- 图片路径**相对于 Markdown 文件所在目录**（如 `images/1.jpg`）。

### Markdown 书写约定（解析器识别规则）

| Markdown 写法 | 生成效果 |
| --- | --- |
| `# 论文标题` | 居中大标题（三号加粗） |
| `## 目录` | **跳过**（不解析手写目录；程序会自动插入 Word 目录域） |
| `## 张三(name)` | 居中作者名（小三） |
| `## 摘要:` | 居中「摘要」标题 |
| `关键词：A；B` | 关键词段（自动在摘要后分页 + 插入 TOC 域 + 再分页） |
| `## 一、研究背景` | 一级标题（中文数字序号，**每章前分页**） |
| `## 参考文献` | 参考文献章（分页）；其下 `[1] xxx` 行为文献条目 |
| `### 1.1 小节` | 二级标题 |
| `#### 1.1.1 子节` | 三级标题 |
| `$$` … `\tag{3}` … `$$` | 块公式，居中 + 右对齐编号 `(3)`，Word 原生公式 |
| `$E = mc^2$`（行内） | 行内公式 |
| `[1]` / `[2][3]` | **上标**引用 |
| `**粗体**` / `*斜体*` / `_斜体_` | 粗体 / 斜体（支持嵌套：`**_粗斜体_**`、`***粗斜体***`） |
| `<table>…</table>`（**单行 HTML 表格**） | 三线表；上一行以「表」开头 → 表题 |
| `![图注](images/1.png)` | 居中图片（自动按尺寸缩放）；下一行以「图」开头 → 图题 |
| `- 列表项` | 项目符号 |
| 段首 4 空格 / Tab | 缩进段落 |

> **注意**：管道式 Markdown 表格（`| a | b |`）**不支持**，请写成**单行**的
> `<table><tr><td>…</td></tr>…</table>`。表格单元格内可用 `$...$` 行内公式。

### 公式与强调：推荐写法（避免踩坑）

| 场景 | ✅ 推荐 | ❌ 避免 |
| --- | --- | --- |
| 块公式（带编号） | 多行写法，`\tag{n}` 单独占一行：<br>`$$`<br>`E = mc^{2} \tag{1}`<br>`$$` | 一行里塞两个块公式：`$$A$$ 和 $$B$$`（会直接报错） |
| 单行块公式 | `$$E = mc^{2} \tag{1}$$` 单独占一行（前面可有引导文字） | 公式后面再跟内容（`$$A$$ 说明`）；会直接报错 |
| 上标星号 | `{Q}^{*}`、`\pi^{*}` | `{Q}^{ * }`（多余空格，不规范） |
| 乘号 | `\times` / `\cdot` | 裸 `*`（会被理解为上标星号） |
| 粗体 / 斜体 | `**程序级**`、`*斜体*`、`**_粗斜体_**` | 不支持的：`~~删除线~~`、`` `代码` ``、文字链接 |

补充规则：

- **块公式出错会直接终止**（不再静默降级成文本）：报错信息含公式原文与原因，
  按提示修正后重跑。所以不要指望「生成出来再看」——有错会当场失败。
- **强调标记不要跨数学**：`**` / `_` 只在 `$...$` 之外解析，
  因此 `{x}_{n}`、`{Q}^{ * }` 这类写法不会被从中间撕开。

### 完成后必须校验（两步都要做）

```bash
# 1) OOXML 结构校验（schema）
cd "${SKILL_DIR}/scripts/office" && python3 validate.py "产出物/论文.docx"

# 2) 内容健康检查（公式是否真的成了 Word 公式、有无残留标记）
python3 "${SKILL_DIR}/scripts/check_formulas.py" "产出物/论文.docx"
```

- 第 1 步输出 `All validations PASSED!` = 结构合法；报错时按工作流 B 排查 XML。
- 第 2 步输出 `OK`（退出码 0）= 内容正确；若 `FAIL`，按明细修正 Markdown 后重跑。
- **两步都通过再交付**：schema 校验保证不了内容——公式整条丢失也能「校验通过」。

## 工作流 B：编辑已有 Word（XML 级）

```bash
cd "${SKILL_DIR}/scripts/office"     # ⚠️ 必须在 office/ 目录下运行（脚本以相对路径 import validators/helpers）

python3 unpack.py "产出物/论文.docx" unpacked/     # 解包为 XML
#   编辑 unpacked/word/document.xml（正文）、styles.xml（样式）等
python3 pack.py unpacked/ "产出物/论文_改.docx" --original "产出物/论文.docx"
python3 validate.py "产出物/论文_改.docx"
```

- 只改 `document.xml` 正文；**不要整体重写 `styles.xml`**（会丢失用户模板样式）。
- 新增段落要带上相邻同类段落的 `<w:pStyle>`，否则会掉回默认样式。
- `unpack.py` 会把中文弯引号转成 `&#x201C;`/`&#x201D;` 实体，
  搜标题文字时若含中文引号，需搜实体形式。
- `pack.py` 会做自动修复（`durableId` 越界、缺 `xml:space="preserve"`）并校验；
  加 `--validate false` 可跳过。

## 工作流 C：向解包目录插入三线表 / 公式（免手写 XML）

```bash
# 三线表（插到含「锚文本」的段落之后）
python3 "${SKILL_DIR}/scripts/table.py" unpacked/ "1-1" "符号说明" \
  --headers "符号,说明" --rows '[["α","学习率"],["γ","折扣因子"]]' \
  --anchor "1.3 本文符号说明"

# 带编号公式（需 pandoc）
python3 "${SKILL_DIR}/scripts/formula.py" unpacked/ 'E = mc^2' 9 \
  --anchor "1.3 本文符号说明"
```

- 省略 `--anchor` 时**只打印 XML 片段**（供手动粘贴），不会写入文件。
- `--rows` 是 JSON 数组；含反斜杠的 LaTeX 要写成合法 JSON（如用 Unicode `α` 而非 `\alpha`）。
- 插入后务必 `pack.py` + `validate.py` 复核。

## 中文学术排版规范（脚本已内置，供核对）

| 元素 | 中文字体 | 英文/数字 | 字号 |
| --- | --- | --- | --- |
| 正文 | 宋体 SimSun | Cambria Math（可用 Times New Roman） | 12pt 小四 |
| 一级标题 | 黑体 SimHei | Cambria Math | 16pt 三号 |
| 二级标题 | 黑体 SimHei | Cambria Math | 14pt 四号 |
| 三级标题 | 黑体 SimHei | Cambria Math | 12pt 小四 |
| 图表题注 | 宋体 SimSun | Cambria Math | 11pt 五号 |

- 页面：A4（11906×16838 DXA），四边页边距 2.5cm（1418 DXA），页脚居中页码。
- 三线表：顶线 1.5pt、栏目线 0.75pt、底线 1.5pt，**无竖线、无内部横线**。
- 公式：无边框三列表格实现「左留白 + 居中公式 + 右对齐编号」。
- 英文字体混排通过 `font: { ascii, eastAsia, hAnsi }` 分别设置，避免英文/数字掉成宋体。

## 关键陷阱（Top 清单）

1. `<w:pPr>` 子元素顺序固定：`pStyle → … → spacing → ind → … → jc → … → rPr`，乱序会被 schema 校验拦下。
2. 三线表正文单元格边框必须逐格显式设为 `NONE`，只保留三条线。
3. 块公式表格要把 `insideHorizontal` / `insideVertical` 一并设为 `NONE`，否则出现内部框线。
4. `PageBreak` 必须包在 `Paragraph` 里，不能独立使用。
5. 行内公式识别要严格（只匹配希腊字母/上下标/数学符号/显式 `$...$`），否则会把 `1992`、`Agent` 误判成公式。
6. 公式单元格加 `verticalAlign: CENTER`，公式与编号才会同基线对齐。
7. 修改已有文档时保留 `<w:rPr>` 原格式、不要重建 `styles.xml`。
8. Word 的目录域需在 Word 中按 F9 更新；脚本插入的是域，不是静态文本。
9. 公式里的裸 `*` 会被当作「上标星号」提升为 `^*`；但它**已经**在 `^{ }` 里时
   （`{Q}^{ * }`）不能再提升，否则产生非法嵌套 → 公式整条丢失。
10. 强调标记（`**` / `_`）与数学必须**先切分再解析**：`$...$` 内部的 `_{n}`
    不能被当作斜体。
11. 「写了生成脚本」≠「所有文本都会被解析」：历史上 `makeBodyParagraph` / `bullet` /
    表格单元格各自有「无数学就直出」的短路，导致强调漏解析。统一走 `buildInlineRuns`
    才能杜绝这类漏洞。

> 上述问题的完整成因与代码示例见 `REFERENCE.md`（Issues 1–11）；
> 本仓库新增的修复见上文「上游来源与许可」第 3–9 条。

## 深入参考

- `REFERENCE.md`：上游完整 SKILL.md 正文 —— 创建/编辑文档的完整流程、
  XML 参考（修订留痕、批注、图片）、Markdown 转换的 11 个问题与解法。
- `README.md`：上游说明与格式模板示例。
- `example/markdown论文/测试论文.md`：可直接运行的示例论文
  （含块公式、三线表、图片、参考文献），用于验证链路是否可用。

## 注意

- 涉及学术诚信：本技能只负责**排版与格式转换**，不得替用户编造数据、结论或参考文献。
- 生成后请提醒用户在 Word/WPS 中按 F9 更新目录，并通读核对公式与表格渲染。
- 各高校格式细则不一（字号、行距、页边距），如用户给出具体《格式要求》，以其为准。
- **交付前务必跑 `scripts/check_formulas.py`**：它检查「内容」，与 `validate.py`
  的「结构」互补 —— 公式整条丢失时 schema 校验照样会通过。
- **上游已知限制（非本仓库引入）**：`new_doc.js` 产物未声明默认段落样式
  （`styles.xml` 缺 `w:default="1"`），且 `Heading1/2/3` 与 `docx` 库内置样式重名
  导致 `styleId` 重复。Word/WPS 可正常打开，但第三方库（如 python-docx）读取时
  `paragraph.style` 可能为 `None`，代码需自行兜底（不要直接 `p.style.name`）。
  已在本仓库的 office-plugin `docx_read` 中做了兜底处理。
