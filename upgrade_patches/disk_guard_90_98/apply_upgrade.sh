#!/usr/bin/env bash
set -Eeuo pipefail

PATCH_NAME="disk_guard_90_98"
SERVICE_NAME="multi-camera-datacollect.service"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR=""
NO_RESTART=0

usage() {
  echo "Usage: $0 [TARGET_DIR] [--no-restart]"
}

while (($#)); do
  case "$1" in
    --no-restart)
      NO_RESTART=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -n "$TARGET_DIR" ]]; then
        usage >&2
        exit 2
      fi
      TARGET_DIR="$1"
      ;;
  esac
  shift
done

if [[ -z "$TARGET_DIR" ]]; then
  TARGET_DIR="$(cd "$PATCH_DIR/../.." && pwd)"
else
  TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"
fi

REQUIRED_FILES=(
  main.py
  run_multi_camera_datacollect.sh
  app/camera_page_base.py
  app/camera_process.py
  app/multi_camera_sync_process.py
)
for required in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$TARGET_DIR/$required" ]]; then
    echo "Upgrade aborted: missing $TARGET_DIR/$required" >&2
    exit 1
  fi
done

for command_name in patch sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Upgrade aborted: required command not found: $command_name" >&2
    exit 1
  fi
done

(cd "$PATCH_DIR" && sha256sum -c SHA256SUMS)

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON="$PYTHON_BIN"
elif [[ -x "$TARGET_DIR/.venv/bin/python" ]]; then
  PYTHON="$TARGET_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

service_targets_target_dir() {
  local service_working_dir service_exec_start
  if ! systemctl --user cat "$SERVICE_NAME" >/dev/null 2>&1; then
    return 1
  fi
  service_working_dir="$(
    systemctl --user show "$SERVICE_NAME" --property=WorkingDirectory --value \
      2>/dev/null || true
  )"
  if [[ -n "$service_working_dir" ]] \
    && [[ "$(readlink -f "$service_working_dir" 2>/dev/null || true)" == "$TARGET_DIR" ]]; then
    return 0
  fi
  service_exec_start="$(
    systemctl --user show "$SERVICE_NAME" --property=ExecStart --value \
      2>/dev/null || true
  )"
  [[ "$service_exec_start" == *"$TARGET_DIR/"* ]]
}

SERVICE_WAS_ACTIVE=0
if systemctl --user is-active --quiet "$SERVICE_NAME" 2>/dev/null \
  && service_targets_target_dir; then
  SERVICE_WAS_ACTIVE=1
fi

mapfile -t MANUAL_PIDS < <(
  for process_dir in /proc/[0-9]*; do
    pid="${process_dir##*/}"
    cwd="$(readlink "$process_dir/cwd" 2>/dev/null || true)"
    [[ "$cwd" == "$TARGET_DIR" ]] || continue
    cmdline="$(tr '\0' ' ' <"$process_dir/cmdline" 2>/dev/null || true)"
    [[ "$cmdline" == *"main.py"* ]] || continue
    echo "$pid"
  done
)

APP_WAS_RUNNING=0
if ((SERVICE_WAS_ACTIVE)) || ((${#MANUAL_PIDS[@]})); then
  APP_WAS_RUNNING=1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$PATCH_DIR/backups/${PATCH_NAME}_$TIMESTAMP"
CHANGED=0
DISK_GUARD_EXISTED=0
MANUAL_APP_STOPPED=0

start_target_app() {
  if service_targets_target_dir; then
    systemctl --user start "$SERVICE_NAME"
    sleep 2
    systemctl --user is-active --quiet "$SERVICE_NAME"
    return
  fi
  mkdir -p "$TARGET_DIR/logs"
  (cd "$TARGET_DIR" && nohup ./run_multi_camera_datacollect.sh >logs/upgrade_start.log 2>&1 &)
}

rollback() {
  status=$?
  trap - ERR
  if ((CHANGED)); then
    echo "Upgrade failed; restoring backup from $BACKUP_DIR" >&2
    cp "$BACKUP_DIR/camera_page_base.py" "$TARGET_DIR/app/camera_page_base.py"
    cp "$BACKUP_DIR/camera_process.py" "$TARGET_DIR/app/camera_process.py"
    cp "$BACKUP_DIR/multi_camera_sync_process.py" "$TARGET_DIR/app/multi_camera_sync_process.py"
    if ((DISK_GUARD_EXISTED)); then
      cp "$BACKUP_DIR/disk_guard.py" "$TARGET_DIR/app/disk_guard.py"
    else
      rm -f "$TARGET_DIR/app/disk_guard.py"
    fi
  fi
  if ((SERVICE_WAS_ACTIVE)); then
    systemctl --user restart "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif ((MANUAL_APP_STOPPED)); then
    start_target_app >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback ERR

if patch --dry-run --batch --forward -d "$TARGET_DIR" -p1 \
  <"$PATCH_DIR/upgrade.patch" >/dev/null 2>&1; then
  mkdir -p "$BACKUP_DIR"
  cp "$TARGET_DIR/app/camera_page_base.py" "$BACKUP_DIR/camera_page_base.py"
  cp "$TARGET_DIR/app/camera_process.py" "$BACKUP_DIR/camera_process.py"
  cp "$TARGET_DIR/app/multi_camera_sync_process.py" "$BACKUP_DIR/multi_camera_sync_process.py"
  if [[ -f "$TARGET_DIR/app/disk_guard.py" ]]; then
    DISK_GUARD_EXISTED=1
    cp "$TARGET_DIR/app/disk_guard.py" "$BACKUP_DIR/disk_guard.py"
  fi
  CHANGED=1
  patch --batch --forward -d "$TARGET_DIR" -p1 <"$PATCH_DIR/upgrade.patch"
elif patch --dry-run --batch --reverse -d "$TARGET_DIR" -p1 \
  <"$PATCH_DIR/upgrade.patch" >/dev/null 2>&1; then
  echo "Disk guard is already applied; running verification."
else
  echo "Upgrade aborted: target is neither the supported original source" \
    "nor the fully upgraded version." >&2
  exit 1
fi

"$PYTHON" -m py_compile \
  "$TARGET_DIR/app/disk_guard.py" \
  "$TARGET_DIR/app/camera_process.py" \
  "$TARGET_DIR/app/multi_camera_sync_process.py" \
  "$TARGET_DIR/app/camera_page_base.py"
QT_QPA_PLATFORM=offscreen "$PYTHON" "$PATCH_DIR/verify_upgrade.py" "$TARGET_DIR"

if ((APP_WAS_RUNNING)) && ((NO_RESTART == 0)); then
  if ((SERVICE_WAS_ACTIVE)); then
    systemctl --user restart "$SERVICE_NAME"
    sleep 2
    systemctl --user is-active --quiet "$SERVICE_NAME"
  else
    for pid in "${MANUAL_PIDS[@]}"; do
      kill -TERM "$pid" 2>/dev/null || true
    done
    for _ in {1..20}; do
      alive=0
      for pid in "${MANUAL_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
          alive=1
        fi
      done
      ((alive == 0)) && break
      sleep 0.25
    done
    for pid in "${MANUAL_PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        echo "Upgrade failed: application process $pid did not stop cleanly." >&2
        false
      fi
    done
    MANUAL_APP_STOPPED=1
    start_target_app
  fi
fi

trap - ERR
echo "Upgrade $PATCH_NAME completed successfully for $TARGET_DIR"
if ((NO_RESTART)); then
  echo "Application restart skipped by --no-restart."
elif ((APP_WAS_RUNNING == 0)); then
  echo "Application was not running; no process was started."
fi
