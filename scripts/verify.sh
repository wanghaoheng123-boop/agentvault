#!/usr/bin/env bash
# Thin DONE wrapper for humans/CI habit ("verify" / "check").
#
#   DONE  = this script → bin/avcoord gate  (exit 0)
#   NOT   = bin/avcoord verify             (CURRENT lease stamp only)
#
# Do not teach agents that verify=tests. See MemoryBank/coord/EXECUTION_GATES.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${ROOT}/bin/avcoord" gate "$@"
