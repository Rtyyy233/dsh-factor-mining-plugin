# coding=utf-8
"""Empty user factor library template.

Copy this file to your own location and fill it with YOUR factors.  The
open-source plugin ships this empty template only: no market data, no factor
definitions, and no mining results are distributed with the plugin.
"""


def list_known_factors():
    """Return a list of dicts: {name, description, formula, ic_ir?}."""
    return []


def get_known_factor(name):
    """Return factor(env) -> np.ndarray for a known factor name."""
    raise KeyError(name)


def query_known_factors(query: str):
    """Return matching entries for a natural-language query."""
    return []
