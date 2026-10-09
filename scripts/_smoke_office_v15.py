# -*- coding: utf-8 -*-
"""office-plugin v1.5.0 冒烟测试。

- 无 LibreOffice 环境：验证降级提示路径 + pdf→png 渲染（零外部依赖）
- 有 LibreOffice 环境（如 CI 装 libreoffice）：完整验证转换/渲染/重算

用法: python3 scripts/_smoke_office_v15.py   （跑完自动清理临时工作区）
依赖: reportlab / openpyxl / pymupdf / matplotlib（有则用，全缺时仅跑参数校验子集）
     litework 可导入则用真包；否则注入最小 ToolDefinition stub（CI 用）
"""
import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "office-plugin"))

try:
    import litework.core.types  # noqa: F401
    from litework.tools.plugin import ToolPlugin as _RealToolPlugin  # noqa: F401
except ImportError:
    # CI / 无主程序环境：注入最小 stub（插件只用到 name/description/parameters
    # 与 ToolPlugin 基类；_gcd_import 对已注册的全限定名直接命中 sys.modules，
    # 因此无需构造真实包结构）
    litework_pkg = type(sys)("litework")
    core_pkg = type(sys)("litework.core")
    types_pkg = type(sys)("litework.core.types")
    tools_pkg = type(sys)("litework.tools")
    plugin_pkg = type(sys)("litework.tools.plugin")

    @dataclass
    class ToolDefinition:  # type: ignore[no-redef]
        name: str
        description: str = ""
        parameters: Dict[str, Any] = None  # type: ignore[assignment]

    class ToolPlugin:  # type: ignore[no-redef]
        """最小基类 stub：plugin.py 末尾的 OfficePlugin 只继承、不调用基类逻辑。"""

    types_pkg.ToolDefinition = ToolDefinition
    plugin_pkg.ToolPlugin = ToolPlugin
    litework_pkg.core = core_pkg
    litework_pkg.tools = tools_pkg
    tools_pkg.plugin = plugin_pkg
    core_pkg.types = types_pkg
    sys.modules["litework"] = litework_pkg
    sys.modules["litework.core"] = core_pkg
    sys.modules["litework.core.types"] = types_pkg
    sys.modules["litework.tools"] = tools_pkg
    sys.modules["litework.tools.plugin"] = plugin_pkg

import plugin as office_plugin  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), name, ("- " + detail if detail and not cond else ""))


async def main():
    ws = tempfile.mkdtemp(prefix="office-smoke-")
    try:
        tools = office_plugin.OfficeTools(ws)

        # ---- 0. 工具注册面
        names = [t.name for t in tools.get_tools()]
        for expect in ("office_convert", "office_render", "xlsx_recalculate"):
            check(f"注册工具 {expect}", expect in names)
        check("工具总数 19（原 16 + 新增 3）", len(names) == 19, f"实际 {len(names)}")

        # ---- 1. 本机 soffice 探测结果
        print("INFO 本机 soffice 探测:", office_plugin._find_soffice())

        # ---- 2. 生成一个 PDF（用 pdf_create，顺便回归老功能）
        r = await tools.execute("pdf_create", {
            "content": "# 冒烟测试\n\n**v1.5.0** 渲染路径验证。", "filename": "smoke.pdf"})
        check("pdf_create 回归", r.startswith("[Office OK]"), r[:120])
        pdf_path = r.split("→")[-1].strip()

        # ---- 3. office_render：pdf 直连路径（零外部依赖，必须成功）
        r = await tools.execute("office_render", {"input_path": pdf_path, "pages": "1", "dpi": 96})
        check("office_render pdf→png", r.startswith("[Office OK]") and os.path.isfile(r.split("→")[-1].strip().split(",")[0]), r[:200])

        # ---- 3b. pages 表达式 + 越界错误
        r = await tools.execute("office_render", {"input_path": pdf_path, "pages": "9"})
        check("office_render 页码越界报错", r.startswith("[Office Error]"), r[:120])

        # ---- 3c. 不支持的输入格式
        fake = os.path.join(ws, "fake.zip"); open(fake, "w").close()
        r = await tools.execute("office_render", {"input_path": fake})
        check("office_render 拒绝 .zip", r.startswith("[Office Error]"), r[:120])

        # ---- 4. office_convert：soffice 依赖路径（本机无 soffice → 降级提示）
        # 先生成一个 docx（Office 格式才是合法输入；.pdf 不是转换的输入格式）
        r = await tools.execute("docx_create", {"content": "# 转换源\n\n测试段落。", "filename": "src.docx"})
        check("docx_create 回归", r.startswith("[Office OK]"), r[:120])
        docx_path = r.split("→")[-1].strip()
        r = await tools.execute("office_convert", {"input_path": docx_path, "output_format": "pdf"})
        if office_plugin._find_soffice() is None:
            check("office_convert 无 soffice 降级提示", "LibreOffice" in r and "安装" in r, r[:200])
        else:
            check("office_convert 有 soffice 实测", r.startswith("[Office OK]"), r[:200])
        # .pdf 输入应被明确拒绝（PDF 不是 Office 格式，转换请走 pdf_create/office_render）
        r = await tools.execute("office_convert", {"input_path": pdf_path, "output_format": "docx"})
        check("office_convert 拒绝 .pdf 输入", r.startswith("[Office Error]"), r[:120])

        # ---- 4b. office_convert 参数校验
        r = await tools.execute("office_convert", {"input_path": pdf_path, "output_format": "exe"})
        check("office_convert 拒绝非法格式", r.startswith("[Office Error]"), r[:120])
        r = await tools.execute("office_convert", {"input_path": "不存在.docx"})
        check("office_convert 文件不存在报错", r.startswith("[Office Error]"), r[:120])

        # ---- 5. xlsx_recalculate：构造公式工作簿 + 降级/实测
        import openpyxl
        xlsx_path = os.path.join(ws, "calc.xlsx")
        wb = openpyxl.Workbook(); wb.active["A1"] = 1; wb.active["A2"] = 2; wb.active["A3"] = "=SUM(A1:A2)"
        wb.save(xlsx_path); wb.close()
        r = await tools.execute("xlsx_recalculate", {"input_path": xlsx_path})
        if office_plugin._find_soffice() is None:
            check("xlsx_recalculate 无 soffice 降级提示", "LibreOffice" in r and "安装" in r, r[:200])
        else:
            check("xlsx_recalculate 有 soffice 实测", r.startswith("[Office OK]") and "2 个公式" in r, r[:300])
            wb = openpyxl.load_workbook(xlsx_path, data_only=True)
            check("重算后读到计算值 3", wb.active["A3"].value == 3, f"A3={wb.active['A3'].value!r}")
            wb.close()

        # ---- 6. _clamp_int / _parse_pages 单元行为
        check("_clamp_int 钳制", office_plugin.OfficeTools._clamp_int(9999, 120, 10, 300, "t") == 300)
        check("_clamp_int 默认", office_plugin.OfficeTools._clamp_int(None, 120, 10, 300, "t") == 120)
        pp = office_plugin.OfficeTools._parse_pages("2,1-2, 5", 10)
        check("_parse_pages 去重排序", pp == [1, 2, 5], str(pp))
        check("_parse_pages 非法", isinstance(office_plugin.OfficeTools._parse_pages("0", 10), str))
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


asyncio.run(main())
