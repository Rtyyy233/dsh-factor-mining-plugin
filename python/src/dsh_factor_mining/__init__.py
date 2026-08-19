# coding=utf-8
"""Portable factor-mining engine for the dsh-factor-mining plugin.

This package intentionally contains no market data, no factor library, and no
machine-specific default paths.  All data, factor libraries, and mining state
are supplied by the user through a data configuration or an explicit path.
"""

__version__ = "0.1.0"

PROTOCOL_SCHEMA_VERSION = 1
