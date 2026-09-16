#!/usr/bin/env python3
"""同步上游 OpenTikZ 到本仓库（自包含收录 + SKILL.md overlay）。

上游事实源：https://github.com/opentikz/opentikz （Code MIT / 内容 CC0）。
上游的技能位于仓库 skills/using-opentikz/SKILL.md，库资源（icons/templates/
examples/reference/catalog.json）在仓库根——lite-work 技能必须自包含单目录，
因此本仓库把两者合并为 skills/academic-diagram/ 单目录收录
（技能对外名 academic-diagram，定位论文配图；上游库品牌仍叫 OpenTikZ）。

受控差异（overlay 权威副本在 scripts/academic_diagram_assets/）：
- SKILL.md 基于 skills/using-opentikz/SKILL.md 打补丁：
  ① frontmatter：name 改 academic-diagram + 追加顶层 version/triggers
     + 论文定位与负向路由（Office → diagram-to-office；专利 → 黑白引擎）
  ② §0 OTROOT 定位：新增「SKILL.md 同级目录含 catalog.json → 该目录即库根」
  ③ §2 第 5 步：无本地 LaTeX 工具链时的降级交付
- 排除上游的 tools/ .github/ assets/ skills-demos/ .claude-plugin/ 等
  仓库基建（Mode B 贡献者工作流请去上游真仓库）
- 不带 requirements.txt（Mode A 出图零 Python 依赖；上游的 jsonschema 仅为
  仓库校验工具所需）

用法：
    python scripts/sync_academic_diagram.py [--ref main] [--version 0.1.0]
版本取上游最新 tag（去 v 前缀）；--ref 可钉 tag 或分支。上游 SKILL.md 与
scripts/academic_diagram_assets/upstream-SKILL.md 不一致时会提示人工核对 overlay。
退出码：0 成功 / 1 失败
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "academic-diagram"
ASSETS = ROOT / "scripts" / "academic_diagram_assets"
MANIFEST = ROOT / "manifest.json"
UPSTREAM_URL = "https://github.com/opentikz/opentikz"

INCLUDE_DIRS = ("icons", "templates", "examples", "reference")
INCLUDE_FILES = ("catalog.json", "LICENSE-CODE", "LICENSE-CONTENT",
                 "CITATION.cff", "CHANGELOG.md", "README.md")
INCLUDE_NESTED = (("docs/DESIGN_GUIDE.md", "docs/DESIGN_GUIDE.md"),)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"命令失败: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        raise SystemExit(1)
    return result


def _latest_tag() -> str:
    result = _run(["git", "ls-remote", "--tags", "--sort=-v:refname", UPSTREAM_URL])
    for line in result.stdout.splitlines():
        ref = line.split("\t")[-1].strip()
        if ref.startswith("refs/tags/v") and not ref.endswith("^{}"):
            return ref.removeprefix("refs/tags/v")
    raise SystemExit("未找到上游 tag（vX.Y.Z）")


def _update_manifest(version: str) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for skill in data.get("skills", []):
        if skill.get("name") == "academic-diagram":
            skill["version"] = version
            MANIFEST.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            return
    print("manifest.json 中未找到 academic-diagram 条目，请手动登记", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步上游 OpenTikZ（自包含收录 + overlay）")
    parser.add_argument("--ref", default="main", help="上游分支或 tag（默认 main）")
    parser.add_argument("--version", help="技能版本（默认取上游最新 tag）")
    args = parser.parse_args(argv)

    if not SKILL.is_dir():
        print(f"目标目录不存在: {SKILL}", file=sys.stderr)
        return 1
    version = args.version or _latest_tag()
    print(f"同步 ref={args.ref} version={version}")

    tmp = Path(tempfile.mkdtemp(prefix="academic-diagram-sync-"))
    try:
        _run(["git", "clone", "--depth", "1", "--branch", args.ref,
              UPSTREAM_URL, str(tmp / "repo")])
        src = tmp / "repo"

        upstream_skill = (src / "skills" / "using-opentikz" / "SKILL.md").read_text(
            encoding="utf-8")
        recorded = (ASSETS / "upstream-SKILL.md").read_text(encoding="utf-8")
        if upstream_skill != recorded:
            print("警告：上游 SKILL.md 与已记录版本不一致，overlay 补丁"
                  "（frontmatter / §0 OTROOT / §2 编译降级）需人工核对后更新"
                  " scripts/academic_diagram_assets/ 下的两个副本", file=sys.stderr)

        for name in INCLUDE_DIRS:
            if not (src / name).is_dir():
                print(f"上游缺少目录: {name}（结构可能已变化）", file=sys.stderr)
                return 1
            shutil.rmtree(SKILL / name, ignore_errors=True)
            shutil.copytree(src / name, SKILL / name)
        for name in INCLUDE_FILES:
            shutil.copy2(src / name, SKILL / name)
        for src_rel, dst_rel in INCLUDE_NESTED:
            (SKILL / dst_rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / src_rel, SKILL / dst_rel)

        # 清理上游可能克隆下来的 0 字节隐藏文件（.gitkeep 等）
        for p in SKILL.rglob(".gitkeep"):
            p.unlink(missing_ok=True)

        (ASSETS / "upstream-SKILL.md").write_text(upstream_skill, encoding="utf-8")
        overlay = (ASSETS / "SKILL.md").read_text(encoding="utf-8")
        overlay = re.sub(r'(?m)^version: ".*"$', f'version: "{version}"', overlay)
        (ASSETS / "SKILL.md").write_text(overlay, encoding="utf-8")
        shutil.copy2(ASSETS / "SKILL.md", SKILL / "SKILL.md")

        _update_manifest(version)
        print(f"manifest.json 版本已更新为 {version}")
        print("后续：核对 README.md 条目 → python scripts/check_plugin_deps.py → 提交")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
