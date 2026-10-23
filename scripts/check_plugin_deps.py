#!/usr/bin/env python3
"""插件依赖审计：检查每个插件的第三方 import 是否被满足。

规则（与 AGENTS.md 固化的约定一致）：
- 主程序已捆绑的包：直接 import，禁止打进 wheels（体积浪费 + 版本冲突）
- 未捆绑的第三方包：必须在插件 wheels/ 目录取 wheel 分发（打包版
  frozen 进程无法 pip install，wheels 是唯一离线分发方式）
- requirements.txt 仅开发态，本审计不认（社区发布以 wheels 为准）

用法：python scripts/check_plugin_deps.py
退出码：0 全部通过 / 1 存在未满足依赖（CI 拦截）
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 主程序（lite-work）已捆绑的包——插件可直接 import，禁止打进 wheels。
# 与主仓库 pyproject.toml dependencies + PyInstaller collect 参数保持同步
# （详见 AGENTS.md「主程序已捆绑的包」）。
BUNDLED = {
    # Web / 服务框架
    "fastapi", "uvicorn", "starlette", "pydantic", "anyio", "httpx", "httpcore",
    # 解析器
    "tree_sitter", "tree_sitter_typescript", "tree_sitter_java",
    "tree_sitter_go", "pathspec",
    # 办公 / 文档
    "docx", "openpyxl", "pptx", "reportlab", "pypdf",
    "lxml", "xlsxwriter", "dateutil",
    # 数据 / 图表
    "pandas", "matplotlib", "numpy", "PIL",
    # OCR / PDF / 反爬
    "rapidocr_onnxruntime", "onnxruntime", "cv2", "pymupdf", "fitz", "curl_cffi",
    # 传递依赖（稳定可用）
    "yaml", "shapely", "pyclipper", "tqdm", "click", "certifi", "idna",
}

# 宿主应用命名空间
HOST = {"litework"}

# import 名 → 发行包名（wheel 文件名的 dist 段），仅列两者不一致的常见项
IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "PIL": "pillow",
    "cv2": "opencv_python",
    "docx": "python_docx",
    "pptx": "python_pptx",
    "dateutil": "python_dateutil",
}


def iter_import_roots(path: Path, local_modules: frozenset = frozenset()):
    """ast 遍历全部 import（含函数体内的延迟导入），产出顶层模块名。

    `local_modules`：同一插件目录内的兄弟模块名（如 jev 的 jev_config）。
    这些是**插件自带的本地模块**（多文件插件），不算第三方依赖，需排除。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in local_modules:
                    continue
                yield root
        elif isinstance(node, ast.ImportFrom):
            # 排除相对导入（level > 0）
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in local_modules:
                    continue
                yield root


def wheel_satisfies(whl_name: str, dist: str) -> bool:
    """wheel 文件名是否覆盖 dist（{dist}-{version}-{python}-{abi}-{platform}.whl）。"""
    norm = dist.lower().replace("-", "_").replace(".", "_")
    return whl_name.lower().startswith(norm + "-")


def local_module_names(plugin_dir: Path, py_files: list) -> frozenset:
    """同目录内的兄弟 `.py` 模块名（多文件插件的本地模块）。

    宿主加载插件时会**把插件目录插入 sys.path**（litework/tools/plugin_loader.py
    的 _load_module），因此插件里的裸 `import jev_config` 能解析到同目录文件。
    这类名不是第三方依赖，审计必须排除，否则多文件插件会被误判为缺 wheels。

    安全护栏：本地模块名**不得**与已捆绑包 / import→dist 映射 / 标准库重名。
    否则插件可以用 `helpers.py` 之类的名字遮蔽真实依赖，让审计放行一个
    实际缺失的包（审计被绕过的风险）。
    """
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    names = {p.stem for p in py_files if p.name != "__init__.py"}
    shadow = sorted(n for n in names
                    if n in BUNDLED or n in IMPORT_TO_DIST or n in stdlib or n in HOST)
    if shadow:
        print(f"WARN {plugin_dir.name}: 本地模块名与现有依赖/标准库重名，已按依赖处理: {shadow}")
        names -= set(shadow)
    return frozenset(names)


def main() -> int:
    plugins_dir = ROOT / "plugins"
    if not plugins_dir.is_dir():
        print("未找到 plugins/ 目录")
        return 1

    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    failures = 0
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue
        py_files = sorted(plugin_dir.glob("*.py"))
        if not py_files:
            continue

        # 插件目录内的本地模块（多文件插件）：不算第三方依赖
        local_modules = local_module_names(plugin_dir, py_files)

        # 收集未捆绑的第三方 import
        unresolved: set[str] = set()
        for py in py_files:
            try:
                roots = list(iter_import_roots(py, local_modules))
            except SyntaxError as exc:
                print(f"FAIL {plugin_dir.name}: {py.name} 语法错误 {exc}")
                failures += 1
                continue
            for root in roots:
                if root in HOST or root in BUNDLED or root in stdlib:
                    continue
                unresolved.add(root)

        if not unresolved:
            print(f"OK   {plugin_dir.name}: 无第三方依赖（或全部已捆绑）")
            continue

        wheels_dir = plugin_dir / "wheels"
        wheels = sorted(wheels_dir.glob("*.whl")) if wheels_dir.is_dir() else []
        if not wheels:
            print(f"FAIL {plugin_dir.name}: 引用了未捆绑的包 {sorted(unresolved)}，但 wheels/ 为空")
            print(f"     修复: pip download <pkg> -d {plugin_dir.relative_to(ROOT) / 'wheels'}")
            failures += 1
            continue

        missing = []
        for imp in sorted(unresolved):
            dist = IMPORT_TO_DIST.get(imp, imp)
            if not any(wheel_satisfies(w.name, dist) for w in wheels):
                missing.append(f"{imp}(需 wheel: {dist})")
        if missing:
            print(f"FAIL {plugin_dir.name}: wheels 未覆盖 {missing}")
            print(f"     现有 wheels: {[w.name for w in wheels]}")
            failures += 1
        else:
            print(f"OK   {plugin_dir.name}: {sorted(unresolved)} 已由 wheels 覆盖")

    if failures:
        print(f"\n{failures} 个插件依赖未满足——打包版将无法 import，禁止合入")
        return 1
    print("\n全部插件依赖检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
