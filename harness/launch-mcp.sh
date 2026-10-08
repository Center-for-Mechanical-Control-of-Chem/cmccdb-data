#!/usr/bin/env bash
set -euo pipefail

# Source-based launch avoids installing an editable package into the data repo.
cmccdb_harness_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cmccdb_data_root="$(cd -- "$cmccdb_harness_root/.." && pwd)"
cmccdb_development_root="$(cd -- "$cmccdb_data_root/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$cmccdb_harness_root/src:$cmccdb_development_root/cmccdb-schema${PYTHONPATH:+:$PYTHONPATH}"

cmccdb_interpreter="${CMCCDB_PYTHON:-$cmccdb_development_root/.codex/envs/python3.11-codex/bin/python}"
if [[ ! -x "$cmccdb_interpreter" ]]; then
  echo "Set CMCCDB_PYTHON to the Python 3.11+ environment containing mcp>=2.2,<3 and cmccdb-schema." >&2
  exit 1
fi
if [[ "$(uname -s)" == Darwin ]]; then
  exec /usr/bin/arch -arm64 "$cmccdb_interpreter" -m cmccdb_extraction.server "$@"
else
  exec "$cmccdb_interpreter" -m cmccdb_extraction.server "$@"
fi
