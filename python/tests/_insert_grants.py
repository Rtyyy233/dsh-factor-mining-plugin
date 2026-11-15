# -*- coding: utf-8 -*-
"""一次性脚本（任务 E1）：在既有 submit 测试的 registry.submit 语句前
自动插入 OOS 凭据授权（B3 硬前置的行为变更适配；不删任何断言）。

用法：python _insert_grants.py <test_file...>
按 ast 定位 dispatch("registry.submit", ...) 调用，取其 source 实参源码段，
在所属语句行前插入同缩进的 grant_oos(<bridge>.state_root, <source_expr>)。
"""
import ast
import sys
from pathlib import Path


def find_sites(tree):
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) \
                or node.func.attr != "dispatch":
            continue
        if not node.args or not (isinstance(node.args[0], ast.Constant)
                                 and node.args[0].value == "registry.submit"):
            continue
        recv = node.func.value
        if not isinstance(recv, ast.Name):
            continue
        # source 来自位置参数字典（dispatch("registry.submit", {...})）
        src = None
        payload = node.args[1] if len(node.args) > 1 else None
        if isinstance(payload, ast.Dict):
            for k, v in zip(payload.keys, payload.values):
                if isinstance(k, ast.Constant) and k.value == "source":
                    src = ast.get_source_segment(SRC, v)
                    break
        if src is None:
            for kw in node.keywords:
                if kw.arg == "source":
                    src = ast.get_source_segment(SRC, kw.value)
                    break
        if src is None:
            continue
        # 所属语句（最近祖先 stmt）的首行
        stmt = node
        while not isinstance(stmt, ast.stmt):
            stmt = getattr(stmt, "_parent", None)
            if stmt is None:
                break
        if stmt is None:
            continue
        sites.append((stmt.lineno, stmt.col_offset, recv.id, src))
    return sites


for path in sys.argv[1:]:
    p = Path(path)
    SRC = p.read_text(encoding="utf-8")
    tree = ast.parse(SRC)
    for parent in ast.walk(tree):
        for ch in ast.iter_child_nodes(parent):
            ch._parent = parent
    sites = sorted(find_sites(tree), reverse=True)
    lines = SRC.splitlines(keepends=True)
    n_ins = 0
    for lineno, col, recv, src in sites:
        indent = " " * col
        grant = f"{indent}grant_oos({recv}.state_root, {src})  # B3 OOS 凭据前置（E1 适配）\n"
        lines.insert(lineno - 1, grant)
        n_ins += 1
    if n_ins:
        if "from _fixedwall import grant_oos" not in SRC:
            # 插到 dsh_factor_mining 首个 import 之后
            for i, ln in enumerate(lines):
                if ln.startswith("from dsh_factor_mining"):
                    lines.insert(i, "from _fixedwall import grant_oos  # noqa: E402\n")
                    break
            else:
                lines.insert(0, "from _fixedwall import grant_oos  # noqa: E402\n")
    p.write_text("".join(lines), encoding="utf-8")
    print(f"{p.name}: {n_ins} grants inserted")
