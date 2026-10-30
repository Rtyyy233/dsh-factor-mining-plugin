# coding=utf-8
"""Prompt 自足性黑名单机械验收（2026-10-02 总任务书 D.2/D.5）。

一切模型面文本的硬判据（黑名单 grep）：
- 历史引用：R17/R24/P0a/方案 B/日期版本号（ISO 格式示例「2026-09-18T08:00」
  不算——那是格式教学不是历史引用）
- 黑话词表（首版）：剥洋葱/挖坟/烧名/洗账/假门/喷雾/回音室/兜底/烟测
- 0.1.22 永续：已删除的「连续 N 次让位真人」文案不得复活（A.3.4 grep 断言）

扫描面 = 模型面文本（注释/维护者文档不在内）：
- index.ts 全部 description 行（工具描述+参数描述）
- SKILL.md 正文（技能正文）
- bridge.py：_STRATEGY_DIRECTIVES 块 + _timeout_rotate_strategy 块 +
  _loop_directive 函数体（去注释后的中文字符串）
- primitives.py：decide() 函数体（去注释后）
- drive.ts：含中文的代码行（注入正文/状态 note）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BLACKLIST = [
    "剥洋葱", "挖坟", "烧名", "洗账", "假门", "喷雾", "回音室", "兜底",
    "烟测",
    "R17", "R24", "P0a", "R27", "R31", "R32",
    "方案 B", "方案B",
    "may_stop",
    "暂停自动推进，等待真人指示",
]
#: 日期引用（ISO 格式示例除外——后面跟 T 的是格式教学）
_DATE_RE = re.compile(r"202[0-9]-[0-9]{2}-[0-9]{2}(?!T)")


def _strip_comments(text: str) -> str:
    # docstring（开发者文档，非模型面）整块剔除
    text = re.sub(r'"""[\s\S]*?"""', "", text)
    out = []
    for ln in text.splitlines():
        # 行注释剔除（字符串内 # 罕见且此处只求保守覆盖）
        i = ln.find("#")
        out.append(ln[:i] if i >= 0 else ln)
    return "\n".join(out)


def _slice_between(text: str, start_pat: str, end_pat: str) -> str:
    m = re.search(start_pat, text, re.M)
    if m is None:
        return ""
    start = m.start()
    m2 = re.search(end_pat, text[start + 10:], re.M)
    end = start + 10 + (m2.start() if m2 else len(text))
    return text[start:end]


def _index_ts_descriptions() -> str:
    p = ROOT.parent / "ts" / "tool-factor-mining" / "src" / "index.ts"
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for ln in lines:
        if "description:" in ln or "description =" in ln:
            out.append(ln)
        elif "record(" in ln and "'" in ln:
            out.append(ln)  # record() 工厂的三件工具描述
    return "\n".join(out)


def _skill_body() -> str:
    # 技能目录可能位于 ~/.zcode/skills 或 ~/.agents/skills（迁移期两处并存）；
    # 存在的副本都扫——陈旧副本若仍带违规文案同样属于模型面暴露
    bodies = []
    for base in (".zcode", ".agents"):
        p = Path.home() / base / "skills" / "factor-mining" / "SKILL.md"
        if p.exists():
            bodies.append(p.read_text(encoding="utf-8"))
    if not bodies:
        raise FileNotFoundError(
            "~/.zcode/skills/factor-mining/SKILL.md 与 "
            "~/.agents/skills/factor-mining/SKILL.md 均不存在")
    return "\n".join(bodies)


def _bridge_model_text() -> str:
    p = ROOT / "src" / "dsh_factor_mining" / "bridge.py"
    s = p.read_text(encoding="utf-8")
    parts = [
        _slice_between(s, r"_STRATEGY_DIRECTIVES = \{", r"^\}\n"),
        _slice_between(s, r"def _timeout_rotate_strategy",
                       r"^def _pick_strategy"),
        _slice_between(s, r"def _loop_directive", r"^    def [a-z_]+\(self"),
    ]
    return _strip_comments("\n".join(parts))


def _primitives_decide_text() -> str:
    p = ROOT / "src" / "dsh_factor_mining" / "primitives.py"
    s = p.read_text(encoding="utf-8")
    return _strip_comments(
        _slice_between(s, r"^def decide\(", r"^def _bump_push_count"))


def _drive_model_text() -> str:
    p = ROOT.parent / "ts" / "tool-factor-mining" / "src" / "drive.ts"
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if re.search(r"[\u4e00-\u9fff]", ln) and "//" not in ln \
                and "*" not in ln and "fileLog(" not in ln \
                and "ctx.logger" not in ln:
            out.append(ln)
    return "\n".join(out)


def _module_model_text(name: str) -> str:
    """新模块模型面文本（注释/文档剔除后）：素材模板/门拒收文案/事件
    规则说明都是模型可读文案，全量过黑名单（2026-10-14 任务书验收）。"""
    p = ROOT / "src" / "dsh_factor_mining" / f"{name}.py"
    return _strip_comments(p.read_text(encoding="utf-8"))


SURFACES = {
    "index.ts descriptions": _index_ts_descriptions,
    "SKILL.md": _skill_body,
    "bridge.py model text": _bridge_model_text,
    "primitives.decide": _primitives_decide_text,
    "drive.ts model text": _drive_model_text,
    "navigation.py materials": lambda: _module_model_text("navigation"),
    "ordergate.py messages": lambda: _module_model_text("ordergate"),
    "divergence.py messages": lambda: _module_model_text("divergence"),
    "collision.py card note": lambda: _module_model_text("collision"),
}


def test_blacklist_clean_on_all_model_surfaces():
    failures = []
    for name, fn in SURFACES.items():
        text = fn()
        assert text.strip(), f"{name} 扫描面为空——提取逻辑坏了"
        for tok in BLACKLIST:
            if tok in text:
                idx = text.index(tok)
                ctx = text[max(0, idx - 60):idx + 60].replace("\n", "⏎")
                failures.append(f"{name}: 「{tok}」…{ctx}")
        for m in _DATE_RE.finditer(text):
            ctx = text[max(0, m.start() - 40):m.start() + 40].replace("\n", "⏎")
            failures.append(f"{name}: 日期引用 {m.group(0)} …{ctx}")
    assert not failures, "黑名单命中 %d 处:\n" % len(failures) + "\n".join(
        failures)


def test_removed_yield_branch_stays_dead():
    """A.3.4 grep 断言：drive.ts 源码不含「让位真人」分支文案；且注入路径
    不再以 maxSimple 早退（consecutiveSimple 仅作计数）。"""
    p = ROOT.parent / "ts" / "tool-factor-mining" / "src" / "drive.ts"
    s = p.read_text(encoding="utf-8")
    assert "暂停自动推进" not in s
    assert "等待真人指示" not in s
    assert "consecutiveSimple >= maxSimple" not in s
