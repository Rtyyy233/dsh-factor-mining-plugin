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

## dsh_strategy_lab (sibling package, 2026-08)

Strategy-layer harness — consumes **admitted factors** (factor registry,
read-only) and validates portfolio strategies: signal-only contract
(`fit`/`apply` → weight path), an exclusive simulator (t+1 open execution,
T+1, limit-lock deferral, fees, cash leg), a five-lock audit (truncation
invariance is the standing test), the G0/G1′-G4′ gate chain with
trade-minhash deflation, and an auto-counting trail/registry under
`.strategy-lab/`.  CLI-first, runs in this IDE, never in dsh web:

```sh
python -m dsh_strategy_lab build-env --env-spec spec.json --out env.npz
python -m dsh_strategy_lab evaluate --source-file s.py --env-npz env.npz --stage development
python -m dsh_strategy_lab submit   --source-file s.py --env-npz env.npz --factor-refs refs.json --name x
python -m dsh_strategy_lab status
```

See `docs/internal/PLAN-strategy-layer-harness-2026-08-27.md` (design) and
`RUNBOOK-strategy-lab-calibration-P6.md` (pending calibration on real data).
