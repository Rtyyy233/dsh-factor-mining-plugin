# coding=utf-8
"""arxiv API 查询层（2026-08-24 通道审计后重写）。

审计实证（session.jsonl 28 次调用）：31% 返回是物理/数学噪声
（cross-section/volume/momentum 等词与物理词汇碰撞）、查询零重复但
结果集 26% 论文级重叠、经典金融文献（期刊发表）结构性不可达。
本层只负责 API 调用与 URL 组合；类目过滤默认、已见去重、台账记录
由 bridge 层持有（台账需要 stateRoot，属用户状态）。
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# 2026-09-17 传输层修复:arxiv 前端(Google Frontend)按 TLS ClientHello
# 指纹拒绝 python ssl 栈——实测 urllib 无论 UA/Accept 怎么配恒 406,
# 同 URL 系统 curl 稳定 200(回源 MISS)。故传输层迁移:curl 优先,
# urllib 仅作无 curl 环境的兜底。注意:改本文件须重启 bridge 才生效
# (进程内热改无效——今日 DSH 会话换头全灭误诊'API 弃用'即此因)。
BASE = "https://export.arxiv.org/api/query"
_HEADERS = {"User-Agent": "dsh-factor-mining/0.1 (research factor-mining; local single-user)"}


def compose_search_url(query: str, max_results: int = 10,
                       category_filter: str | None = None,
                       sort_by: str = "relevance", start: int = 0) -> str:
    """纯函数：组合查询 URL（可单测，不触网）。

    category_filter 是完整的类目表达式（如 "cat:q-fin.ST" 或
    "(cat:q-fin.ST OR cat:q-fin.PM)"）；None 不过滤。默认的 q-fin
    OR 链由 bridge 层注入——本层不持有金融类目知识。
    sort_by: relevance | submittedDate | lastUpdatedDate；
    start: 分页偏移（同查询走向深处，替代反复撞 top-N）。
    """
    q = query
    if category_filter:
        q = f"{category_filter} AND ({query})"
    params = {"search_query": q, "max_results": max_results,
              "sortBy": sort_by, "start": start}
    return BASE + "?" + urllib.parse.urlencode(params)


def _fetch(url: str, timeout: int = 30):
    """传输层:curl 优先(python ssl 指纹被 Google Frontend 406),urllib 兜底。"""
    import shutil as _sh
    import subprocess as _sp
    curl = _sh.which("curl")
    if curl:
        try:
            r = _sp.run([curl, "-s", "--max-time", str(timeout), url],
                        capture_output=True)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            return {"error": f"curl rc={r.returncode}: {r.stderr.decode('utf-8', 'replace')[:200]}"}
        except Exception as e:
            pass                        # 落到 urllib 兜底
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "dsh-factor-mining/0.1 (research factor-mining)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        return {"error": str(e)}


def search(query: str, max_results: int = 10, category_filter: str | None = None,
           sort_by: str = "relevance", start: int = 0):
    """检索并解析 Atom feed → [{title, summary, arxiv_id, published}]。

    失败返回 [{"error": ...}]（调用方直通给 agent，不静默吞）。"""
    if not query:
        return []
    url = compose_search_url(query, max_results, category_filter, sort_by, start)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    body = _fetch(url)
    if isinstance(body, dict):          # 传输层失败 {"error": ...}
        return [body]
    try:
        root = ET.fromstring(body)
    except Exception as e:
        return [{"error": f"atom parse: {e}"}]
    out = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
        # 摘要不再截断到 600（旧版截断使方法论细节不可达——浅读偏差）；
        # 1000 是防超长摘要撑爆上下文的保守上限
        summary = (entry.findtext("a:summary", "", ns) or "").strip().replace("\n", " ")
        link = entry.find("a:id", ns)
        arxiv_id = link.text.strip() if link is not None and link.text else ""
        published = entry.findtext("a:published", "", ns) or ""
        out.append({"title": title, "summary": summary[:1000],
                    "arxiv_id": arxiv_id, "published": published[:10]})
    return out
