# coding=utf-8
"""包内按名导入的缝检查（2026-09-14 正交门事故回归）。

事故:bridge.py `from .book_ranks import load_book_cache` 在 worktree 缺
定义 → ImportError 被 try 吞掉 → 门静默跳过(不拒绝不烧名),09-13 直驱
会话 11 笔提交全部未经正交检查。修复(load_book_cache 包装)只落在主树,
worktree 从未回流——两树分歧制造了单边修复。

本测试静态扫描包内全部相对导入(含函数体内的延迟导入),逐名验证符号
存在于目标模块。任何"导入名≠定义名"的缝在此处爆炸,而不是在生产
submit 的 try 块里静默。"""
import ast
import importlib
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "src" / "dsh_factor_mining"
PKG = "dsh_factor_mining"


def _relative_imports():
    """yield (file, lineno, module_str, level, [names])。"""
    for py in sorted(PKG_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level >= 1:
                yield (py, node.lineno, node.module or "", node.level,
                       [a.name for a in node.names])


def test_no_internal_import_seams():
    seams = []
    checked = 0
    for py, ln, mod, level, names in _relative_imports():
        # 相对导入解析:level=1 → 当前包;2 → 上一级…
        base_parts = ["src", PKG] if py.name != "__init__.py" else ["src"]
        # 用文件自身包路径推导更稳:
        rel = py.relative_to(PKG_DIR).parent  # 包内子路径
        anchor_parts = [PKG] + list(rel.parts) + ["__self__"]
        anchor_parts = anchor_parts[:len(anchor_parts) - level]
        target = ".".join(anchor_parts + ([mod] if mod else []))
        try:
            m = importlib.import_module(target)
        except Exception as e:
            seams.append(f"{py.name}:{ln} 模块不可导入 {target}: {e}")
            continue
        for n in names:
            checked += 1
            if hasattr(m, n):
                continue
            # `from .X import Y` 在 Y 是 X 的子模块文件时同样合法
            mod_file = getattr(m, "__file__", None)
            if mod_file and mod_file.endswith("__init__.py"):
                pkg_dir = Path(mod_file).parent
                if (pkg_dir / f"{n}.py").exists() or (pkg_dir / n).is_dir():
                    continue
            seams.append(f"{py.name}:{ln} 导入 {target} 的不存在的名字 {n!r}")
    assert not seams, (
        f"包内按名导入缝 {len(seams)} 处(事故形态:submit 期 ImportError "
        f"被 try 吞掉→门静默跳过):\n  " + "\n  ".join(seams))
    # 完整性自检:扫描器至少应覆盖到事故点本身
    assert checked > 50, f"扫描覆盖异常地少({checked} 个名字)——扫描器可能失效"
