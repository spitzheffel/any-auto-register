"""把领域 skill 从唯一来源 ``.agents/skills/`` 镜像到 ``.cursor/skills/``。

Cursor 只读 ``.cursor/skills/``，Codex 等工具读 ``.agents/skills/``；Windows 上符号链接
不可靠，所以用复制 + 校验。只同步 ``DOMAIN_SKILLS`` 列出的领域 skill，不碰 trellis 等
通用研发流程 skill（它们的来源与生命周期由各自的工具链管）。

用法::

    python scripts/sync_skills.py          # 复制
    python scripts/sync_skills.py --check  # 只校验两边一致（CI / 测试用），不一致退出码 1
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / ".agents" / "skills"
MIRROR = ROOT / ".cursor" / "skills"

DOMAIN_SKILLS: tuple[str, ...] = (
    "step-engine-conventions",
    "platform-drift-check",
    "platform-from-capture",
    "failure-triage",
)


def _files(skill_dir: Path) -> list[Path]:
    return sorted(p for p in skill_dir.rglob("*") if p.is_file())


def sync(check_only: bool = False) -> list[str]:
    """返回不一致（或已修复）的相对路径列表。"""
    problems: list[str] = []
    for name in DOMAIN_SKILLS:
        src_dir = SOURCE / name
        dst_dir = MIRROR / name
        if not src_dir.is_dir():
            problems.append(f"missing source: {src_dir.relative_to(ROOT)}")
            continue
        for src in _files(src_dir):
            rel = src.relative_to(src_dir)
            dst = dst_dir / rel
            if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
                continue
            problems.append(f"{'out of sync' if dst.exists() else 'missing mirror'}: {dst.relative_to(ROOT)}")
            if not check_only:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        if dst_dir.is_dir():
            src_rel = {p.relative_to(src_dir) for p in _files(src_dir)}
            for extra in _files(dst_dir):
                if extra.relative_to(dst_dir) not in src_rel:
                    problems.append(f"stale mirror file: {extra.relative_to(ROOT)}")
                    if not check_only:
                        extra.unlink()
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="只校验不修改")
    args = parser.parse_args(argv)

    problems = sync(check_only=args.check)
    if not problems:
        print("skills in sync")
        return 0
    verb = "out of sync" if args.check else "synced"
    print(f"{verb}:")
    for item in problems:
        print(f"  {item}")
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
