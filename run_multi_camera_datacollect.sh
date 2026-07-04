#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python}"
if [[ -x "/home/descfly/miniconda3/envs/rc_gloves/bin/python" ]]; then
  PYTHON_BIN="/home/descfly/miniconda3/envs/rc_gloves/bin/python"
fi
exec "$PYTHON_BIN" main.py
