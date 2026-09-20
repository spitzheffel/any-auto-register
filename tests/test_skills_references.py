"""领域 skill 的防过期守卫。

skill 里引用的仓库路径必须存在、``path.py::Symbol`` 里的符号必须真的定义过，
`.cursor/skills` 镜像必须与 `.agents/skills` 一致。改名、删文件、忘了同步时这里会红，
比靠人记住"顺手改 skill"可靠。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import sync_skills  # noqa: E402

# 反引号里形如 core/xxx.py、tests/test_x.py 的仓库路径；带占位符 <x> / {} / * 的模板不算
_REPO_PATH = re.compile(
    r"`((?:core|application|api|platforms|providers|tests|infrastructure|services|scripts|docs|frontend/src|data)"
    r"/[\w./-]+?)(?:::[\w.]+)?`"
)
# 反引号里形如 `core/xxx.py::Symbol` 的符号引用
_REPO_SYMBOL = re.compile(
    r"`((?:core|application|api|platforms|providers|tests|infrastructure|services|scripts)"
    r"/[\w./-]+\.py)::([\w.]+)`"
)
_PLACEHOLDER = re.compile(r"[<>{}*]")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _skill_files() -> list[Path]:
    return [sync_skills.SOURCE / name / "SKILL.md" for name in sync_skills.DOMAIN_SKILLS]


def _defined_names(path: Path) -> set[str]:
    """文件里定义过的名字：类、函数、赋值目标、参数。

    走 ``ast.walk`` 而不是只看顶层——skill 引用的常常是类里的方法
    （``core/base_platform.py::register``）或测试里的嵌套辅助
    （``tests/test_step_flow_wiring.py::lock_is_free_from_another_thread``）。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda p: p.parent.name)
def test_frontmatter_has_name_matching_directory_and_a_description(skill_file: Path):
    text = skill_file.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    assert match, f"{skill_file} 缺少 frontmatter"
    fields = dict(
        line.split(":", 1) for line in match.group(1).splitlines() if ":" in line
    )
    assert fields.get("name", "").strip() == skill_file.parent.name
    assert len(fields.get("description", "").strip().strip('"')) > 40


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda p: p.parent.name)
def test_referenced_repo_paths_exist(skill_file: Path):
    text = skill_file.read_text(encoding="utf-8")
    missing: list[str] = []
    for raw in _REPO_PATH.findall(text):
        candidate = raw.rstrip(".,;:")
        if _PLACEHOLDER.search(candidate):
            continue
        # data/ 下是运行期产物，只要求目录层级合理，不要求存在
        if candidate.startswith("data/"):
            continue
        if not (ROOT / candidate).exists():
            missing.append(candidate)
    assert not missing, f"{skill_file.parent.name} 引用了不存在的路径: {missing}"


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda p: p.parent.name)
def test_referenced_symbols_exist(skill_file: Path):
    """``path.py::Symbol`` 里的符号必须真的在那个文件里定义过。

    只验路径是不够的：把 CAPABILITY_CHANNEL 改个名、把 ScriptedPage 删掉，
    文件照样在，skill 却已经指不到东西了。
    """
    text = skill_file.read_text(encoding="utf-8")
    missing: list[str] = []
    for raw_path, symbol in _REPO_SYMBOL.findall(text):
        if _PLACEHOLDER.search(raw_path) or _PLACEHOLDER.search(symbol):
            continue
        target = ROOT / raw_path
        if not target.exists():
            continue  # 路径不存在由 test_referenced_repo_paths_exist 负责报
        # A.b 这种写成"类.方法"的引用只校验第一段，够抓住改名与删除
        if symbol.split(".")[0] not in _defined_names(target):
            missing.append(f"{raw_path}::{symbol}")
    assert not missing, f"{skill_file.parent.name} 引用了不存在的符号: {missing}"


def test_domain_skills_reference_the_conventions_base():
    for name in sync_skills.DOMAIN_SKILLS:
        if name == "step-engine-conventions":
            continue
        text = (sync_skills.SOURCE / name / "SKILL.md").read_text(encoding="utf-8")
        assert "step-engine-conventions" in text, f"{name} 应当先指向约定底座，而不是自己复述约定"


def test_cursor_mirror_is_in_sync():
    problems = sync_skills.sync(check_only=True)
    assert not problems, "运行 python scripts/sync_skills.py 同步镜像：\n" + "\n".join(problems)
