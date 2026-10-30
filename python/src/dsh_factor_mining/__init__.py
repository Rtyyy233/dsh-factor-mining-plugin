# coding=utf-8
"""Portable factor-mining engine for the dsh-factor-mining plugin.

This package intentionally contains no market data, no factor library, and no
machine-specific default paths.  All data, factor libraries, and mining state
are supplied by the user through a data configuration or an explicit path.
"""

# 0.1.1（2026-10-06 假阴性救助线）：null 地形 schema 演进（n 缺省 200 +
# a4_dd/famsize 分位段）——环境指纹随版本自然 bump，旧 n=50 地形判
# mismatch 强制重校准（两生产 root 已随本版本重校准）
__version__ = "0.1.1"

PROTOCOL_SCHEMA_VERSION = 1
