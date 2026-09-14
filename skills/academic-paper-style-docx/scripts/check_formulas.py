#!/usr/bin/env python3
"""交付前公式/格式健康检查（独立兜底，不依赖 XSD schema 校验）。

背景：XSD schema 校验只能保证 XML 结构合法，**保证不了内容正确**——
公式整条丢失、原始 LaTeX 变成正文文字，schema 都是「通过」的。
下面每一项检查都来自真实故障案例：

  1. 正文残留 `$`
     单行块公式 `$$ ... $$` 若未被识别，会被当成行内公式切开，
     正文里留下孤立的 `$` / `$$`。
  2. 公式未转成 Word 原生公式
     `<m:t>` 里出现反斜杠命令名，说明该宏不被支持、被当成普通文本写进了公式
     （例如 `\\commandnotexist`、`\\tag`）。
  3. Markdown emphasis 未解析
     `<w:t>` 里出现字面 `**` / `~~`，说明该处文本没有走内联解析。
  4. 上标星号伪影
     `<m:t>` 里出现 `^*`，说明裸星号被错误地提升了一次上标。

用法：
    python3 check_formulas.py <file.docx | unpacked_dir>

退出码：0 = 通过；1 = 发现问题（并打印明细）
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

RUN_PATTERN = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
MATH_RUN_PATTERN = re.compile(r"<m:t[^>]*>(.*?)</m:t>", re.S)

# 每类问题：(标题, 命中正则, 命中文本说明)
TEXT_CHECKS = [
    ("正文残留字面 `$`（块公式未被识别）", re.compile(r"\$"), "w:t"),
    ("Markdown emphasis 未解析（字面 `**`）", re.compile(r"\*\*"), "w:t"),
    ("Markdown 删除线未解析（字面 `~~`）", re.compile(r"~~"), "w:t"),
]
MATH_CHECKS = [
    ("公式内含未转换的 LaTeX 命令（反斜杠）", re.compile(r"\\[a-zA-Z]+"), "m:t"),
    ("公式内含上标星号伪影 `^*`", re.compile(r"\^!?\*|\^\s*\*"), "m:t"),
]


def unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
    )


def load_document_xml(target: Path) -> str:
    """支持 .docx 文件或已解包的目录。"""
    if target.is_dir():
        doc = target / "word" / "document.xml"
        if not doc.is_file():
            raise SystemExit(f"Error: 未找到 {doc}")
        return doc.read_text(encoding="utf-8")
    if target.suffix.lower() == ".docx":
        with zipfile.ZipFile(target) as zf:
            return zf.read("word/document.xml").decode("utf-8")
    raise SystemExit(f"Error: 仅支持 .docx 文件或解包目录，收到 {target}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Error: {target} 不存在")
        return 2

    xml = load_document_xml(target)
    texts = [unescape(t) for t in RUN_PATTERN.findall(xml)]
    maths = [unescape(t) for t in MATH_RUN_PATTERN.findall(xml)]

    problems = []
    for title, pattern, _kind in TEXT_CHECKS:
        hits = [t for t in texts if pattern.search(t)]
        if hits:
            problems.append((title, hits))
    for title, pattern, _kind in MATH_CHECKS:
        hits = [t for t in maths if pattern.search(t)]
        if hits:
            problems.append((title, hits))

    if not problems:
        print(f"OK   {target.name}: 公式与格式检查通过"
              f"（文本片段 {len(texts)}，公式片段 {len(maths)}）")
        return 0

    print(f"FAIL {target.name}: 发现 {len(problems)} 类问题")
    for title, hits in problems:
        print(f"  - {title}：命中 {len(hits)} 处")
        for h in hits[:5]:
            snippet = h.strip().replace("\n", " ")
            print(f"      {snippet[:90]!r}")
        if len(hits) > 5:
            print(f"      ...（其余 {len(hits) - 5} 处略）")
    print("\n修复建议：\n"
          "  · 块公式一律用多行写法，`\\tag{n}` 单独占一行：\n"
          "      $$\n      E = mc^{2} \\tag{1}\n      $$\n"
          "  · 检查公式里是否用了 temml/KaTeX 不支持的宏；\n"
          "  · 粗体/斜体写成 `**粗体**` / `*斜体*`（会正常解析，不应残留符号）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
