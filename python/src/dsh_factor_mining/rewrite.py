# coding=utf-8
"""LLM 重写层（2026-09-01 效率编译层）——mimo-v2.5（用户指定；glm 系禁用：太贵）。

引擎侧强模型把烟测门拒绝的慢因子重写为**严格等价**的向量化实现。安全
模型：LLM 输出永远不直接执行——先过安全扫描（仅 np/pd/math、禁 import
与危险调用），再在烟测 worker 子进程内与原实现比对 allclose(rtol 1e-6)。
DECLARE 声明仅作参考，验证才是裁决（2026-09-01 mimo 实测：会漏声明、
会谎报 EXACT——都被验证兜住；T3 EWM 递推一发过证明真实能力）。

接入零新依赖：opencode-go endpoint（OpenAI 兼容）+ 机器级环境变量
OPENCODE_GO_API_KEY（bridge 由 node spawn 天然继承）。两个实测坑：
①UA 必须自定义——Cloudflare 拦 Python-urllib 默认 UA（403 code 1010）；
②mimo 是推理模型，max_tokens 8k 会被推理烧尽 -> content null。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"
DEFAULT_MODEL = "mimo-v2.5"
FORBIDDEN_MODEL_PREFIXES = ("glm-",)   # 用户铁律：glm 系太贵，误配即回落默认
_HTTP_TIMEOUT_S = 180.0
_MAX_TOKENS = 16384
_USER_AGENT = "dsh-factor-engine/1.0"
_MAX_SOURCE_LINES = 200

SYSTEM_PROMPT = """你是数值计算代码优化器。任务：把慢的 factor(env) 实现重写为快版本。

铁律：
1. 只重写实现，不改变计算语义——输出必须与原实现数值等价（调用方会用 np.allclose(rtol=1e-6) 严格验证，不等价即作废）。
2. 若无法在严格等价前提下大幅加速，第一行写 DECLARE: CANNOT_PRESERVE，并在代码块外给出语义近似方案说明（不会被自动采用）。
3. 只允许使用 numpy（np）/pandas（pd）/math，禁止任何 import 语句（np/pd/math 已在全局命名空间），禁止 open/eval/exec/os/sys。
4. 输出格式：第一行 DECLARE: EXACT 或 DECLARE: APPROXIMATE(一句差异说明) 或 DECLARE: CANNOT_PRESERVE；随后一个 ```python 代码块，其中只含 def factor(env) 函数本体。

环境契约：env.c/env.v/env.o/env.h/env.l 是 float64 numpy (T,N) 二维数组（行=日期升序，列=标的）；env.T/env.N 是 int；env.listed 是 bool (T,N)。输出必须是与 env.c 同形状的 (T,N) numpy 数组。
面板规模：T≈2600, N≈5300（约 1400 万格）。任何 Python 层逐 cell 循环必超时；t 循环里逐日调用 numpy 行算子也是常数慢。优先：矩阵广播、cumsum/前缀和类 O(T·N) 重排、pandas rolling/ewm（C 实现）。
注意：赋值目标必须是自有数组（np.zeros/np.full 分配），不要写入输入数组或 pandas 只读视图。"""


def rewrite_config() -> dict | None:
    """可用性解析：显式关闭 / 无 key -> None（重写层静默降级，门与 njit
    层不受影响）。glm 前缀误配回落默认模型（铁律：绝不调用 glm 系）。"""
    if os.environ.get("DSH_FACTOR_REWRITE", "1") == "0":
        return None
    model = os.environ.get("DSH_FACTOR_REWRITE_MODEL", DEFAULT_MODEL)
    if any(model.startswith(p) for p in FORBIDDEN_MODEL_PREFIXES):
        model = DEFAULT_MODEL
    key = (os.environ.get("DSH_FACTOR_REWRITE_API_KEY")
           or os.environ.get("OPENCODE_GO_API_KEY") or "")
    if not key:
        return None
    return {"model": model,
            "base": os.environ.get("DSH_FACTOR_REWRITE_BASE_URL", DEFAULT_BASE_URL),
            "key": key}


def call_llm(cfg: dict, user_msg: str) -> tuple[str, dict]:
    """OpenAI 兼容 chat completion（网络层 1 次重试）。返回 (content, usage)。"""
    body = json.dumps({
        "model": cfg["model"], "temperature": 0, "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
    }).encode("utf-8")
    last_err: Exception | None = None
    for _attempt in (1, 2):
        try:
            req = urllib.request.Request(
                cfg["base"], data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {cfg['key']}",
                         # 实测 403 code 1010：Cloudflare 拦默认 UA
                         "User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (data.get("choices") or [{}])[0].get(
                "message", {}).get("content")
            if not content:
                raise RuntimeError(
                    f"空 content（推理预算耗尽？usage={data.get('usage')}）")
            return content, data.get("usage") or {}
        except Exception as e:  # noqa: BLE001 — 网络重试
            last_err = e
            time.sleep(1.0)
    raise RuntimeError(f"LLM 调用失败: {type(last_err).__name__}: {last_err}")


def parse_reply(content: str) -> tuple[str, str | None, str]:
    """-> (declare, code, note)。DECLARE 缺席（mimo 实测会漏）按 UNKNOWN
    处理——不影响采用判定，验证才是裁决。"""
    dm = re.search(r"DECLARE:\s*(EXACT|APPROXIMATE|CANNOT_PRESERVE)",
                   content, re.I)
    declare = dm.group(1).upper() if dm else "UNKNOWN"
    m = re.search(r"```python\s*\n(.*?)```", content, re.S)
    if m is None:
        m = re.search(r"```\s*\n(.*?)```", content, re.S)
    code = m.group(1).strip() if m else None
    head = content[:dm.start()].strip() if dm else content[:200]
    return declare, code, head


_BANNED_SNIPPETS = ("open(", "eval(", "exec(", "__import__", "compile(",
                    "os.", "sys.", "socket", "urllib", "requests",
                    "subprocess", "shutil", "pathlib", "getattr(", "globals(",
                    "locals(")


def safety_scan(code: str) -> list[str]:
    """重写源码白名单扫描：仅 np/pd/math、无 import、无危险调用、单函数、
    行数上限。LLM 输出（可能含 prompt 注入）未经本扫描+子进程验证绝不执行。"""
    problems: list[str] = []
    if re.search(r"^\s*(import|from)\s+", code, re.M):
        problems.append("import 语句（仅 np/pd/math 全局可用）")
    for bad in _BANNED_SNIPPETS:
        if bad in code:
            problems.append(bad)
    if "def factor" not in code:
        problems.append("无 def factor")
    if len([ln for ln in code.splitlines() if ln.strip().startswith("def ")]) > 1:
        problems.append("多个 def")
    if len(code.splitlines()) > _MAX_SOURCE_LINES:
        problems.append(f"超过 {_MAX_SOURCE_LINES} 行")
    return problems


def build_user_message(source: str, profile: str) -> str:
    return (f"以下因子实现被引擎实测为慢，请按铁律重写。\n\n"
            f"性能画像：{profile}\n目标：全面板单次计算 < 5s。\n\n"
            f"```python\n{source}\n```")
