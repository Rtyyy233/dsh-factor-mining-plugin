# coding=utf-8
"""Open-source privacy gate for dsh-factor-mining-plugin.

Checks that no user data, local absolute paths, private harness paths, or
known-factor names ship in the distributable sources.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAN = [
    ROOT / "python" / "src",
    ROOT / "ts",
    ROOT / "preset",
    ROOT / "README.md",
    ROOT / "python" / "README.md",
]

FORBIDDEN_DATA_EXTS = {".parquet", ".pkl", ".npz", ".csv"}
FORBIDDEN_TEXT = [
    r"C:\Users",
    "/Users/",
    "RTyyyy",
    "life-os",
    "factor-mining/harness",
    "g73_c6",
    "accel_inv",
    "grjmom",
    "amihud_illiq",
]


def main() -> int:
    failures = []
    for base in SCAN:
        if base.is_dir():
            files = [p for p in base.rglob("*") if p.is_file() and "node_modules" not in p.parts]
        else:
            files = [base]
        for path in files:
            if path.suffix.lower() in FORBIDDEN_DATA_EXTS:
                failures.append(f"data file: {path.relative_to(ROOT)}")
                continue
            if path.suffix not in {".py", ".ts", ".json", ".yml", ".yaml", ".md", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in FORBIDDEN_TEXT:
                if needle in text:
                    failures.append(f"forbidden text {needle!r}: {path.relative_to(ROOT)}")
    if failures:
        for failure in failures:
            print(f"PRIVACY FAIL: {failure}")
        return 1
    print("PRIVACY PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
