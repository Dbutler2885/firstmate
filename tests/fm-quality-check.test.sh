#!/usr/bin/env bash
# Public-interface regressions for deterministic quality execution, receipts,
# freshness, timeouts, failure feedback, and task-incarnation isolation.
set -eu
# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
python3 "$ROOT/tests/quality_check_tests.py" "$ROOT"
