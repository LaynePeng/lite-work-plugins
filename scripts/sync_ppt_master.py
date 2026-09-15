#!/usr/bin/env python3
"""同步上游 ppt-master 到本仓库（精简收录 + overlay 增量）。

上游事实源：https://github.com/hugohe3/ppt-master 的 skills/ppt-master/。
本仓库收录时做了两类受控修改，同步脚本负责在上游新版本落地后原样重放：

1. 精简收录（控整仓克隆体积，社区安装是整仓 git clone）：
   - references/ai-image-comparison/   整目录（AI 生图风格参考图，约 43MB）
   - templates/sounds/{bigsoundbank,kenney-interface,kenney-ui}/
                                        音效 wav 库（约 12MB）
   - templates/sounds/sounds_index.json
   - scripts/tests/                     pytest 专用
   被剔资源由技能内 scripts/fetch_optional_assets.py 从上游 Releases 按需补回。

2. lite-work 适配增量（overlay，权威副本在 scripts/ppt_master_assets/）：
   - SKILL.md frontmatter 追加顶层 version + triggers（社区更新检查读顶层
     version；上游嵌套 metadata 块受 attribution_guard 防篡改门保护，不能动）
   - requirements.txt 换精简核心版（核心链路依赖才随导入自动安装）
   - scripts/fetch_optional_assets.py（上游不存在，防 rsync --delete 误删）

用法：
    python scripts/sync_ppt_master.py --tag v6.4.1
退出码：0 成功 / 1 失败（任何一步校验不过即中止，仓库保持原状由 git 兜底）
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
SKILL = ROOT / "skills" / "ppt-master"
ASSETS = ROOT / "scripts" / "ppt_master_assets"
MANIFEST = ROOT / "manifest.json"
UPSTREAM_URL = "https://github.com/hugohe3/ppt-master"

TRIGGERS = "ppt-master,PPT,ppt,pptx,幻灯片,演示文稿,presentation,slide,PPT大师"

RSYNC_EXCLUDES = [
    "--exclude=references/ai-image-comparison/",
    "--exclude=templates/sounds/bigsoundbank/",
    "--exclude=templates/sounds/kenney-interface/",
    "--exclude=templates/sounds/kenney-ui/",
    "--exclude=templates/sounds/sounds_index.json",
    "--exclude=scripts/tests/",
    "--exclude=scripts/fetch_optional_assets.py",
    "--exclude=__pycache__",
    "--exclude=.DS_Store",
]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"命令失败: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        raise SystemExit(1)
    return result


def _skill_version(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s+version:\s*"([^"]+)"', text)
    if not match:
        print("无法从上游 SKILL.md 解析 metadata.version", file=sys.stderr)
        raise SystemExit(1)
    return match.group(1)


def _patch_frontmatter(skill_md: Path, version: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    if not text.startswith("---\n") or end < 0:
        print("SKILL.md frontmatter 结构异常", file=sys.stderr)
        raise SystemExit(1)
    frontmatter = re.sub(r"(?m)^(version|triggers):.*\n", "", text[4:end])
    addition = f'\nversion: "{version}"\ntriggers: {TRIGGERS}'
    skill_md.write_text(
        f"---{frontmatter.rstrip()}{addition}{text[end:]}", encoding="utf-8")


def _update_manifest(version: str) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for skill in data.get("skills", []):
        if skill.get("name") == "ppt-master":
            skill["version"] = version
            MANIFEST.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            return
    print("manifest.json 中未找到 ppt-master 条目，请手动登记", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步上游 ppt-master（精简收录 + overlay）")
    parser.add_argument("--tag", required=True, help="上游版本 tag，如 v6.4.1")
    args = parser.parse_args(argv)

    if not SKILL.is_dir():
        print(f"目标目录不存在: {SKILL}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="ppt-master-sync-"))
    try:
        print(f"克隆上游 {args.tag} …")
        _run(["git", "clone", "--depth", "1", "--branch", args.tag,
              UPSTREAM_URL, str(tmp / "repo")])
        src = tmp / "repo" / "skills" / "ppt-master"
        if not (src / "SKILL.md").is_file():
            print("上游仓库中未找到 skills/ppt-master/SKILL.md", file=sys.stderr)
            return 1

        version = _skill_version(src / "SKILL.md")
        print(f"上游版本: {version}")

        _run(["rsync", "-a", "--delete", *RSYNC_EXCLUDES,
              f"{src}/", f"{SKILL}/"])

        _patch_frontmatter(SKILL / "SKILL.md", version)
        shutil.copy2(ASSETS / "requirements.txt", SKILL / "requirements.txt")
        shutil.copy2(ASSETS / "fetch_optional_assets.py",
                     SKILL / "scripts" / "fetch_optional_assets.py")

        _run([sys.executable, str(SKILL / "scripts" / "attribution_guard.py")])
        print("完整性校验通过（attribution_guard exit 0）")

        _update_manifest(version)
        print(f"manifest.json 版本已更新为 {version}")
        print("后续：核对 README.md 条目 → python scripts/check_plugin_deps.py → 提交")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
