#!/usr/bin/env python3
"""回归套件：覆盖本技能历史上真实出过问题的场景。

断言：
  A0 好用例生成成功（退出码 0）
  A1 单行块公式（整行 / 文字之后）→ 渲染为带编号的块公式（3 个，编号 1/2/3）
  A2 正文不残留字面 `$`
  A3 行内公式转成 Word 原生公式（存在 oMath）
  A4 Markdown 强调正确渲染：**粗体** / *斜体* / **_粗斜体_**；词内下划线不误伤
  A5 非法 LaTeX → 非零退出 + 明确报错（响亮失败，不静默降级）
  A6 一行两个块公式 → 非零退出 + 明确报错（不静默丢内容）
  A7 check_formulas.py 对好文档不误报（退出码 0）
  A8 check_formulas.py 能抓出坏文档（退出码 1）
  A9 示例论文（80+ 公式 + 强调）整体通过（防误报 / 防回归）

用法：
    python3 tests/run_regression.py     # 需先在技能目录执行 npm install
退出码：0 全部通过 / 1 有失败 / 2 环境未就绪
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SKILL = TESTS.parent
NEW_DOC = SKILL / "scripts" / "new_doc.js"
CHECKER = SKILL / "scripts" / "check_formulas.py"
SAMPLE = SKILL / "example" / "markdown论文" / "测试论文.md"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    line = f"  {'PASS' if ok else 'FAIL'}  {name}"
    if detail and not ok:
        line += f"\n        细节：{detail}"
    print(line)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def build(md, out):
    return run(["node", str(NEW_DOC), str(md), str(out)])


def doc_xml(path):
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def texts(xml):
    return re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)


def math_texts(xml):
    return re.findall(r"<m:t[^>]*>(.*?)</m:t>", xml, re.S)


def runs_with(xml, needle):
    """包含 needle 的 run：返回 [(text, bold, italic)]"""
    found = []
    for r in re.findall(r"<w:r>(?:(?!</w:r>).)*</w:r>", xml, re.S):
        t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", r))
        if needle in t:
            found.append((t, "<w:b/>" in r, "<w:i/>" in r))
    return found


def block_formula_tables(xml):
    tables = re.findall(r"<w:tbl\b.*?</w:tbl>", xml, re.S)
    return [t for t in tables if 'w:w="7936"' in t]


def finish():
    failed = [n for n, ok in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(failed)}/{len(RESULTS)} 项失败：" + ", ".join(failed))
        return 1
    print(f"全部 {len(RESULTS)} 项断言通过")
    return 0


def main():
    if not NEW_DOC.is_file():
        print(f"未找到 {NEW_DOC}")
        return 2
    if not (SKILL / "node_modules").is_dir():
        print("请先在技能目录执行：npm install")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="docxskill-regression-"))
    try:
        # ------------------------------------------------------------ 好用例
        good_out = tmp / "good.docx"
        rc, out, err = build(TESTS / "fixture-formulas.md", good_out)
        record("A0 好用例生成成功（退出码 0）", rc == 0, (err or out).strip()[:200])
        if rc != 0 or not good_out.is_file():
            return finish()

        xml = doc_xml(good_out)

        tables = block_formula_tables(xml)
        nums = []
        for t in tables:
            m = re.findall(r"<w:t[^>]*>\((\d+)\)</w:t>", t)
            if m:
                nums.append(m[0])
        record("A1 单行块公式渲染为带编号块公式（3 个，编号 1/2/3）",
               len(tables) == 3 and nums == ["1", "2", "3"],
               f"块公式表格={len(tables)}, 编号={nums}")

        plain = texts(xml)
        bad_dollar = [t for t in plain if "$" in t]
        record("A2 正文不残留字面 `$`", not bad_dollar, f"命中 {len(bad_dollar)}: {bad_dollar[:3]}")

        inline_par = [p for p in re.findall(r"<w:p\b.*?</w:p>", xml, re.S) if "时成立" in p]
        ok_a3 = (
            bool(inline_par)
            and "<m:oMath" in inline_par[0]
            and "$" not in "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", inline_par[0], re.S))
        )
        record("A3 行内公式转为 Word 原生公式（段落含 oMath 且无字面 `$`）",
               ok_a3, f"oMath 总数={xml.count('<m:oMath')}")

        e_bold = runs_with(xml, "程序级")
        e_ital = runs_with(xml, "斜体")
        e_bi = runs_with(xml, "粗斜体")
        e_word = runs_with(xml, "foo_bar_baz")
        ok_bold = any(b and not i for _t, b, i in e_bold)
        ok_ital = any(i and not b for _t, b, i in e_ital)
        ok_bi = any(b and i for _t, b, i in e_bi)
        ok_no_star = not [t for t in plain if "**" in t]
        ok_word = bool(e_word) and not any(i for _t, _b, i in e_word)
        record("A4 Markdown 强调正确渲染（粗体/斜体/粗斜体 + 词内下划线不误伤）",
               ok_bold and ok_ital and ok_bi and ok_no_star and ok_word,
               f"程序级={e_bold[:1]} 斜体={e_ital[:1]} 粗斜体={e_bi[:1]} "
               f"foo_bar_baz={e_word[:1]} 残留**={not ok_no_star}")

        # ------------------------------------------------------ check_formulas.py
        rc_ok, out_ok, _ = run([sys.executable, str(CHECKER), str(good_out)])
        record("A7 check_formulas.py 对好文档不误报（退出码 0）", rc_ok == 0, out_ok.strip()[:160])

        broken = tmp / "broken.docx"
        with zipfile.ZipFile(good_out) as zin, zipfile.ZipFile(broken, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = data.replace(
                        b"</w:body>",
                        "<w:p><w:r><w:t>残留 $ 与 **标记**</w:t></w:r></w:p></w:body>".encode("utf-8"),
                    )
                zout.writestr(item, data)
        rc_bad, out_bad, _ = run([sys.executable, str(CHECKER), str(broken)])
        record("A8 check_formulas.py 能抓出坏文档（退出码 1）", rc_bad == 1, out_bad.strip()[:160])

        # ------------------------------------------------------------ 坏用例
        rc, _out, err = build(TESTS / "fixture-bad-latex.md", tmp / "bad_latex.docx")
        record("A5 非法 LaTeX 响亮失败（非零退出 + 明确报错）",
               rc != 0 and "LaTeX 解析失败" in err,
               f"rc={rc}, err={(err or '').strip()[:160]}")

        rc, _out, err = build(TESTS / "fixture-two-blocks-per-line.md", tmp / "two.docx")
        record("A6 一行两个块公式报错（不静默丢内容）",
               rc != 0 and "无法解析的块公式写法" in err,
               f"rc={rc}, err={(err or '').strip()[:160]}")

        # ------------------------------------------------------ 示例论文（防误报）
        sample_out = tmp / "sample.docx"
        rc, _out, err = build(SAMPLE, sample_out)
        detail = ""
        ok = rc == 0 and sample_out.is_file()
        if ok:
            sxml = doc_xml(sample_out)
            leftover = [t for t in texts(sxml) if "$" in t or "**" in t]
            rawtex = [t for t in math_texts(sxml) if "\\" in t]
            rc2, out2, _ = run([sys.executable, str(CHECKER), str(sample_out)])
            ok = not leftover and not rawtex and rc2 == 0
            detail = (f"残留={len(leftover)}, 原始LaTeX={len(rawtex)}, checker: "
                      + out2.strip()[:80])
        else:
            detail = (err or "").strip()[:200]
        record("A9 示例论文（80+ 公式 + 强调）整体通过，无残留 / 无误报", ok, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return finish()


if __name__ == "__main__":
    sys.exit(main())
