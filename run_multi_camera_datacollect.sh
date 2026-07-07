#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in \
    "$HOME/miniconda3/envs/rc_gloves/bin/python" \
    "$HOME/anaconda3/envs/rc_gloves/bin/python"
  do
    if [[ -x "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
PYTHON_BIN="${PYTHON_BIN:-python}"
exec "$PYTHON_BIN" main.py
