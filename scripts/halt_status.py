"""
scripts/halt_status.py

Print the executor's halt state. Used by the unattended daily runner to
decide whether to proceed, and useful on its own for a quick check.

Usage:
    uv run python scripts/halt_status.py

Output (single line):
    OK
    HALTED <reason>

Exit code:
    0  not halted
    1  halted
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_crypto.audit import AuditStore  # noqa: E402
from turtle_crypto.config import DEFAULT_AUDIT_DB_PATH  # noqa: E402


def main() -> int:
    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    try:
        if audit.is_halted():
            print(f"HALTED {audit.halt_reason() or '(no reason recorded)'}")
            return 1
        print("OK")
        return 0
    finally:
        audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
