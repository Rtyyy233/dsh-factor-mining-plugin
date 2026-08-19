# dsh-factor-mining

Portable Python engine and JSON-RPC bridge for factor mining.

This package ships **no data, no factors, and no mining results**.  A user
supplies:

- a data configuration that maps their local parquet/CSV files to OHLCV
  fields;
- optionally, a factor library module or registry;
- a writable state root for trail, explored paths, search paths, registries
  and caches.

See the repository [README](../README.md) for architecture, the research
integrity gates each evaluation enforces, and a usage guide.

License: AGPL-3.0-only (see [LICENSE](../LICENSE)).

## Install (development)

```sh
pip install -e python/
```

## Bridge

```sh
python -m dsh_factor_mining.bridge --state-root /path/to/state
```

The bridge speaks NDJSON JSON-RPC 2.0 on stdio.  The first message is a
`ready` notification.

## Parity gate

From `python/`:

```sh
PYTHONPATH=src python tests/test_parity_with_reference_harness.py
```

The parity test imports a read-only reference harness when present and
compares diagnostics on synthetic matrices.  It never reads real data.
