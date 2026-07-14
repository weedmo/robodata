#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${CURATION_DATA_ROOT:-/mnt/synology/data/data_div/2026_1}"
RAW_ROOT="${RAW_ROOT:-${DATA_ROOT%/}/raw}"
FIX_SCRIPT="${FIX_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fix_raw_permissions.sh}"
LOG_DIR="${LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/curation-tools}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/raw-permission-auto-fix.log}"

mkdir -p "$LOG_DIR"

timestamp() {
  date -Is
}

log() {
  printf '[%s] %s\n' "$(timestamp)" "$*" | tee -a "$LOG_FILE"
}

if [[ ! -x "$FIX_SCRIPT" ]]; then
  log "fix script is not executable: $FIX_SCRIPT"
  exit 1
fi

if [[ ! -d "$RAW_ROOT" ]]; then
  log "raw root missing: $RAW_ROOT"
  exit 0
fi

TMP_OUTPUT="$(mktemp)"
trap 'rm -f "$TMP_OUTPUT"' EXIT

find_rc=0
for cell_dir in "$RAW_ROOT"/cell*; do
  [[ -d "$cell_dir" ]] || continue

  if [[ ! -r "$cell_dir" || ! -x "$cell_dir" ]]; then
    printf '%s\n' "$cell_dir" >>"$TMP_OUTPUT"
    find_rc=1
    continue
  fi

  set +e
  timeout 20 find "$cell_dir" \
    -maxdepth 3 \
    -path '*/.omx' -prune -o \
    -type d -print >>/dev/null 2>"$TMP_OUTPUT.cell"
  cell_find_rc=$?
  set -e

  if [[ $cell_find_rc -ne 0 ]]; then
    find_rc=$cell_find_rc
    {
      printf '%s: scan failed (exit %s)\n' "$cell_dir" "$cell_find_rc"
      sed -n '1,5p' "$TMP_OUTPUT.cell"
    } >>"$TMP_OUTPUT"
  fi
  rm -f "$TMP_OUTPUT.cell"
done

if [[ $find_rc -eq 0 && ! -s "$TMP_OUTPUT" ]]; then
  log "raw permissions ok"
  exit 0
fi

log "raw permission issue detected; running fix"
cat "$TMP_OUTPUT" >>"$LOG_FILE"

if CELL_TIMEOUT=60 timeout 10m "$FIX_SCRIPT" --no-sudo >>"$LOG_FILE" 2>&1; then
  log "raw permission auto-fix succeeded"
else
  rc=$?
  log "raw permission auto-fix failed with exit code $rc"
  exit "$rc"
fi
