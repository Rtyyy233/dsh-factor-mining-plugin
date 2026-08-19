# coding=utf-8
"""Minimal arxiv API search used for methodology lookup."""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def search(query: str, max_results: int = 10, category: str | None = None):
    if not query:
        return []
    base = "http://export.arxiv.org/api/query"
    q = query
    if category:
        q = f"cat:{category} AND ({query})"
    params = {"search_query": q, "max_results": max_results, "sortBy": "relevance"}
    url = base + "?" + urllib.parse.urlencode(params)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        return [{"error": str(e)}]
    out = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("a:summary", "", ns) or "").strip().replace("\n", " ")
        link = entry.find("a:id", ns)
        arxiv_id = link.text.strip() if link is not None and link.text else ""
        published = entry.findtext("a:published", "", ns) or ""
        out.append({"title": title, "summary": summary[:600], "arxiv_id": arxiv_id,
                    "published": published[:10]})
    return out
